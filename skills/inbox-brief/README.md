# inbox-brief

An [Agent Skill](https://agentskills.io) that reads the unread mail of your IMAP accounts through
[himalaya](https://github.com/pimalaya/himalaya), sorts it into **keep / newsletter / promo / junk**,
summarizes the newsletters, and tidies the rest into the trash once you approve. Ask for it in chat
("summarize my mail") or run it as a daily scheduled job.

- **Many accounts, any IMAP provider.** Gmail, Outlook, Naver, Fastmail and so on, anything himalaya
  supports.
- **Pick the classifier.** It can be the chat model itself, a one-shot [Hermes](https://github.com/NousResearch/hermes-agent)
  call with any provider and model, or the [TypeSafe](https://typesafe.ai) decision API (Jev).
  Deterministic sender and subject rules run first and are free.
- **Newsletter digest.** The bodies are read without marking them seen and summarized into a short
  section. Nothing is invented: if a body is empty, the digest says so.
- **Safe moves.**
  - Nothing moves without `--yes`, or without `auto_trash` for unattended runs.
  - Every id is re-checked against its Message-ID before a move.
  - Mail goes to the trash, which you can recover from. The trash is never emptied.
- **A kept-mail queue.** Mail the brief told you to look at stays in the inbox. Later, "trash the kept
  mail" moves it in one step, and you can name the ones to keep.

## Install

```
# Hermes Agent
hermes skills install Zamoca42/agent-skills/inbox-brief
# other agents: npx skills add Zamoca42/agent-skills --skill inbox-brief
# or copy this folder into your agent's skills folder (e.g. ~/.hermes/skills/productivity/inbox-brief)

cp templates/config.example.toml ~/.config/inbox-brief/config.toml   # then edit accounts
python scripts/inbox_brief.py doctor
```

You need Python 3.11+ and a working himalaya account for each mailbox. Passwords and API keys are read
from environment variables, files, the OS credential store or a password manager. See
[`references/secrets.md`](references/secrets.md).

## Use

In chat: "summarize my mail", "메일 요약해줘". The agent follows `SKILL.md`: scan → classify → digest →
show the plan → apply on approval → report.

From a shell:

```
python scripts/inbox_brief.py run          # brief only (needs hermes/typesafe backends)
python scripts/inbox_brief.py run --yes    # brief + move non-keep mail to trash
python scripts/inbox_brief.py kept list
```

Scheduling: [`references/scheduling.md`](references/scheduling.md).

## What leaves your machine

| Step | Data sent | Where |
|---|---|---|
| classify (`agent`) | sender + subject of unlabeled unread mail | your chat model provider |
| classify (`hermes`) | sender + subject of unlabeled unread mail | the provider/model you configure |
| classify (`typesafe`) | sender + subject of unlabeled unread mail | api.typesafe.ai |
| digest (`agent` / `hermes`) | newsletter bodies (HTML stripped, truncated) | your chat model / the configured model |
| rules, apply, report, kept | nothing | local only |

Run state (labels, the kept queue, the action log) is stored under `state_dir` on your machine. It
contains senders and subjects, so keep it out of shared folders.

## Develop

```
python -m pytest -q tests/inbox-brief      # from the repo root; fake himalaya, no network
```

## License

MIT
