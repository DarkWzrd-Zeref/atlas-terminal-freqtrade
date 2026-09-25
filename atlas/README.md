# Atlas Terminal deployment

This fork runs the original Freqtrade application and FreqUI. Upstream licenses
and source remain intact. The initial strategy is the upstream SampleStrategy,
an operational example, not a performance recommendation.

Railway needs a persistent volume at `/freqtrade/user_data` and
`RAILWAY_RUN_UID=0`. The entry point grants that directory to the image's existing
unprivileged user and drops privileges before running Freqtrade. No public domain
is required. The eventual Terminal gateway connects through private networking.

Kraken spot data, BTC/USD and ETH/USD, USD 1,000 simulated wallet, USD 100 stakes,
two maximum positions. No exchange credentials are used. Live trading is forced
off; FREQTRADE environment overrides are stripped. API credentials are random,
persistent, and never logged; the gateway password can be supplied through the
service's `ATLAS_FREQTRADE_PASSWORD` secret.

The service refuses to run without its volume. Strategy files, market data,
backtests, API credentials, logs and the paper database are on that volume.
Do not expose this service publicly without the authenticated Terminal gateway.
