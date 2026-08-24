"""Email updates: a full daily digest and short alerts, via plain SMTP.

Two kinds of email, both best-effort (they never break the trading runs):

- **Digest** — the complete picture: paper-account equity & positions with live
  P&L, the latest scan's signals, backtest alpha vs buy&hold ("how are we
  doing"), system health and insights. Reuses ``dashboard.collect_context()``.
- **Alert** — a short immediate notice (a stop-loss fired, a scheduled run
  failed) so problems surface in your inbox, not just in a log file.

Configuration comes from the environment (.env), never hardcoded:

    EMAIL_SMTP_HOST=smtp.gmail.com     # any SMTP provider works
    EMAIL_SMTP_PORT=587                # 465 = implicit SSL, else STARTTLS
    EMAIL_SMTP_USER=you@gmail.com
    EMAIL_SMTP_PASSWORD=xxxxxxxxxxxx   # Gmail: an App Password
    EMAIL_TO=you@gmail.com             # comma-separated recipients
    EMAIL_FROM=optional                # defaults to EMAIL_SMTP_USER

No third-party dependency: only stdlib ``smtplib``/``email``.
"""

from __future__ import annotations

import html
import os
import smtplib
import sys
from email.message import EmailMessage

from .dashboard import DISPLAY_TZ, collect_context


class EmailError(RuntimeError):
    """Raised when email is unconfigured or the SMTP send fails."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _config() -> dict:
    host = os.getenv("EMAIL_SMTP_HOST", "").strip()
    user = os.getenv("EMAIL_SMTP_USER", "").strip()
    password = os.getenv("EMAIL_SMTP_PASSWORD", "").strip()
    to_raw = os.getenv("EMAIL_TO", "").strip()
    try:
        port = int(os.getenv("EMAIL_SMTP_PORT", "").strip() or "587")
    except ValueError:
        raise EmailError(f"EMAIL_SMTP_PORT is not a number: {os.getenv('EMAIL_SMTP_PORT')!r}")

    missing = [
        name for name, val in [
            ("EMAIL_SMTP_HOST", host), ("EMAIL_SMTP_USER", user),
            ("EMAIL_SMTP_PASSWORD", password), ("EMAIL_TO", to_raw),
        ] if not val
    ]
    if missing:
        raise EmailError(
            "Email reports not configured — set " + ", ".join(missing)
            + " in .env (see .env.example)."
        )
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from": os.getenv("EMAIL_FROM", "").strip() or user,
        "to": [a.strip() for a in to_raw.split(",") if a.strip()],
    }


def email_configured() -> bool:
    """True when all required EMAIL_* variables are present."""
    try:
        return bool(_config())
    except EmailError:
        return False


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def _build_message(subject: str, text_body: str, html_body: str | None) -> EmailMessage:
    cfg = _config()
    msg = EmailMessage()
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(cfg["to"])
    msg["Subject"] = subject
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


def send_message(subject: str, text_body: str, html_body: str | None = None) -> None:
    """Send via SMTP; SSL on port 465, STARTTLS otherwise. Raises EmailError."""
    cfg = _config()
    msg = _build_message(subject, text_body, html_body)
    timeout = 30
    try:
        if cfg["port"] == 465:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=timeout) as s:
                s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=timeout) as s:
                s.starttls()
                s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise EmailError(f"SMTP send failed ({cfg['host']}:{cfg['port']}): {e}") from e


def send_alert(title: str, body_text: str) -> None:
    """Short immediate notice; body is plain text."""
    send_message(f"[money lab] {title}", body_text.strip() + "\n")


# --------------------------------------------------------------------------- #
# Digest content
# --------------------------------------------------------------------------- #
def _money(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "—"


def _pct(v: float, signed: bool = True) -> str:
    return f"{v:+.2f}%" if signed else f"{v:.2f}%"


def build_digest(ctx: dict | None = None) -> tuple[str, str, str]:
    """Compose the daily update. Returns (subject, plain-text body, HTML body)."""
    ctx = dict(ctx or collect_context())
    # The dashboard's generic "Reminder: paper money only…" insight would
    # duplicate the email's own footer disclaimer — drop it here.
    ctx["insights"] = [
        s for s in ctx.get("insights", []) if "paper money only" not in s.lower()
    ]
    acct = ctx.get("account")
    activity = ctx.get("activity", {})
    actions = sum(v for k, v in activity.get("counts", {}).items() if k != "none")
    n_pos = len(acct["positions"]) if acct else 0

    bits = [f"equity {_money(acct['equity'])}" if acct else "account offline"]
    if n_pos:
        pl = sum(p["unrealized_pl"] for p in acct["positions"])
        bits.append(f"{n_pos} position(s), open P&L {_money(pl)}")
    if actions:
        bits.append(f"{actions} lifetime action(s)")
    subject = "[money lab] daily update — " + " · ".join(bits)

    return subject, _render_text(ctx), _render_html(ctx)


# --- plain text -------------------------------------------------------------
def _render_text(ctx: dict) -> str:
    out: list[str] = []
    w = out.append
    generated = datetime_now_str()
    w(f"MONEY LAB — DAILY UPDATE ({generated})")

    acct = ctx.get("account")
    w("\n-- Paper account (Alpaca, fake money) --")
    if not acct:
        w("  offline / no API keys — showing local data only")
    else:
        w(f"  equity {_money(acct['equity'])} · cash {_money(acct['cash'])}"
          f" · buying power {_money(acct['buying_power'])}")
        if acct["positions"]:
            for p in acct["positions"]:
                w(f"    {p['symbol']:<10} qty={p['qty']:.6f}  "
                  f"value {_money(p['market_value'] or 0)}  "
                  f"P&L {_money(p['unrealized_pl'])} ({_pct(p['unrealized_plpc'])})")
        else:
            w("  no open positions")

    run2 = ctx.get("run2")
    w("\n-- Auditable Run 2 --")
    if not run2:
        w("  not initialized")
    else:
        halt = f" · HALT: {run2['halt_reason']}" if run2.get("halt_reason") else ""
        w(f"  status={run2['status']}{halt} · fees {_money(run2.get('fees', 0))}")
        for name, values in run2.get("portfolios", {}).items():
            w(
                f"    {name:<20} equity {_money(values['equity'])} "
                f"return {_pct(values['return_pct'])}  closed={values['completed_trades']} "
                f"maxDD={values['max_drawdown_pct']:.2f}%"
            )

    act = ctx.get("activity", {})
    w(f"\n-- Last scan{(' (' + act['last_when'] + ')') if act.get('last_when') else ''} "
      f"[strategy: {ctx.get('strategy', '?')}] --")
    rows = act.get("last_run") or []
    if rows:
        for r in rows:
            mark = "" if r["action"] == "none" else "   <-- ACTION"
            w(f"    {r['symbol']:<10} signal={r['signal']:<7} holding={str(r['holding']):<5} "
              f"action={r['action']}{mark}")
    else:
        w("  nothing logged yet")

    bts = ctx.get("backtests") or []
    w("\n-- Backtests vs buy & hold (latest per symbol) --")
    if bts:
        for b in bts[:10]:
            w(f"    {b['symbol']:<10} return {_pct(b['return_pct'], False):>8}"
              f"  B&H {_pct(b['buy_hold_pct'], False):>8}  alpha {_pct(b['alpha'], False):>7}")
    else:
        w("  no backtest runs logged yet")

    hbs = ctx.get("heartbeats") or []
    if hbs:
        w("\n-- System health --")
        for h in hbs:
            state = "ok" if h["ok"] else "STALE"
            age = f"{h['age_h']:.1f}h ago" if h["age_h"] is not None else ""
            w(f"    [{state:>5}] {h['label']}: {h['when']} {age}")

    if ctx.get("insights"):
        w("\n-- Insights --")
        for s in ctx["insights"]:
            w(f"  * {_strip_tags(s)}")

    w("\nPaper money only — not investment advice.")
    return "\n".join(out)


# --- HTML -------------------------------------------------------------------
_C = {"green": "#0a8f4f", "red": "#d33d3d", "grey": "#6b7785", "line": "#e6e9ee"}


def _th(*cols: str) -> str:
    tds = "".join(
        f'<th style="text-align:right;padding:4px 8px;color:{_C["grey"]};'
        f'font-weight:600;font-size:12px">{c}</th>' if i else
        f'<th style="text-align:left;padding:4px 8px;color:{_C["grey"]};'
        f'font-weight:600;font-size:12px">{c}</th>'
        for i, c in enumerate(cols)
    )
    return f"<tr>{tds}</tr>"


def _tds(*cells: (str | tuple[str, str])) -> str:
    tds = ""
    for i, c in enumerate(cells):
        style = f'text-align:{"left" if i == 0 else "right"};padding:4px 8px;font-size:13px;'
        color = ""
        if isinstance(c, tuple):
            val, cls = c
            if cls in _C:
                color = f"color:{_C[cls]};"
            tds += f'<td style="{style}{color}">{val}</td>'
        else:
            tds += f'<td style="{style}">{c}</td>'
    return f"<tr>{tds}</tr>"


def _card(title: str, inner: str) -> str:
    return (
        f'<div style="border:1px solid {_C["line"]};border-radius:10px;'
        f'padding:14px 16px;margin-bottom:14px;background:#ffffff">'
        f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;'
        f'color:{_C["grey"]};margin-bottom:8px">{title}</div>{inner}</div>'
    )


def _pl_span(v: float) -> str:
    color = _C["green"] if v > 0 else _C["red"] if v < 0 else _C["grey"]
    return f'<span style="color:{color}">{html.escape(_money(v))}</span>'


def _pl_pct_span(v: float) -> str:
    color = _C["green"] if v > 0 else _C["red"] if v < 0 else _C["grey"]
    return f'<span style="color:{color}">{_pct(v)}</span>'


def _render_html(ctx: dict) -> str:
    esc = html.escape
    parts: list[str] = []

    acct = ctx.get("account")
    if not acct:
        inner = (f'<p style="margin:0;color:{_C["grey"]};font-size:13px">'
                 'Live snapshot unavailable (offline or no API keys).</p>')
    else:
        pos_rows = ""
        for p in acct["positions"]:
            pos_rows += _tds(esc(p["symbol"]), f"{p['qty']:.6f}",
                             _money(p["market_value"] or 0),
                             (_pl_span(p["unrealized_pl"]), ""),
                             (_pl_pct_span(p["unrealized_plpc"]), ""))
        if not pos_rows:
            pos_rows = (f'<tr><td colspan="5" style="padding:4px 8px;color:{_C["grey"]};'
                        'font-size:13px">no open positions</td></tr>')
        inner = (
            f'<div style="font-size:24px;font-weight:700;margin-bottom:2px">'
            f'{esc(_money(acct["equity"]))}</div>'
            f'<div style="color:{_C["grey"]};font-size:12px;margin-bottom:10px">'
            f'equity · cash {esc(_money(acct["cash"]))} · '
            f'buying power {esc(_money(acct["buying_power"]))}</div>'
            '<table style="border-collapse:collapse;width:100%">'
            + _th("symbol", "qty", "value", "P&amp;L", "%") + pos_rows + "</table>"
        )
    parts.append(_card("Paper account (Alpaca, fake money)", inner))

    run2 = ctx.get("run2")
    if run2:
        run_rows = ""
        for name, values in run2.get("portfolios", {}).items():
            run_rows += _tds(
                esc(name),
                _money(values["equity"]),
                (_pct(values["return_pct"]), "green" if values["return_pct"] > 0 else "red"),
                str(values["completed_trades"]),
                f'{values["max_drawdown_pct"]:.2f}%',
            )
        halt = (
            f'<div style="color:{_C["red"]};font-size:12px;margin-bottom:6px">'
            f'HALT: {esc(str(run2["halt_reason"]))}</div>'
            if run2.get("halt_reason") else ""
        )
        inner = (
            f'<div style="font-size:12px;margin-bottom:6px">status: '
            f'<b>{esc(run2["status"])}</b> · fees {esc(_money(run2.get("fees", 0)))}</div>'
            + halt
            + '<table style="border-collapse:collapse;width:100%">'
            + _th("portfolio", "equity", "return", "closed", "max DD")
            + run_rows
            + "</table>"
        )
    else:
        inner = f'<p style="margin:0;color:{_C["grey"]};font-size:13px">not initialized</p>'
    parts.append(_card("Auditable Run 2", inner))

    act = ctx.get("activity", {})
    scan_rows = ""
    for r in act.get("last_run") or []:
        color = _C["green"] if r["action"] != "none" else _C["grey"]
        scan_rows += _tds(esc(str(r["symbol"])), esc(str(r["signal"])),
                          esc(str(r["holding"])),
                          (f'<span style="color:{color}">{esc(str(r["action"]))}</span>', ""))
    if scan_rows:
        sub = f' · {act["last_when"]}' if act.get("last_when") else ""
        parts.append(_card(
            f"Last scan — {esc(ctx.get('strategy', '?'))}{sub}",
            '<table style="border-collapse:collapse;width:100%">'
            + _th("symbol", "signal", "holding", "action") + scan_rows + "</table>"))

    bt_rows = ""
    for b in (ctx.get("backtests") or [])[:10]:
        bt_rows += _tds(esc(b["symbol"]), _pct(b["return_pct"]), _pct(b["buy_hold_pct"]),
                        (_pct(b["alpha"]), "green" if b["alpha"] > 0 else "red"))
    if bt_rows:
        parts.append(_card(
            "Backtests vs buy &amp; hold (latest per symbol)",
            '<table style="border-collapse:collapse;width:100%">'
            + _th("symbol", "return", "buy&amp;hold", "alpha") + bt_rows + "</table>"
            + f'<div style="color:{_C["grey"]};font-size:12px;margin-top:6px">'
            "alpha = strategy return &minus; buy&amp;hold (&gt;0 beats holding)</div>"))

    hb_rows = ""
    for h in ctx.get("heartbeats") or []:
        pill_color, pill_bg = (("green", "#e6f6ee") if h["ok"] else ("red", "#fdeaea"))
        age = f"{h['age_h']:.1f}h ago" if h["age_h"] is not None else "—"
        hb_rows += (
            f'<tr><td style="padding:4px 8px;font-size:13px">{esc(h["label"])}</td>'
            f'<td style="text-align:right"><span style="background:{pill_bg};color:{_C[pill_color]};'
            f'padding:1px 8px;border-radius:20px;font-size:12px;font-weight:600">'
            f'{"ok" if h["ok"] else "stale"}</span> '
            f'<span style="color:{_C["grey"]};font-size:12px">{esc(h["when"])} {age}</span></td></tr>'
        )
    if hb_rows:
        parts.append(_card("System health",
                           f'<table style="border-collapse:collapse;width:100%">{hb_rows}</table>'))

    items = "".join(
        f'<li style="margin-bottom:6px;font-size:13px;line-height:1.5">{s}</li>'
        for s in ctx.get("insights") or []
    )
    if items:
        parts.append(_card("Insights", f"<ul style=\"margin:0;padding-left:18px\">{items}</ul>"))

    generated = datetime_now_str()
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',system-ui,sans-serif;'
        'max-width:640px;margin:0 auto;color:#1c2430">'
        '<h1 style="font-size:19px;margin:0">money lab — daily update</h1>'
        f'<div style="color:{_C["grey"]};font-size:12px;margin:2px 0 16px">'
        f'generated {generated} · deployed strategy: <b>{esc(ctx.get("strategy", "?"))}</b></div>'
        + "".join(parts)
        + f'<div style="color:{_C["grey"]};font-size:11px;margin-top:4px">'
        "Paper money only — simulation, not predictions or investment advice.</div></div>"
    )


# --------------------------------------------------------------------------- #
# Helpers shared by both renderers
# --------------------------------------------------------------------------- #
def datetime_now_str() -> str:
    from datetime import datetime

    return datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z")


def _strip_tags(s: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", s).replace("&amp;", "&").replace("&nbsp;", " ")


# --------------------------------------------------------------------------- #
# CLI entry points
# --------------------------------------------------------------------------- #
def run_digest(dry_run: bool = False) -> int:
    """Build (and send, unless dry-run) the daily digest. Returns exit code."""
    subject, text_body, html_body = build_digest()
    print(f"\nSubject: {subject}\n")
    print(text_body)
    if dry_run:
        print(f"\n(dry run — not sending; HTML body would be {len(html_body)} bytes)")
        return 0
    if not email_configured():
        print(
            "\nEmail reports not configured — add EMAIL_* keys to .env"
            " (see .env.example). Nothing was sent.",
            file=sys.stderr,
        )
        return 2
    send_message(subject, text_body, html_body)
    print(f"Sent to {', '.join(_config()['to'])}.")
    return 0


def run_alert_from_stdin(title: str) -> int:
    """Send a short alert whose body arrives on stdin. Returns exit code."""
    body = sys.stdin.read().strip() or "(no detail)"
    if not email_configured():
        print("Email reports not configured — alert not sent.", file=sys.stderr)
        return 2
    send_alert(title, body)
    return 0
