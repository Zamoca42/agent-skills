# agent-skills

[Agent Skills](https://agentskills.io) by Zamoca42. They are written for
[Hermes Agent](https://github.com/NousResearch/hermes-agent) and use the standard `SKILL.md` format,
so other agents that read Agent Skills can load them too.

| Skill | What it does |
|---|---|
| [inbox-brief](skills/inbox-brief) | Summarizes and tidies unread mail across IMAP accounts (himalaya): sorts it into keep / newsletter / promo / junk, writes a newsletter digest, and moves mail to the trash after you approve. |

## Install

```
hermes skills install Zamoca42/agent-skills/<skill>
# other agents
npx skills add Zamoca42/agent-skills --skill <skill>
```

Or copy `skills/<skill>/` into your agent's skills folder.

## Develop

```
python -m pytest -q tests      # each skill has its own tests/<skill>/ folder; no network needed
```

## License

MIT
