# kpk options hub

One page to decide whether an option subscription makes sense: the live market
state for volatility, a checker that converts any provider quote into implied
volatility and benchmarks it against the listed market, and a comparison table
that re-marks every saved quote against today's surface. Written for a mixed
audience — every verdict is in plain language, every number is reproducible.

**Live:** https://taravello.github.io/kpk-options-hub/

## What it answers

1. **Is now a good time to be selling volatility at all?**
   30-day realised vs 30-day implied (the seller's margin), the DVOL percentile
   over the past year, a 2-year volatility cone, and the full ATM term
   structure — with a one-paragraph regime verdict.
   An **analysis date** picker (presets or any custom day in the stored 2-year
   history) rewinds the whole market-state section: spot, realised windows,
   DVOL and its percentile, the cone markers and the regime verdict are all
   recomputed as of the selected day, for both assets. Listed option quotes
   cannot be rewound — board-dependent cards keep the live snapshot and say so.
2. **Is this specific provider quote good?**
   Enter a quote in any convention a provider uses (upfront % of notional, APR
   on collateral, USD or asset per unit). The hub backs out the quote's implied
   vol with Black-76 on the Deribit forward and compares it against the listed
   **bid** at the matched strike and tenor — the price the treasury could
   actually sell at instead. Verdicts follow three ordered rules:
   below realised vol → **blocked** (hard rule from the July 2026 research);
   ≥ 3 vol pts over the listed bid → **attractive**; inside the spread →
   **fair, negotiable**; under the bid → **decline**.
3. **How do open opportunities compare?**
   Saved quotes are re-marked on every page load against the current surface
   and remaining tenor, ranked by edge. A baseline grid shows the annualised
   premium the listed market itself pays a seller today across strikes and
   tenors — the floor any provider must beat.

## Run it

```bash
pip install -r requirements.txt
python refresh.py              # pull a fresh snapshot (~30s, no API keys)
python validate.py             # pricing engine self-test (run after any change)
```

Open `index.html` (loads `options-data.js` over `file://`), or share
`kpk-options-hub.html` — one self-contained file, no server, no secrets.

## Accuracy

This project treats pricing accuracy as a tested property, not an intention:

- `validate.py` checks put-call parity to 1e-9, IV round-trips to 1e-6, and
  reproduces the two published anchors from the July 2026 options research
  (BTC 60k CSP → 36.35% IV, ETH 1800 CSP → 53.9% IV).
- The page's JS engine re-prices the same reference cases on every load and
  reports the maximum deviation in the footer (typically < 0.001 vol pts).
- `refresh.py` backs out IVs locally from Deribit prices and reports the
  median gap vs Deribit's own mark IVs (~0.25 vol pts) as a convention check.
- Conventions carried over from `Options modelling/lpvol/venues.py`:
  CSP APR premium = APR × T × strike; covered-call APR premium = APR × T ×
  spot; sellers are benchmarked against the **bid**, never the mark.

## Data

| What | Source | Key |
|---|---|---|
| Option board, mark IVs, forwards | Deribit public API | none |
| DVOL history (2y) | Deribit public API | none |
| Daily closes (2y) + 5m bars (30d) | Binance public API (Deribit perp fallback) | none |

Realised vol short windows (7/14/30d) use 5-minute bars — the honest
estimator — long windows and the cone use daily closes. Saved provider quotes
live in the browser's localStorage only; nothing leaves the machine.

## Configuration

Everything lives in `config.yaml`: the asset list (add a Deribit currency +
Binance symbol and it appears on the page), the provider dropdown, the listed
universe bounds, realised-vol windows, and the decision thresholds
(±3 vol pts, block-below-realised).

## Daily refresh

`.github/workflows/refresh.yml` runs at 07:40 UTC (plus a manual **Run
workflow** button), refreshes the snapshot, re-runs the pricing self-test,
commits, and GitHub Pages redeploys. No repository secrets are needed.

## Publishing (first-time setup)

1. Push this repo to the **personal** account (`taravello`) — never the
   KPK Labs org; the committed `pre-push` guard in `hooks/` blocks org
   remotes mechanically (`git config core.hooksPath hooks` activates it).
2. On GitHub: **Settings → Pages → Deploy from a branch → main / (root)**.
3. Done — `index.html` is served at the Pages URL and the workflow keeps it
   fresh daily. Share the URL internally; all data on the page is public.

## Files

| Path | Role |
|---|---|
| `index.html` | The page (inline SVG charts, no dependencies) |
| `options-data.js` | Generated snapshot the page reads |
| `refresh.py` | The builder — Deribit + Binance → snapshot |
| `validate.py` | Pricing accuracy harness |
| `config.yaml` | Assets, providers, thresholds |
| `data/latest.json` | The snapshot — source of truth |
| `kpk-options-hub.html` | Single-file offline copy |
