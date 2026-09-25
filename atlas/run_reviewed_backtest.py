"""Fixed fresh-process launcher for native Freqtrade backtesting of one reviewed file.

Normal native directory discovery may choose another file declaring the same class.
For this process only, bind its loader to the manifest's exact entrypoint, using the
upstream loader/validator. All actual backtesting, settings and result serialization
remain native Freqtrade code. This launcher never selects or starts paper/live trade.
"""

import argparse
import json
import os
from pathlib import Path, PurePosixPath

from atlas.handoff import BundleError, MAX_MANIFEST_BYTES, _read_regular, verify_candidate


def exact_file_loader(resolver, directory, digest):
    """Return a native resolver hook that cannot discover a different source file."""
    directory = Path(directory)
    bundle = verify_candidate(directory, expected_sha256=digest)
    manifest = bundle.manifest
    entrypoint = directory.joinpath(*PurePosixPath(manifest["entrypoint"]).parts)

    def load(strategy_name, config, extra_dir=None):
        if strategy_name != manifest["strategy_class"]:
            raise BundleError("Native backtest requested a different class than the reviewed manifest.")
        verify_candidate(directory, expected_sha256=digest)
        found = next(resolver._get_valid_object(entrypoint, strategy_name), None)
        if found is None:
            raise BundleError("Reviewed entrypoint does not define the requested native IStrategy class.")
        strategy_class, source = found
        strategy_class.__file__ = str(entrypoint)
        strategy_class.__source__ = source
        return resolver.validate_strategy(strategy_class(config=config))

    return load


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    if os.environ.get("ATLAS_FREQTRADE_ROLE") != "lab":
        raise SystemExit("Reviewed backtests require the isolated lab role.")
    bundle = verify_candidate(Path(args.candidate), expected_sha256=args.sha256)
    manifest = bundle.manifest
    config = json.loads(_read_regular(Path(args.config), MAX_MANIFEST_BYTES * 2))
    if (config.get("dry_run") is not True or config.get("strategy") != manifest["strategy_class"]
            or config.get("timeframe") != manifest["timeframe"]
            or config.get("atlas_handoff", {}).get("candidate_sha256") != args.sha256):
        raise SystemExit("Native backtest configuration differs from the reviewed candidate.")
    entry_parent = Path(args.candidate).joinpath(*PurePosixPath(manifest["entrypoint"]).parts).parent
    from freqtrade.main import main as freqtrade_main
    from freqtrade.resolvers import StrategyResolver
    original = StrategyResolver._load_strategy
    StrategyResolver._load_strategy = staticmethod(exact_file_loader(StrategyResolver, Path(args.candidate), args.sha256))
    try:
        freqtrade_main(["backtesting", "--config", args.config,
                        "--strategy", manifest["strategy_class"], "--strategy-path", str(entry_parent),
                        "--timeframe", manifest["timeframe"], "--timerange", config["timerange"],
                        "--cache", "none", "--export", "trades", "--export-directory", config["exportdirectory"]])
    finally:
        StrategyResolver._load_strategy = staticmethod(original)


if __name__ == "__main__":
    main()
