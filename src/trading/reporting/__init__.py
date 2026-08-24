"""Run reporting: summarize stats and append a learning journal."""

from .journal import RESULTS_DIR, record_paper_action, record_run, summarize
from .dashboard import write_dashboard
from .email_report import EmailError, build_digest, email_configured, send_alert, send_message

__all__ = [
    "RESULTS_DIR",
    "record_paper_action",
    "record_run",
    "summarize",
    "write_dashboard",
    "EmailError",
    "build_digest",
    "email_configured",
    "send_alert",
    "send_message",
]
