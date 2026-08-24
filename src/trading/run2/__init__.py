"""Auditable Run 2 paper-trading experiment.

The package is intentionally paper-only.  It adds a frozen experiment manifest,
an append-only fill ledger, portfolio risk checks, and isolated shadow arms on
top of the small learning lab without introducing a live brokerage route.
"""

from .config import RunConfig, load_run_config
from .ledger import RunLedger

__all__ = ["RunConfig", "RunLedger", "load_run_config"]

