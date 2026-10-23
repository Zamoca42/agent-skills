"""End-to-end tests for inbox_brief.py against a fake himalaya. stdlib + pytest, no network."""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SCRIPTS = os.path.join(ROOT, "skills", "inbox-brief", "scripts")
sys.path.insert(0, SCRIPTS)
import inbox_brief  # noqa: E402
import secret  # noqa: E402

FAKE = os.path.join(HERE, "fake_himalaya.py")


def env(i, name, email, subject, seen=False):
    return {"id": str(i), "message-id": f"<m{i}@example.test>", "flags": ["Seen"] if seen else [],
            "subject": subject, "from": [{"name": name, "email": email}], "date": "2026-01-02T03:04:05Z"}


@pytest.fixture
def box(tmp_path, monkeypatch):
    db = {
        "work": {"inbox": [
            env(1, "Example Weekly", "news@weekly.example", "This week in tools"),
            env(2, "Shop", "deals@shop.example", "(광고) 50% off today"),
            env(3, "Bank", "alerts@bank.example", "Security alert: new sign-in"),
            env(4, "Alice", "alice@example.test", "Lunch tomorrow?"),
            env(5, "Old", "old@example.test", "already read", seen=True),
        ], "bodies": {"1": "<p>Tool X shipped v2 with 3x faster builds.</p><style>x{}</style>"}, "moved": []},
        "home": {"inbox": [env(7, "Receipts", "noreply@store.example", "Your receipt")], "bodies": {}, "moved": []},
    }
    dbp = tmp_path / "db.json"
    dbp.write_text(json.dumps(db), encoding="utf-8")
    monkeypatch.setenv("FAKE_HIMALAYA_DB", str(dbp))
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"""
himalaya = [{json.dumps(sys.executable)}, {json.dumps(FAKE)}]
state_dir = {json.dumps(str(tmp_path / "state"))}
[[accounts]]
name = "work"
[[accounts]]
name = "home"
trash = "Bin"
[rules]
newsletter_senders = ["weekly.example"]
junk_senders = ["noreply@store.example"]
keep_keywords = ["security alert"]
""", encoding="utf-8")
    return {"cfg": str(cfg), "db": dbp, "state": tmp_path / "state", "tmp": tmp_path}


def run(box, *args, stdin=None):
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "inbox_brief.py"), "--config", box["cfg"], *args],
                       capture_output=True, text=True, encoding="utf-8", input=stdin)
    return r.returncode, r.stdout + r.stderr


def db(box):
    return json.loads(box["db"].read_text(encoding="utf-8"))


def latest(box):
    rid = (box["state"] / "latest").read_text().strip()
    return json.loads((box["state"] / "runs" / rid / "run.json").read_text(encoding="utf-8"))


def test_scan_rules_and_agent_flow(box):
    code, out = run(box, "scan")
    assert code == 0 and "5 unread" in out and "1 need classify" in out
    labels = {i["ref"]: (i["label"], i["source"]) for i in latest(box)["items"]}
    assert labels["work:1"] == ("newsletter", "rule:newsletter_senders")
    assert labels["work:2"] == ("promo", "rule:promo_subject_markers")
    assert labels["work:3"] == ("keep", "rule:keep_keywords")
    assert labels["home:7"] == ("junk", "rule:junk_senders")
    assert labels["work:4"] == (None, None)

    code, out = run(box, "classify")  # agent backend prints the task
    assert code == 0 and "work:4 | Alice" in out and "NEXT:" in out
    code, out = run(box, "label", "-", stdin=json.dumps({"work:4": "keep"}))
    assert code == 0 and "keep 2" in out

    code, out = run(box, "digest")
    assert code == 0 and "Digest input:" in out
    text = open(latest(box)["digest"]["input"], encoding="utf-8").read()
    assert "3x faster builds" in text and "<p>" not in text
    code, out = run(box, "digest", "--save", "-", stdin="📰 Newsletters\n- **Example Weekly** — Tool X v2.")
    assert code == 0

    code, out = run(box, "apply")
    assert code == 3 and "confirmation required" in out
    assert len(db(box)["work"]["inbox"]) == 5  # nothing moved without --yes

    code, out = run(box, "apply", "--yes")
    assert code == 0 and "moved 3" in out
    state = db(box)
    assert {m["id"] for m in state["work"]["moved"]} == {"1", "2"}
    assert state["home"]["moved"] == [{"id": "7", "to": "Bin"}]

    code, out = run(box, "report")
    assert "📬 Unread 5 → trash 3 / keep 2" in out and "Security alert" in out and "Tool X v2" in out
    assert "not moved yet" not in out

    code, out = run(box, "kept", "list")
    assert "1. [work] Bank" in out and "2. [work] Alice" in out


def test_apply_refuses_unlabeled_and_undigested(box):
    run(box, "scan")
    code, out = run(box, "apply", "--yes")
    assert code == 1 and "unlabeled" in out
    run(box, "label", "-", stdin='{"work:4": "keep"}')
    code, out = run(box, "apply", "--yes")
    assert code == 1 and "digest first" in out


def test_apply_skips_changed_ids_and_retries_batch(box, monkeypatch):
    run(box, "scan")
    run(box, "label", "-", stdin='{"work:4": "bogus"}')  # unknown label falls back to keep
    assert latest(box)["items"][3]["source"] == "fallback:bogus"
    state = db(box)
    state["work"]["inbox"][1]["message-id"] = "<other@example.test>"  # id 2 now points at another mail
    box["db"].write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("FAKE_HIMALAYA_REJECT_BATCH", "1")
    code, out = run(box, "digest", "--backend", "none")
    assert "Example Weekly (1)" in out
    code, out = run(box, "apply", "--yes")
    assert "skipped 1" in out and "moved 2" in out  # batch rejected, singles succeeded
    assert {m["id"] for m in db(box)["work"]["moved"]} == {"1"}


def test_kept_trash_with_keep_match(box):
    run(box, "scan")
    run(box, "label", "-", stdin='{"work:4": "keep"}')
    run(box, "digest", "--backend", "none")
    run(box, "apply", "--yes")
    code, out = run(box, "kept", "trash", "--keep-match", "alice")
    assert code == 3 and "would trash [work] Bank" in out
    code, out = run(box, "kept", "trash", "--keep-match", "alice", "--yes")
    assert code == 0 and "moved 1, left in inbox 1" in out
    code, out = run(box, "kept", "list")
    assert "empty" in out


def test_report_korean(box):
    with open(box["cfg"], "a", encoding="utf-8") as fh:
        fh.write('\n')
    text = open(box["cfg"], encoding="utf-8").read().replace("state_dir", 'language = "ko"\nstate_dir', 1)
    open(box["cfg"], "w", encoding="utf-8").write(text)
    run(box, "scan")
    code, out = run(box, "report")
    assert "미읽음 5건" in out and "아직 옮기지 않음" in out


def test_parse_label_json_and_rules():
    assert inbox_brief.parse_label_json('ok:\n```json\n{"a:1": "Promo"}\n```') == {"a:1": "promo"}
    cfg = inbox_brief.deep_merge(inbox_brief.DEFAULTS, {"rules": {"keep_senders": ["boss@"]}})
    item = {"from": "Boss", "email": "boss@corp.example", "subject": "(광고) not really"}
    assert inbox_brief.rule_label(cfg, item) == ("keep", "rule:keep_senders")


def test_secret_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_TEST_SECRET", "abcd efgh\n")
    assert secret.resolve(["env:IB_MISSING", "env:IB_TEST_SECRET"]) == "abcdefgh"
    f = tmp_path / "pw.txt"
    f.write_text("wxyz 1234\nsecond line\n", encoding="utf-8")
    assert secret.resolve([f"file:{f}"]) == "wxyz1234"
    with pytest.raises(secret.SecretError) as e:
        secret.resolve(["env:IB_MISSING"])
    assert "env:IB_MISSING" in str(e.value)
    monkeypatch.delenv("INBOX_BRIEF_ALLOW_CMD", raising=False)
    with pytest.raises(secret.SecretError):
        secret.resolve(["cmd:echo hi"])
