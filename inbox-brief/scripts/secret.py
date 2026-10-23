#!/usr/bin/env python3
"""Resolve a secret (mail app password, API key) from one or more sources.

A spec is `<scheme>:<value>`; several specs may be given and the first that
resolves wins:

  env:NAME              environment variable
  file:PATH             first line of a text file (~ is expanded)
  keyring:SERVICE/USER  OS credential store via the `keyring` package
                        (macOS Keychain, Secret Service, Windows Credential Manager)
  wincred:TARGET        Windows Credential Manager generic credential, no extra package
  cmd:COMMAND           stdout of a command (pass, op, bw, ...); opt-in, see below

All whitespace is stripped: app passwords are often shown as "abcd efgh ijkl mnop"
and editors append newlines, both of which break SASL PLAIN.

CLI (used by himalaya's `auth.cmd` and by inbox_brief.py):
  python secret.py get env:GMAIL_APP_PASSWORD file:~/.secrets/gmail.txt
  python secret.py set wincred:inbox-brief/personal      (prompts, no echo)
  python secret.py set keyring:inbox-brief/personal
  python secret.py check env:X keyring:svc/user          (reports which spec resolves, never the value)

`cmd:` runs only when INBOX_BRIEF_ALLOW_CMD=1 is set, so a config file alone
cannot make the tool execute arbitrary commands.
"""
import getpass
import os
import subprocess
import sys


class SecretError(Exception):
    pass


def _strip(s):
    return "".join((s or "").split())


def _wincred_read(target):
    import ctypes
    import ctypes.wintypes as wt

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
            ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
            ("CredentialBlobSize", wt.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
            ("Attributes", ctypes.c_void_p), ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR),
        ]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    ptr = ctypes.POINTER(CREDENTIAL)()
    if not adv.CredReadW(target, 1, 0, ctypes.byref(ptr)):  # 1 = CRED_TYPE_GENERIC
        return None
    try:
        c = ptr.contents
        return bytes(ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)).decode("utf-8")
    finally:
        adv.CredFree(ptr)


def _wincred_write(target, secret):
    import ctypes
    import ctypes.wintypes as wt

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
            ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
            ("CredentialBlobSize", wt.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
            ("Attributes", ctypes.c_void_p), ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR),
        ]

    blob = secret.encode("utf-8")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    c = CREDENTIAL()
    c.Type = 1
    c.TargetName = target
    c.UserName = target
    c.CredentialBlobSize = len(blob)
    c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    c.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    if not ctypes.WinDLL("advapi32", use_last_error=True).CredWriteW(ctypes.byref(c), 0):
        raise SecretError(f"CredWriteW failed for {target}")


def _keyring_split(value):
    if "/" not in value:
        raise SecretError("keyring spec must be keyring:SERVICE/USER")
    return value.split("/", 1)


def resolve_one(spec):
    """Return the secret for one spec, or None when that source has no value."""
    scheme, _, value = spec.partition(":")
    if not value:
        raise SecretError(f"bad secret spec {spec!r} (expected scheme:value)")
    if scheme == "env":
        return os.environ.get(value) or None
    if scheme == "file":
        path = os.path.expanduser(value)
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8-sig") as fh:
            return fh.readline() or None
    if scheme == "keyring":
        try:
            import keyring  # optional dependency
        except ImportError:
            return None
        service, user = _keyring_split(value)
        return keyring.get_password(service, user)
    if scheme == "wincred":
        return _wincred_read(value) if os.name == "nt" else None
    if scheme == "cmd":
        if os.environ.get("INBOX_BRIEF_ALLOW_CMD") != "1":
            raise SecretError("cmd: secrets are disabled; set INBOX_BRIEF_ALLOW_CMD=1 to allow them")
        r = subprocess.run(value, shell=True, capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 and r.stdout.strip() else None
    raise SecretError(f"unknown secret scheme {scheme!r}")


def resolve(specs):
    """First non-empty secret among specs (str or list). Raises SecretError naming the specs only."""
    if isinstance(specs, str):
        specs = [specs]
    for spec in specs or []:
        value = _strip(resolve_one(spec))
        if value:
            return value
    raise SecretError("no secret found in: " + ", ".join(specs or []))  # specs name sources, never values


def main(argv):
    if len(argv) < 2 or argv[0] not in ("get", "set", "check"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    cmd, specs = argv[0], argv[1:]
    try:
        if cmd == "get":
            sys.stdout.write(resolve(specs))
            return 0
        if cmd == "check":
            for spec in specs:
                ok = bool(_strip(resolve_one(spec)))
                print(f"{'found' if ok else 'missing':7} {spec}")
            return 0
        spec = specs[0]
        scheme, _, value = spec.partition(":")
        secret = _strip(getpass.getpass(f"secret for {spec}: "))
        if not secret:
            raise SecretError("empty secret, nothing stored")
        if scheme == "wincred":
            if os.name != "nt":
                raise SecretError("wincred: is Windows-only; use keyring: elsewhere")
            _wincred_write(value, secret)
        elif scheme == "keyring":
            import keyring
            service, user = _keyring_split(value)
            keyring.set_password(service, user, secret)
        else:
            raise SecretError("set supports wincred: and keyring: only")
        print(f"stored {spec}")
        return 0
    except SecretError as exc:
        print(f"secret: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
