# Running the scheduled jobs on Linux (systemd user timers)

This document records the port of the two scheduled paper-trading jobs from
**macOS launchd** to **Linux systemd user units**, and everything needed to
install, verify, test, and remove them. The project originally shipped only
launchd plists; it now supports both platforms. The code being scheduled is
unchanged — same wrapper scripts, same CLI, same Alpaca **paper** account.

## What changed

| File | Role |
|---|---|
| `scripts/systemd/money-paperscan.service` | Runs `scripts/daily_paper_run.sh` (daily signal scan) |
| `scripts/systemd/money-paperscan.timer` | Fires it daily at **18:05 local time** |
| `scripts/systemd/money-stopmonitor.service` | Runs `scripts/intraday_stop_run.sh` (stop-loss monitor) |
| `scripts/systemd/money-stopmonitor.timer` | Fires it every 30 minutes (:00/:30) and ~10 min after login |
| `scripts/_lib.sh` | `notify()` now uses `notify-send` on Linux (`osascript` kept for macOS); log rotation creates the target directory if missing |
| `README.md` | Automation section rewritten; Linux is documented first |

The old plists (`scripts/com.money.*.plist`) are kept untouched for anyone
still running macOS.

## Why these schedules

Both match the original launchd intent:

- **18:05 America/Mexico_City** is just after the 00:00 UTC crypto daily close,
  so crypto signals evaluate the candle that just finished rather than one
  nearly a day old — and after the US equity close.
- **Every 30 minutes** stop checks cut a falling position intraday instead of
  waiting for the once-a-day scan.
- `Persistent=true` fires any run missed while the machine slept or was off as
  soon as possible after wake/boot (the equivalent of launchd running a missed
  job once the Mac wakes).

## launchd → systemd concept map

| launchd (macOS) | systemd (Linux) |
|---|---|
| `~/Library/LaunchAgents/*.plist` | `~/.config/systemd/user/*.{service,timer}` |
| `StartCalendarInterval` 18:05 | `OnCalendar=*-*-* 18:05:00` + `Persistent=true` |
| `StartInterval` 1800 | `OnCalendar=*:0/30` (+ `OnStartupSec=10min` ≈ `RunAtLoad`) |
| `launchctl load -w …` | `systemctl --user daemon-reload && systemctl --user enable --now …` |
| `launchctl list \| grep money` | `systemctl --user list-timers 'money-*'` |
| `launchctl unload -w …` | `systemctl --user disable --now …` |

## Install / verify / disable

```bash
cd <repo>                                  # e.g. ~/Documents/personal/money

# One-time install
mkdir -p ~/.config/systemd/user
cp scripts/systemd/*.service scripts/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now money-paperscan.timer money-stopmonitor.timer

# Verify: both should report "enabled", list-timers shows next fire times
systemctl --user is-enabled money-paperscan.timer money-stopmonitor.timer
systemctl --user list-timers 'money-*'

# Disable both schedules (units stay installed)
systemctl --user disable --now money-paperscan.timer money-stopmonitor.timer
```

### If the project moves

The `.service` files hardcode `WorkingDirectory` and the absolute script path.
Update both lines in each `.service`, re-copy them, then
`systemctl --user daemon-reload`. The timer files need no changes.

## Testing without placing orders

Run the wrappers exactly as systemd does, but with `DRY_RUN=1`:

```bash
DRY_RUN=1 bash scripts/daily_paper_run.sh       # tail results/paper_scan.log
DRY_RUN=1 bash scripts/intraday_stop_run.sh     # tail results/stop_monitor.log
```

Expected: an exit-code line of `Exit code: 0` in each log. The daily run prints
a per-symbol HOLD/BUY/SELL table; dry-run never submits orders. To make the
real scheduled run a no-op temporarily, use
`systemctl --user edit money-paperscan` and add `Environment=DRY_RUN=1`
under the `[Service]` heading (drop-in override).

## Where output goes

| Artifact | Path |
|---|---|
| Daily scan log (rotated to ~1000 lines) | `results/paper_scan.log` |
| Stop monitor log (rotated to ~1000 lines) | `results/stop_monitor.log` |
| Success heartbeats (unix timestamps) | `results/.last_success_paperscan`, `results/.last_success_stopmonitor` |
| Paper-trade decision journal | `results/paper_journal.csv` |
| Desktop notification on failure / order placed | via `notify-send` |
| **Email digest** after each real daily run; **alert email** on stop-loss or failure | via `money email-report` (`EMAIL_*` keys in `.env`) |

A stale heartbeat file (timestamp far in the past) means the schedule has not
succeeded recently — check the matching log first.

## Troubleshooting

- **Timers don't fire when logged out** — user timers belong to your login
  session. To let them run with no active session:
  `loginctl enable-linger $USER`.
- **`Failed to connect to user scope bus`** — the shell can't reach your user
  systemd manager (e.g. inside some sandboxes/containers). Run the
  `systemctl --user` commands from a normal terminal session.
- **Missed the 18:05 window anyway?** Nothing is lost: `Persistent=true` fires
  it on next boot/login, or trigger it manually with
  `systemctl --user start money-paperscan.service` (add `--dry-run` thinking by
  setting `Environment=DRY_RUN=1` first if you want a preview).
- **Check what actually ran** — `journalctl --user -u money-paperscan.service`
  or `-u money-stopmonitor.service`; the wrapper's own detail is always in the
  `results/*.log` files above.

## Still on macOS?

Everything in `scripts/com.money.paperscan.plist` /
`scripts/com.money.stopmonitor.plist` works as before — see the "macOS
(launchd)" part of the README's *Automated daily runs* section. The two
platforms are independent; only install the set that matches your OS.
