---
name: danta-bot-daily-review
description: Analyze manually downloaded Docker zip bundles containing codex-exec and telegram-gateway runtime artifacts for daily-trading postmortems. Use when Codex is asked to inspect a file such as /home/uhug/Downloads/docker.zip for a specific trading date, compare prior-date logs and the current danta-bot git history, extract evidence-backed issues, or create analysis-only codex fork prompts for each issue.
---

# Danta Bot Daily Review

Use this skill after the user has already downloaded a zip bundle from another machine. Do not implement remote collection or Telegram attachment download in this workflow.

## Workflow

1. Run `scripts/analyze_bundle.py` against the zip, date, repo, and requested issue count.
2. Treat copied zip artifacts as runtime truth for the requested date.
3. Use previous dates and the current local git history only as comparison evidence.
4. Read `analysis.md`, `evidence.json`, and `issues.json` before giving the final analysis.
5. Always create analysis-only fork conversations for every generated issue. Pass `--fork-session-id <session-id>` when the target session is known; otherwise the helper must use `CODEX_THREAD_ID` from the current Codex environment. Do not treat fork creation as optional unless the user explicitly requests `--dry-run`.

## Command

```bash
python3 /home/uhug/git/danta-bot/.agents/skills/danta-bot-daily-review/scripts/analyze_bundle.py \
  --zip /home/uhug/Downloads/docker.zip \
  --date 2026-07-02 \
  --issue-count 3 \
  --repo /home/uhug/git/danta-bot \
  --output-dir /tmp/danta-bot-daily-review/2026-07-02 \
  --force
```

Fork execution is mandatory for normal runs:

```bash
python3 /home/uhug/git/danta-bot/.agents/skills/danta-bot-daily-review/scripts/analyze_bundle.py \
  --zip /home/uhug/Downloads/docker.zip \
  --date 2026-07-02 \
  --issue-count 3 \
  --repo /home/uhug/git/danta-bot \
  --output-dir /tmp/danta-bot-daily-review/2026-07-02 \
  --execute-forks \
  --force
```

`--execute-forks` is kept for compatibility, but normal runs fork by default. Without an explicit `--fork-session-id`, the helper uses `CODEX_THREAD_ID`. Use `--dry-run` only when the user explicitly asks not to create fork conversations.

## Analysis Rules

- Start from the target date.
- Use `execution.json` as the source of truth for submitted, blocked, skipped, and failed order rows.
- Reconcile `today-fills.json`, `account-before-order.json`, `pipeline-summary.json`, `decision-brief.json`, `telegram-summary.txt`, the dated portfolio report, and `telegram-gateway/memory/telegram-conversations/<YYYY-MM-DD>.jsonl`.
- If multiple runs exist for the date, use `--run-id` when provided. Otherwise use the latest same-date run as the primary run and summarize earlier same-date runs as intraday context.
- `--previous-days N` means previous N calendar days. Use those dates only to identify repeated or changed patterns.
- Use current repo `git log`, `git status`, and `git diff --stat` only to annotate whether an issue may already be fixed or related to current local changes.
- If fewer than the requested issue count have concrete evidence, report fewer issues and say why.

## Fork Rules

Fork conversations are analysis-only. The helper must construct prompts containing:

```text
<N>번째 문제점에 대해서 구체적으로 설명해봐. 코드/파일 수정, git 작업, patch 적용은 하지 말고 분석만 해줘.
```

Use `codex fork` with a positional session id and read-only options. The session id must come from `--fork-session-id` or `CODEX_THREAD_ID`:

```bash
codex fork --sandbox read-only --ask-for-approval never <SESSION_ID> <PROMPT>
```

Do not create git worktrees for this workflow. Do not ask forked conversations to modify code.
