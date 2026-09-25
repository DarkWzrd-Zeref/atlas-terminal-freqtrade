"""HTTP/storage/process tests. Full native Freqtrade execution is a deployment check.

The process fixture intentionally writes fixture results: it tests subprocess
isolation, arguments, persisted status, and failure handling, not backtest accuracy.
"""

import ast
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from atlas import api_handoff as api
from atlas.handoff import install_candidate, validate_bundle
from atlas.run_reviewed_backtest import exact_file_loader
from test_handoff import make_manifest


def authenticated(request: Request):
    if request.headers.get("authorization") != "Bearer test-owner":
        raise HTTPException(status_code=401, detail="Authentication required")


def webserver_mode():
    return None


class NativeFlag:
    def __init__(self):
        self.value = False

    def __call__(self, reserve=False, release=False):
        if reserve:
            if self.value:
                raise HTTPException(status_code=409)
            self.value = True
        if release:
            self.value = False
        return self.value


class CandidateRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = {"user_data_dir": self.root, "api_server": {"password": "private-test-password"}}
        self.files = [("ReviewedStrategy.py", b"raise RuntimeError('Do not import during install')\n")]
        self.manifest = make_manifest(self.files)
        self.bundle = validate_bundle(self.manifest, self.files)
        self.body = {"manifest": self.manifest,
                     "files": [{"path": p, "content_base64": base64.b64encode(b).decode()} for p, b in self.files],
                     "reviewed_sha256": self.bundle.sha256}
        app = FastAPI()
        app.include_router(api.router, prefix="/api/v1", dependencies=[Depends(authenticated), Depends(webserver_mode)])
        app.dependency_overrides[api._get_config] = lambda: self.config
        self.client = TestClient(app)
        self.headers = {"authorization": "Bearer test-owner"}
        self.env = patch.dict(os.environ, {"ATLAS_FREQTRADE_ROLE": "lab"})
        self.env.start()

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.tmp.cleanup()

    def upload(self):
        return self.client.post("/api/v1/atlas/candidates", json=self.body, headers=self.headers)

    def test_unauthenticated_and_paper_calls_cannot_install(self):
        self.assertEqual(self.client.post("/api/v1/atlas/candidates", json=self.body).status_code, 401)
        with patch.dict(os.environ, {"ATLAS_FREQTRADE_ROLE": "paper"}):
            self.assertEqual(self.upload().status_code, 403)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_native_registration_requires_both_auth_and_webserver_in_lab_only(self):
        source = Path(__file__).resolve().parents[1] / "freqtrade/rpc/api_server/webserver.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        guard = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                     and "ATLAS_FREQTRADE_ROLE" in ast.unparse(node.test))
        guarded = ast.unparse(guard)
        self.assertIn("== 'lab'", guarded)
        self.assertIn("Depends(http_basic_or_jwt_token)", guarded)
        self.assertIn("Depends(is_webserver_mode)", guarded)
        self.assertIn("atlas_handoff", guarded)

    def test_install_and_read_are_inert_and_report_no_executable_validation(self):
        with patch.object(api.subprocess, "Popen", side_effect=AssertionError("Install must stay inert")):
            response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["executable_validation"], "not_performed")
        self.assertFalse(response.json()["automatically_promoted"])
        listing = self.client.get("/api/v1/atlas/candidates", headers=self.headers).json()
        self.assertEqual(listing["candidates"][0]["sha256"], self.bundle.sha256)
        self.assertEqual(listing["candidates"][0]["integrity"], "not_checked")
        detail = self.client.get(f"/api/v1/atlas/candidates/{self.bundle.sha256}", headers=self.headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["integrity"], "verified")

    def test_tampered_hash_bad_base64_duplicates_and_body_bounds(self):
        self.body["reviewed_sha256"] = "0" * 64
        self.assertEqual(self.upload().status_code, 400)
        self.body["reviewed_sha256"] = self.bundle.sha256
        self.body["files"][0]["content_base64"] = "not#base64"
        self.assertEqual(self.upload().status_code, 400)
        response = self.client.post("/api/v1/atlas/candidates", content='{"a":1,"a":2}',
                                    headers={**self.headers, "content-type": "application/json"})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/v1/atlas/candidates", content=b"{}",
                                    headers={**self.headers, "content-type": "application/json",
                                             "content-length": str(api.MAX_REQUEST_BYTES + 1)})
        self.assertEqual(response.status_code, 413)

    def test_backtest_rechecks_bytes_before_dispatch(self):
        self.assertEqual(self.upload().status_code, 201)
        directory = api._storage(self.config) / ("atlas_" + self.bundle.sha256)
        (directory / "ReviewedStrategy.py").write_text("changed")
        with patch.object(api, "_jobs", side_effect=AssertionError("Changed candidate cannot launch")):
            response = self.client.post(f"/api/v1/atlas/candidates/{self.bundle.sha256}/backtest",
                                        json={"timerange": "20260920-20260921"}, headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_backtest_requires_explicit_interval_and_preserves_reviewed_timeframe(self):
        self.assertEqual(self.upload().status_code, 201)
        invalid = ({}, {"timerange": "20260920-"}, {"timerange": "20260920-20260921", "timeframe": "5m"},
                   {"timerange": "20260920-20260921", "strategy": "Other"},
                   {"timerange": "20260920-20260921", "strategy_path": "/elsewhere"})
        for body in invalid:
            with self.subTest(body=body), patch.object(api, "_jobs", side_effect=AssertionError("No launch")):
                response = self.client.post(f"/api/v1/atlas/candidates/{self.bundle.sha256}/backtest",
                                            json=body, headers=self.headers)
                self.assertEqual(response.status_code, 400)


_CHILD_FIXTURE = """
import json, pathlib, sys, uuid
sys.path.insert(0, sys.argv[1])
import helper
print('HELPER=' + helper.VERSION, flush=True)
results = pathlib.Path(sys.argv[2])
filename = 'backtest-result-' + uuid.uuid4().hex + '.zip'
(results / filename).write_bytes(b'fixture-result-not-a-real-backtest')
(results / (pathlib.Path(filename).stem + '.meta.json')).write_text(json.dumps({'ReviewedStrategy': {}}))
(results / '.last_result.json').write_text(json.dumps({'latest_backtest': filename}))
"""


class CandidateProcesses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.strategies = self.root / "strategies"
        self.strategies.mkdir()
        self.manager = api.CandidateJobs(self.root)
        self.config = {"user_data_dir": self.root, "dry_run": False, "timeframe": "5m",
                       "api_server": {"password": "private-test-password", "jwt_secret_key": "private-test-jwt"},
                       "exchange": {"name": "kraken", "key": "private-exchange-key", "secret": "private-exchange-secret"}}
        self.options = {"timerange": "20260920-20260921", "timeframe": "1h", "strategy": "ReviewedStrategy",
                        "enable_protections": False, "backtest_cache": "none"}
        self.flag = NativeFlag()
        self.patch_busy = patch.object(api, "_native_busy", side_effect=self.flag)
        self.patch_busy.start()

    def tearDown(self):
        self.manager.stop()
        for _ in range(100):
            if not self.manager.active:
                break
            time.sleep(.02)
        self.patch_busy.stop()
        self.tmp.cleanup()

    def candidate(self, version):
        files = [("ReviewedStrategy.py", b"# Native fixture source is not loaded in these plumbing tests\n"),
                 ("helper.py", f"VERSION = {version!r}\n".encode())]
        bundle = validate_bundle(make_manifest(files), files)
        result = install_candidate(self.strategies, bundle, role="lab", reviewed_sha256=bundle.sha256)
        return bundle, result.directory

    def wait_for_job(self, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.manager.status(job_id)
            if not state["running"]:
                return state
            time.sleep(.02)
        self.fail("Fixture process did not finish")

    def test_fresh_interpreters_native_fixed_args_and_persistent_status(self):
        real_popen = subprocess.Popen
        commands = []

        def launch(command, **kwargs):
            commands.append(command)
            self.assertEqual(command[:3], [sys.executable, "-m", "atlas.run_reviewed_backtest"])
            self.assertNotIn("shell", kwargs)
            self.assertEqual(kwargs["env"]["FREQTRADE__DRY_RUN"], "true")
            self.assertNotIn("ATLAS_FREQTRADE_PASSWORD", kwargs["env"])
            runtime = json.loads(Path(command[command.index("--config") + 1]).read_text())
            self.assertTrue(runtime["dry_run"])
            self.assertEqual(runtime["timeframe"], "1h")
            self.assertNotIn("api_server", runtime)
            self.assertNotIn("key", runtime["exchange"])
            self.assertNotIn("secret", runtime["exchange"])
            self.assertFalse(runtime["recursive_strategy_search"])
            self.assertIn("candidate_sha256", runtime["atlas_handoff"])
            self.assertEqual(runtime["atlas_handoff"]["candidate_sha256"], command[command.index("--sha256") + 1])
            self.assertEqual(runtime["timerange"], self.options["timerange"])
            self.assertEqual(runtime["backtest_cache"], "none")
            return real_popen([sys.executable, "-c", _CHILD_FIXTURE,
                               runtime["strategy_path"], runtime["exportdirectory"]], **kwargs)

        with patch.object(api.subprocess, "Popen", side_effect=launch), patch.dict(os.environ, {"ATLAS_FREQTRADE_PASSWORD": "secret"}):
            for version in ("first-version", "second-version"):
                bundle, directory = self.candidate(version)
                job = self.manager.start(directory, bundle.sha256, self.options, self.config)
                state = self.wait_for_job(job["job_id"])
                self.assertEqual(state["status"], "completed", state)
                self.assertIn("HELPER=" + version, state["log_tail"])
                restarted = api.CandidateJobs(self.root)
                self.assertEqual(restarted.status(job["job_id"])["result_filename"], state["result_filename"])
        self.assertEqual(len(commands), 2)
        self.assertFalse(self.flag.value)

    def test_nonzero_exit_and_missing_result_are_not_completed(self):
        real_popen = subprocess.Popen
        for script in ("raise SystemExit(7)", "print('no result')"):
            with patch.object(api.subprocess, "Popen", side_effect=lambda command, **kwargs: real_popen(
                    [sys.executable, "-c", script], **kwargs)):
                bundle, directory = self.candidate("failure")
                job = self.manager.start(directory, bundle.sha256, self.options, self.config)
                state = self.wait_for_job(job["job_id"])
                self.assertEqual(state["status"], "failed")
                self.assertIsNone(state["result_filename"])

    def test_busy_native_analysis_rejects_before_spawn(self):
        self.flag.value = True
        bundle, directory = self.candidate("busy")
        with patch.object(api.subprocess, "Popen", side_effect=AssertionError("Must not spawn")):
            with self.assertRaises(HTTPException) as raised:
                self.manager.start(directory, bundle.sha256, self.options, self.config)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertTrue(self.flag.value)
        self.assertIsNone(self.manager.active)

    def test_native_nested_settings_remain_available(self):
        config = {"freqai": {"enabled": True, "feature_parameters": {"include_timeframes": ["1h"]}},
                  "stake_amount": 100}
        api._merge_options(config, {"freqai": {"identifier": "reviewed-model"}, "stake_amount": None})
        self.assertEqual(config["freqai"]["feature_parameters"]["include_timeframes"], ["1h"])
        self.assertTrue(config["freqai"]["enabled"])
        self.assertEqual(config["freqai"]["identifier"], "reviewed-model")
        self.assertEqual(config["stake_amount"], 100)

    def test_log_tail_is_bounded_and_known_secrets_controls_redacted(self):
        real_popen = subprocess.Popen
        script = "print('x' * 50000); print('\\x1b[31mprivate-test-password\\x00')"
        with patch.object(api.subprocess, "Popen", side_effect=lambda command, **kwargs: real_popen(
                [sys.executable, "-c", script], **kwargs)):
            bundle, directory = self.candidate("logs")
            job = self.manager.start(directory, bundle.sha256, self.options, self.config)
            state = self.wait_for_job(job["job_id"])
        self.assertLessEqual(len(state["log_tail"]), api.MAX_LOG_BYTES)
        self.assertNotIn("private-test-password", state["log_tail"])
        self.assertNotIn("\x1b", state["log_tail"])
        self.assertNotIn("\x00", state["log_tail"])
        self.assertIn("[redacted]", state["log_tail"])

    @unittest.skipUnless(os.name == "posix", "Production process-group termination is POSIX")
    def test_timeout_terminates_child_group(self):
        real_popen = subprocess.Popen
        with patch.object(api, "BACKTEST_TIMEOUT_SECONDS", .05), patch.object(api.subprocess, "Popen",
                side_effect=lambda command, **kwargs: real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)):
            bundle, directory = self.candidate("timeout")
            job = self.manager.start(directory, bundle.sha256, self.options, self.config)
            state = self.wait_for_job(job["job_id"])
        self.assertEqual(state["status"], "timed_out")
        self.assertFalse(state["running"])


class ExactEntrypoint(unittest.TestCase):
    def test_loader_calls_native_import_for_exact_entrypoint_only(self):
        files = [("ReviewedStrategy.py", b"# intended"), ("Backup.py", b"# same class must not be discovered")]
        bundle = validate_bundle(make_manifest(files), files)
        calls = []

        class FakeNativeClass:
            def __init__(self, config):
                self.config = config

        class Resolver:
            @staticmethod
            def _get_valid_object(path, name):
                calls.append((path.name, name))
                yield FakeNativeClass, "# intended"

            @staticmethod
            def validate_strategy(instance):
                return instance

        with tempfile.TemporaryDirectory() as folder:
            candidate = install_candidate(Path(folder), bundle, role="lab", reviewed_sha256=bundle.sha256)
            loader = exact_file_loader(Resolver, candidate.directory, bundle.sha256)
            result = loader("ReviewedStrategy", {"test": "value"})
            self.assertEqual(Path(result.__file__).name, "ReviewedStrategy.py")
            self.assertEqual(calls, [("ReviewedStrategy.py", "ReviewedStrategy")])
            with self.assertRaises(ValueError):
                loader("OtherClass", {})

    def test_actual_native_resolver_uses_reviewed_file_helpers_and_parameters(self):
        try:
            from freqtrade.resolvers import StrategyResolver
        except ImportError as exc:
            self.skipTest(f"Full native Freqtrade dependencies are unavailable locally: {exc.name}")
        intended = b'''from freqtrade.strategy import IStrategy
from atlas_exact_helper import VALUE
class ReviewedStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    minimal_roi = {"0": 0.1}
    stoploss = -0.1
    helper_value = VALUE
    def populate_indicators(self, dataframe, metadata): return dataframe
    def populate_entry_trend(self, dataframe, metadata):
        dataframe["enter_long"] = 0
        return dataframe
    def populate_exit_trend(self, dataframe, metadata):
        dataframe["exit_long"] = 0
        return dataframe
'''
        files = [("ReviewedStrategy.py", intended),
                 ("Backup.py", b'raise RuntimeError("Wrong entrypoint was executed")\nclass ReviewedStrategy: pass\n'),
                 ("atlas_exact_helper.py", b"VALUE = 37\n"),
                 ("ReviewedStrategy.json", b'{"strategy_name":"ReviewedStrategy","params":{"stoploss":{"stoploss":-0.15}}}')]
        bundle = validate_bundle(make_manifest(files), files)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = install_candidate(root, bundle, role="lab", reviewed_sha256=bundle.sha256)
            config = {"strategy": "ReviewedStrategy", "timeframe": "1h", "user_data_dir": root,
                      "trading_mode": "spot", "stake_currency": "USD"}
            previous_bytecode = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            try:
                loader = exact_file_loader(StrategyResolver, candidate.directory, bundle.sha256)
                with patch.object(StrategyResolver, "_load_strategy", staticmethod(loader)):
                    strategy = StrategyResolver.load_strategy(config)
                self.assertEqual(Path(strategy.__file__), candidate.directory / "ReviewedStrategy.py")
                self.assertEqual(strategy.helper_value, 37)
                self.assertEqual(strategy.timeframe, "1h")
                self.assertEqual(strategy.stoploss, -0.15)
            finally:
                sys.dont_write_bytecode = previous_bytecode
                sys.modules.pop("atlas_exact_helper", None)


if __name__ == "__main__":
    unittest.main()
