---
name: inbox-brief
description: Summarize and tidy unread mail across IMAP accounts.
version: 0.1.0
author: Zamoca42 (Zamoca42), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Email, IMAP, himalaya, Newsletter, Digest, Inbox]
    related_skills: [himalaya]
---

# Inbox Brief Skill

Reads the unread mail of one or more himalaya accounts, labels each message `keep`, `newsletter`, `promo` or
`junk`, summarizes newsletter bodies, and (only when approved) moves everything but `keep` to the trash. It
prints a short brief. Labeling runs on the chat model itself, a one-shot Hermes call, or the TypeSafe API.
It never sends, replies, opens links, downloads attachments, or empties the trash.

## When to Use

- The user asks for a mail summary or cleanup: "summarize my mail", "anything important in my inbox?",
  "메일 요약해줘", "메일 정리해줘".
- The user asks to trash mail an earlier brief kept: "trash the kept mail", "보관 메일 휴지통으로".
- A scheduled job runs this skill (see `references/scheduling.md`).
- Don't use for: writing or replying to mail, searching old mail, or managing a single message. Use the
  `himalaya` skill directly for those.

## Prerequisites

- Python 3.11+ and [himalaya](https://github.com/pimalaya/himalaya) with each account working:
  `terminal(command="himalaya -a <account> envelope list --page-size 1")`.
- A config at `~/.config/inbox-brief/config.toml` (or `$INBOX_BRIEF_CONFIG`), copied from
  `templates/config.example.toml`. It lists the accounts and picks the backends.
- Mail passwords and API keys come from env vars, files, the OS credential store or a password manager.
  See `references/secrets.md`. Never ask the user to paste a secret into chat.
- Optional: the `hermes` CLI for the `hermes` backends, and a `TYPESAFE_API_KEY` for the `typesafe` backend.

## How to Run

Every command goes through `terminal`. Use timeout 600 for `scan`, `classify` and `digest`, because large
inboxes and model calls are slow.

```
S=<skill dir>/scripts/inbox_brief.py
terminal(command="python $S doctor")                       # first run: config, accounts, keys
terminal(command="python $S scan", timeout=600)
terminal(command="python $S classify", timeout=600)
terminal(command="python $S label <labels.json>")          # agent backend only
terminal(command="python $S digest", timeout=600)
terminal(command="python $S digest --save <digest.md>")    # agent backend only
terminal(command="python $S apply --dry-run")
terminal(command="python $S apply --yes")                  # after the user approves
terminal(command="python $S report")
```

## Quick Reference

- `run [--yes]`: scan, classify, digest and report in one call. It needs non-agent backends. It moves mail
  only with `--yes` or `apply.auto_trash`.
- `kept list`: show the queue of mail kept by earlier briefs.
- `kept trash [2,5] [--keep-match WORD] [--yes]`: move the queue to the trash, except the listed numbers or
  matching words.
- `kept drop 3,4`: forget queue entries without moving them.
- `classify --backend agent|hermes|typesafe` and `digest --backend agent|hermes|none` override the config
  for one call.

## Procedure

1. **Scan.** Run `scan`. Done when it prints `run <id>: N unread, … need classify`. If an account reports an
   error, continue with the others and mention it in the brief.
2. **Classify.**
   - With the `agent` backend, `classify` prints the label definitions and one `ref | sender | subject`
     line per open mail. Decide one label for every ref, using the definitions only. When unsure whether
     the user must act, choose `keep`. Write the JSON object to a file with `write_file`, then run
     `label <file>`.
   - With `hermes` or `typesafe`, the script labels the mail itself.
   - Done when the printed counts cover every unread mail. Missing or unknown labels fall back to `keep`.
3. **Digest.**
   - With the `agent` backend, `digest` writes the newsletter bodies to a file and prints its path. Read
     the whole file with `read_file` and write the section it asks for, using only facts from the file.
     Save it to a file and run `digest --save <file>`.
   - With `hermes` or `none`, the script does this itself.
   - Done when `digest` exits 0, or prints `no newsletters`.
4. **Approve and apply.**
   - On demand: run `apply --dry-run`, show the user the counts and any `keep` vs trash decisions that
     look borderline, and wait for approval. Then run `apply --yes`.
   - Unattended: pass `--yes` only when `apply.auto_trash = true`.
   - Done when `apply` prints `moved …, skipped …, failed …`. `apply` re-checks every id against its
     Message-ID and skips mail that changed.
5. **Report.** Run `report` and send its output as the reply, unedited. You may add one line about errors.
   Done when the reply contains the head line and the "needs attention" list.

## Pitfalls

- **Order matters:** digest runs before apply. After a move the ids change, and `apply` refuses to run while
  newsletters are unsummarized unless you pass `--skip-digest`.
- **`keep_keywords` overrides newsletter and promo models.** A word like "security" also keeps a
  newsletter about security. Prefer sender rules and the criteria text.
- **himalaya ids are per-mailbox and change after a move.** Never reuse ids from an old run. `kept trash`
  re-verifies them.
- **Gmail may reject a batched move** with `NO System Error`. The script retries one by one. Trash
  folder names differ per provider: set `trash` per account (an alias like `trash`, or `[Gmail]/Trash`).
- **External data flow:** the `hermes`, `typesafe` and agent backends send sender and subject lines to a
  model provider, and digests send newsletter bodies. Tell the user if they have not configured this.
- **Windows:** prompts go through files because the command line is capped near 32k characters. The
  `hermes -z` call is given only the `file` toolset.

## Verification

- `python <skill dir>/scripts/inbox_brief.py doctor` reports every account `ok`.
- After `apply --yes`, `kept list` shows the kept mail, and the trash folder holds the moved mail
  (`terminal(command="himalaya -a <account> envelope list -m <trash> --page-size 5")`).
- The repository tests pass: `terminal(command="python -m pytest -q tests")`. They use a fake himalaya and
  no network.
