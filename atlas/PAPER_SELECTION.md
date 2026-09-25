# Reviewed paper selection

This service remains dry-run Kraken spot with empty exchange credentials. The
existing SampleStrategy bytes and sidecar/helper files are captured once in
`user_data/atlas_paper/bundles/atlas_<sha256>`. Existing paper trades, orders,
wallet accounting and the database path are preserved. Initial baseline startup
behavior is unchanged. After the first promotion, every selection (including
rollback and ordinary host restarts) starts stopped; native Start is a separate
deliberate action. The existing baseline already disables force-entry.

Authenticated Lab `GET /api/v1/atlas/candidates/{sha}/bundle` returns reverified
`manifest`, `files` (path/base64), `sha256`, and `reviewed_sha256`.

Authenticated paper endpoints (available only in the separate paper role):

- `GET /api/v1/atlas/paper/selection`: strategy, timeframe, selected/loaded/
  previous/staged digests, corresponding display names, status/error,
  runtime_state, baseline, paper_only and selected backtest reference.
- `POST /api/v1/atlas/paper/stage`: manifest, files, reviewed_sha256 and
  backtest `{job_id,result_filename,candidate_sha256}`. Installs inert bytes and
  changes only the staged reference. The authenticated portal verifies the
  actual completed Lab job/report before forwarding; paper validates the binding
  and records that evidence, not an independent cryptographic attestation.
- `POST /api/v1/atlas/paper/activate`: `{reviewed_sha256}`.
- `POST /api/v1/atlas/paper/rollback`: `{expected_current_sha256}`. Protects
  against a stale browser reverting a different current selection.

Activate/rollback return 202 only after a real checkpoint request is persisted.
Actual native open-trade and open-order queries must both be empty. The worker
rechecks at the end of its current iteration, after in-flight entries finish.
A failed checkpoint retains the original selection and restores its prior
runtime state to manage any in-flight position; nothing is closed or reset.
Native Start/reload reject while the change is pending. Successful checkpoints
use upstream cleanup and full process exit before the supervisor switches the
journal and launches a fresh interpreter. The native resolver is bound to the
manifest's exact entrypoint and rechecks every file hash before import. The
loaded hash is recorded only after native initialization succeeds and the
promoted strategy is stopped. Initialization failure restores the prior bundle
stopped. A host restart during an incomplete change restores/retains the last
confirmed selection, rather than assuming a checkpoint passed.

Status values: `baseline`, `staged`, `checkpoint_pending`, `activating`, `ready`,
`failed`, `rolled_back`. `loaded_sha256` identifies this process's verified load;
202 and `activating` are not claims that loading succeeded.

Owner-reviewed Python can access its paper service, paper API credentials and
paper database; this is not a Python sandbox. Parent Railway, portal, Lab and AI
provider credentials are excluded from the child environment. No dependency
installation, live trading activation, code rewriting, synthetic strategy DSL,
automatic trade closure, balance reset or automatic native Start is performed.
The baseline snapshot binds local source/support bytes; third-party Python
dependencies remain pinned by the deployed image, not embedded in each bundle.

Run `python -m unittest discover -s atlas -p 'test*.py' -v`. The Docker build runs
this command with native Freqtrade dependencies, including native Worker/RPC
checkpoint hooks and real SQLite Trade/Order queries. Windows-only local runs
skip full-native and POSIX checks when their dependencies/platform are absent.
Live verification must additionally confirm a stopped native load, selected
digest persistence across service restart, and rollback against the original
unchanged paper database.
