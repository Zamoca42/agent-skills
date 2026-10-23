# Scheduling a daily brief (optional)

On-demand use needs no schedule. To get a brief every morning, pick one of these.

## A. Script-only cron (no agent turn, cheapest)

Needs non-agent backends (`classify.backend = "hermes"` or `"typesafe"`, `digest.backend = "hermes"` or
`"none"`). The script's stdout is the message.

```
hermes cron create "0 8 * * *" --name "inbox brief" --no-agent \
  --script inbox_brief_run.py --deliver telegram
```

with `~/.hermes/scripts/inbox_brief_run.py`:

```python
import subprocess, sys
SKILL = "/path/to/inbox-brief/scripts/inbox_brief.py"
sys.exit(subprocess.call([sys.executable, SKILL, "run"]))          # report only, nothing moved
# sys.exit(subprocess.call([sys.executable, SKILL, "run", "--yes"]))  # also moves mail (unattended)
```

Use `run` without `--yes` until you trust the labels; add `--yes` (or `apply.auto_trash = true`) only after
a few days of reports look right.

## B. Agent cron (the agent classifies)

```
hermes cron create "0 8 * * *" --name "inbox brief" --skill inbox-brief --deliver telegram \
  "Run the inbox-brief skill end to end. This is unattended: do not ask questions. Do not move mail \
   unless apply.auto_trash is true in the config; then pass --yes. Reply with the report output only."
```

This costs one agent turn per run and works with the `agent` backends.

## Delivery caveat

Delivering a script-only job's output into a Bot Chat (rather than a messaging channel) still starts an
agent turn in that chat. Prefer a messaging target for a plain report.
