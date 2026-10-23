"""Minimal stand-in for the himalaya CLI used by the tests.

State lives in the JSON file named by $FAKE_HIMALAYA_DB:
  {"<account>": {"inbox": [envelope, ...], "bodies": {"<id>": "<html>"}, "moved": []}}
Supported: envelope search|list (--page/--page-size), message read <id>, message move --to <box> <ids...>.
$FAKE_HIMALAYA_REJECT_BATCH=1 makes a multi-id move fail, like Gmail sometimes does.
"""
import json
import os
import sys


def main(argv):
    db_path = os.environ["FAKE_HIMALAYA_DB"]
    db = json.load(open(db_path, encoding="utf-8"))
    args = list(argv)
    acct = args[args.index("-a") + 1]
    args = [a for i, a in enumerate(args) if a not in ("-a", "--json") and (i == 0 or args[i - 1] != "-a")]
    box = db[acct]
    if args[:1] == ["envelope"]:
        size = int(args[args.index("--page-size") + 1]) if "--page-size" in args else 200
        page = int(args[args.index("--page") + 1]) if "--page" in args else 1
        envs = box["inbox"]
        if args[1] == "search":
            envs = [e for e in envs if "Seen" not in e.get("flags", [])]
        print(json.dumps({"envelopes": envs[(page - 1) * size: page * size]}))
        return 0
    if args[:2] == ["message", "read"]:
        mid = args[2]
        if mid not in box["bodies"]:
            print(f"message {mid} not found", file=sys.stderr)
            return 1
        print(box["bodies"][mid])
        return 0
    if args[:2] == ["message", "move"]:
        ids = args[4:]
        if len(ids) > 1 and os.environ.get("FAKE_HIMALAYA_REJECT_BATCH") == "1":
            print("NO System Error", file=sys.stderr)
            return 1
        keep = [e for e in box["inbox"] if e["id"] not in ids]
        box["moved"] += [{"id": e["id"], "to": args[3]} for e in box["inbox"] if e["id"] in ids]
        box["inbox"] = keep
        json.dump(db, open(db_path, "w", encoding="utf-8"))
        return 0
    print(f"unsupported: {args}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
