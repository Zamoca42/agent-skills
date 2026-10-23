# Secrets: mail app passwords and API keys

`scripts/secret.py` resolves a secret from an ordered list of specs; the first that yields a value wins.
All whitespace is stripped (app passwords are often shown as `abcd efgh ijkl mnop`).

| Spec | Source | Notes |
|---|---|---|
| `env:NAME` | environment variable | simplest for cron and containers |
| `file:PATH` | first line of a text file | keep it outside any repo; `chmod 600` on Unix |
| `keyring:SERVICE/USER` | OS credential store via the `keyring` package | macOS Keychain, Secret Service, Windows; `pip install keyring` |
| `wincred:TARGET` | Windows Credential Manager (generic credential) | no extra package |
| `cmd:COMMAND` | stdout of a command (`pass show mail/work`, `op read ...`) | disabled unless `INBOX_BRIEF_ALLOW_CMD=1` |

Store one without echoing it:

```
python scripts/secret.py set wincred:inbox-brief/personal
python scripts/secret.py set keyring:inbox-brief/personal
python scripts/secret.py check env:PERSONAL_MAIL_PW wincred:inbox-brief/personal   # prints found/missing only
```

## himalaya

Point the account's password command at the resolver, so himalaya and inbox-brief share one source:

```toml
[accounts.personal]
# ... imap/smtp host, port, login ...
backend.auth.type = "password"
backend.auth.cmd = "python /path/to/inbox-brief/scripts/secret.py get env:PERSONAL_MAIL_PW wincred:inbox-brief/personal"
```

Key names differ between himalaya releases (`imap.passwd.cmd` in v1, `backend.auth.cmd` in v2); check
`himalaya account configure` or the himalaya docs for your version. Verify with
`terminal(command="himalaya -a personal envelope list --page-size 1")`.

## TypeSafe (Jev) key

`[classify] typesafe_key` takes the same specs, for example
`["env:TYPESAFE_API_KEY", "file:~/.config/inbox-brief/typesafe.key"]`.

Never paste a secret into the chat, a config file inside a git repo, or a cron prompt.
