#!/usr/bin/env python3
"""Gold Heads Community Data — drop validator.

Validates contributed OOTP tournament stat exports, quarantines bad files
loudly, and files good ones into the corpus with a manifest row.

Naming convention (community contributions):
    <format>_<YYYY-MM-DD>_<manager>[_<park-or-note>].csv
    e.g. earlygold_2026-08-25_awbees.csv
         goldslots_2026-08-24_hellboy_1965-astrodome.csv

Legacy files (Nick's Input/ corpus): <format>_<number>.csv are accepted with
manager="nick" and date taken from file mtime.

Usage:
    python3 validate_drop.py                      # validate community/drops/
    python3 validate_drop.py --drop /path/to/dir  # validate another folder
    python3 validate_drop.py --dry-run            # report only, move nothing
"""
import argparse, csv, difflib, hashlib, json, os, re, shutil, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
DROP_DIR = os.path.join(HERE, "drops")
CORPUS_DIR = os.path.join(HERE, "corpus")
QUAR_DIR = os.path.join(HERE, "quarantine")
MANIFEST = os.path.join(HERE, "manifest.csv")

# Formats we know about today. New formats are ACCEPTED (new tournaments are
# expected) but flagged so typos get noticed.
KNOWN_FORMATS = [
    "earlygold", "livegolddaily", "goldenheart", "lgretro", "goldenchild",
    "goldslots", "goldcap", "goldquick", "goldrush", "goldceiling",
    "sandlot", "goldfather",
]

# Columns every real OOTP tournament export has. Header fingerprint.
REQUIRED_COLS = ["POS", "Name", "ORG", "VAL", "CID", "Tier", "Title",
                 "G", "PA", "AB", "H", "HR", "BB", "K", "IP", "BF", "ER"]

MANIFEST_FIELDS = ["file", "format", "date", "manager", "note", "sha", "sig",
                   "rows", "teams", "league_pa", "league_ops", "added_utc", "source"]

NAME_RE = re.compile(r"^([a-z0-9]+)_(\d{4}-\d{2}-\d{2})_([a-z0-9\-]+?)(?:_([a-z0-9\-]+))?\.csv$")
LEGACY_RE = re.compile(r"^([a-z0-9]+)_(\d+)(?:_tourn_export)?\.csv$")


def load_manifest():
    rows = []
    if os.path.exists(MANIFEST):
        with open(MANIFEST, newline="") as f:
            rows = list(csv.DictReader(f))
    return rows


def save_manifest(rows):
    with open(MANIFEST, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in MANIFEST_FIELDS})


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def signature(rows):
    """Near-duplicate signature: the top-20 (CID, PA) pairs. Two files of the
    same tournament instance (e.g. a mid-event snapshot plus the final export)
    share this even if the files differ byte-wise."""
    pairs = sorted(((r.get("CID", ""), r.get("PA", "0")) for r in rows),
                   key=lambda p: -int(p[1] or 0))[:20]
    return hashlib.sha256(json.dumps(pairs).encode()).hexdigest()[:16]


def parse_name(fname):
    m = NAME_RE.match(fname)
    if m:
        return {"format": m.group(1), "date": m.group(2),
                "manager": m.group(3), "note": m.group(4) or ""}
    m = LEGACY_RE.match(fname)
    if m:
        return {"format": m.group(1), "date": "", "manager": "nick", "note": "",
                "legacy": True}
    return None


def check_file(path, meta, manifest, seen_sigs):
    """Returns (status, reasons, info). status: ok | warn | fail"""
    reasons, warns = [], []
    info = {}
    # --- header / schema ---
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            missing = [c for c in REQUIRED_COLS if c not in header]
            if missing:
                reasons.append(f"not an OOTP tournament export — missing columns: {missing[:6]}")
                return "fail", reasons, info
            rows = list(reader)
    except Exception as e:
        return "fail", [f"unreadable CSV: {e}"], info

    n = len(rows)
    info["rows"] = n
    if n < 150:
        reasons.append(f"only {n} rows — expected a full tournament export (150+)")
    if n > 8000:
        reasons.append(f"{n} rows — larger than any known tournament export")

    # --- completed-tournament sanity ---
    try:
        league_pa = sum(int(r.get("PA") or 0) for r in rows)
    except ValueError:
        league_pa = 0
    info["league_pa"] = league_pa
    if league_pa < 3000:
        reasons.append(f"total PA is {league_pa} — looks like an in-progress or empty tournament; export after it completes")

    teams = len({r.get("ORG", "") for r in rows if r.get("ORG")})
    info["teams"] = teams
    if teams < 4:
        warns.append(f"only {teams} teams found — is this a tournament export?")

    # --- league environment plausibility (the AW mislabel guard) ---
    def s(col):
        t = 0
        for r in rows:
            v = r.get(col) or "0"
            try:
                t += int(v)
            except ValueError:
                pass
        return t
    AB, H, BB, HP, SF = s("AB"), s("H"), s("BB"), s("HP"), s("SF")
    HR, D2, D3 = s("HR"), s("2B_1"), s("3B_1")
    ops = 0.0
    if AB > 0:
        obp_den = AB + BB + HP + SF
        obp = (H + BB + HP) / obp_den if obp_den else 0
        slg = (H + D2 + 2 * D3 + 3 * HR) / AB
        ops = obp + slg
    info["league_ops"] = round(ops, 3)
    if ops and not (0.500 <= ops <= 1.100):
        warns.append(f"league OPS {ops:.3f} is outside any plausible tournament environment — check the file")
    # compare against other files claiming the same format
    fmt_ops = [float(m["league_ops"]) for m in manifest
               if m.get("format") == meta["format"] and m.get("league_ops")]
    if len(fmt_ops) >= 5 and ops:
        mean = sum(fmt_ops) / len(fmt_ops)
        sd = (sum((x - mean) ** 2 for x in fmt_ops) / len(fmt_ops)) ** 0.5
        if sd > 0 and abs(ops - mean) / sd > 3.0:
            warns.append(f"league OPS {ops:.3f} is far from this format's average "
                         f"({mean:.3f} ± {sd:.3f}) — possible MISLABELED tournament "
                         f"(wrong format in the filename)")

    # --- duplicates ---
    sha = sha256(path)
    info["sha"] = sha
    if any(m.get("sha") == sha for m in manifest):
        reasons.append("exact duplicate of a file already in the corpus")
    sig = signature(rows)
    info["sig"] = sig
    prior = seen_sigs.get(sig)
    if prior and not reasons:
        reasons.append(f"same tournament instance as '{prior}' (snapshot of an "
                       f"already-submitted event — stats would double-count)")
    seen_sigs[sig] = os.path.basename(path)

    # --- format name check ---
    fmt = meta["format"]
    if fmt not in KNOWN_FORMATS:
        close = difflib.get_close_matches(fmt, KNOWN_FORMATS, n=1, cutoff=0.8)
        if close:
            warns.append(f"format '{fmt}' looks like a typo of '{close[0]}' — "
                         f"rename if so, otherwise it becomes a new tab")
        else:
            warns.append(f"new format '{fmt}' — a new dashboard tab will be created")

    if reasons:
        return "fail", reasons + warns, info
    if warns:
        return "warn", warns, info
    return "ok", [], info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", default=DROP_DIR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for d in (args.drop, CORPUS_DIR, QUAR_DIR):
        os.makedirs(d, exist_ok=True)

    manifest = load_manifest()
    # seed near-dup signatures from prior accepted files (stored in manifest)
    seen_sigs = {m["sig"]: m["file"] for m in manifest if m.get("sig")}
    files = sorted(f for f in os.listdir(args.drop) if f.lower().endswith(".csv"))
    if not files:
        print(f"No .csv files in {args.drop} — nothing to do.")
        return 0

    ok = bad = warned = 0
    for fname in files:
        path = os.path.join(args.drop, fname)
        meta = parse_name(fname.lower())
        if meta is None:
            _quarantine(path, ["filename doesn't match the convention "
                               "<format>_<YYYY-MM-DD>_<manager>.csv"], args.dry_run)
            bad += 1
            continue
        if meta.get("legacy") and not meta["date"]:
            meta["date"] = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path)))
        status, reasons, info = check_file(path, meta, manifest, seen_sigs)
        if status == "fail":
            _quarantine(path, reasons, args.dry_run)
            bad += 1
            continue
        if status == "warn":
            warned += 1
            print(f"  WARN {fname}:")
            for r in reasons:
                print(f"       - {r}")
        dest_dir = os.path.join(CORPUS_DIR, meta["format"])
        dest = os.path.join(dest_dir, fname.lower())
        if not args.dry_run:
            os.makedirs(dest_dir, exist_ok=True)
            if os.path.abspath(path) != os.path.abspath(dest):
                shutil.move(path, dest)
        manifest.append({
            "file": fname.lower(), "format": meta["format"], "date": meta["date"],
            "manager": meta["manager"], "note": meta.get("note", ""),
            "sha": info.get("sha", ""), "sig": info.get("sig", ""),
            "rows": info.get("rows", ""),
            "teams": info.get("teams", ""), "league_pa": info.get("league_pa", ""),
            "league_ops": info.get("league_ops", ""),
            "added_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "community" if meta["manager"] != "nick" else "nick",
        })
        ok += 1
        print(f"  OK   {fname} -> corpus/{meta['format']}/  "
              f"({info.get('rows','?')} rows, {info.get('teams','?')} teams, "
              f"lgOPS {info.get('league_ops','?')})")

    if not args.dry_run:
        save_manifest(manifest)
    print(f"\n=== {ok} accepted ({warned} with warnings), {bad} quarantined ===")
    if bad:
        print(f"Quarantined files and reasons are in: {QUAR_DIR}")
    return 0 if bad == 0 else 1


def _quarantine(path, reasons, dry):
    fname = os.path.basename(path)
    print(f"  FAIL {fname}:")
    for r in reasons:
        print(f"       - {r}")
    if not dry:
        os.makedirs(QUAR_DIR, exist_ok=True)
        shutil.move(path, os.path.join(QUAR_DIR, fname))
        with open(os.path.join(QUAR_DIR, fname + ".reason.txt"), "w") as f:
            f.write("\n".join(reasons) + "\n")


if __name__ == "__main__":
    sys.exit(main())
