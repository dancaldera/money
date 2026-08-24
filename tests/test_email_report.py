"""Tests for the email digest/alert builder (no network, no SMTP)."""

from __future__ import annotations

import pytest

from trading.reporting import email_report as er


@pytest.fixture
def ctx():
    return {
        "generated": "2026-08-23 18:05 CST",
        "strategy": "sma_cross",
        "heartbeats": [{"label": "Daily signal scan", "when": "now", "age_h": 0.1, "ok": True}],
        "account": {
            "equity": 101_234.56, "cash": 50_000.0, "buying_power": 200_000.0,
            "positions": [{
                "symbol": "AAVEUSD", "qty": 0.5, "avg_entry": 100.0,
                "current_price": 110.0, "market_value": 55.0,
                "unrealized_pl": 5.0, "unrealized_plpc": 10.0,
            }],
        },
        "backtests": [{"symbol": "AAPL", "return_pct": 20.0, "buy_hold_pct": 5.0,
                       "alpha": 15.0, "sharpe": 0.6}],
        "coverage": [],
        "activity": {"counts": {"bought": 3}, "last_when": "2026-08-23 18:04",
                     "last_run": [{"symbol": "AAVE/USD", "signal": "BUY",
                                   "holding": False, "action": "bought"}]},
        "run2": {
            "status": "active", "halt_reason": None, "fees": 1.25,
            "portfolios": {
                "baseline": {"equity": 10_050, "return_pct": 0.5,
                             "completed_trades": 2, "max_drawdown_pct": -0.4},
            },
        },
        "insights": ["Equity <b>$101,235</b> across <b>1</b> position(s)."],
    }


def test_subject_carries_equity_and_positions(ctx):
    subject, _, _ = er.build_digest(ctx)
    assert "$101,234.56" in subject
    assert "1 position(s)" in subject


def test_text_body_lists_all_sections(ctx):
    text = er.build_digest(ctx)[1]
    for marker in ["Paper account", "equity $101,234.56", "AAVEUSD", "Last scan",
                   "Auditable Run 2", "baseline", "Backtests vs buy & hold", "alpha",
                   "System health", "Insights"]:
        assert marker in text


def test_html_escapes_user_data_and_marks_actions(ctx):
    body = er.build_digest(ctx)[2]
    assert "<b>AAVE/USD</b>" not in body and "&lt;b&gt;" not in body
    assert "AAVE/USD" in body          # plain symbol survives as-is
    assert "action" in body
    # insight HTML is intentionally kept (it's authored by us), but the
    # plain-text mirror of it must be tag-free:
    assert "<b>" not in er.build_digest(ctx)[1].split("Insights")[1]


def test_account_offline_still_renders(ctx):
    ctx["account"] = None
    subject, text, body = er.build_digest(ctx)
    assert "offline" in subject
    assert "offline" in text and "Live snapshot unavailable" in body


def test_empty_context_renders():
    subject, text, _ = er.build_digest({"account": None, "activity": {},
                                        "backtests": [], "heartbeats": []})
    assert "[money lab]" in subject and text


def test_email_configured_reflects_env(monkeypatch):
    for var in ["EMAIL_SMTP_HOST", "EMAIL_SMTP_USER", "EMAIL_SMTP_PASSWORD",
                "EMAIL_TO", "EMAIL_FROM"]:
        monkeypatch.delenv(var, raising=False)
    assert er.email_configured() is False

    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("EMAIL_SMTP_PORT", "587")
    monkeypatch.setenv("EMAIL_SMTP_USER", "me@test.local")
    monkeypatch.setenv("EMAIL_SMTP_PASSWORD", "secret")
    monkeypatch.setenv("EMAIL_TO", "a@test.local, b@test.local")
    assert er.email_configured() is True

    msg = er._build_message("s", "text", "<p>html</p>")
    assert msg["To"] == "a@test.local, b@test.local"
    assert msg.get_content_type() == "multipart/alternative"   # text + html
    parts = list(msg.iter_parts())
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
