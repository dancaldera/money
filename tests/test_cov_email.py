"""Coverage for trading.reporting.email_report sending paths and CLI entry points."""

from __future__ import annotations

import io
import smtplib

import pytest

from trading.reporting import email_report as er


@pytest.fixture
def configured(monkeypatch):
    for var in ["EMAIL_SMTP_HOST", "EMAIL_SMTP_USER", "EMAIL_SMTP_PASSWORD",
                "EMAIL_TO", "EMAIL_FROM", "EMAIL_SMTP_PORT"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("EMAIL_SMTP_USER", "me@test.local")
    monkeypatch.setenv("EMAIL_SMTP_PASSWORD", "secret")
    monkeypatch.setenv("EMAIL_TO", "a@test.local")


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.tls = False
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.user = user

    def send_message(self, msg):
        self.sent.append(msg)


def test_bad_port_number_rejected(configured, monkeypatch):
    monkeypatch.setenv("EMAIL_SMTP_PORT", "not-a-port")
    with pytest.raises(er.EmailError, match="not a number"):
        er._config()


def test_send_message_uses_ssl_on_465(configured, monkeypatch):
    monkeypatch.setenv("EMAIL_SMTP_PORT", "465")
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    _FakeSMTP.instances.clear()
    er.send_message("s", "body")
    assert _FakeSMTP.instances[0].port == 465
    assert _FakeSMTP.instances[0].tls is False


def test_send_message_uses_starttls_otherwise(configured, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.instances.clear()
    er.send_message("s", "body", "<p>h</p>")
    assert _FakeSMTP.instances[0].tls is True
    assert _FakeSMTP.instances[0].sent[0]["Subject"] == "s"


def test_send_message_wraps_smtp_and_os_errors(configured, monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise smtplib.SMTPException("nope")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(smtplib, "SMTP", Boom)
    with pytest.raises(er.EmailError, match="SMTP send failed"):
        er.send_message("s", "body")

    def raise_os(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(smtplib, "SMTP", raise_os)
    with pytest.raises(er.EmailError, match="SMTP send failed"):
        er.send_message("s", "body")


def test_send_alert_prefixes_subject(monkeypatch):
    calls = []
    monkeypatch.setattr(er, "send_message", lambda s, t, h=None: calls.append((s, t)))
    er.send_alert("stopped", "  detail  ")
    assert calls == [("[money lab] stopped", "detail\n")]


def test_digest_renders_empty_positions():
    ctx = {"account": {"equity": 100.0, "cash": 100.0, "buying_power": 100.0,
                       "positions": []},
           "activity": {}, "backtests": [], "heartbeats": [], "run2": None,
           "strategy": "sma_cross", "insights": []}
    subject, text, html = er.build_digest(ctx)
    assert "no open positions" in text and "no open positions" in html
    assert "not initialized" in text


def test_run_digest_dry_run(monkeypatch, capsys):
    monkeypatch.setattr(er, "build_digest", lambda: ("subj", "text", "<p>h</p>"))
    assert er.run_digest(dry_run=True) == 0
    assert "dry run" in capsys.readouterr().out


def test_run_digest_refuses_unconfigured(monkeypatch, capsys):
    monkeypatch.setattr(er, "build_digest", lambda: ("subj", "text", "<p>h</p>"))
    monkeypatch.setattr(er, "email_configured", lambda: False)
    assert er.run_digest() == 2
    assert "not configured" in capsys.readouterr().err


def test_run_digest_sends_when_configured(configured, monkeypatch, capsys):
    monkeypatch.setattr(er, "build_digest", lambda: ("subj", "text", "<p>h</p>"))
    calls = []
    monkeypatch.setattr(er, "send_message", lambda s, t, h: calls.append(s))
    assert er.run_digest() == 0
    assert calls == ["subj"] and "Sent to" in capsys.readouterr().out


def test_run_alert_from_stdin(monkeypatch, capsys):
    monkeypatch.setattr(er, "email_configured", lambda: False)
    monkeypatch.setattr("sys.stdin", io.StringIO("disk full"))
    assert er.run_alert_from_stdin("oops") == 2
    monkeypatch.setattr(er, "email_configured", lambda: True)
    calls = []
    monkeypatch.setattr(er, "send_alert", lambda t, b: calls.append((t, b)))
    monkeypatch.setattr("sys.stdin", io.StringIO("  "))
    assert er.run_alert_from_stdin("oops") == 0
    assert calls == [("oops", "(no detail)")]
