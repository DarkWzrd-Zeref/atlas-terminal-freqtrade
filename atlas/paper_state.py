"""Durable reviewed paper selections. No Python execution happens in this module.

The native child owns writes while running; the supervisor writes only after that
child has exited. A thread lock serializes API requests with native checkpoints.
This is reviewed-code storage, not a sandbox against a malicious selected strategy.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import threading
import uuid

from atlas.handoff import (BundleError, MAX_FILES, MAX_FILE_BYTES, MAX_MANIFEST_BYTES,
                           _install_reviewed_bundle, _link, _plain_directory,
                           _read_regular, validate_bundle, verify_candidate)

SHA = re.compile(r"[0-9a-f]{64}\Z")
JOB = re.compile(r"[0-9a-f]{32}\Z")
_stores = {}
_stores_lock = threading.Lock()


def atomic_json(path, value):
    path = Path(path)
    _plain_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".paper-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def store_for(data):
    key = str(Path(data).resolve())
    with _stores_lock:
        if key not in _stores:
            _stores[key] = PaperStore(Path(key))
        return _stores[key]


class PaperStore:
    def __init__(self, data):
        self.data = Path(data)
        self.root = self.data / "atlas_paper"
        self.bundles = self.root / "bundles"
        self.path = self.root / "selection.json"
        self.loaded_path = self.root / "loaded.json"
        self.lock = threading.RLock()

    def read(self):
        _plain_directory(self.root)
        value = json.loads(_read_regular(self.path, MAX_MANIFEST_BYTES))
        if value.get("version") != 1 or not isinstance(value.get("selected"), dict):
            raise BundleError("Paper selection journal is invalid.")
        return value

    def write(self, value):
        atomic_json(self.path, value)

    def directory(self, digest):
        if not isinstance(digest, str) or not SHA.fullmatch(digest):
            raise BundleError("Invalid paper selection digest.")
        return self.bundles / ("atlas_" + digest)

    def verify(self, reference):
        bundle = verify_candidate(self.directory(reference["sha256"]),
                                  expected_sha256=reference["sha256"])
        if (bundle.manifest["strategy_class"] != reference["strategy"]
                or bundle.manifest["timeframe"] != reference["timeframe"]):
            raise BundleError("Selected metadata differs from its reviewed bytes.")
        return bundle

    @staticmethod
    def reference(bundle, backtest=None, baseline=False):
        return {"sha256": bundle.sha256, "strategy": bundle.manifest["strategy_class"],
                "timeframe": bundle.manifest["timeframe"], "baseline": baseline,
                "backtest": backtest}

    def initialize(self, config):
        """Snapshot existing native files once; never regenerate an existing baseline."""
        with self.lock:
            self.root.mkdir(exist_ok=True)
            self.bundles.mkdir(exist_ok=True)
            _plain_directory(self.bundles)
            if self.path.exists():
                state = self.read()
                self.verify(state["selected"])
                return state
            if config["strategy"] != "SampleStrategy":
                raise BundleError("First initialization requires the existing SampleStrategy baseline.")
            source = _plain_directory(self.data / "strategies")
            files = []
            pending = [source]
            scanned = 0
            while pending:
                directory = pending.pop()
                for path in directory.iterdir():
                    scanned += 1
                    if scanned > MAX_FILES * 16:
                        raise BundleError("Baseline directory exceeds its bounded snapshot limit.")
                    info = path.lstat()
                    if _link(info):
                        raise BundleError("Baseline cannot contain symbolic links.")
                    if path.name == "__pycache__":
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(info.st_mode):
                        files.append((path.relative_to(source).as_posix(), _read_regular(path, MAX_FILE_BYTES)))
                        if len(files) > MAX_FILES:
                            raise BundleError("Too many baseline files.")
                    else:
                        raise BundleError("Baseline cannot contain special files.")
            manifest = {"version": 1, "strategy_class": config["strategy"],
                        "timeframe": config["timeframe"], "entrypoint": "sample_strategy.py",
                        "provenance": {"kind": "baseline", "source": "Existing paper strategy files at initialization"},
                        "files": [{"path": p, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                                  for p, b in files]}
            bundle = validate_bundle(manifest, files)
            _install_reviewed_bundle(self.bundles, bundle, reviewed_sha256=bundle.sha256)
            state = {"version": 1, "selected": self.reference(bundle, baseline=True),
                     "previous": None, "staged": None, "request": None, "promoted": False,
                     "status": "baseline", "error": None}
            self.write(state)
            return state

    def stage(self, bundle, reviewed_sha256, backtest):
        if (not isinstance(backtest, dict) or set(backtest) != {"job_id", "result_filename", "candidate_sha256"}
                or not isinstance(backtest["job_id"], str) or not JOB.fullmatch(backtest["job_id"])
                or backtest["candidate_sha256"] != bundle.sha256
                or not isinstance(backtest["result_filename"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}\.zip", backtest["result_filename"])):
            raise BundleError("Backtest evidence must identify this exact candidate and a native result.")
        with self.lock:
            state = self.read()
            if state["request"]:
                raise BundleError("A paper strategy change is already pending.")
            _install_reviewed_bundle(self.bundles, bundle, reviewed_sha256=reviewed_sha256)
            state.update(staged=self.reference(bundle, deepcopy(backtest)), status="staged", error=None)
            self.write(state)
            return state

    def request(self, action, digest, original_state):
        """Caller holds the native exit lock and has checked actual open DB records."""
        with self.lock:
            state = self.read()
            if state["request"]:
                raise BundleError("A paper strategy change is already pending.")
            if action == "activate":
                target = state["staged"]
                if not target or target["sha256"] != digest:
                    raise BundleError("Activation must match the currently reviewed staged bundle.")
            elif action == "rollback":
                if digest != state["selected"]["sha256"]:
                    raise BundleError("Selection changed; refresh before requesting rollback.")
                target = state["previous"]
                if not target:
                    raise BundleError("No prior paper selection is available.")
            else:
                raise BundleError("Unknown paper selection action.")
            self.verify(target)
            if target["sha256"] == state["selected"]["sha256"]:
                raise BundleError("That bundle is already selected.")
            request = {"id": uuid.uuid4().hex, "action": action, "target": target,
                       "phase": "requested", "original_state": original_state,
                       "prior_selected": state["selected"], "prior_previous": state["previous"]}
            state.update(request=request, status="checkpoint_pending", error=None)
            self.write(state)
            return request

    def checkpoint(self, *, flat):
        with self.lock:
            state = self.read()
            request = state["request"]
            if not request or request["phase"] != "requested":
                return None
            if not flat:
                state.update(request=None, status="failed",
                             error="Strategy change refused: native checkpoint found open trades or orders.")
                self.write(state)
                return {**request, "phase": "rejected"}
            self.verify(request["target"])
            request["phase"] = "checkpoint_passed"
            state["status"] = "activating"
            self.write(state)
            return request

    def child_exited(self):
        """Supervisor only: commit selection after the native child has fully exited."""
        with self.lock:
            state = self.read()
            request = state["request"]
            if request and request["phase"] == "checkpoint_passed":
                self.verify(request["target"])
                state.update(previous=state["selected"], selected=request["target"], staged=None,
                             promoted=True, status="activating", error=None,
                             request={**request, "phase": "loading"})
                self.write(state)
                return True
            return False

    def recover_start(self):
        """A host crash never counts as a completed native checkpoint/load."""
        with self.lock:
            state = self.read()
            request = state["request"]
            if request:
                if request["phase"] == "loading":
                    self.verify(request["prior_selected"])
                    state.update(selected=request["prior_selected"], previous=request["prior_previous"],
                                 staged=request["target"])
                state.update(request=None, promoted=True, status="failed", error="Paper strategy change was interrupted; retained the last confirmed selection stopped.")
                self.write(state)
            self.clear_loaded()

    def clear_loaded(self):
        atomic_json(self.loaded_path, {})

    def mark_loaded(self, *, digest, strategy, timeframe, runtime_state, pid):
        with self.lock:
            state = self.read()
            selected = state["selected"]
            self.verify(selected)
            if (digest, strategy, timeframe) != (selected["sha256"], selected["strategy"], selected["timeframe"]):
                raise BundleError("Loaded native strategy does not match the selected bundle.")
            if state["promoted"] and runtime_state != "stopped":
                raise BundleError("A promoted paper selection must initially load stopped.")
            request = state["request"]
            if request and request["phase"] == "loading":
                state.update(status="rolled_back" if request["action"] == "rollback" else "ready", request=None)
                self.write(state)
            atomic_json(self.loaded_path, {"sha256": digest, "strategy": strategy,
                        "timeframe": timeframe, "pid": pid, "runtime_state": runtime_state})

    def loading_failed(self):
        with self.lock:
            state = self.read()
            if not state["request"] or state["request"]["phase"] != "loading":
                return False
            request = state["request"]
            self.verify(request["prior_selected"])
            state.update(selected=request["prior_selected"], previous=request["prior_previous"],
                         staged=request["target"])
            state.update(request=None, status="failed",
                         error="Selected strategy failed to initialize; restored the prior selection stopped.")
            self.write(state)
            self.clear_loaded()
            return True

    def public(self, *, pid=None, runtime_state=None):
        with self.lock:
            state = self.read()
            loaded = json.loads(_read_regular(self.loaded_path, MAX_MANIFEST_BYTES)) if self.loaded_path.exists() else {}
            if pid is not None and loaded.get("pid") != pid:
                loaded = {}
            selected, previous, staged = state["selected"], state["previous"] or {}, state["staged"] or {}
            return {"strategy": selected["strategy"], "timeframe": selected["timeframe"],
                    "selected_sha256": selected["sha256"], "loaded_sha256": loaded.get("sha256"),
                    "previous_sha256": previous.get("sha256"), "previous_strategy": previous.get("strategy"),
                    "previous_timeframe": previous.get("timeframe"), "staged_sha256": staged.get("sha256"),
                    "staged_strategy": staged.get("strategy"), "staged_timeframe": staged.get("timeframe"),
                    "status": state["status"], "error": state["error"], "paper_only": True,
                    "runtime_state": runtime_state or loaded.get("runtime_state"),
                    "backtest": selected.get("backtest"), "baseline": selected["baseline"]}

    def runtime_config(self, base):
        state = self.read()
        bundle = self.verify(state["selected"])
        config = deepcopy(base)
        config.update(dry_run=True, trading_mode="spot", margin_mode="", strategy=bundle.manifest["strategy_class"],
                      timeframe=bundle.manifest["timeframe"], recursive_strategy_search=False,
                      cancel_open_orders_on_exit=False, force_entry_enable=False)
        config["exchange"].update(name="kraken", key="", secret="", password="", uid="")
        config["strategy_path"] = str(self.directory(bundle.sha256).joinpath(*PurePosixPath(bundle.manifest["entrypoint"]).parts).parent)
        config["atlas_paper_sha256"] = bundle.sha256
        if state["promoted"]:
            config["initial_state"] = "stopped"
        return config
