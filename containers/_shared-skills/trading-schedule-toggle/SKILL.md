---
name: trading-schedule-toggle
description: "Toggle only `daily-{number}` schedules in a codex-exec `schedules.yaml` file. Use when the user asks Codex to enable, activate, turn on, disable, deactivate, turn off, pause, or resume daily trading schedules, especially after `$check-holiday` determines whether the Korean market is open or closed."
---

# Trading Schedule Toggle

## Purpose

Enable or disable codex-exec daily trading schedules whose IDs match `daily-{number}`.

## Rules

- Only modify schedules with IDs matching `^daily-[0-9]+$`.
- Do not modify `pre-open`, `trading-toggle`, or any other non-daily schedule.
- Prefer the schedule file path in `SCHEDULE_FILE`.
- If `SCHEDULE_FILE` is unset, use `/app/config/schedules.yaml`.
- If the user gives a specific schedule number, modify only that ID, for example `1` means `daily-1`.
- If the user does not give specific daily numbers, modify all `daily-{number}` schedules.
- Treat an open trading day, `활성화`, `켜줘`, `on`, `enable`, or `resume` as `on`.
- Treat a holiday, closed day, `비활성화`, `꺼줘`, `off`, `disable`, or `pause` as `off`.
- If the requested state is unclear, ask the user before modifying the file.

## Workflow

1. Resolve the target state as `on` or `off`.
2. Resolve the target schedule file.
3. Run a dry run first:

   ```bash
   python3 scripts/toggle_daily_schedules.py --state on --dry-run
   ```

4. If the dry run would change the intended `daily-{number}` schedules only, run it without `--dry-run`:

   ```bash
   python3 scripts/toggle_daily_schedules.py --state on
   ```

5. Report the file path, changed IDs, unchanged IDs, and final state.

## Script Usage

Toggle all daily schedules:

```bash
python3 scripts/toggle_daily_schedules.py --state off
```

Toggle selected daily schedules:

```bash
python3 scripts/toggle_daily_schedules.py --state on --numbers 1,3
```

Use an explicit file path:

```bash
python3 scripts/toggle_daily_schedules.py --file /app/config/schedules.yaml --state off
```
