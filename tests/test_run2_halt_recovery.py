"""Drawdown halt recovery: an opt-in alternative to the frozen one-way latch.

The audited run keeps the latch (``halt_recovery_*`` absent). These tests pin the
whole policy — the latch default, the reconciliation halts that must never
recover, the drawdown/cooldown recovery, the ledger evidence and drawdown
re-baseline, and the replay that must apply exactly the same rule.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest
import yaml

from trading.run2.config import RunConfigError, load_run_config
from trading.run2.ledger import LedgerError, RunLedger
from trading.run2.portfolio import simulate_portfolio
from trading.run2.risk import DRAWDOWN_HALT_PREFIX, halt_action
from trading.run2.service import Run2Service

from .run2_helpers import ROOT, initialized_ledger, ohlcv, run2_config

DD_HALT = f"{DRAWDOWN_HALT_PREFIX}5.1000%"


def recover_config(**portfolio):
    """The frozen manifest with an opt-in recovery rule (never the live run)."""
    base = run2_config()
    fields = {"halt_recovery_drawdown_pct": 2.5, "halt_recovery_days": 20, **portfolio}
    return replace(base, portfolio=replace(base.portfolio, **fields))


def _manifest(tmp_path, mutate, name="exp-recover.yaml"):
    raw = deepcopy(run2_config().raw)
    mutate(raw)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(raw))
    return path


# --- the policy itself ---------------------------------------------------------- #
def test_default_manifest_keeps_the_one_way_latch():
    cfg = run2_config()
    assert cfg.portfolio.halt_recovery_drawdown_pct is None
    assert cfg.portfolio.halt_recovery_days is None
    assert halt_action(cfg, 5.0, halted=False, halt_reason=None) == "halt"
    assert halt_action(cfg, 0.0, halted=False, halt_reason=None) == "none"
    # halted: no recovery configured and no cooldown => nothing can re-arm it
    assert halt_action(cfg, 0.0, halted=True, halt_reason=DD_HALT, days_since_halt=9999) == "none"


def test_only_drawdown_halts_can_recover():
    cfg = recover_config()
    for reason in (None, "", "reconciliation_failed:unknown_orders=[X],qty=[]"):
        assert halt_action(
            cfg, 0.0, halted=True, halt_reason=reason, days_since_halt=9999
        ) == "none"


def test_recovery_fires_on_drawdown_or_cooldown():
    cfg = recover_config()
    # drawdown fell back to the recovery level: resume immediately
    assert halt_action(cfg, 2.0, halted=True, halt_reason=DD_HALT, days_since_halt=1) == "resume"
    # still deep in drawdown and inside the cooldown: stay halted, and a stale
    # peak above the halt threshold must not re-halt (that would pin the latch)
    assert halt_action(cfg, 9.0, halted=True, halt_reason=DD_HALT, days_since_halt=19) == "none"
    # cooldown elapsed: resume even though drawdown never recovered
    assert halt_action(cfg, 9.0, halted=True, halt_reason=DD_HALT, days_since_halt=20) == "resume"
    assert halt_action(cfg, 9.0, halted=True, halt_reason=DD_HALT, days_since_halt=20.5) == "resume"
    # no clock supplied (a caller that cannot measure the cooldown)
    assert halt_action(cfg, 9.0, halted=True, halt_reason=DD_HALT, days_since_halt=None) == "none"


def test_drawdown_recovery_needs_a_clock_only_when_configured_that_way():
    cfg = recover_config(halt_recovery_days=None)
    assert halt_action(cfg, 9.0, halted=True, halt_reason=DD_HALT, days_since_halt=None) == "none"
    assert halt_action(cfg, 2.0, halted=True, halt_reason=DD_HALT) == "resume"


# --- manifest validation -------------------------------------------------------- #
def test_experiment_manifest_carries_the_recovery_rule(tmp_path):
    path = _manifest(
        tmp_path,
        lambda raw: (
            raw.update(run_id="exp-recover"),
            raw["portfolio"].update(
                halt_recovery_drawdown_pct=2.5, halt_recovery_days=20
            ),
        ),
    )
    cfg = load_run_config(path, strict=False)
    assert cfg.portfolio.halt_recovery_drawdown_pct == 2.5
    assert cfg.portfolio.halt_recovery_days == 20


@pytest.mark.parametrize(
    "portfolio, match",
    [
        ({"halt_recovery_drawdown_pct": 0}, "halt_recovery_drawdown_pct"),
        ({"halt_recovery_drawdown_pct": 5}, "halt_recovery_drawdown_pct"),
        ({"halt_recovery_drawdown_pct": 7.5}, "halt_recovery_drawdown_pct"),
        ({"halt_recovery_days": 0}, "halt_recovery_days"),
        ({"halt_recovery_days": -5}, "halt_recovery_days"),
    ],
)
def test_recovery_knobs_are_validated(tmp_path, portfolio, match):
    path = _manifest(
        tmp_path,
        lambda raw: (raw.update(run_id="exp-bad"), raw["portfolio"].update(**portfolio)),
    )
    with pytest.raises(RunConfigError, match=match):
        load_run_config(path, strict=False)


@pytest.mark.parametrize(
    "portfolio",
    [{"halt_recovery_drawdown_pct": 2.5}, {"halt_recovery_days": 20}],
)
def test_live_run_cannot_opt_into_recovery(tmp_path, portfolio):
    path = _manifest(
        tmp_path, lambda raw: raw["portfolio"].update(**portfolio), name="run2.yaml"
    )
    with pytest.raises(RunConfigError, match="frozen values changed"):
        load_run_config(path)  # strict: the audited run stays latched


# --- ledger: resume evidence + drawdown re-baseline ----------------------------- #
def test_resume_rearms_and_keeps_the_halt_as_evidence(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        ledger.halt(cfg.run_id, DD_HALT)
        assert ledger.is_halted(cfg.run_id)
        ledger.resume(cfg.run_id, "resumed:%s@2.4000%%" % DD_HALT)
        assert not ledger.is_halted(cfg.run_id)
        row = ledger.run(cfg.run_id)
        assert row["status"] == "active"
        assert row["halt_reason"] == f"resumed:{DD_HALT}@2.4000%"
        assert row["halted_at"]  # the resume marks the new drawdown baseline
    finally:
        ledger.close()


def test_resume_accepts_an_explicit_clock(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        ledger.halt(cfg.run_id, DD_HALT)
        ledger.resume(cfg.run_id, "resumed", when="2026-09-18T00:00:00+00:00")
        assert ledger.run(cfg.run_id)["halted_at"] == "2026-09-18T00:00:00+00:00"
    finally:
        ledger.close()


def test_resume_of_an_unknown_run_raises(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        with pytest.raises(LedgerError, match="not initialized"):
            ledger.resume("nope", "resumed")
    finally:
        ledger.close()


def _snapshot(ledger, cfg, equity, captured_at):
    ledger.record_equity(
        {
            "run_id": cfg.run_id,
            "portfolio": "baseline",
            "captured_at": captured_at,
            "equity": str(equity),
            "cash": str(equity),
            "gross_exposure": "0",
            "drawdown_pct": 0.0,
            "source": "alpaca-paper",
        }
    )


def test_high_water_can_be_measured_since_a_resume(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        _snapshot(ledger, cfg, 100_000, "2026-08-24T00:00:00+00:00")
        _snapshot(ledger, cfg, 95_000, "2026-08-25T00:00:00+00:00")
        assert ledger.high_water(cfg.run_id) == 100_000
        assert ledger.high_water(cfg.run_id, since="2026-08-25T00:00:00+00:00") == 95_000
        assert ledger.high_water(cfg.run_id, since="2026-09-01T00:00:00+00:00") is None
    finally:
        ledger.close()


# --- live service --------------------------------------------------------------- #
def test_live_service_halts_then_resumes_after_the_cooldown(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    cfg = recover_config(halt_recovery_days=20)
    try:
        service = Run2Service(cfg, ledger)
        service.record_account_snapshot({"equity": 100_000.0, "cash": 100_000.0})
        service.record_account_snapshot({"equity": 94_000.0, "cash": 94_000.0})
        assert ledger.is_halted(cfg.run_id)
        assert ledger.run(cfg.run_id)["halt_reason"] == "drawdown_halt:6.0000%"

        # Backdate the halt so the cooldown has elapsed.
        ledger.conn.execute(
            "UPDATE runs SET halted_at = ? WHERE run_id = ?",
            ("2026-08-01T00:00:00+00:00", cfg.run_id),
        )
        ledger.conn.commit()
        service.record_account_snapshot({"equity": 93_000.0, "cash": 93_000.0})
        assert not ledger.is_halted(cfg.run_id)
        assert ledger.run(cfg.run_id)["halt_reason"].startswith("resumed:drawdown_halt:")

        # Re-baselined: the peak already lost must not re-halt the run.
        service.record_account_snapshot({"equity": 93_000.0, "cash": 93_000.0})
        assert not ledger.is_halted(cfg.run_id)
        assert ledger.run(cfg.run_id)["halt_reason"].startswith("resumed:")
    finally:
        ledger.close()


def test_live_service_never_resumes_a_reconciliation_halt(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    cfg = recover_config()
    try:
        service = Run2Service(cfg, ledger)
        service.record_account_snapshot({"equity": 100_000.0, "cash": 100_000.0})
        ledger.halt(cfg.run_id, "reconciliation_failed:unknown_orders=[X],qty=[]")
        ledger.conn.execute(
            "UPDATE runs SET halted_at = ? WHERE run_id = ?",
            ("2026-01-01T00:00:00+00:00", cfg.run_id),
        )
        ledger.conn.commit()
        service.record_account_snapshot({"equity": 100_000.0, "cash": 100_000.0})
        assert ledger.is_halted(cfg.run_id)  # fail-closed stays fail-closed
    finally:
        ledger.close()


def test_live_service_without_recovery_keeps_the_latch(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        service = Run2Service(cfg, ledger)
        service.record_account_snapshot({"equity": 100_000.0, "cash": 100_000.0})
        service.record_account_snapshot({"equity": 94_000.0, "cash": 94_000.0})
        ledger.conn.execute(
            "UPDATE runs SET halted_at = ? WHERE run_id = ?",
            ("2026-01-01T00:00:00+00:00", cfg.run_id),
        )
        ledger.conn.commit()
        service.record_account_snapshot({"equity": 99_999.0, "cash": 99_999.0})
        assert ledger.is_halted(cfg.run_id)
        assert ledger.run(cfg.run_id)["halt_reason"] == "drawdown_halt:6.0000%"
    finally:
        ledger.close()


# --- replay parity -------------------------------------------------------------- #
def _halt_then_recover_frames():
    """Cross up, get stopped out past the halt, then offer a fresh cross later."""
    return {"AAPL": ohlcv([10.0] * 40 + [20.0] * 10 + [10.0] * 30 + [25.0] * 6)}


def test_replay_latches_entries_off_after_the_halt():
    cfg = replace(run2_config(), starting_equity=1000.0)
    sim = simulate_portfolio(cfg, _halt_then_recover_frames())
    assert list(sim.trades["side"]) == ["buy", "sell"]  # the fresh cross is lost
    assert sim.trades.iloc[-1]["reason"] == "stop"
    # The lost cross is not just missing: it is recorded as suppressed by the halt,
    # so a replay can tell a halted desk from a full one.
    blocked = sim.decisions.loc[sim.decisions["blocked_by"] == "run_halted"]
    assert len(blocked) == 1 and blocked.iloc[0]["signal"] == "BUY"
    assert sim.metrics["completed_trades"] == 1.0


def test_replay_resumes_entries_after_the_cooldown():
    cfg = replace(
        run2_config(),
        starting_equity=1000.0,
        portfolio=replace(
            run2_config().portfolio,
            halt_recovery_drawdown_pct=2.5,
            halt_recovery_days=5,
        ),
    )
    sim = simulate_portfolio(cfg, _halt_then_recover_frames())
    assert list(sim.trades["side"]) == ["buy", "sell", "buy"]  # re-armed, took the cross
    assert sim.trades.iloc[-1]["date"] > sim.trades.iloc[1]["date"]
    assert sim.metrics["completed_trades"] == 1.0


def test_recovered_replay_needs_the_same_equity_path_as_the_latch_until_it_resumes():
    frames = _halt_then_recover_frames()
    latched = simulate_portfolio(replace(run2_config(), starting_equity=1000.0), frames)
    resumed = simulate_portfolio(
        replace(
            run2_config(),
            starting_equity=1000.0,
            portfolio=replace(
                run2_config().portfolio,
                halt_recovery_drawdown_pct=2.5,
                halt_recovery_days=5,
            ),
        ),
        frames,
    )
    first_sell = resumed.trades.iloc[1]["date"]
    assert latched.equity.loc[:first_sell].equals(resumed.equity.loc[:first_sell])


def test_repo_experiment_manifests_load_with_their_recovery_rules():
    """The committed recovery manifests must stay loadable and honest."""
    expectations = {
        "scale-2x-recover.yaml": ("exp-scale-2x-recover", 1250, 2.5, 20),
        "scale-4x-recover.yaml": ("exp-scale-4x-recover", 2500, 2.5, 20),
        "scale-4x-recover-10d.yaml": ("exp-scale-4x-recover-10d", 2500, 2.5, 10),
        "scale-4x-recover-60d.yaml": ("exp-scale-4x-recover-60d", 2500, 2.5, 60),
    }
    for name, (run_id, notional, dd, days) in expectations.items():
        cfg = load_run_config(ROOT / "config" / "experiments" / name, strict=False)
        assert cfg.run_id == run_id
        assert cfg.portfolio.position_notional == notional
        assert cfg.portfolio.halt_recovery_drawdown_pct == dd
        assert cfg.portfolio.halt_recovery_days == days
        assert cfg.portfolio.drawdown_halt_pct == 5


def test_scale_1x_manifest_is_a_faithful_copy_of_the_frozen_run():
    """The ladder's 1x reference row must not drift from the live manifest."""
    frozen = run2_config()
    one_x = load_run_config(ROOT / "config" / "experiments" / "scale-1x.yaml", strict=False)
    assert one_x.run_id == "exp-scale-1x"
    assert one_x.portfolio == frozen.portfolio
    assert one_x.strategy == frozen.strategy
    assert one_x.execution == frozen.execution
    assert one_x.symbols == frozen.symbols
    assert one_x.portfolio.halt_recovery_drawdown_pct is None


def test_ledger_close_reopens_cleanly(tmp_path):
    """Guard the helper pattern above: a closed ledger can be re-opened."""
    cfg, ledger = initialized_ledger(tmp_path)
    ledger.close()
    again = RunLedger(tmp_path / "ledger.sqlite")
    try:
        assert again.high_water(cfg.run_id) is None
    finally:
        again.close()
