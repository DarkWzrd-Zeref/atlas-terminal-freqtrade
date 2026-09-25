"""One native paper child at a time; reviewed changes always use a new interpreter."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from atlas.handoff import MAX_MANIFEST_BYTES, _read_regular
from atlas.paper_state import atomic_json, store_for

INITIALIZE_TIMEOUT = 180


def child_environment():
    # Never inherit Railway/portal/lab/provider credentials into strategy code.
    allowed = {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
               "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
               "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "LD_LIBRARY_PATH"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1", FREQTRADE__DRY_RUN="true",
               ATLAS_FREQTRADE_ROLE="paper")
    return env


def terminate(process):
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25, check=False)
    process.wait(timeout=5)


def supervise_paper(config, runtime):
    store = store_for(config["user_data_dir"])
    store.initialize(config)
    store.recover_start()
    stopping = False
    child = None

    def stop(*_args):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            store.clear_loaded()
            atomic_json(runtime, store.runtime_config(config))
            command = [sys.executable, "-m", "atlas.run_reviewed_paper", "--config", str(runtime)]
            kwargs = {"env": child_environment(), "cwd": str(Path(__file__).resolve().parents[1])}
            if os.name == "posix":
                kwargs["start_new_session"] = True
            else:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            child = subprocess.Popen(command, **kwargs)
            deadline = time.monotonic() + INITIALIZE_TIMEOUT
            initialized = False
            while not stopping and child.poll() is None:
                loaded = json.loads(_read_regular(store.loaded_path, MAX_MANIFEST_BYTES))
                if loaded.get("pid") == child.pid:
                    initialized = True
                if not initialized and time.monotonic() > deadline:
                    terminate(child)
                    break
                time.sleep(0.25)
            if stopping:
                break
            # Cleanup the entire owned child group before starting another one.
            terminate(child)
            if store.child_exited():
                continue
            if not initialized and store.loading_failed():
                continue
            raise SystemExit(child.returncode or 1)
    finally:
        if child is not None:
            terminate(child)
