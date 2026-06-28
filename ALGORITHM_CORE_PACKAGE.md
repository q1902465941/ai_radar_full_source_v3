# AI Radar Algorithm Core Package

This archive extracts only the current algorithm core from the local project.

Included areas:

- Radar scoring, factor normalization, top symbol ranking, and signal assembly.
- Binance market-data adapters for kline, depth, open interest, taker volume, and funding inputs.
- Strategy generation, validation gates, replay memory, and sample-driven strategy evolution.
- Trading execution decision logic, risk checks, order sizing, and live executor interfaces.
- Position lifecycle management, close evaluation, and protection logic.
- Core persistence models required by the algorithm layer.
- Focused core tests and algorithm/API documentation.

Excluded areas:

- `.env` and any local API credentials.
- Local databases and runtime data under `data/`.
- Virtual environments, caches, bytecode, logs, and pytest artifacts.
- Frontend UI source and build output.

The packaged source is intended for algorithm review, migration, or isolated testing. It does not include private runtime state.
