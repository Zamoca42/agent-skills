#!/usr/bin/env python3
"""inbox-brief: summarize and tidy unread mail across himalaya accounts.

Steps (each reads/writes the current run under <state_dir>/runs/<run-id>/run.json):
  scan       list unread mail of every configured account and apply the deterministic rules
  classify   label what the rules left open      (backend: agent | hermes | typesafe)
  label      store labels decided by the calling agent (agent backend)
  digest     summarize newsletter bodies          (backend: agent | hermes | none)
  apply      move newsletter/promo/junk to trash and queue kept mail (needs --yes or apply.auto_trash)
  report     print the brief
  run        scan -> classify -> digest -> [apply] -> report in one go (hermes/typesafe/none backends)
  kept       list | trash | drop the kept-mail queue
  doctor     check config, himalaya, accounts and secrets

Config: --config PATH, $INBOX_BRIEF_CONFIG, or ~/.config/inbox-brief/config.toml (see templates/).
Labels: keep (stays in the inbox), newsletter (summarized, then trash), promo (trash), junk (trash).
Nothing here sends mail, opens links, downloads attachments or empties the trash.
"""
import argparse
import copy
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import secret as secret_store  # noqa: E402

LABELS = ("keep", "newsletter", "promo", "junk")
TRASH_LABELS = ("newsletter", "promo", "junk")

DEFAULTS = {
    "himalaya": "himalaya",
    "himalaya_config": "",
    "hermes": "hermes",
    "state_dir": "~/.local/state/inbox-brief",
    "language": "en",
    "query": "not flag seen",
    "page_size": 200,
    "accounts": [],
    "classify": {
        "backend": "agent",
        "provider": "",
        "model": "",
        "timeout": 420,
        "typesafe_url": "https://api.typesafe.ai/v1/systemone",
        "typesafe_model": "jev-1.13.0",
        "typesafe_key": ["env:TYPESAFE_API_KEY"],
        "min_confidence": 0.7,
        "batch": 120,
    },
    "digest": {"backend": "agent", "provider": "", "model": "", "timeout": 420, "max_mails": 20,
               "per_mail_chars": 7000},
    "rules": {
        "keep_senders": [],
        "newsletter_senders": [],
        "promo_senders": [],
        "junk_senders": [],
        "promo_subject_markers": ["(광고)", "[광고]", "(AD)", "[AD]"],
        "keep_keywords": [],  # subject words that force keep before any model runs; see config example
    },
    "criteria": {
        "version": "v1",
        "context": "Daily inbox cleanup. Classify each unread mail from its sender and subject only. When a "
                   "mail could be newsletter or promo, choose promo if it sells something. When unsure whether "
                   "the user must act, choose keep.",
        "keep": "Needs the user's attention or a decision: security alert, new sign-in or 2FA/recovery change, "
                "failed payment, bill, refund, overdue notice, terms-of-service change, service shutdown, trial "
                "or subscription ending, account suspension or deletion, domain or hosting expiry, or mail "
                "personally written by a human to the user.",
        "newsletter": "Editorial content meant to be read: news briefing, article digest, essay, blog post or "
                      "community update. Its main purpose is information, not selling.",
        "promo": "Advertising or marketing: product launches, sales, discounts, coupons, webinars and events, "
                 "store or course promotions, upsells. A subject marked as an ad is always promo.",
        "junk": "Routine mail with nothing to read or do: receipts, purchase or shipping confirmations, "
                "verification codes, social-media notifications, routine service notices, job recommendations.",
    },
    "apply": {"auto_trash": False, "batch": 25},
    "report": {"max_keep_items": 8},
}

TEXT = {
    "en": {"head": "📬 Unread {n} → trash {t} / keep {k}", "pending": " (not moved yet)",
           "attention": "**Needs attention**", "none": "Nothing needs attention.",
           "news": "📰 Newsletters", "others": "- Others: {x}", "errors": "⚠ Check: {x}",
           "note": "Trash is recoverable. Kept mail is queued: say \"trash the kept mail\" to move it later.",
           "digest_lang": "English"},
    "ko": {"head": "📬 미읽음 {n}건 → 휴지통 {t} / 보관 {k}", "pending": " (아직 옮기지 않음)",
           "attention": "**봐야 할 것**", "none": "봐야 할 것 없음",
           "news": "📰 뉴스레터 요약", "others": "- 그 외: {x}", "errors": "⚠ 확인 필요: {x}",
           "note": "휴지통은 복구할 수 있다. 보관 메일은 대기열에 있다. '보관 메일 휴지통으로' 라고 하면 한 번에 옮긴다.",
           "digest_lang": "Korean"},
}


class BriefError(Exception):
    pass


# ---------------------------------------------------------------- config / state

def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path=None):
    path = path or os.environ.get("INBOX_BRIEF_CONFIG") or "~/.config/inbox-brief/config.toml"
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise BriefError(f"config not found: {path} (copy templates/config.example.toml there)")
    import tomllib
    with open(path, "rb") as fh:
        cfg = deep_merge(DEFAULTS, tomllib.load(fh))
    if not cfg["accounts"]:
        raise BriefError("config has no [[accounts]]")
    for acc in cfg["accounts"]:
        acc.setdefault("trash", "trash")
        if not acc.get("name"):
            raise BriefError("every [[accounts]] entry needs name = <himalaya account>")
    cfg["state_dir"] = os.path.expanduser(cfg["state_dir"])
    cfg["_path"] = path
    return cfg


def t(cfg, key, **kw):
    return TEXT.get(cfg["language"], TEXT["en"])[key].format(**kw)


def run_dir(cfg, run_id):
    return os.path.join(cfg["state_dir"], "runs", run_id)


def latest_run_id(cfg):
    try:
        with open(os.path.join(cfg["state_dir"], "latest"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        raise BriefError("no run yet: start with `scan`")


def load_run(cfg, run_id=None):
    run_id = run_id or latest_run_id(cfg)
    with open(os.path.join(run_dir(cfg, run_id), "run.json"), encoding="utf-8") as fh:
        return json.load(fh)


def save_run(cfg, run):
    d = run_dir(cfg, run["id"])
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "run.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(run, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(d, "run.json"))
    with open(os.path.join(cfg["state_dir"], "latest"), "w", encoding="utf-8") as fh:
        fh.write(run["id"])


def append_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- himalaya

def _argv(value):
    if isinstance(value, list):
        return list(value)
    if os.path.exists(os.path.expanduser(value)) or " " not in value:  # a plain path, even with spaces
        return [os.path.expanduser(value)]
    return shlex.split(value, posix=os.name != "nt")


def hm(cfg, account, *args, as_json=True, timeout=180):
    cmd = _argv(cfg["himalaya"])
    if cfg.get("himalaya_config"):
        cmd += ["-c", os.path.expanduser(cfg["himalaya_config"])]
    cmd += ["-a", account] + (["--json"] if as_json else []) + list(args)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise BriefError(f"{account}: himalaya {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:200]}")
    if not as_json:
        return r.stdout
    return json.loads(r.stdout) if r.stdout.strip() else {}


def _envelopes(data):
    return data.get("envelopes", []) if isinstance(data, dict) else (data or [])


def sender_of(env):
    f = env.get("from") or {}
    if isinstance(f, list):
        f = f[0] if f else {}
    return (f.get("name") or "").strip().strip('"'), (f.get("email") or f.get("addr") or "").strip()


def unread(cfg, account):
    query = account.get("query") or cfg["query"]
    out, page = [], 1
    while page <= 50:
        batch = _envelopes(hm(cfg, account["name"], "envelope", "search", "--page-size", str(cfg["page_size"]),
                              "--page", str(page), query))
        out += batch
        if not batch:
            break
        page += 1
    return out


def inbox_ids(cfg, account):
    ids, page = {}, 1
    while page <= 200:
        batch = _envelopes(hm(cfg, account, "envelope", "list", "--page-size", "200", "--page", str(page)))
        for e in batch:
            ids[str(e["id"])] = e.get("message-id")
        if len(batch) < 200:
            break
        page += 1
    return ids


def to_text(raw):
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", raw)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    import html
    s = html.unescape(s)
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"[ \t\u200c\u00a0\u034f\u200b]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


# ---------------------------------------------------------------- rules

def _hit(needles, text):
    text = text.lower()
    return next((n for n in needles if n and n.lower() in text), None)


def rule_label(cfg, item):
    r = cfg["rules"]
    who = f"{item['from']} <{item['email']}>"
    for label, key in (("keep", "keep_senders"),):
        if _hit(r[key], who):
            return label, f"rule:{key}"
    if _hit(r["promo_subject_markers"], item["subject"]):
        return "promo", "rule:promo_subject_markers"
    for label in ("newsletter", "promo", "junk"):
        if _hit(r[f"{label}_senders"], who):
            return label, f"rule:{label}_senders"
    if _hit(r["keep_keywords"], item["subject"]):
        return "keep", "rule:keep_keywords"
    return None, None


# ---------------------------------------------------------------- model backends

def hermes_ask(cfg, section, prompt_path, ask):
    """Run a one-shot Hermes call; the prompt sits in a file because Windows caps argv near 32k chars."""
    c = cfg[section]
    cmd = _argv(cfg["hermes"]) + ["-z", ask, "-t", "file"]
    if c.get("provider"):
        cmd += ["--provider", c["provider"]]
    if c.get("model"):
        cmd += ["-m", c["model"]]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=c["timeout"], encoding="utf-8", errors="replace",
                           cwd=os.path.dirname(prompt_path))
    except subprocess.TimeoutExpired:
        raise BriefError(f"hermes {section} timed out after {c['timeout']}s")
    if r.returncode != 0:
        raise BriefError(f"hermes {section} failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout or ""


def classify_prompt(cfg, pending):
    c = cfg["criteria"]
    lines = [f"Context: {c['context']}", "", "Labels:"] + [f"- {k}: {c[k]}" for k in LABELS]
    lines += ["", "Return ONLY a JSON object mapping every ref to one label, e.g. {\"acct:12\": \"promo\"}.", "",
              "Mails (ref | sender | subject):"]
    lines += [f"{i['ref']} | {i['from']} <{i['email']}> | {i['subject']}" for i in pending]
    return "\n".join(lines)


def parse_label_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise BriefError("model returned no JSON object")
    data = json.loads(m.group(0))
    return {str(k): str(v).strip().lower() for k, v in data.items()}


def classify_hermes(cfg, run, pending):
    path = os.path.join(run_dir(cfg, run["id"]), "classify-prompt.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(classify_prompt(cfg, pending))
    ask = (f"Read the file {path.replace(os.sep, '/')} with read_file (all of it, use offset if needed) and "
           "follow its instructions. Output only the JSON object. Do not use any other tool.")
    return {ref: (lab, "hermes") for ref, lab in parse_label_json(hermes_ask(cfg, "classify", path, ask)).items()}


def classify_typesafe(cfg, run, pending):
    c, crit = cfg["classify"], cfg["criteria"]
    key = secret_store.resolve(c["typesafe_key"])
    out, usage = {}, 0
    for start in range(0, len(pending), c["batch"]):
        chunk = pending[start:start + c["batch"]]
        body = {"model": c["typesafe_model"],
                "state": {"context": crit["context"],
                          "mails": {f"#{i}": f"From: {m['from']} <{m['email']}> | Subject: {m['subject']}"
                                    for i, m in enumerate(chunk)}},
                "questions": {f"q{i}": {"type": "choice", "instructions": f"Classify mail #{i}.",
                                        "criteria": {k: crit[k] for k in LABELS}} for i in range(len(chunk))}}
        req = urllib.request.Request(c["typesafe_url"], data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=c["timeout"]) as r:
            res = json.load(r)
        usage += int(res.get("usage", {}).get("input_tokens", 0))
        for i, m in enumerate(chunk):
            a = res["answers"][f"q{i}"]
            p = float(a.get("probabilities", {}).get(a["choice"], 0))
            low = p < c["min_confidence"]
            out[m["ref"]] = ("keep" if low else a["choice"], f"typesafe:{a['choice']}:{p:.2f}" + (":low" if low else ""))
    run["classify_usage"] = {"backend": "typesafe", "input_tokens": usage}
    return out


# ---------------------------------------------------------------- commands

def cmd_scan(cfg, args):
    run = {"id": dt.datetime.now().strftime("%Y%m%d-%H%M%S"), "started": dt.datetime.now().isoformat(timespec="seconds"),
           "criteria_version": cfg["criteria"]["version"], "items": [], "errors": {}}
    for acc in cfg["accounts"]:
        try:
            for e in unread(cfg, acc):
                name, email = sender_of(e)
                item = {"ref": f"{acc['name']}:{e['id']}", "account": acc["name"], "id": str(e["id"]),
                        "message_id": e.get("message-id"), "from": name or email, "email": email,
                        "subject": (e.get("subject") or "").strip(), "date": e.get("date") or ""}
                item["label"], item["source"] = rule_label(cfg, item)
                run["items"].append(item)
        except (BriefError, subprocess.TimeoutExpired, ValueError) as exc:
            run["errors"][acc["name"]] = str(exc)[:200]
    save_run(cfg, run)
    open_n = sum(1 for i in run["items"] if not i["label"])
    print(f"run {run['id']}: {len(run['items'])} unread, {len(run['items']) - open_n} labeled by rules, "
          f"{open_n} need classify" + (f"; account errors: {', '.join(run['errors'])}" if run["errors"] else ""))
    return 0


def cmd_classify(cfg, args):
    run = load_run(cfg, args.run)
    backend = args.backend or cfg["classify"]["backend"]
    pending = [i for i in run["items"] if not i["label"]]
    if not pending:
        print("nothing to classify")
        return 0
    if backend == "agent":
        path = os.path.join(run_dir(cfg, run["id"]), "classify-prompt.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(classify_prompt(cfg, pending))
        print(classify_prompt(cfg, pending))
        print(f"\nNEXT: decide one label per ref, write the JSON object to a file, then run:\n"
              f"  python {os.path.abspath(__file__)} label <file>")
        return 0
    labels = classify_hermes(cfg, run, pending) if backend == "hermes" else classify_typesafe(cfg, run, pending)
    merge_labels(run, labels, default_source=backend)
    save_run(cfg, run)
    print(summary_line(run))
    return 0


def merge_labels(run, labels, default_source):
    """labels: {ref: label or (label, source)}. Unknown labels and missing refs fall back to keep."""
    for item in run["items"]:
        if item["label"]:
            continue
        got = labels.get(item["ref"])
        lab, src = (got if isinstance(got, tuple) else (got, default_source))
        if lab in LABELS:
            item["label"], item["source"] = lab, src
        else:
            item["label"], item["source"] = "keep", f"fallback:{lab or 'missing'}"


def cmd_label(cfg, args):
    run = load_run(cfg, args.run)
    raw = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    data = json.loads(raw)
    if isinstance(data, list):
        data = {d["ref"]: d["label"] for d in data}
    merge_labels(run, {str(k): str(v).lower() for k, v in data.items()}, default_source="agent")
    save_run(cfg, run)
    print(summary_line(run))
    return 0


def summary_line(run):
    counts = {k: sum(1 for i in run["items"] if i["label"] == k) for k in LABELS}
    return "labels: " + ", ".join(f"{k} {v}" for k, v in counts.items())


def pick_newsletters(cfg, run):
    news = [i for i in run["items"] if i["label"] == "newsletter"]
    first, rest, seen = [], [], set()
    for i in news:  # one per sender first, then the rest
        (rest if i["from"] in seen else first).append(i)
        seen.add(i["from"])
    return (first + rest)[:cfg["digest"]["max_mails"]], news


def titles_section(cfg, news):
    by = {}
    for i in news:
        by.setdefault(i["from"], []).append(i["subject"])
    lines = [t(cfg, "news")]
    for who, subs in list(by.items())[:12]:
        lines.append(f"- {who} ({len(subs)}): " + ", ".join(subs[:2]))
    return "\n".join(lines)


def cmd_digest(cfg, args):
    run = load_run(cfg, args.run)
    d = run_dir(cfg, run["id"])
    if args.save:
        text = sys.stdin.read() if args.save == "-" else open(args.save, encoding="utf-8").read()
        run["digest"] = {"backend": "agent", "text": text.strip()}
        save_run(cfg, run)
        print("digest saved")
        return 0
    backend = args.backend or cfg["digest"]["backend"]
    chosen, news = pick_newsletters(cfg, run)
    if not news:
        run["digest"] = {"backend": backend, "text": ""}
        save_run(cfg, run)
        print("no newsletters")
        return 0
    if backend == "none":
        run["digest"] = {"backend": "none", "text": titles_section(cfg, news)}
        save_run(cfg, run)
        print(run["digest"]["text"])
        return 0
    parts, failed = [], []
    for i in chosen:
        try:
            body = to_text(hm(cfg, i["account"], "message", "read", i["id"], as_json=False))  # no --seen
            parts.append(f"===== {i['from']} — {i['subject']} =====\n" + body[:cfg["digest"]["per_mail_chars"]])
        except (BriefError, subprocess.TimeoutExpired) as exc:
            failed.append(str(exc)[:120])
    if not parts:
        run["digest"] = {"backend": "none", "text": titles_section(cfg, news), "error": "; ".join(failed)}
        save_run(cfg, run)
        print(run["digest"]["text"])
        return 1
    header = t(cfg, "news")
    instructions = (f"Below are newsletter bodies converted from HTML (truncated). Write a section in "
                    f"{t(cfg, 'digest_lang')} whose first line is exactly `{header}`.\n"
                    "- One bullet per newsletter: `- **Sender** — Subject`, then 1-3 sentences on what is new "
                    "(facts, numbers, conclusions). Ignore ads, subscription notices and footers.\n"
                    "- Several issues from one sender may be merged. Write \"no body (subject only)\" when the text "
                    "is empty. Never invent content.\n- At most 2,000 characters. No intro or closing sentence.\n\n")
    path = os.path.join(d, "digest-input.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(instructions + "\n\n".join(parts))
    leftover = sorted({i["from"] for i in news} - {i["from"] for i in chosen})
    run["digest"] = {"backend": backend, "input": path, "others": leftover, "text": ""}
    if backend == "agent":
        save_run(cfg, run)
        print(f"Digest input: {path}\nNEXT: read it fully, write the section it asks for into a file, then run:\n"
              f"  python {os.path.abspath(__file__)} digest --save <file>")
        return 0
    ask = (f"Read the file {path.replace(os.sep, '/')} with read_file (all of it, use offset if needed) and write "
           "only the section its first paragraph asks for. Do not use any other tool.")
    try:
        out = hermes_ask(cfg, "digest", path, ask).strip()
        if header not in out:
            raise BriefError("digest output lacks its header line")
        run["digest"]["text"] = out[out.index(header):]
    except BriefError as exc:
        run["digest"].update(text=titles_section(cfg, news), error=str(exc))
    save_run(cfg, run)
    print(run["digest"]["text"])
    return 0 if "error" not in run["digest"] else 1


def move_to_trash(cfg, account, trash, items, moved, failed):
    for start in range(0, len(items), cfg["apply"]["batch"]):
        chunk = items[start:start + cfg["apply"]["batch"]]
        try:
            hm(cfg, account, "message", "move", "--to", trash, *[c["id"] for c in chunk])
            moved += chunk
        except BriefError:
            for c in chunk:  # some servers (Gmail) reject a batch; retry one by one
                try:
                    hm(cfg, account, "message", "move", "--to", trash, c["id"])
                    moved.append(c)
                except BriefError as exc:
                    failed.append((c, str(exc)[:160]))


def kept_path(cfg):
    return os.path.join(cfg["state_dir"], "kept.json")


def load_kept(cfg):
    try:
        with open(kept_path(cfg), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def save_kept(cfg, items):
    os.makedirs(cfg["state_dir"], exist_ok=True)
    tmp = kept_path(cfg) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, kept_path(cfg))


def cmd_apply(cfg, args):
    run = load_run(cfg, args.run)
    if any(not i["label"] for i in run["items"]):
        raise BriefError("some mail is still unlabeled: run classify (and label) first")
    trash = [i for i in run["items"] if i["label"] in TRASH_LABELS]
    keep = [i for i in run["items"] if i["label"] == "keep"]
    if any(i["label"] == "newsletter" for i in trash) and "digest" not in run and not args.skip_digest:
        raise BriefError("newsletters are not summarized yet: run digest first (or pass --skip-digest)")
    print(f"plan: trash {len(trash)}, keep {len(keep)}")
    if args.dry_run:
        for i in trash:
            print(f"  would trash [{i['account']}] {i['label']}: {i['from']} — {i['subject']}")
        return 0
    if not (args.yes or cfg["apply"]["auto_trash"]):
        print("confirmation required: show the plan to the user, then rerun with --yes")
        return 3
    moved, skipped, failed = [], [], []
    trash_of = {a["name"]: a["trash"] for a in cfg["accounts"]}
    for acc in sorted({i["account"] for i in trash}):
        live = inbox_ids(cfg, acc)
        ok = []
        for i in (x for x in trash if x["account"] == acc):
            (ok if live.get(i["id"]) == i["message_id"] else skipped).append(i)  # id must still be the same mail
        move_to_trash(cfg, acc, trash_of[acc], ok, moved, failed)
    queue = load_kept(cfg)
    have = {(k["account"], k["message_id"]) for k in queue}
    for i in keep:
        if (i["account"], i["message_id"]) not in have:
            queue.append({k: i[k] for k in ("account", "id", "message_id", "from", "subject", "date")} |
                         {"recorded": run["id"]})
    save_kept(cfg, queue)
    now = dt.datetime.now().isoformat(timespec="seconds")
    append_jsonl(os.path.join(cfg["state_dir"], "actions.jsonl"),
                 [{"at": now, "run": run["id"], "action": "trash", "ref": i["ref"], "label": i["label"]} for i in moved]
                 + [{"at": now, "run": run["id"], "action": "skip", "ref": i["ref"]} for i in skipped])
    run["applied"] = {"at": now, "moved": len(moved), "skipped": len(skipped), "failed": len(failed)}
    save_run(cfg, run)
    print(f"moved {len(moved)}, skipped {len(skipped)} (id changed or gone), failed {len(failed)}, "
          f"kept queue {len(queue)}")
    for c, err in failed:
        print(f"  failed [{c['account']}] {c['subject']}: {err}")
    return 1 if failed else 0


def cmd_report(cfg, args):
    run = load_run(cfg, args.run)
    items = run["items"]
    n_trash = sum(1 for i in items if i["label"] in TRASH_LABELS)
    keep = [i for i in items if i["label"] in ("keep", None)]
    per_acc = " / ".join(f"{a['name']} {sum(1 for i in items if i['account'] == a['name'])}" for a in cfg["accounts"])
    head = t(cfg, "head", n=len(items), t=n_trash, k=len(keep)) + ("" if run.get("applied") else t(cfg, "pending"))
    lines = [f"{head} ({per_acc})", "", t(cfg, "attention")]
    lines += [f"- [{i['account']}] {i['from']} — {i['subject']} ({i['date'][:10]})"
              for i in keep[:cfg["report"]["max_keep_items"]]] or [t(cfg, "none")]
    if len(keep) > cfg["report"]["max_keep_items"]:
        lines.append(f"- … +{len(keep) - cfg['report']['max_keep_items']}")
    dig = run.get("digest") or {}
    if dig.get("text"):
        lines += ["", dig["text"]]
        if dig.get("others"):
            lines.append(t(cfg, "others", x=", ".join(dig["others"])))
    elif any(i["label"] == "newsletter" for i in items):
        lines += ["", titles_section(cfg, [i for i in items if i["label"] == "newsletter"])]
    notes = []
    if run["errors"]:
        notes.append(t(cfg, "errors", x="; ".join(f"{k}: {v}" for k, v in run["errors"].items())))
    if dig.get("error"):
        notes.append(t(cfg, "errors", x=f"digest: {dig['error']}"))
    if (run.get("applied") or {}).get("failed"):
        notes.append(t(cfg, "errors", x=f"move failed {run['applied']['failed']}"))
    lines += [""] + notes + [t(cfg, "note")]
    print("\n".join(lines).strip())
    return 0


def cmd_run(cfg, args):
    if (args.classify_backend or cfg["classify"]["backend"]) == "agent":
        raise BriefError("`run` needs a non-agent classify backend (hermes or typesafe); follow SKILL.md steps "
                         "for the agent backend")
    cmd_scan(cfg, args)
    a = argparse.Namespace(run=None, backend=args.classify_backend)
    cmd_classify(cfg, a)
    dig_backend = args.digest_backend or cfg["digest"]["backend"]
    if dig_backend == "agent":
        dig_backend = "none"
    cmd_digest(cfg, argparse.Namespace(run=None, backend=dig_backend, save=None))
    if args.yes or cfg["apply"]["auto_trash"]:
        cmd_apply(cfg, argparse.Namespace(run=None, yes=True, dry_run=False, skip_digest=False))
    return cmd_report(cfg, argparse.Namespace(run=None))


def cmd_kept(cfg, args):
    items = load_kept(cfg)
    if args.action == "list":
        if not items:
            print("kept queue is empty")
        for n, i in enumerate(items, 1):
            print(f"{n}. [{i['account']}] {i['from']} — {i['subject']} ({i['date'][:10]})")
        return 0
    nums = {int(x) - 1 for x in (args.nums or "").replace(" ", "").split(",") if x.isdigit()}
    if args.action == "drop":
        save_kept(cfg, [i for n, i in enumerate(items) if n not in nums])
        print(f"dropped {len(nums & set(range(len(items))))}")
        return 0
    keep = set(nums)
    for m in args.keep_match:
        pat = re.compile(rf"(?<![a-z0-9]){re.escape(m.lower())}(?![a-z0-9])") if m.isascii() else None
        for n, i in enumerate(items):
            text = f"{i['from']} {i['subject']}".lower()
            if pat.search(text) if pat else m.lower() in text:
                keep.add(n)
    todo = [i for n, i in enumerate(items) if n not in keep]
    stay = [i for n, i in enumerate(items) if n in keep]
    print(f"plan: trash {len(todo)}, keep {len(stay)}")
    if args.dry_run or not args.yes:
        for i in todo:
            print(f"  would trash [{i['account']}] {i['from']} — {i['subject']}")
        if not args.dry_run:
            print("confirmation required: rerun with --yes")
        return 0 if args.dry_run else 3
    moved, skipped, failed = [], [], []
    trash_of = {a["name"]: a["trash"] for a in cfg["accounts"]}
    for acc in sorted({i["account"] for i in todo}):
        live = inbox_ids(cfg, acc)
        ok = []
        for i in (x for x in todo if x["account"] == acc):
            (ok if live.get(i["id"]) == i["message_id"] else skipped).append(i)
        move_to_trash(cfg, acc, trash_of.get(acc, "trash"), ok, moved, failed)
    done = {id(x) for x in moved + skipped + stay}
    save_kept(cfg, [i for i in items if id(i) not in done])
    print(f"moved {len(moved)}, left in inbox {len(stay)}, skipped {len(skipped)}, failed {len(failed)}")
    return 1 if failed else 0


def cmd_doctor(cfg, args):
    ok = True
    print(f"config  {cfg['_path']}")
    exe = _argv(cfg["himalaya"])[0]
    print(f"himalaya {'ok' if shutil.which(exe) or os.path.isfile(exe) else 'NOT FOUND'}  {exe}")
    for acc in cfg["accounts"]:
        try:
            hm(cfg, acc["name"], "envelope", "list", "--page-size", "1", timeout=90)
            print(f"account ok       {acc['name']} (trash -> {acc['trash']})")
        except (BriefError, subprocess.TimeoutExpired) as exc:
            ok = False
            print(f"account FAILED   {acc['name']}: {exc}")
    for section in ("classify", "digest"):
        backend = cfg[section]["backend"]
        line = f"{section:8} backend {backend}"
        if backend == "hermes":
            line += " (hermes " + ("ok" if shutil.which(_argv(cfg["hermes"])[0]) else "NOT FOUND") + ")"
        if section == "classify" and backend == "typesafe":
            try:
                secret_store.resolve(cfg["classify"]["typesafe_key"])
                line += " (key found)"
            except secret_store.SecretError as exc:
                ok = False
                line += f" ({exc})"
        print(line)
    return 0 if ok else 1


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    p = sub.add_parser("classify")
    p.add_argument("--backend", choices=["agent", "hermes", "typesafe"])
    p.add_argument("--run")
    p = sub.add_parser("label")
    p.add_argument("file", help="JSON {ref: label} or [{ref, label}], '-' for stdin")
    p.add_argument("--run")
    p = sub.add_parser("digest")
    p.add_argument("--backend", choices=["agent", "hermes", "none"])
    p.add_argument("--save", help="store an agent-written digest from FILE ('-' for stdin)")
    p.add_argument("--run")
    p = sub.add_parser("apply")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-digest", action="store_true")
    p.add_argument("--run")
    p = sub.add_parser("report")
    p.add_argument("--run")
    p = sub.add_parser("run")
    p.add_argument("--classify-backend", choices=["hermes", "typesafe"])
    p.add_argument("--digest-backend", choices=["hermes", "none"])
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("kept")
    p.add_argument("action", choices=["list", "trash", "drop"])
    p.add_argument("nums", nargs="?", help="list numbers: to keep (trash) or to forget (drop), e.g. 2,5")
    p.add_argument("--keep-match", action="append", default=[])
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("doctor")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        return globals()[f"cmd_{args.cmd}"](cfg, args)
    except (BriefError, secret_store.SecretError) as exc:
        print(f"inbox-brief: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
