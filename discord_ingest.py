#!/usr/bin/env python3
"""Discord data-drop channel ingestion for the Gold Heads dashboard.

Runs inside the discord.yml workflow on a schedule. Two phases:

  python discord_ingest.py fetch   # pull new CSV/zip attachments -> drops/
  python discord_ingest.py reply   # after validate+bake, post results to the channel

State (last seen message id) lives in discord_state.json, committed to the
repo. First ever run just records the current newest message and ingests
nothing, so old channel history isn't slurped in.

Env: DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID (repo Actions secrets).
"""
import glob, io, json, os, re, sys, urllib.request, zipfile

API = "https://discord.com/api/v10"
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CID = os.environ.get("DISCORD_CHANNEL_ID", "")
STATE = "discord_state.json"
BATCH = "/tmp/discord_batch.json"
MAX_BYTES = 40 * 1024 * 1024
DASH_URL = "https://capes2.github.io/gh-stats/"


def api(path, method="GET", payload=None):
    req = urllib.request.Request(API + path, method=method)
    req.add_header("Authorization", "Bot " + TOKEN)
    req.add_header("User-Agent", "goldheads-stats-bot/1.0")
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=60) as r:
        body = r.read()
    return json.loads(body) if body else {}


def dl(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "goldheads-stats-bot/1.0")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read(MAX_BYTES + 1)


def clean(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name)).lower()


def save_csv(name, data, author, saved):
    name = clean(name)
    if not name.endswith(".csv") or len(data) < 100 or len(data) > MAX_BYTES:
        return
    os.makedirs("drops", exist_ok=True)
    with open(os.path.join("drops", name), "wb") as f:
        f.write(data)
    saved[name] = {"author": author}


def fetch():
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    last = state.get("last_message_id")
    if not last:
        msgs = api(f"/channels/{CID}/messages?limit=1")
        state["last_message_id"] = msgs[0]["id"] if msgs else "0"
        with open(STATE, "w") as f:
            json.dump(state, f)
        with open(BATCH, "w") as f:
            json.dump({}, f)
        print("bootstrap: recorded current position; no history ingested")
        print("count=0")
        return
    saved = {}
    max_id = int(last)
    after = last
    while True:
        msgs = api(f"/channels/{CID}/messages?after={after}&limit=100")
        if not msgs:
            break
        msgs.sort(key=lambda m: int(m["id"]))
        for m in msgs:
            max_id = max(max_id, int(m["id"]))
            author = (m.get("author") or {}).get("username", "?")
            for a in m.get("attachments", []):
                fn = clean(a.get("filename", ""))
                if a.get("size", 0) > MAX_BYTES:
                    continue
                if fn.endswith(".csv"):
                    try:
                        save_csv(fn, dl(a["url"]), author, saved)
                    except Exception as e:
                        print(f"ERROR downloading {fn}: {e}", file=sys.stderr)
                elif fn.endswith(".zip"):
                    try:
                        zf = zipfile.ZipFile(io.BytesIO(dl(a["url"])))
                        for zi in zf.infolist():
                            if zi.filename.lower().endswith(".csv") and zi.file_size <= MAX_BYTES:
                                save_csv(zi.filename, zf.read(zi), author, saved)
                    except Exception as e:
                        print(f"ERROR reading zip {fn}: {e}", file=sys.stderr)
        after = str(max_id)
        if len(msgs) < 100:
            break
    state["last_message_id"] = str(max_id)
    with open(STATE, "w") as f:
        json.dump(state, f)
    with open(BATCH, "w") as f:
        json.dump(saved, f)
    for fn in sorted(saved):
        print(f"  fetched {fn} (from {saved[fn]['author']})")
    print(f"count={len(saved)}")


def reply():
    batch = json.load(open(BATCH)) if os.path.exists(BATCH) else {}
    if not batch:
        return
    lines = []
    for fn, meta in sorted(batch.items()):
        if glob.glob(f"corpus/*/{fn}"):
            status = "accepted ✅"
        elif os.path.exists(f"quarantine/{fn}"):
            reason = "see repo quarantine"
            rpath = f"quarantine/{fn}.reason.txt"
            if os.path.exists(rpath):
                reason = open(rpath).read().strip().splitlines()[0]
            status = f"rejected ❌ — {reason}"
        else:
            status = "not processed ❓"
        lines.append(f"• `{fn}` ({meta.get('author','?')}): {status}")
    content = ("**Gold Heads data bot** — new submissions processed:\n"
               + "\n".join(lines)
               + f"\nDashboard updates in ~2 min: {DASH_URL}")
    api(f"/channels/{CID}/messages", "POST", {"content": content[:1900]})
    print("posted channel reply")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    if not TOKEN or not CID:
        print("DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID not set", file=sys.stderr)
        sys.exit(1)
    fetch() if mode == "fetch" else reply()
