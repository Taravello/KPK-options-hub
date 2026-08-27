"""
refresh.py - builds the kpk options hub snapshot from public, keyless APIs.

    pip install -r requirements.txt
    python refresh.py              # full refresh (Deribit + Binance)
    python refresh.py --offline    # rebuild page files from data/latest.json

Outputs
    data/latest.json        the snapshot - source of truth
    options-data.js         generated file the page reads
    kpk-options-hub.html    single self-contained copy (index.html + data inline)

Data sources (all public, no API key anywhere)
    Deribit  /public/get_book_summary_by_currency   full option board, mark IVs
    Deribit  /public/get_index_price                spot index
    Deribit  /public/get_volatility_index_data      DVOL history
    Binance  /api/v3/klines                         daily + 5m price history
             (falls back to Deribit PERPETUAL chart data if unreachable)

Accuracy notes
    - Bid/ask IVs are backed out locally with Black-76 on the per-expiry
      forward, r = 0, premium converted at the spot index. The refresh prints
      the median reconstruction error of Deribit's own mark_iv as a self-check;
      it should be well under 1 vol pt.
    - Realised vol short windows (7/14/30d) use 5-minute bars; long windows and
      the cone use daily closes. Log returns, annualised sqrt(365 * bars/day).
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
UA = {"User-Agent": "kpk-options-hub/1.0"}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


# ---------------------------------------------------------------- maths
def _N(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black76(F: float, K: float, T: float, sigma: float, kind: str) -> float:
    """Undiscounted Black-76 (r = 0). Price in USD per unit of underlying."""
    if T <= 0 or sigma <= 0:
        return max(F - K, 0.0) if kind == "call" else max(K - F, 0.0)
    v = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / v
    d2 = d1 - v
    if kind == "call":
        return F * _N(d1) - K * _N(d2)
    return K * _N(-d2) - F * _N(-d1)


def implied_vol(price: float, F: float, K: float, T: float, kind: str,
                lo: float = 1e-4, hi: float = 8.0) -> float | None:
    """Bisection, same scheme validated in Options modelling/lpvol/venues.py."""
    if price <= 0 or T <= 0:
        return None
    intrinsic = max(F - K, 0.0) if kind == "call" else max(K - F, 0.0)
    if price <= intrinsic or price >= (F if kind == "call" else K):
        return None
    for _ in range(100):
        m = 0.5 * (lo + hi)
        if black76(F, K, T, m, kind) < price:
            lo = m
        else:
            hi = m
    return 0.5 * (lo + hi)


def ann_vol(closes: list[float], bars_per_year: float) -> float | None:
    if len(closes) < 3:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    if len(rets) < 2:
        return None
    return statistics.stdev(rets) * math.sqrt(bars_per_year)


def percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile, p in [0, 100]."""
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100.0
    f = math.floor(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


# ---------------------------------------------------------------- fetchers
def get(url: str, params: dict, tries: int = 3) -> dict | list:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:                            # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {url} failed after {tries} tries: {last}")


def deribit(method: str, params: dict):
    out = get(f"https://www.deribit.com/api/v2/public/{method}", params)
    return out["result"]


def parse_instrument(name: str):
    """'ETH-27MAR26-3000-C' -> (expiry datetime UTC 08:00, strike, kind)."""
    parts = name.split("-")
    if len(parts) != 4:
        return None
    d = parts[1]
    try:
        day, mon, yr = int(d[:-5]), MONTHS[d[-5:-2]], 2000 + int(d[-2:])
        exp = datetime(yr, mon, day, 8, 0, tzinfo=timezone.utc)
        strike = float(parts[2].replace("d", "."))
        kind = "call" if parts[3] == "C" else "put"
        return exp, strike, kind
    except (ValueError, KeyError):
        return None


def fetch_board(currency: str, spot: float, uni: dict, now: datetime):
    """Full option board -> per-expiry ladders with locally backed-out IVs."""
    rows = deribit("get_book_summary_by_currency",
                   {"currency": currency, "kind": "option"})
    by_exp: dict[str, dict] = {}
    iv_err = []
    for r in rows:
        parsed = parse_instrument(r["instrument_name"])
        if not parsed:
            continue
        exp, K, kind = parsed
        T_days = (exp - now).total_seconds() / 86400.0
        if not (uni["min_days"] <= T_days <= uni["max_days"]):
            continue
        F = r.get("underlying_price") or spot
        if not (uni["min_moneyness"] <= K / F <= uni["max_moneyness"]):
            continue
        T = T_days / 365.0
        code = r["instrument_name"].split("-")[1]
        e = by_exp.setdefault(code, {
            "code": code, "date": exp.strftime("%Y-%m-%d"),
            "days": round(T_days, 2), "fwd": [], "strikes": {}})
        e["fwd"].append(F)
        s = e["strikes"].setdefault(K, {})
        # premium: Deribit quotes in base currency; USD = price * spot index
        leg = {}
        for side, px in (("bid", r.get("bid_price")),
                         ("ask", r.get("ask_price")),
                         ("mark", r.get("mark_price"))):
            usd = px * spot if px else None
            leg[side] = round(usd, 2) if usd else None
            iv = implied_vol(usd, F, K, T, kind) if usd else None
            leg[side[0] + "iv"] = round(iv * 100, 2) if iv else None
        if r.get("mark_iv"):
            leg["miv_venue"] = r["mark_iv"]
            if leg.get("miv"):
                iv_err.append(abs(leg["miv"] - r["mark_iv"]))
            leg["miv"] = r["mark_iv"]        # publish the venue's own mark IV
        leg["oi"] = r.get("open_interest") or 0
        leg.pop("miv_venue", None)
        s[kind] = leg

    expiries = []
    for code, e in sorted(by_exp.items(),
                          key=lambda kv: kv[1]["days"]):
        F = statistics.median(e["fwd"])
        ladder = []
        for K in sorted(e["strikes"]):
            row = {"k": K}
            row.update({"call": e["strikes"][K].get("call"),
                        "put": e["strikes"][K].get("put")})
            ladder.append(row)
        # ATM mark IV: linear interpolation of OTM-side mark IVs at K = F
        pts = []
        for row in ladder:
            leg = row["put"] if row["k"] < F else row["call"]
            if leg and leg.get("miv"):
                pts.append((row["k"], leg["miv"]))
        atm = None
        for i in range(1, len(pts)):
            (k0, v0), (k1, v1) = pts[i - 1], pts[i]
            if k0 <= F <= k1:
                atm = v0 + (v1 - v0) * (F - k0) / (k1 - k0) if k1 > k0 else v0
                break
        if atm is None and pts:
            atm = min(pts, key=lambda p: abs(p[0] - F))[1]
        expiries.append({"code": code, "date": e["date"], "days": e["days"],
                         "fwd": round(F, 2),
                         "atm": round(atm, 2) if atm else None,
                         "strikes": ladder})
    med_err = statistics.median(iv_err) if iv_err else None
    return expiries, med_err


def fetch_dvol(currency: str, days: int = 730):
    end = int(time.time() * 1000)
    start = end - days * 86400_000
    out, seen = [], set()
    for _ in range(6):
        res = deribit("get_volatility_index_data",
                      {"currency": currency, "resolution": "86400",
                       "start_timestamp": start, "end_timestamp": end})
        batch = res.get("data") or []
        for ts, _o, _h, _l, c in batch:
            if ts not in seen:
                seen.add(ts)
                out.append((ts, c))
        cont = res.get("continuation")
        if not cont or not batch:
            break
        end = cont
    out.sort()
    return [[datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
             .strftime("%Y-%m-%d"), round(c, 2)] for ts, c in out]


def binance_klines(symbol: str, interval: str, start_ms: int | None = None,
                   limit: int = 1000):
    p = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms:
        p["startTime"] = start_ms
    return get("https://api.binance.com/api/v3/klines", p)


def fetch_daily_closes(symbol: str, days: int, deribit_ccy: str):
    """[[iso_date, close], ...] oldest first. Binance, Deribit perp fallback."""
    try:
        rows = binance_klines(symbol, "1d", limit=min(days, 1000))
        return [[datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc)
                 .strftime("%Y-%m-%d"), float(r[4])] for r in rows]
    except Exception as e:                                # noqa: BLE001
        print(f"  Binance daily failed ({e}); falling back to Deribit perp")
        end = int(time.time())
        res = deribit("get_tradingview_chart_data",
                      {"instrument_name": f"{deribit_ccy}-PERPETUAL",
                       "resolution": "1D",
                       "start_timestamp": (end - days * 86400) * 1000,
                       "end_timestamp": end * 1000})
        return [[datetime.fromtimestamp(t / 1000, tz=timezone.utc)
                 .strftime("%Y-%m-%d"), float(c)]
                for t, c in zip(res["ticks"], res["close"])]


def fetch_5m_closes(symbol: str, days: int, deribit_ccy: str):
    """Close series of 5m bars covering the last `days` days, oldest first."""
    try:
        out = []
        start = int((time.time() - days * 86400) * 1000)
        while True:
            rows = binance_klines(symbol, "5m", start_ms=start)
            if not rows:
                break
            out.extend(float(r[4]) for r in rows)
            if len(rows) < 1000:
                break
            start = rows[-1][0] + 1
            time.sleep(0.15)
        return out
    except Exception as e:                                # noqa: BLE001
        print(f"  Binance 5m failed ({e}); falling back to Deribit perp")
        end = int(time.time())
        res = deribit("get_tradingview_chart_data",
                      {"instrument_name": f"{deribit_ccy}-PERPETUAL",
                       "resolution": "5",
                       "start_timestamp": (end - days * 86400) * 1000,
                       "end_timestamp": end * 1000})
        return [float(c) for c in res["close"]]


# ---------------------------------------------------------------- assembly
def build_asset(acfg: dict, cfg: dict, now: datetime) -> dict:
    sym = acfg["symbol"]
    print(f"[{sym}] spot index ...")
    spot = deribit("get_index_price",
                   {"index_name": acfg["deribit_index"]})["index_price"]

    print(f"[{sym}] option board ...")
    expiries, med_err = fetch_board(acfg["deribit_currency"], spot,
                                    cfg["universe"], now)
    n_opt = sum(len(e["strikes"]) for e in expiries)
    print(f"  {len(expiries)} expiries, {n_opt} strike rows, "
          f"mark-IV reconstruction median error "
          f"{med_err:.2f} vol pts" if med_err is not None else "  no IV check")

    print(f"[{sym}] DVOL history ...")
    dvol_hist = fetch_dvol(acfg["deribit_currency"],
                           cfg["realised"]["history_days"])
    dvol_now = dvol_hist[-1][1] if dvol_hist else None
    dvol_1y = [c for d, c in dvol_hist[-365:]]
    dvol_pct = (100.0 * sum(1 for c in dvol_1y if c <= dvol_now)
                / len(dvol_1y)) if dvol_1y and dvol_now else None

    print(f"[{sym}] daily history ...")
    daily = fetch_daily_closes(acfg["binance_symbol"],
                               cfg["realised"]["history_days"],
                               acfg["deribit_currency"])
    closes = [c for _d, c in daily]

    print(f"[{sym}] 5m bars (last 30d) ...")
    m5 = fetch_5m_closes(acfg["binance_symbol"], 30, acfg["deribit_currency"])

    # headline realised vols: short windows on 5m bars, long on daily closes
    realised = {}
    for w in cfg["realised"]["intraday_windows"]:
        seg = m5[-(w * 288 + 1):]
        v = ann_vol(seg, 365 * 288)
        if v:
            realised[str(w)] = {"vol": round(v * 100, 2), "src": "5m"}
    for w in cfg["realised"]["daily_windows"]:
        v = ann_vol(closes[-(w + 1):], 365)
        if v and str(w) not in realised:
            realised[str(w)] = {"vol": round(v * 100, 2), "src": "1d"}

    # vol cone on daily closes: rolling window vols across the whole history
    cone = {"windows": [], "p05": [], "p25": [], "p50": [], "p75": [],
            "p95": [], "cur": []}
    for w in sorted(set(cfg["realised"]["intraday_windows"] +
                        cfg["realised"]["daily_windows"])):
        samples = []
        for i in range(w + 1, len(closes) + 1):
            v = ann_vol(closes[i - w - 1:i], 365)
            if v:
                samples.append(v * 100)
        if len(samples) < 30:
            continue
        cur = samples[-1]
        samples_sorted = sorted(samples)
        cone["windows"].append(w)
        for p, key in ((5, "p05"), (25, "p25"), (50, "p50"),
                       (75, "p75"), (95, "p95")):
            cone[key].append(round(percentile(samples_sorted, p), 2))
        cone["cur"].append(round(cur, 2))

    # rolling 30d realised vol series for the history chart
    rv30 = []
    for i in range(31, len(daily) + 1):
        v = ann_vol(closes[i - 31:i], 365)
        if v:
            rv30.append([daily[i - 1][0], round(v * 100, 2)])

    return {
        "spot": round(spot, 2),
        "spot_history": [[d, round(c, 2)] for d, c in daily],
        "realised": realised,
        "cone": cone,
        "rv30_history": rv30,
        "dvol": {"current": dvol_now, "pct_1y": round(dvol_pct, 1)
                 if dvol_pct is not None else None, "history": dvol_hist},
        "expiries": expiries,
        "iv_check_median_err": round(med_err, 3) if med_err is not None
        else None,
    }


def write_outputs(snapshot: dict):
    DATA.mkdir(exist_ok=True)
    (DATA / "latest.json").write_text(json.dumps(snapshot), encoding="utf-8")
    js = ("// generated by refresh.py - do not edit\n"
          "window.KPK_OPTIONS_DATA = " + json.dumps(snapshot) + ";\n")
    (ROOT / "options-data.js").write_text(js, encoding="utf-8")
    idx = ROOT / "index.html"
    if idx.exists():
        html = idx.read_text(encoding="utf-8")
        tag = '<script src="options-data.js"></script>'
        if tag in html:
            html = html.replace(
                tag, "<script>\nwindow.KPK_OPTIONS_DATA = "
                     + json.dumps(snapshot) + ";\n</script>")
        (ROOT / "kpk-options-hub.html").write_text(html, encoding="utf-8")
    sizes = {p.name: f"{p.stat().st_size/1024:.0f} KB"
             for p in [DATA / 'latest.json', ROOT / 'options-data.js']
             if p.exists()}
    print("written:", sizes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="rebuild page files from data/latest.json")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

    if args.offline:
        snapshot = json.loads((DATA / "latest.json").read_text("utf-8"))
        write_outputs(snapshot)
        return

    now = datetime.now(timezone.utc)
    snapshot = {
        "generated_utc": now.strftime("%Y-%m-%d %H:%M UTC"),
        "generated_ts": int(now.timestamp() * 1000),
        "assets": {},
        "meta": {
            "providers": cfg.get("providers", []),
            "thresholds": cfg.get("thresholds", {}),
        },
    }
    for acfg in cfg["assets"]:
        snapshot["assets"][acfg["symbol"]] = build_asset(acfg, cfg, now)
    write_outputs(snapshot)
    print("done:", snapshot["generated_utc"])


if __name__ == "__main__":
    sys.exit(main())
