"""Build a simple self-contained HTML dashboard from the local lab data.

Reads only what's on disk (the journals, the cached candles, the run
heartbeats) plus a best-effort live paper-account snapshot, and renders a single
static HTML file — no server, no JavaScript, no extra dependencies.
"""

from __future__ import annotations

import glob
import html
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

REPO_DIR = Path(__file__).resolve().parents[3]
RESULTS_DIR = REPO_DIR / "results"
DATA_DIR = REPO_DIR / "data"

# Journals are stored in UTC (unambiguous); everything shown to the user is
# converted to this local zone for display.
DISPLAY_TZ = ZoneInfo("America/Mexico_City")


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #
def _deployed_strategy() -> str:
    """Read STRATEGY= from the daily run wrapper (best effort)."""
    try:
        text = (REPO_DIR / "scripts" / "daily_paper_run.sh").read_text()
        m = re.search(r'^STRATEGY="([^"]+)"', text, re.MULTILINE)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "unknown"


def _heartbeats() -> list[dict]:
    out = []
    for name, label, stale_h in [
        ("paperscan", "Daily signal scan", 30),
        ("stopmonitor", "Stop-loss monitor", 2),
    ]:
        path = RESULTS_DIR / f".last_success_{name}"
        if path.exists():
            try:
                ts = int(path.read_text().strip())
                age_h = (time.time() - ts) / 3600
                out.append({
                    "label": label,
                    "when": datetime.fromtimestamp(ts, DISPLAY_TZ).strftime("%Y-%m-%d %H:%M"),
                    "age_h": age_h,
                    "ok": age_h <= stale_h,
                })
                continue
            except (OSError, ValueError):
                pass
        out.append({"label": label, "when": "never", "age_h": None, "ok": False})
    return out


def _account() -> dict | None:
    """Best-effort live paper-account snapshot; None if unavailable (offline/no keys)."""
    try:
        from ..live import PaperBroker

        broker = PaperBroker()
        acct = broker.account()
        acct["positions"] = broker.positions()
        return acct
    except Exception:  # noqa: BLE001 — dashboard must render without the broker
        return None


def _backtests(strategy: str) -> list[dict]:
    """Latest backtest row per symbol for ``strategy`` from journal.csv."""
    path = RESULTS_DIR / "journal.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path)
    df = df[df["strategy"] == strategy]
    if df.empty:
        return []
    df = df.sort_values("timestamp").groupby("symbol", as_index=False).last()
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "symbol": r["symbol"],
            "return_pct": float(r["return_pct"]),
            "buy_hold_pct": float(r["buy_hold_pct"]),
            "alpha": round(float(r["return_pct"]) - float(r["buy_hold_pct"]), 1),
            "sharpe": float(r["sharpe"]),
        })
    return sorted(rows, key=lambda x: x["alpha"], reverse=True)


def _coverage() -> list[dict]:
    rows = []
    for f in sorted(glob.glob(str(DATA_DIR / "*.parquet"))):
        try:
            df = pd.read_parquet(f)
        except Exception:  # noqa: BLE001
            continue
        age_h = (time.time() - os.path.getmtime(f)) / 3600
        rows.append({
            "name": os.path.basename(f).replace(".parquet", ""),
            "rows": len(df),
            "first": str(df.index.min().date()) if len(df) else "-",
            "last": str(df.index.max().date()) if len(df) else "-",
            "nans": int(df.isna().any(axis=1).sum()),
            "age_h": age_h,
        })
    return rows


def _activity() -> dict:
    """Summarise the paper journal: counts and the most recent scan's signals."""
    path = RESULTS_DIR / "paper_journal.csv"
    if not path.exists():
        return {"counts": {}, "last_run": [], "last_when": None}
    df = pd.read_csv(path)
    if df.empty:
        return {"counts": {}, "last_run": [], "last_when": None}
    counts = df["action"].value_counts().to_dict()
    df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    # Show the most recent *signal scan* (exclude the stop-loss monitor rows),
    # collapsed to one row per symbol so two near-simultaneous runs don't mix.
    scan = df[df["strategy"] != "stop-monitor"]
    if scan.empty:
        scan = df
    last_ts = scan["ts"].max()
    last = scan[scan["ts"] >= last_ts - pd.Timedelta(minutes=5)]
    last = last.drop_duplicates(subset="symbol", keep="last")
    last_run = [
        {"symbol": r["symbol"], "signal": r["signal"],
         "holding": r["holding"], "action": r["action"]}
        for _, r in last.iterrows()
    ]
    last_when = last_ts.tz_convert(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M") if pd.notna(last_ts) else None
    return {"counts": counts, "last_run": last_run, "last_when": last_when}


def collect_context() -> dict:
    strategy = _deployed_strategy()
    account = _account()
    backtests = _backtests(strategy)
    coverage = _coverage()
    activity = _activity()

    insights: list[str] = []
    insights.append(f"Paper account is trading <b>{html.escape(strategy)}</b> daily on a held-out, evidence-based basis (see <code>money validate</code>).")
    if account:
        pl = sum(p["unrealized_pl"] for p in account["positions"])
        n = len(account["positions"])
        insights.append(
            f"Equity <b>${account['equity']:,.0f}</b> across <b>{n}</b> position(s); "
            f"aggregate unrealised P&amp;L <b>${pl:,.0f}</b>."
        )
    if backtests:
        beat = sum(1 for b in backtests if b["alpha"] > 0)
        insights.append(f"Latest backtests: {html.escape(strategy)} beat buy &amp; hold on <b>{beat}/{len(backtests)}</b> symbols (by alpha).")
    if coverage:
        stale = sum(1 for c in coverage if c["age_h"] > 24)
        nans = sum(c["nans"] for c in coverage)
        if nans == 0 and stale == 0:
            insights.append(f"All <b>{len(coverage)}</b> cached datasets are fresh and NaN-free.")
        else:
            insights.append(f"<b>{len(coverage)}</b> datasets: <b>{stale}</b> stale (&gt;24h), <b>{nans}</b> NaN rows.")
    if activity["counts"]:
        bought = activity["counts"].get("bought", 0)
        stopped = activity["counts"].get("stopped", 0)
        insights.append(f"Lifetime paper activity: <b>{bought}</b> buys, <b>{stopped}</b> stop-loss exit(s).")
    insights.append("Reminder: paper money only — these strategies are regime-dependent, not a proven edge.")

    return {
        "generated": datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z"),
        "strategy": strategy,
        "heartbeats": _heartbeats(),
        "account": account,
        "backtests": backtests,
        "coverage": coverage,
        "activity": activity,
        "insights": insights,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _sign(v: float) -> str:
    return "pos" if v > 0 else "neg" if v < 0 else "zero"


def _styles() -> str:
    return """
<style>
  .mlab { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
          color: #1c2430; max-width: 1000px; margin: 0 auto; }
  .mlab h1 { font-size: 20px; margin: 0 0 2px; }
  .mlab .sub { color: #6b7785; font-size: 12px; margin-bottom: 18px; }
  .mlab .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; }
  .mlab .card { background: #fff; border: 1px solid #e6e9ee; border-radius: 12px; padding: 14px 16px; }
  .mlab .card h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .05em;
                   color: #8a94a2; margin: 0 0 10px; }
  .mlab table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .mlab th { text-align: right; color: #8a94a2; font-weight: 500; padding: 3px 6px; }
  .mlab th:first-child, .mlab td:first-child { text-align: left; }
  .mlab td { text-align: right; padding: 3px 6px; border-top: 1px solid #f0f2f5;
             font-variant-numeric: tabular-nums; }
  .mlab .pos { color: #0a8f4f; } .mlab .neg { color: #d33; } .mlab .zero { color: #6b7785; }
  .mlab .pill { display: inline-block; padding: 1px 8px; border-radius: 20px; font-size: 12px; font-weight: 600; }
  .mlab .pill.ok { background: #e6f6ee; color: #0a8f4f; }
  .mlab .pill.bad { background: #fdeaea; color: #d33; }
  .mlab .hb { display: flex; justify-content: space-between; align-items: center; padding: 5px 0; font-size: 13px; }
  .mlab ul { margin: 0; padding-left: 18px; font-size: 13px; line-height: 1.7; }
  .mlab .big { font-size: 26px; font-weight: 700; }
  .mlab .muted { color: #8a94a2; font-size: 12px; }
  .mlab .full { grid-column: 1 / -1; }
  @media (prefers-color-scheme: dark) {
    .mlab { color: #dfe5ec; }
    .mlab .card { background: #161a20; border-color: #262c35; }
    .mlab td { border-color: #232a33; }
    .mlab .sub, .mlab .muted, .mlab th, .mlab .card h2 { color: #8a94a2; }
  }
</style>
"""


def _account_card(acct: dict | None) -> str:
    if not acct:
        return ('<div class="card"><h2>Paper account</h2>'
                '<p class="muted">Live snapshot unavailable (offline or no API keys). '
                'Showing local data only.</p></div>')
    rows = ""
    for p in acct["positions"]:
        plc = _sign(p["unrealized_pl"])
        rows += (f"<tr><td>{html.escape(p['symbol'])}</td>"
                 f"<td>${(p['market_value'] or 0):,.0f}</td>"
                 f"<td class='{plc}'>${p['unrealized_pl']:,.0f}</td>"
                 f"<td class='{plc}'>{p['unrealized_plpc']:+.1f}%</td></tr>")
    if not rows:
        rows = "<tr><td colspan='4' class='muted'>no open positions</td></tr>"
    return (
        '<div class="card"><h2>Paper account</h2>'
        f'<div class="big">${acct["equity"]:,.0f}</div>'
        f'<div class="muted">equity &middot; ${acct["cash"]:,.0f} cash</div>'
        '<table style="margin-top:10px"><tr><th>symbol</th><th>value</th><th>P&amp;L</th><th>%</th></tr>'
        f'{rows}</table></div>'
    )


def _health_card(hbs: list[dict]) -> str:
    rows = ""
    for h in hbs:
        pill = '<span class="pill ok">ok</span>' if h["ok"] else '<span class="pill bad">stale</span>'
        age = f"{h['age_h']:.1f}h ago" if h["age_h"] is not None else "—"
        rows += (f'<div class="hb"><span>{html.escape(h["label"])}</span>'
                 f'<span>{pill} <span class="muted">{age}</span></span></div>')
    return f'<div class="card"><h2>System health</h2>{rows}</div>'


def _insights_card(insights: list[str]) -> str:
    items = "".join(f"<li>{s}</li>" for s in insights)
    return f'<div class="card full"><h2>Insights</h2><ul>{items}</ul></div>'


def _backtests_card(bts: list[dict], strategy: str) -> str:
    if not bts:
        return '<div class="card"><h2>Backtests</h2><p class="muted">No backtest runs logged yet.</p></div>'
    rows = ""
    for b in bts:
        rows += (f"<tr><td>{html.escape(b['symbol'])}</td>"
                 f"<td class='{_sign(b['return_pct'])}'>{b['return_pct']:+.0f}%</td>"
                 f"<td>{b['buy_hold_pct']:+.0f}%</td>"
                 f"<td class='{_sign(b['alpha'])}'>{b['alpha']:+.0f}%</td></tr>")
    return (
        f'<div class="card"><h2>Backtests &mdash; {html.escape(strategy)}</h2>'
        '<table><tr><th>symbol</th><th>return</th><th>buy&amp;hold</th><th>alpha</th></tr>'
        f'{rows}</table>'
        '<div class="muted" style="margin-top:8px">alpha = return &minus; buy&amp;hold (positive = beat holding)</div></div>'
    )


def _coverage_card(cov: list[dict]) -> str:
    if not cov:
        return '<div class="card"><h2>Data coverage</h2><p class="muted">No cached data.</p></div>'
    rows = ""
    for c in cov:
        nan_cls = "neg" if c["nans"] else "muted"
        rows += (f"<tr><td>{html.escape(c['name'])}</td><td>{c['rows']}</td>"
                 f"<td class='muted'>{c['first']}&rarr;{c['last']}</td>"
                 f"<td class='{nan_cls}'>{c['nans']}</td></tr>")
    return (
        '<div class="card full"><h2>Data coverage</h2>'
        '<table><tr><th>dataset</th><th>rows</th><th>range</th><th>NaNs</th></tr>'
        f'{rows}</table></div>'
    )


def _activity_card(act: dict) -> str:
    if not act["last_run"]:
        return '<div class="card"><h2>Last scan</h2><p class="muted">No paper activity logged yet.</p></div>'
    rows = ""
    for r in act["last_run"]:
        act_cls = "muted" if r["action"] == "none" else "pos"
        rows += (f"<tr><td>{html.escape(str(r['symbol']))}</td>"
                 f"<td>{html.escape(str(r['signal']))}</td>"
                 f"<td class='{act_cls}'>{html.escape(str(r['action']))}</td></tr>")
    when = f' &middot; {act["last_when"]}' if act["last_when"] else ""
    return (
        f'<div class="card"><h2>Last scan{when}</h2>'
        '<table><tr><th>symbol</th><th>signal</th><th>action</th></tr>'
        f'{rows}</table></div>'
    )


def render_body(ctx: dict) -> str:
    return (
        _styles()
        + '<div class="mlab">'
        + f'<h1>money lab &mdash; dashboard</h1>'
        + f'<div class="sub">generated {ctx["generated"]} &middot; deployed strategy: <b>{html.escape(ctx["strategy"])}</b></div>'
        + '<div class="grid">'
        + _account_card(ctx["account"])
        + _health_card(ctx["heartbeats"])
        + _activity_card(ctx["activity"])
        + _backtests_card(ctx["backtests"], ctx["strategy"])
        + _insights_card(ctx["insights"])
        + _coverage_card(ctx["coverage"])
        + '</div></div>'
    )


def render_page(ctx: dict) -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>money lab — dashboard</title></head>'
        '<body style="margin:0;padding:24px;background:#f4f6f9">'
        + render_body(ctx)
        + '</body></html>'
    )


def write_dashboard(output: Path | None = None) -> Path:
    ctx = collect_context()
    path = output or (RESULTS_DIR / "dashboard.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_page(ctx))
    return path
