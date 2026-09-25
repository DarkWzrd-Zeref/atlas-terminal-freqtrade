"""Fresh native paper worker bound to one exact reviewed entrypoint.

The end-of-iteration checkpoint rechecks native DB truth after any in-flight
entry logic has finished. KeyboardInterrupt follows upstream's normal cleanup;
the supervisor waits for full process exit before changing the selection.
"""
import argparse
import json
import os
from pathlib import Path

from atlas.handoff import BundleError, MAX_MANIFEST_BYTES, _read_regular
from atlas.paper_state import store_for
from atlas.run_reviewed_backtest import exact_file_loader


def install_native_hooks(store, digest):
    from freqtrade.enums import State
    from freqtrade.resolvers import StrategyResolver
    from freqtrade.rpc import RPC, RPCException
    from freqtrade.worker import Worker
    from atlas.api_paper import native_flat

    original_init, original_worker = Worker._init, Worker._worker
    original_start, original_reload = RPC._rpc_start, RPC._rpc_reload_config
    StrategyResolver._load_strategy = staticmethod(exact_file_loader(StrategyResolver, store.directory(digest), digest))

    def initialize(worker, reconfig):
        original_init(worker, reconfig)
        config = worker.freqtrade.config
        if config.get("dry_run") is not True or config["exchange"]["name"] != "kraken" or config.get("trading_mode") != "spot":
            raise BundleError("Reviewed paper worker must remain dry-run Kraken spot.")
        store.mark_loaded(digest=digest, strategy=type(worker.freqtrade.strategy).__name__,
                          timeframe=worker.freqtrade.strategy.timeframe,
                          runtime_state=worker.freqtrade.state.name.lower(), pid=os.getpid())

    def iteration(worker, old_state):
        result = original_worker(worker, old_state)
        with worker.freqtrade._exit_lock, store.lock:
            state = store.read()
            if state["request"] and state["request"]["phase"] == "requested":
                checkpoint = store.checkpoint(flat=native_flat())
                if checkpoint["phase"] == "rejected":
                    # An already-running iteration may have opened a trade after
                    # the API check. Restore its prior state to manage that trade.
                    worker.freqtrade.state = State[checkpoint["original_state"].upper()]
                else:
                    worker.freqtrade.state = State.STOPPED
                    raise KeyboardInterrupt()
        return result

    def guarded(function):
        def call(rpc):
            with store.lock:
                if store.read()["request"]:
                    raise RPCException("Paper strategy change is pending; wait for the new stopped selection.")
                return function(rpc)
        return call

    Worker._init, Worker._worker = initialize, iteration
    RPC._rpc_start, RPC._rpc_reload_config = guarded(original_start), guarded(original_reload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    if os.environ.get("ATLAS_FREQTRADE_ROLE") != "paper":
        raise SystemExit("Reviewed trade worker requires the isolated paper role.")
    config = json.loads(_read_regular(Path(args.config), MAX_MANIFEST_BYTES * 2))
    store = store_for(config["user_data_dir"])
    expected = store.runtime_config(config)
    for key in ("dry_run", "trading_mode", "strategy", "timeframe", "strategy_path", "atlas_paper_sha256", "initial_state"):
        if config.get(key) != expected.get(key):
            raise SystemExit("Paper runtime differs from the exact reviewed selection.")
    if config["exchange"]["name"] != "kraken" or any(config["exchange"].get(k) for k in ("key", "secret", "password", "uid")):
        raise SystemExit("Paper exchange configuration must contain no trading credentials.")
    install_native_hooks(store, config["atlas_paper_sha256"])
    from freqtrade.main import main as freqtrade_main
    freqtrade_main(["trade", "--config", args.config,
                    "--logfile", str(store.data / "logs/freqtrade.log")])


if __name__ == "__main__":
    main()
