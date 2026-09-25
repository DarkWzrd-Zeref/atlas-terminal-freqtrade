"""Railway entry point for the real Freqtrade runtime, restricted to paper trading."""

import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import time
from copy import deepcopy


def prepare_config(data: Path, source: Path, environ: dict) -> dict:
    data.mkdir(parents=True, exist_ok=True)
    for directory in ("strategies", "logs", "data", "backtest_results"):
        (data / directory).mkdir(exist_ok=True)
    # Credentials survive restarts but never appear in the image or logs.
    credentials = data / "api-credentials.json"
    if credentials.exists():
        auth = json.loads(credentials.read_text())
    else:
        auth = {key: secrets.token_urlsafe(48) for key in ("password", "jwt_secret_key", "ws_token")}
        with credentials.open("x", encoding="utf-8") as handle:
            json.dump(auth, handle)
        credentials.chmod(0o600)
    if environ.get("ATLAS_FREQTRADE_PASSWORD"):
        auth["password"] = environ["ATLAS_FREQTRADE_PASSWORD"]
    config = json.loads((source / "atlas/config.paper.json").read_text())
    config["api_server"].update(auth)
    config["api_server"]["listen_port"] = int(environ.get("PORT", "8080"))
    config["user_data_dir"] = str(data)
    config["db_url"] = "sqlite:///" + str(data / "trades-paper.sqlite")
    sample = data / "strategies/sample_strategy.py"
    if not sample.exists():
        shutil.copyfile(source / "freqtrade/templates/sample_strategy.py", sample)
    return config


def prepare_lab_config(paper: dict, data: Path, port: int = 8081) -> dict:
    """Native webserver mode exposes Freqtrade's own backtests and downloads.

    Keep lab strategies/results separate from the running paper allocation.
    A research file cannot silently replace the bot's installed strategy.
    """
    lab = data / "lab"
    for directory in (lab, lab / "strategies", lab / "data", lab / "backtest_results"):
        directory.mkdir(exist_ok=True)
    sample = lab / "strategies/sample_strategy.py"
    if not sample.exists():
        shutil.copyfile(data / "strategies/sample_strategy.py", sample)
    config = deepcopy(paper)
    config["user_data_dir"] = str(lab)
    config["db_url"] = "sqlite:///" + str(lab / "unused-paper.sqlite")
    config["api_server"]["listen_port"] = port
    config["bot_name"] = "Atlas Strategy Lab"
    # A candidate's native timeframe must not be silently overridden by the
    # initial paper bot's five-minute configuration.
    config.pop("timeframe", None)
    return config


def service_role(environ: dict) -> str:
    # Combined mode is retained only during the non-destructive migration of
    # the existing lab data. Deployed roles switch to paper/lab separately.
    role = environ.get("ATLAS_FREQTRADE_ROLE", "combined")
    if role not in ("paper", "lab", "combined"):
        raise ValueError("ATLAS_FREQTRADE_ROLE must be paper, lab or combined")
    return role


def write_config(path: Path, config: dict):
    with path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
    path.chmod(0o600)


def supervise(commands: list[list[str]], env: dict):
    children = []
    stopping = False
    def stop(*_args):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                child.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for command in commands:
            children.append(subprocess.Popen(command, env=env))
        while not stopping:
            for child in children:
                if child.poll() is not None:
                    raise SystemExit(child.returncode or 1)
            time.sleep(1)
    finally:
        stop()
        for child in children:
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main():
    data = Path("/freqtrade/user_data")
    if not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
        raise SystemExit("Persistent Railway volume required before starting the paper bot.")
    if Path(os.environ["RAILWAY_VOLUME_MOUNT_PATH"]) != data:
        raise SystemExit("Mount the Freqtrade volume at /freqtrade/user_data.")
    # Railway mounts volumes as root. Grant the existing image user access,
    # then drop root before invoking the upstream application.
    if os.getuid() == 0:
        os.chown(data, 1000, 1000)
        os.setgroups([])
        os.setgid(1000)
        os.setuid(1000)
        os.environ["HOME"] = "/home/ftuser"
    config = prepare_config(data, Path("/freqtrade"), os.environ)
    role = service_role(os.environ)
    runtime = data / "config.paper.runtime.json"
    write_config(runtime, config)
    lab_runtime = data / "config.lab.runtime.json"
    if role != "paper":
        write_config(lab_runtime, prepare_lab_config(config, data, int(os.environ.get("PORT", "8080")) if role == "lab" else 8081))
    # Environment overrides must not turn this deployment into a live bot or
    # inject exchange keys. Configuration changes belong to a reviewed release.
    env = {key: value for key, value in os.environ.items() if not key.startswith("FREQTRADE__")}
    env["FREQTRADE__DRY_RUN"] = "true"
    if role == "paper":
        from atlas.paper_supervisor import supervise_paper
        supervise_paper(config, runtime)
        return
    if role == "lab":
        os.execvpe("freqtrade", ["freqtrade", "webserver", "--config", str(lab_runtime)], env)
    supervise([
        ["freqtrade", "trade", "--config", str(runtime), "--logfile", str(data / "logs/freqtrade.log")],
        ["freqtrade", "webserver", "--config", str(lab_runtime)],
    ], env)


if __name__ == "__main__":
    main()
