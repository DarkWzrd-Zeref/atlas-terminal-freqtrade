"""Paper state/HTTP tests, plus native Worker/RPC/DB checks in the dependency image."""
import ast
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from atlas import api_paper as api
from atlas.handoff import BundleError, validate_bundle
from atlas.paper_state import PaperStore, store_for
from atlas.paper_supervisor import child_environment
from atlas import paper_supervisor
from test_handoff import make_manifest
from test_api_handoff import authenticated


def setup_store(root):
    (root / "strategies").mkdir()
    (root / "strategies/sample_strategy.py").write_bytes(b"# Existing baseline bytes\n")
    (root / "strategies/sample_strategy.json").write_bytes(b'{"params":{"stoploss":-0.1}}')
    config = {"user_data_dir": str(root), "strategy": "SampleStrategy", "timeframe": "5m",
              "initial_state": "running", "dry_run": True, "trading_mode": "spot",
              "exchange": {"name": "kraken", "key": "", "secret": ""},
              "api_server": {"password": "paper-only-credential"},
              "db_url": "sqlite:///" + str(root / "trades-paper.sqlite")}
    store = store_for(root)
    store.initialize(config)
    return store, config


def candidate():
    files = [("ReviewedStrategy.py", b"raise RuntimeError('Must stay inert while staged')\n"),
             ("helper.py", b"value=42\n"), ("ReviewedStrategy.json", b'{"params":{}}')]
    return validate_bundle(make_manifest(files), files)


def evidence(bundle):
    return {"job_id": "a" * 32, "result_filename": "backtest-result-2026-09-25_12-00-00.zip",
            "candidate_sha256": bundle.sha256}


def stage_bundle(store):
    bundle = candidate()
    store.stage(bundle, bundle.sha256, evidence(bundle))
    return bundle


def transition(store, bundle):
    store.request("activate", bundle.sha256, "running")
    store.checkpoint(flat=True)
    assert store.child_exited()


class PaperPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store, self.config = setup_store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_baseline_snapshots_actual_source_and_parameters_only_once(self):
        baseline = self.store.read()["selected"]
        bundle = self.store.verify(baseline)
        self.assertEqual(dict(bundle.files)["sample_strategy.json"], b'{"params":{"stoploss":-0.1}}')
        self.assertEqual(self.store.runtime_config(self.config)["initial_state"], "running")
        (self.root / "strategies/sample_strategy.py").write_bytes(b"# new upstream image")
        self.store.initialize(self.config)
        self.assertEqual(self.store.read()["selected"], baseline)
        self.assertEqual(dict(self.store.verify(baseline).files)["sample_strategy.py"], b"# Existing baseline bytes\n")

    def test_inert_stage_restart_activate_stopped_and_explicit_rollback(self):
        baseline = self.store.read()["selected"]
        with patch("builtins.compile", side_effect=AssertionError("staging cannot execute code")):
            bundle = stage_bundle(self.store)
        self.assertEqual(self.store.read()["selected"], baseline)
        restart = PaperStore(self.root)
        self.assertEqual(restart.read()["staged"]["sha256"], bundle.sha256)
        transition(restart, bundle)
        config = restart.runtime_config(self.config)
        self.assertEqual((config["strategy"], config["timeframe"], config["initial_state"]), ("ReviewedStrategy", "1h", "stopped"))
        self.assertEqual(config["db_url"], self.config["db_url"])
        self.assertEqual(config["api_server"], self.config["api_server"])
        self.assertTrue(config["dry_run"])
        with self.assertRaises(BundleError):
            restart.mark_loaded(digest=bundle.sha256, strategy="ReviewedStrategy", timeframe="1h", runtime_state="running", pid=123)
        restart.mark_loaded(digest=bundle.sha256, strategy="ReviewedStrategy", timeframe="1h", runtime_state="stopped", pid=123)
        self.assertEqual(restart.public(pid=123)["loaded_sha256"], bundle.sha256)
        self.assertIsNone(restart.public(pid=124)["loaded_sha256"])
        with self.assertRaises(BundleError):
            restart.request("rollback", "0" * 64, "stopped")
        restart.request("rollback", bundle.sha256, "stopped")
        restart.checkpoint(flat=True)
        self.assertTrue(restart.child_exited())
        restart.mark_loaded(digest=baseline["sha256"], strategy="SampleStrategy", timeframe="5m", runtime_state="stopped", pid=124)
        self.assertEqual(restart.public(pid=124)["status"], "rolled_back")
        self.assertEqual(restart.runtime_config(self.config)["initial_state"], "stopped")
        self.assertEqual(PaperStore(self.root).read()["selected"], baseline)

    def test_native_checkpoint_refusal_keeps_original_selection(self):
        baseline = self.store.read()["selected"]
        bundle = stage_bundle(self.store)
        self.store.request("activate", bundle.sha256, "running")
        refusal = self.store.checkpoint(flat=False)
        self.assertEqual(refusal["original_state"], "running")
        self.assertEqual(refusal["phase"], "rejected")
        self.assertFalse(self.store.child_exited())
        self.assertEqual(self.store.read()["selected"], baseline)
        self.assertEqual(self.store.public()["status"], "failed")

    def test_hash_and_evidence_mismatch_cannot_stage_or_activate(self):
        bundle = candidate()
        wrong = evidence(bundle)
        wrong["candidate_sha256"] = "0" * 64
        with self.assertRaises(BundleError):
            self.store.stage(bundle, bundle.sha256, wrong)
        with self.assertRaises(BundleError):
            self.store.stage(bundle, "0" * 64, evidence(bundle))
        stage_bundle(self.store)
        (self.store.directory(bundle.sha256) / "helper.py").write_bytes(b"modified")
        with self.assertRaises(BundleError):
            self.store.request("activate", bundle.sha256, "stopped")
        self.assertIsNone(self.store.read()["request"])

    def test_failed_load_restores_previous_stopped_without_touching_database(self):
        baseline = self.store.read()["selected"]
        database = self.root / "trades-paper.sqlite"
        database.write_bytes(b"Existing persistent paper records")
        bundle = stage_bundle(self.store)
        transition(self.store, bundle)
        self.assertTrue(self.store.loading_failed())
        self.assertEqual(self.store.read()["selected"], baseline)
        self.assertEqual(self.store.runtime_config(self.config)["initial_state"], "stopped")
        self.assertEqual(database.read_bytes(), b"Existing persistent paper records")
        self.assertFalse(self.store.loading_failed())

    def test_restart_never_treats_incomplete_checkpoint_as_permission(self):
        baseline = self.store.read()["selected"]
        bundle = stage_bundle(self.store)
        self.store.request("activate", bundle.sha256, "running")
        self.store.checkpoint(flat=True)
        self.store.recover_start()
        self.assertEqual(self.store.read()["selected"], baseline)
        self.assertIsNone(self.store.read()["request"])
        self.assertEqual(self.store.runtime_config(self.config)["initial_state"], "stopped")
        transition(self.store, bundle)
        self.store.recover_start()
        self.assertEqual(self.store.read()["selected"], baseline)
        self.assertEqual(self.store.runtime_config(self.config)["initial_state"], "stopped")

    def test_failed_third_selection_preserves_last_confirmed_rollback_lineage(self):
        baseline = self.store.read()["selected"]
        bundle = stage_bundle(self.store)
        transition(self.store, bundle)
        self.store.mark_loaded(digest=bundle.sha256, strategy="ReviewedStrategy", timeframe="1h", runtime_state="stopped", pid=123)
        files = [("ReviewedStrategy.py", b"raise RuntimeError('Third strategy cannot load')\n")]
        third = validate_bundle(make_manifest(files), files)
        for recover in (self.store.loading_failed, self.store.recover_start):
            self.store.stage(third, third.sha256, evidence(third))
            transition(self.store, third)
            recover()
            self.assertEqual(self.store.read()["selected"]["sha256"], bundle.sha256)
            self.assertEqual(self.store.read()["previous"], baseline)
            self.assertEqual(self.store.read()["staged"]["sha256"], third.sha256)
        self.store.request("rollback", bundle.sha256, "stopped")
        self.assertEqual(self.store.read()["request"]["target"], baseline)

    def test_environment_and_runtime_cannot_inherit_live_or_parent_credentials(self):
        with patch.dict(os.environ, {"ATLAS_FREQTRADE_PASSWORD": "private", "LAB_PASSWORD": "private",
                                   "OPENAI_API_KEY": "private", "FREQTRADE__DRY_RUN": "false"}):
            env = child_environment()
        self.assertNotIn("ATLAS_FREQTRADE_PASSWORD", env)
        self.assertNotIn("LAB_PASSWORD", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["FREQTRADE__DRY_RUN"], "true")
        dirty = copy.deepcopy(self.config)
        dirty.update(dry_run=False, trading_mode="futures")
        dirty["exchange"].update(name="other", key="secret", secret="secret")
        config = self.store.runtime_config(dirty)
        self.assertEqual((config["dry_run"], config["trading_mode"], config["exchange"]["name"]), (True, "spot", "kraken"))
        self.assertFalse(any(config["exchange"][k] for k in ("key", "secret", "password", "uid")))

    def test_supervisor_uses_fresh_children_and_restores_after_failed_initialization(self):
        # This fixture tests process supervision, not native exchange execution.
        # Separate native Worker/DB tests below and deployed stopped-load checks
        # cover that boundary. Every launch still uses a real fresh interpreter.
        stage_bundle(self.store)
        runtime = self.root / "runtime.json"
        launches = []
        popen = subprocess.Popen
        script = """
import json, os, sys, time
from pathlib import Path
from atlas.paper_state import PaperStore
config = json.loads(Path(sys.argv[1]).read_text())
store = PaperStore(Path(config['user_data_dir']))
state = store.read()
if config['strategy'] == 'ReviewedStrategy':
    raise SystemExit(7)
store.mark_loaded(digest=state['selected']['sha256'], strategy='SampleStrategy',
                  timeframe='5m', runtime_state=config['initial_state'], pid=os.getpid())
if not state['promoted']:
    store.request('activate', state['staged']['sha256'], 'running')
    store.checkpoint(flat=True)
else:
    time.sleep(.5)
raise SystemExit(130)
"""
        def launch(command, **kwargs):
            self.assertEqual(command[:3], [sys.executable, "-m", "atlas.run_reviewed_paper"])
            self.assertNotIn("ATLAS_FREQTRADE_PASSWORD", kwargs["env"])
            config = json.loads(runtime.read_text())
            child = popen([sys.executable, "-c", script, str(runtime)], **kwargs)
            launches.append((child.pid, config))
            return child
        with patch.object(paper_supervisor.subprocess, "Popen", side_effect=launch), \
             patch.object(paper_supervisor.signal, "signal"), \
             patch.dict(os.environ, {"ATLAS_FREQTRADE_PASSWORD": "parent-only"}):
            with self.assertRaises(SystemExit):
                paper_supervisor.supervise_paper(self.config, runtime)
        self.assertEqual(len(launches), 3)
        self.assertEqual(len({pid for pid, _ in launches}), 3)
        self.assertEqual([cfg["strategy"] for _, cfg in launches], ["SampleStrategy", "ReviewedStrategy", "SampleStrategy"])
        self.assertEqual([cfg["initial_state"] for _, cfg in launches], ["running", "stopped", "stopped"])
        self.assertEqual(self.store.read()["status"], "failed")
        self.assertIsNone(self.store.read()["previous"], "Failed candidate must not become rollback target")


class PaperRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store, self.config = setup_store(self.root)
        self.rpc = types.SimpleNamespace(_freqtrade=types.SimpleNamespace(
            state=types.SimpleNamespace(name="RUNNING"), _exit_lock=threading.Lock()))
        app = FastAPI()
        app.include_router(api.router, prefix="/api/v1", dependencies=[Depends(authenticated)])
        app.dependency_overrides[api._get_config] = lambda: self.config
        app.dependency_overrides[api._get_rpc] = lambda: self.rpc
        self.client = TestClient(app)
        self.env = patch.dict(os.environ, {"ATLAS_FREQTRADE_ROLE": "paper"})
        self.env.start()
        self.headers = {"authorization": "Bearer test-owner"}

    def tearDown(self):
        self.env.stop()
        self.client.close()
        self.tmp.cleanup()

    def test_registration_requires_auth_trading_mode_and_paper_role(self):
        source = Path(__file__).resolve().parents[1] / "freqtrade/rpc/api_server/webserver.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        guard = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and "ATLAS_FREQTRADE_ROLE" in ast.unparse(n.test) and "'paper'" in ast.unparse(n.test))
        self.assertIn("Depends(http_basic_or_jwt_token)", ast.unparse(guard))
        self.assertIn("Depends(is_trading_mode)", ast.unparse(guard))
        self.assertEqual(self.client.get("/api/v1/atlas/paper/selection").status_code, 401)
        with patch.dict(os.environ, {"ATLAS_FREQTRADE_ROLE": "lab"}):
            self.assertEqual(self.client.get("/api/v1/atlas/paper/selection", headers=self.headers).status_code, 403)

    def test_stage_endpoint_is_inert_and_selection_has_baseline_digest(self):
        bundle = candidate()
        body = {"manifest": bundle.manifest, "reviewed_sha256": bundle.sha256,
                "files": [{"path": p, "content_base64": base64.b64encode(b).decode()} for p, b in bundle.files],
                "backtest": evidence(bundle)}
        response = self.client.post("/api/v1/atlas/paper/stage", json=body, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.text)
        value = response.json()
        self.assertTrue(value["selected_sha256"])
        self.assertNotEqual(value["selected_sha256"], bundle.sha256)
        self.assertEqual(value["staged_sha256"], bundle.sha256)
        self.assertIsNone(value["loaded_sha256"])
        self.assertEqual(self.rpc._freqtrade.state.name, "RUNNING")

    def test_activate_requires_flat_native_records_and_records_only_a_request(self):
        bundle = stage_bundle(self.store)
        body = {"reviewed_sha256": bundle.sha256}
        with patch.object(api, "native_flat", return_value=False):
            response = self.client.post("/api/v1/atlas/paper/activate", json=body, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.store.read()["request"])
        self.assertEqual(self.rpc._freqtrade.state.name, "RUNNING")
        with patch.object(api, "native_flat", return_value=True):
            response = self.client.post("/api/v1/atlas/paper/activate", json=body, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "checkpoint_pending")
        self.assertNotEqual(response.json()["selected_sha256"], bundle.sha256)
        self.assertIsNone(response.json()["loaded_sha256"])
        self.assertEqual(self.rpc._freqtrade.state.name, "STOPPED")
        with patch.object(api, "native_flat", return_value=True):
            duplicate = self.client.post("/api/v1/atlas/paper/activate", json=body, headers=self.headers)
        self.assertEqual(duplicate.status_code, 409)

    def test_native_database_failure_never_appears_flat_or_records_a_change(self):
        bundle = stage_bundle(self.store)
        with patch.object(api, "native_flat", side_effect=RuntimeError("database unavailable")):
            with self.assertRaises(RuntimeError):
                self.client.post("/api/v1/atlas/paper/activate", json={"reviewed_sha256": bundle.sha256}, headers=self.headers)
        self.assertIsNone(self.store.read()["request"])
        self.assertEqual(self.rpc._freqtrade.state.name, "RUNNING")


class NativePaperCheckpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from freqtrade.worker import Worker
            from freqtrade.persistence import Order, Trade, init_db
            from freqtrade.rpc import RPC, RPCException
            from freqtrade.enums import State
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f"Full native dependency image required: {exc}")
        cls.Worker, cls.Trade, cls.Order, cls.init_db = Worker, Trade, Order, staticmethod(init_db)
        cls.RPC, cls.RPCException, cls.State = RPC, RPCException, State

    def test_real_native_database_counts_open_trade_and_closed_trade_open_order(self):
        self.init_db("sqlite://")
        self.assertTrue(api.native_flat())
        trade = self.Trade(pair="BTC/USD", exchange="kraken", is_open=True, stake_amount=100,
                           amount=1, open_rate=100, fee_open=0, fee_close=0)
        self.Trade.session.add(trade)
        self.Trade.commit()
        self.assertFalse(api.native_flat())
        trade.is_open = False
        self.Trade.commit()
        self.assertTrue(api.native_flat())
        order = self.Order(ft_trade_id=trade.id, ft_pair="BTC/USD", order_id="test-open-order", ft_order_side="buy",
                           ft_is_open=True, ft_amount=1, ft_price=100)
        self.Trade.session.add(order)
        self.Trade.commit()
        self.assertFalse(api.native_flat(), "Even an order without an open trade must prevent promotion")
        order.ft_is_open = False
        self.Trade.commit()
        self.assertTrue(api.native_flat())
        self.Trade.session.remove()

    def test_real_native_worker_checkpoint_and_rpc_start_guard(self):
        from atlas.run_reviewed_paper import install_native_hooks
        from freqtrade.resolvers import StrategyResolver
        with tempfile.TemporaryDirectory() as folder:
            store, _ = setup_store(Path(folder))
            bundle = stage_bundle(store)
            store.request("activate", bundle.sha256, "running")
            bot = types.SimpleNamespace(state=self.State.STOPPED, _exit_lock=threading.Lock())
            worker = object.__new__(self.Worker)
            worker.freqtrade = bot
            rpc = object.__new__(self.RPC)
            rpc._freqtrade = bot
            with patch.object(self.Worker, "_worker", return_value=self.State.STOPPED), \
                 patch.object(self.Worker, "_init"), \
                 patch.object(self.RPC, "_rpc_start", self.RPC._rpc_start), \
                 patch.object(self.RPC, "_rpc_reload_config", self.RPC._rpc_reload_config), \
                 patch.object(StrategyResolver, "_load_strategy", StrategyResolver._load_strategy), \
                 patch.object(api, "native_flat", return_value=False) as flat:
                install_native_hooks(store, store.read()["selected"]["sha256"])
                with self.assertRaises(self.RPCException):
                    rpc._rpc_start()
                worker._worker(self.State.RUNNING)
                self.assertEqual(bot.state, self.State.RUNNING)
                self.assertIsNone(store.read()["request"])
                store.request("activate", bundle.sha256, "stopped")
                flat.return_value = True
                with self.assertRaises(KeyboardInterrupt):
                    worker._worker(self.State.STOPPED)
                self.assertEqual(store.read()["request"]["phase"], "checkpoint_passed")
                self.assertEqual(bot.state, self.State.STOPPED)


if __name__ == "__main__":
    unittest.main()
