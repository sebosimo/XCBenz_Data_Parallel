# Repository Map For Agents

This repository is `sebosimo/XCBenz_Data_Parallel`, the public XCBenz forecast
fetch, export, and publication repository.

Related repository:

- `sebosimo/XCBenz_Temps`: private Streamlit app repository. It contains app/UI
  code and private thermal-model code.

## Production Ownership

- The Coding Server polls MeteoSwiss every five minutes and is the primary
  publisher of browser-ready artifacts to
  `https://data.xcbenz.com/web_exports/`.
- Infomaniak is the production data source of truth.
- The Hetzner weather server checks the public manifest every 30 minutes and
  dispatches GitHub Actions only after two consecutive stale observations of
  the same complete CH1/CH2 source pair.
- GitHub Actions retains an independent six-hour schedule as a final recovery
  layer and rechecks the live manifest before heavy work.
- GitHub Actions is the only writer of the generated `data-web` branch. The
  Coding Server must keep `XCBENZ_PUSH_DATA_BRANCH=false`.

Rules of thumb:

- Production fetcher, `locations.json`, manifest, cache layout, direct web
  exports, and publication safety changes belong here.
- UI and rendering changes belong in `XCBenz_Temps`.
- If a change affects both generated data and app reading or display, update
  both repositories together.
- Keep private app code and thermal-model code out of this public repository.
- Do not manually force-push `main` or `data-web`.
- Preserve the shared remote publish lock, downgrade guard, atomic swap, and
  remote validation on every production publication path.
