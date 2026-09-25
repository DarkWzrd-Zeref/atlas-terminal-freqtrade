"""Lab-only native strategy review and backtests behind Freqtrade authentication.

Installed Python stays inert until an explicit candidate backtest request. Every
backtest starts a fresh native Freqtrade interpreter: helper modules cannot leak
between candidates through sys.modules. This is lab containment, not a Python
sandbox. The application must run on a separate volume with separate credentials
from paper trading. Owner-reviewed strategies can access their lab environment.
"""

from __future__ import annotations

import base64
import binascii
from collections import deque
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from atlas.handoff import (
    BundleError, MANIFEST_NAME, MAX_BUNDLE_BYTES, MAX_FILE_BYTES, MAX_FILES,
    MAX_MANIFEST_BYTES, _plain_directory, _read_regular, install_candidate,
    parse_manifest_json, validate_bundle, verify_candidate,
)


MAX_REQUEST_BYTES = ((MAX_BUNDLE_BYTES + 2) // 3) * 4 + 2 * MAX_MANIFEST_BYTES
MAX_LOG_BYTES = 16384
BACKTEST_TIMEOUT_SECONDS = 1800
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
_CLOSED_RANGE = re.compile(r"(?:\d{8}(?:T\d{4}(?:\d{2})?)?|\d{10}|\d{13})-(?:\d{8}(?:T\d{4}(?:\d{2})?)?|\d{10}|\d{13})\Z")
_managers: dict[str, "CandidateJobs"] = {}
_manager_lock = threading.Lock()


def _lab_role():
    if os.environ.get("ATLAS_FREQTRADE_ROLE") != "lab":
        raise HTTPException(status_code=403, detail="Candidate operations require the isolated lab role.")


def _get_config():
    from freqtrade.rpc.api_server.deps import get_config
    return get_config()


def _native_busy(reserve=False, release=False):
    from freqtrade.rpc.api_server.webserver_bgwork import ApiBG
    if release:
        ApiBG.analysis_running = False
    elif reserve:
        if ApiBG.analysis_running:
            raise HTTPException(status_code=409, detail="A native analysis is already running.")
        ApiBG.analysis_running = True
    return ApiBG.analysis_running


def _storage(config):
    return Path(config["user_data_dir"]) / "strategies" / "atlas_candidates"


def _candidate(config, digest):
    if not isinstance(digest, str) or not _SHA.fullmatch(digest):
        raise HTTPException(status_code=404, detail="Candidate not found.")
    directory = _storage(config) / ("atlas_" + digest)
    if not directory.exists():
        raise HTTPException(status_code=404, detail="Candidate not found.")
    return directory


def _summary(bundle, *, verified=True):
    return {"sha256": bundle.sha256, "manifest": bundle.manifest,
            "integrity": "verified" if verified else "not_checked",
            "executable_validation": "not_performed", "automatically_promoted": False}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BundleError("Duplicate JSON keys are forbidden.")
        result[key] = value
    return result


async def _body(request: Request, limit=MAX_REQUEST_BYTES):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="An application/json body is required.")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid content length.")
        if length < 0 or length > limit:
            raise HTTPException(status_code=413, detail="Request exceeds the handoff size limit.")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail="Request exceeds the handoff size limit.")
        data.extend(chunk)
    try:
        body = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="Invalid or duplicate-key JSON body.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")
    return body


def _error(exc):
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail="Candidate or stored result not found.")
    if isinstance(exc, BundleError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=409, detail="Candidate storage could not be read or updated.")


def _decode_bundle(body):
    if set(body) != {"manifest", "files", "reviewed_sha256"}:
        raise BundleError("Expected manifest, files and reviewed_sha256 fields.")
    # Validate manifest before allocating decoded file buffers.
    try:
        manifest = parse_manifest_json(json.dumps(body["manifest"], ensure_ascii=False).encode("utf-8"))
    except UnicodeError as exc:
        raise BundleError("Manifest strings must be valid UTF-8.") from exc
    payloads = body["files"]
    if not isinstance(payloads, list) or not 1 <= len(payloads) <= MAX_FILES:
        raise BundleError("Invalid number of file payloads.")
    files = []
    total = 0
    for item in payloads:
        if not isinstance(item, dict) or set(item) != {"path", "content_base64"}:
            raise BundleError("Each file payload needs path and content_base64.")
        encoded = item["content_base64"]
        if not isinstance(encoded, str) or len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4:
            raise BundleError("A file payload exceeds the size limit.")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise BundleError("Invalid base64 file content.") from exc
        total += len(decoded)
        if total > MAX_BUNDLE_BYTES:
            raise BundleError("Decoded bundle exceeds the size limit.")
        files.append((item["path"], decoded))
    return validate_bundle(manifest, files)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _clean_log(data, secrets):
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)
    for value in secrets:
        text = text.replace(value, "[redacted]")
    return text[-MAX_LOG_BYTES:]


def _secret_values(value, key=""):
    found = []
    if isinstance(value, dict):
        for name, child in value.items():
            found.extend(_secret_values(child, name))
    elif isinstance(value, str) and len(value) >= 8 and any(s in key.lower() for s in ("key", "secret", "password", "token")):
        found.append(value)
    return found


def _child_environment():
    allowed = {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
               "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
               "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "LD_LIBRARY_PATH"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1", FREQTRADE__DRY_RUN="true",
               ATLAS_FREQTRADE_ROLE="lab")
    return env


def _terminate(process):
    if os.name == "posix":
        # The parent may have exited while a strategy-created child is still in
        # its group. Always clean up the owned group, not only the parent PID.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait(timeout=5)
    elif process.poll() is None:
        # Only the process group launched by this module is targeted.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        process.wait(timeout=5)


def _native_options(body, manifest):
    if {"strategy", "backtest_cache", "strategy_path", "recursive_strategy_search"} & set(body):
        raise HTTPException(status_code=400, detail="Unsupported or protected backtest option.")
    if body.get("timeframe", manifest["timeframe"]) != manifest["timeframe"]:
        raise HTTPException(status_code=400, detail="Backtest timeframe must match the reviewed manifest.")
    timerange = body.get("timerange")
    if not isinstance(timerange, str) or not _CLOSED_RANGE.fullmatch(timerange):
        raise HTTPException(status_code=400, detail="An explicit start and end timerange is required.")
    from freqtrade.configuration.timerange import TimeRange
    from freqtrade.exceptions import ConfigurationError
    from freqtrade.rpc.api_server.api_schemas import BacktestRequest
    from pydantic import ValidationError
    allowed = set(BacktestRequest.model_fields) - {"strategy", "backtest_cache"}
    if set(body) - allowed:
        raise HTTPException(status_code=400, detail="Unsupported or protected backtest option.")
    try:
        parsed = TimeRange.parse_timerange(timerange)
        if not parsed.startts or not parsed.stopts or parsed.startts >= parsed.stopts:
            raise ValueError("Invalid date order")
        options = BacktestRequest(**{**body, "strategy": manifest["strategy_class"],
                                    "timeframe": manifest["timeframe"], "backtest_cache": "none",
                                    "enable_protections": body.get("enable_protections", False)})
    except (ValueError, ValidationError, ConfigurationError) as exc:
        raise HTTPException(status_code=400, detail="Invalid native backtest settings or timerange.") from exc
    values = options.model_dump(exclude_none=True)
    if isinstance(values.get("stake_amount"), str) and values["stake_amount"] != "unlimited":
        try:
            values["stake_amount"] = float(values["stake_amount"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid stake amount.") from exc
    return values


def _merge_options(config, settings):
    """Match native backtest settings merge without replacing nested FreqAI config."""
    for key, value in settings.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            _merge_options(config[key], value)
        else:
            config[key] = deepcopy(value)


class CandidateJobs:
    """One fresh native child at a time; job metadata and native history survive restart."""

    def __init__(self, user_data):
        self.user_data = _plain_directory(Path(user_data))
        self.root = self.user_data / "atlas_handoff" / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        _plain_directory(self.root)
        self.lock = threading.RLock()
        self.active = None
        self.process = None
        self.cancelled = False
        self.live = None
        self.log = deque()
        self.log_bytes = 0
        self.secrets = []

    def start(self, directory, digest, options, config):
        with self.lock:
            if self.active:
                raise HTTPException(status_code=409, detail="A candidate backtest is already running.")
            _native_busy(reserve=True)
            try:
                bundle = verify_candidate(directory, expected_sha256=digest)
                job_id = uuid.uuid4().hex
                job_dir = self.root / job_id
                job_dir.mkdir()
                self.active = job_id
                self.cancelled = False
                self.log.clear()
                self.log_bytes = 0
                self.secrets = _secret_values(config)
                self.live = {"job_id": job_id, "candidate_sha256": digest, "strategy": bundle.manifest["strategy_class"],
                             "status": "running", "running": True, "exit_code": None,
                             "started_at": _now(), "finished_at": None, "result_filename": None,
                             "error": None, "log_tail": "", "settings": options,
                             "status_url": f"/api/v1/atlas/jobs/{job_id}",
                             "result_url": f"/api/v1/atlas/jobs/{job_id}/result"}
                _write_json(job_dir / "job.json", self.live)
                thread = threading.Thread(target=self._run, args=(job_id, directory, bundle, options, deepcopy(config)),
                                          name="atlas-native-backtest", daemon=True)
                thread.start()
                return dict(self.live)
            except Exception:
                self.active = None
                _native_busy(release=True)
                raise

    def _capture(self, stream):
        try:
            while chunk := os.read(stream.fileno(), 4096):
                with self.lock:
                    self.log.append(chunk)
                    self.log_bytes += len(chunk)
                    while self.log_bytes > MAX_LOG_BYTES and len(self.log) > 1:
                        self.log_bytes -= len(self.log.popleft())
        except (OSError, ValueError):
            pass

    def _run(self, job_id, directory, bundle, options, config):
        job_dir = self.root / job_id
        runtime = job_dir / "config.backtest.json"
        reader = None
        process = None
        state, error, result_filename, exit_code = "failed", None, None, None
        try:
            # Verify again immediately before handing files to the native interpreter.
            bundle = verify_candidate(directory, expected_sha256=bundle.sha256)
            manifest = bundle.manifest
            entry_parent = directory.joinpath(*PurePosixPath(manifest["entrypoint"]).parts).parent
            results = self.user_data / "backtest_results"
            results.mkdir(exist_ok=True)
            _plain_directory(results)
            before = {p.name for p in results.iterdir()}
            _merge_options(config, options)
            config.update(strategy=manifest["strategy_class"], timeframe=manifest["timeframe"],
                          strategy_path=str(entry_parent), recursive_strategy_search=False,
                          user_data_dir=str(self.user_data), dry_run=True, backtest_cache="none",
                          export="trades", exportdirectory=str(results),
                          atlas_handoff={"candidate_sha256": bundle.sha256, "manifest": manifest},
                          telegram={"enabled": False, "token": "", "chat_id": ""})
            for key in ("config_files", "original_config", "runmode", "db_url", "strategy_list", "logfile",
                        "api_server", "discord", "webhook", "coingecko"):
                config.pop(key, None)
            exchange = config.setdefault("exchange", {})
            for key in ("key", "api_key", "apiKey", "secret", "password", "uid", "account_id", "accountId",
                        "private_key", "privateKey", "wallet_address", "walletAddress"):
                exchange.pop(key, None)
            # Config values from native startup include Paths and enums; preserve their strings.
            config = json.loads(json.dumps(config, default=str))
            _write_json(runtime, config)
            runtime.chmod(0o600)
            command = [sys.executable, "-m", "atlas.run_reviewed_backtest", "--config", str(runtime),
                       "--candidate", str(directory), "--sha256", bundle.sha256]
            kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                      "env": _child_environment(), "cwd": str(Path(__file__).resolve().parents[1])}
            if os.name == "posix":
                kwargs["start_new_session"] = True
            else:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            with self.lock:
                if self.cancelled:
                    raise RuntimeError("Backtest was cancelled before launch.")
                process = subprocess.Popen(command, **kwargs)
                self.process = process
            reader = threading.Thread(target=self._capture, args=(process.stdout,), daemon=True)
            reader.start()
            deadline = time.monotonic() + BACKTEST_TIMEOUT_SECONDS
            while process.poll() is None:
                if self.cancelled or time.monotonic() >= deadline:
                    _terminate(process)
                    state = "cancelled" if self.cancelled else "timed_out"
                    error = "Backtest cancelled." if self.cancelled else "Native backtest exceeded its time limit."
                    break
                time.sleep(0.1)
            exit_code = process.wait(timeout=5)
            reader.join(timeout=2)
            if exit_code == 0 and error is None:
                marker = json.loads(_read_regular(results / ".last_result.json", MAX_MANIFEST_BYTES))
                filename = marker.get("latest_backtest")
                if (not isinstance(filename, str) or Path(filename).name != filename
                        or filename in before or not (results / filename).is_file()):
                    raise ValueError("Native output did not identify a new backtest result.")
                metadata_file = results / (Path(filename).stem + ".meta.json")
                metadata = json.loads(_read_regular(metadata_file, MAX_MANIFEST_BYTES))
                if manifest["strategy_class"] not in metadata:
                    raise ValueError("Native result metadata does not identify the reviewed strategy.")
                result_filename = filename
                state = "completed"
            elif error is None:
                error = "Native Freqtrade backtest failed; review the bounded log tail."
        except Exception:
            error = error or "Native backtest could not complete or produce a new result."
        finally:
            if process is not None:
                try:
                    _terminate(process)
                except (OSError, subprocess.SubprocessError):
                    pass
                if process.stdout:
                    process.stdout.close()
            if reader:
                reader.join(timeout=2)
            with self.lock:
                self.live.update(status=state, running=False, exit_code=exit_code,
                                 finished_at=_now(), result_filename=result_filename, error=error,
                                 log_tail=_clean_log(b"".join(self.log), self.secrets))
                try:
                    _write_json(job_dir / "job.json", self.live)
                finally:
                    self.process = None
                    self.active = None
                    _native_busy(release=True)

    def status(self, job_id):
        if not _JOB_ID.fullmatch(job_id):
            raise HTTPException(status_code=404, detail="Backtest job not found.")
        with self.lock:
            if self.active == job_id:
                return {**self.live, "log_tail": _clean_log(b"".join(self.log), self.secrets)}
            path = self.root / job_id / "job.json"
            try:
                record = json.loads(_read_regular(path, 256 * 1024))
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail="Backtest job not found.")
            if record.get("running"):
                record.update(status="interrupted", running=False,
                              error="The lab restarted before this job recorded completion.")
            return record

    def stop(self):
        with self.lock:
            self.cancelled = True
            process = self.process
        if process is not None:
            _terminate(process)


def _jobs(config):
    key = str(Path(config["user_data_dir"]).resolve())
    with _manager_lock:
        if key not in _managers:
            _managers[key] = CandidateJobs(key)
        return _managers[key]


@asynccontextmanager
async def _lifespan(_app):
    yield
    with _manager_lock:
        managers = list(_managers.values())
    for manager in managers:
        manager.stop()


router = APIRouter(dependencies=[Depends(_lab_role)], lifespan=_lifespan)


@router.post("/atlas/candidates", status_code=201)
async def create_candidate(request: Request, config=Depends(_get_config)):
    body = await _body(request)
    try:
        bundle = _decode_bundle(body)
        root = _storage(config)
        root.mkdir(parents=True, exist_ok=True)
        install_candidate(root, bundle, role=os.environ.get("ATLAS_FREQTRADE_ROLE"),
                          reviewed_sha256=body["reviewed_sha256"])
        return _summary(bundle)
    except (BundleError, OSError) as exc:
        raise _error(exc) from exc


@router.get("/atlas/candidates")
def list_candidates(config=Depends(_get_config)):
    root = _storage(config)
    if not root.exists():
        return {"candidates": [], "truncated": False}
    try:
        _plain_directory(root)
        candidates = []
        truncated = False
        for directory in root.iterdir():
            digest = directory.name.removeprefix("atlas_")
            if not directory.name.startswith("atlas_") or not _SHA.fullmatch(digest):
                continue
            if len(candidates) >= 256:
                truncated = True
                break
            try:
                _plain_directory(directory)
                manifest = parse_manifest_json(_read_regular(directory / MANIFEST_NAME, MAX_MANIFEST_BYTES))
                candidates.append({"sha256": digest, "manifest": manifest, "integrity": "not_checked",
                                   "executable_validation": "not_performed", "automatically_promoted": False})
            except (BundleError, OSError):
                candidates.append({"sha256": digest, "integrity": "invalid", "error": "Stored candidate is unreadable."})
        return {"candidates": sorted(candidates, key=lambda item: item["sha256"]), "truncated": truncated}
    except (BundleError, OSError) as exc:
        raise _error(exc) from exc


@router.get("/atlas/candidates/{digest}")
def read_candidate(digest: str, config=Depends(_get_config)):
    try:
        return _summary(verify_candidate(_candidate(config, digest), expected_sha256=digest))
    except (BundleError, OSError) as exc:
        raise _error(exc) from exc


@router.post("/atlas/candidates/{digest}/backtest", status_code=202)
async def backtest_candidate(digest: str, request: Request, config=Depends(_get_config)):
    body = await _body(request, 64 * 1024)
    try:
        directory = _candidate(config, digest)
        bundle = verify_candidate(directory, expected_sha256=digest)
        options = _native_options(body, bundle.manifest)
        return _jobs(config).start(directory, digest, options, config)
    except (BundleError, OSError) as exc:
        raise _error(exc) from exc


@router.get("/atlas/jobs/{job_id}")
def backtest_status(job_id: str, config=Depends(_get_config)):
    return _jobs(config).status(job_id)


@router.get("/atlas/jobs/{job_id}/result")
def backtest_result(job_id: str, config=Depends(_get_config)):
    job = _jobs(config).status(job_id)
    if job["status"] != "completed" or not job.get("result_filename"):
        raise HTTPException(status_code=409, detail="A completed native backtest result is not available.")
    from freqtrade.rpc.api_server.api_backtest import api_backtest_history_result
    return api_backtest_history_result(job["result_filename"], job["strategy"], config=config)
