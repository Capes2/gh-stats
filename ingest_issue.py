#!/usr/bin/env python3
"""Pull CSV attachments out of a GitHub issue body into drops/.

Runs inside the ingest workflow. The issue body arrives via the ISSUE_BODY
env var (never shell-interpolated — treat as data). Only files attached
through GitHub's own uploader are fetched (github.com/user-attachments/...),
so contributors can't point us at arbitrary hosts. Accepts .csv directly or
.zip containing CSVs. Prints the saved filenames, one per line.
"""
import io, os, re, sys, time, urllib.request, zipfile

try:
    import fmt_detect
except ImportError:
    fmt_detect = None

CONV_RE = re.compile(r"^[a-z0-9]+_\d{4}-\d{2}-\d{2}_[a-z0-9\-]+(_[a-z0-9\-]+)?\.csv$")
LEGACY_RE = re.compile(r"^[a-z0-9]+_\d+(_tourn_export)?\.csv$")

MAX_FILES = 20
MAX_BYTES = 40 * 1024 * 1024  # per attachment

body = os.environ.get("ISSUE_BODY", "") or ""
urls = re.findall(r"https://github\.com/user-attachments/files/\d+/[^\s\)\]\"'<>]+",
                  body)[:MAX_FILES]
os.makedirs("drops", exist_ok=True)
saved = []

hint = (fmt_detect.format_from_text(body)
        if fmt_detect else None)  # tournament name typed in the issue
author = re.sub(r"[^a-z0-9\-]", "", os.environ.get("ISSUE_AUTHOR", "").lower()) or "anon"
today = time.strftime("%Y-%m-%d", time.gmtime())


def put(name, data):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name)).lower()
    if not name.endswith(".csv") or len(data) < 100:
        return
    if not (CONV_RE.match(name) or LEGACY_RE.match(name)):
        fmt, reason = hint, ("named in the issue" if hint else "")
        if not fmt and fmt_detect is not None:
            tmp = os.path.join("drops", ".probe.csv")
            with open(tmp, "wb") as f:
                f.write(data)
            fmt, reason = fmt_detect.detect(tmp)
            os.remove(tmp)
        if not fmt:
            print(f"SKIPPED {name}: couldn't identify the tournament — "
                  f"{reason}. Type the tournament name in the issue text "
                  f"(e.g. 'golden heart') or rename the file.", file=sys.stderr)
            return
        base = f"{fmt}_{today}_{author}"
        newname, n = base + ".csv", 2
        while os.path.exists(os.path.join("drops", newname)):
            newname = f"{base}-{n}.csv"
            n += 1
        print(f"auto-named {name} -> {newname} ({reason})", file=sys.stderr)
        name = newname
    with open(os.path.join("drops", name), "wb") as f:
        f.write(data)
    saved.append(name)

for u in urls:
    try:
        with urllib.request.urlopen(u, timeout=90) as r:
            data = r.read(MAX_BYTES + 1)
    except Exception as e:
        print(f"ERROR downloading {u}: {e}", file=sys.stderr)
        continue
    if len(data) > MAX_BYTES:
        print(f"ERROR: attachment too large (> {MAX_BYTES>>20} MB): {u}", file=sys.stderr)
        continue
    name = u.rsplit("/", 1)[-1]
    if name.lower().endswith(".zip"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
            for zi in zf.infolist():
                if zi.filename.lower().endswith(".csv") and zi.file_size <= MAX_BYTES:
                    put(zi.filename, zf.read(zi))
        except zipfile.BadZipFile:
            print(f"ERROR: bad zip: {name}", file=sys.stderr)
    else:
        put(name, data)

print("\n".join(saved))
