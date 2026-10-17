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

from ..data.cache import cache_file_role

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
        ("context", "Shadow context collection", 30),
        ("stockexecution", "Guarded stock execution", 96),
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
    """Cached datasets, tagged by role.

    Legacy per-day keys are counted but not opened: a few hundred of them used
    to dominate the stale census and hide a genuinely stale live-window file.
    """
    rows = []
    for f in sorted(glob.glob(str(DATA_DIR / "*.parquet"))):
        name = os.path.basename(f)
        role = cache_file_role(name)
        age_h = (time.time() - os.path.getmtime(f)) / 3600
        if role == "legacy":
            rows.append({
                "name": name.removesuffix(".parquet"),
                "rows": 0,
                "first": "-",
                "last": "-",
                "nans": 0,
                "age_h": age_h,
                "role": role,
            })
            continue
        try:
            df = pd.read_parquet(f)
        except Exception:  # noqa: BLE001
            continue
        rows.append({
            "name": name.removesuffix(".parquet"),
            "rows": len(df),
            "first": str(df.index.min().date()) if len(df) else "-",
            "last": str(df.index.max().date()) if len(df) else "-",
            "nans": int(df.isna().any(axis=1).sum()),
            "age_h": age_h,
            "role": role,
        })
    return rows


def _empty_activity() -> dict:
    return {"counts": {}, "last_run": [], "last_when": None}


def _activity() -> dict:
    """Most recent scan: the legacy journal, else the live ledger.

    The frozen desk records decisions in the ledger and never writes
    ``paper_journal.csv``. A journal-only digest then says "nothing logged
    yet" on the scan that just sold. A journal that has rows still wins —
    that path is the legacy ``paper-run`` command.
    """
    journal = _journal_activity()
    if journal["last_run"] or journal["counts"]:
        return journal
    return _ledger_activity() or journal


def _journal_activity() -> dict:
    path = RESULTS_DIR / "paper_journal.csv"
    if not path.exists():
        return _empty_activity()
    df = pd.read_csv(path)
    if df.empty:
        return _empty_activity()
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


def _held_at_scan(reason: object) -> bool:
    """True only when this scan row was written while the book held the name.

    A down-cross on a name we do not hold is recorded as SELL +
    ``no_fresh_cross``. Treating every SELL as a holding made the digest
    list AVAX as held when the book had no AVAX position.
    """
    text = str(reason or "")
    return text == "holding" or text == "reverse_cross" or text.startswith("stop:")


def _ledger_activity() -> dict | None:
    """Latest baseline scan from the live ledger, shaped like a journal scan.

    Crypto and equity close on different ``bar_end`` clocks, so the window is
    the scan's ``decided_at`` (one run writes both). A later STOP must not
    hide that scan: it is a different event, and the stop monitor already
    logs it.
    """
    config_path = REPO_DIR / "config" / "run3.yaml"
    if not config_path.exists():
        return None
    try:
        from ..run2 import RunLedger, load_run_config

        cfg = load_run_config(config_path)
        ledger_path = RESULTS_DIR / cfg.run_id / "ledger.sqlite"
        if not ledger_path.exists():
            return None
        with RunLedger(ledger_path) as ledger:
            rows = [dict(r) for r in ledger.decisions(cfg.run_id, portfolio="baseline")]
    except Exception:  # noqa: BLE001 — the digest must stay best-effort
        return None
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    frame["ts"] = pd.to_datetime(frame["decided_at"], errors="coerce", utc=True)
    scan = frame[frame["signal"] != "STOP"]
    if scan.empty:
        scan = frame
    last_ts = scan["ts"].max()
    if pd.isna(last_ts):
        return None
    last = scan[scan["ts"] >= last_ts - pd.Timedelta(minutes=5)]
    last = last.drop_duplicates(subset="symbol", keep="last").sort_values("symbol")
    last_run = [
        {
            "symbol": r["symbol"],
            "signal": r["signal"],
            "holding": _held_at_scan(r["reason"]),
            "action": r["action"],
            "reason": r["reason"],
        }
        for _, r in last.iterrows()
    ]
    return {
        "counts": {},
        "last_run": last_run,
        "last_when": last_ts.tz_convert(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M"),
        "source": "ledger",
    }


def _run2_summary() -> dict | None:
    """Read the live run's ledger without requiring broker connectivity."""
    config_path = REPO_DIR / "config" / "run3.yaml"
    if not config_path.exists():
        return None
    try:
        from ..run2 import RunLedger, load_run_config
        from ..run2.reporting import collect_run_report

        cfg = load_run_config(config_path)
        ledger_path = RESULTS_DIR / cfg.run_id / "ledger.sqlite"
        if not ledger_path.exists():
            return None
        with RunLedger(ledger_path) as ledger:
            return collect_run_report(cfg, ledger)
    except Exception:  # noqa: BLE001 — dashboard must remain best-effort
        return None


def collect_context() -> dict:
    strategy = _deployed_strategy()
    account = _account()
    backtests = _backtests(strategy)
    coverage = _coverage()
    activity = _activity()
    run2 = _run2_summary()

    insights: list[str] = []
    insights.append(
        f"Legacy diagnostics use <b>{html.escape(strategy)}</b>; per-symbol alpha is not "
        "portfolio evidence. The frozen live-run ledger is authoritative when initialized."
    )
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
        # Alarm only on the window the desk reads, plus non-alpaca files.
        # Replay age is normal between experiment sittings (they refetch on
        # use); legacy keys are dead and must not look like a data outage.
        watched = [c for c in coverage if c.get("role") not in ("legacy", "replay")]
        legacy_n = sum(1 for c in coverage if c.get("role") == "legacy")
        replay_nans = sum(c["nans"] for c in coverage if c.get("role") == "replay")
        stale = sum(1 for c in watched if c["age_h"] > 24)
        nans = sum(c["nans"] for c in watched)
        if not watched:
            insights.append("No live-window datasets cached.")
        elif nans == 0 and stale == 0:
            insights.append(
                f"All <b>{len(watched)}</b> live-window datasets are fresh and NaN-free."
            )
        else:
            insights.append(
                f"<b>{len(watched)}</b> live-window datasets: <b>{stale}</b> stale "
                f"(&gt;24h), <b>{nans}</b> NaN rows."
            )
        if legacy_n:
            insights.append(
                f"<b>{legacy_n}</b> legacy daily-key files ignored "
                "(dead cache keys, not a data outage)."
            )
        if replay_nans:
            insights.append(
                f"Full-history replay cache has <b>{replay_nans}</b> NaN rows."
            )
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
        "run2": run2,
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
    shown = [c for c in cov if c.get("role") != "legacy"]
    legacy_n = len(cov) - len(shown)
    if not shown:
        return (
            '<div class="card full"><h2>Data coverage</h2>'
            f'<p class="muted">{legacy_n} legacy daily-key files, no live dataset.</p></div>'
        )
    rows = ""
    for c in shown:
        nan_cls = "neg" if c["nans"] else "muted"
        role = html.escape(c.get("role") or "—")
        rows += (f"<tr><td>{html.escape(c['name'])}</td><td class='muted'>{role}</td>"
                 f"<td>{c['rows']}</td>"
                 f"<td class='muted'>{c['first']}&rarr;{c['last']}</td>"
                 f"<td class='{nan_cls}'>{c['nans']}</td></tr>")
    note = (
        f'<div class="muted">{legacy_n} legacy daily-key files omitted '
        "(dead cache keys, not a data outage).</div>"
        if legacy_n else ""
    )
    return (
        '<div class="card full"><h2>Data coverage</h2>'
        '<table><tr><th>dataset</th><th>role</th><th>rows</th><th>range</th><th>NaNs</th></tr>'
        f'{rows}</table>{note}</div>'
    )


def _activity_card(act: dict) -> str:
    if not act["last_run"]:
        return '<div class="card"><h2>Last scan</h2><p class="muted">No paper activity logged yet.</p></div>'
    rows = ""
    for r in act["last_run"]:
        act_cls = "muted" if r["action"] == "none" else "pos"
        reason = f" ({html.escape(str(r['reason']))})" if r.get("reason") else ""
        rows += (f"<tr><td>{html.escape(str(r['symbol']))}</td>"
                 f"<td>{html.escape(str(r['signal']))}</td>"
                 f"<td class='{act_cls}'>{html.escape(str(r['action']))}{reason}</td></tr>")
    when = f' &middot; {act["last_when"]}' if act["last_when"] else ""
    return (
        f'<div class="card"><h2>Last scan{when}</h2>'
        '<table><tr><th>symbol</th><th>signal</th><th>action</th></tr>'
        f'{rows}</table></div>'
    )


def _run2_card(report: dict | None) -> str:
    if not report:
        return (
            '<div class="card"><h2>Auditable Run 3</h2>'
            '<p class="muted">Not initialized. Run <code>money run-init --run-id run3</code> '
            'only after the paper account is empty and set to $100,000.</p></div>'
        )
    rows = ""
    for name, values in report["portfolios"].items():
        rows += (
            f"<tr><td>{html.escape(name)}</td>"
            f"<td class='{_sign(values['return_pct'])}'>{values['return_pct']:+.2f}%</td>"
            f"<td>{values['completed_trades']}</td>"
            f"<td>{values['max_drawdown_pct']:.2f}%</td></tr>"
        )
    status_cls = "bad" if report["status"] != "active" else "ok"
    halt = ""
    if report.get("halt_reason"):
        # A resumed run keeps its halt text as evidence; it is not a live halt.
        text = html.escape(str(report["halt_reason"]))
        halt = (
            f'<div class="muted">recovered from {text}</div>'
            if report["status"] == "active"
            else f'<div class="neg muted">{text}</div>'
        )
    return (
        '<div class="card full"><h2>Auditable Run 3</h2>'
        f'<span class="pill {status_cls}">{html.escape(report["status"])}</span>{halt}'
        '<table style="margin-top:10px"><tr><th>portfolio</th><th>return</th>'
        f'<th>closed</th><th>max DD</th></tr>{rows}</table></div>'
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
        + _run2_card(ctx.get("run2"))
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
