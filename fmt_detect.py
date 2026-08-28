#!/usr/bin/env python3
"""Tournament-format identification for default-named OOTP exports.

Two mechanisms, used by the ingest lanes:
  * format_from_text(text) — fuzzy-match a format name typed in a Discord
    message or issue title/body ("golden heart" -> goldenheart).
  * detect(path) — fingerprint the export (tier mix, league OPS/K%/HR rate)
    against per-format signatures built from seeds/, and return a format only
    when the match is DECISIVE. Sister formats with the same card pool (e.g.
    GoldenChild vs GoldenHeart) are often statistically indistinguishable —
    ambiguity returns None rather than a guess.
"""
import csv, difflib, glob, json, math, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
SEED_DIR = os.path.join(HERE, "seeds")

# fingerprint feature weights (scales chosen so each feature contributes
# comparably); thresholds tuned on the 615-file labeled corpus
W = {"gold": 1, "silver": 1, "sub": 2, "ops": 6, "kpct": 8, "hrpa": 40}
MAX_DIST = 0.20     # best match must be at least this close
MIN_MARGIN = 0.06   # ...and beat the runner-up by at least this much

ALIASES = {
    "earlygold": ["early gold", "earlygold", "eg"],
    "livegolddaily": ["live gold daily", "livegolddaily", "lgd", "live gold"],
    "goldenheart": ["golden heart", "goldenheart", "gh"],
    "lgretro": ["lg retro", "lgretro", "retro"],
    "goldenchild": ["golden child", "goldenchild", "gc"],
    "goldslots": ["gold slots", "goldslots", "slots"],
    "goldcap": ["gold cap", "goldcap"],
    "goldquick": ["gold quick", "goldquick"],
    "goldrush": ["gold rush", "goldrush"],
    "goldceiling": ["gold ceiling", "goldceiling", "ceiling"],
    "sandlot": ["sandlot", "sporer", "sporers sandlot", "sporer's sandlot"],
    "goldfather": ["goldfather", "gold father"],
}


def format_from_text(text):
    """Find a tournament format named in free text. Longest alias wins."""
    if not text:
        return None
    t = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    t = " " + re.sub(r"\s+", " ", t).strip() + " "
    hits = []
    for fmt, names in ALIASES.items():
        for n in names:
            if f" {n} " in t:
                hits.append((len(n), fmt))
    if hits:
        return max(hits)[1]
    # fuzzy single-word fallback (typos like "goldnheart")
    words = t.split()
    flat = {n.replace(" ", ""): f for f, ns in ALIASES.items() for n in ns}
    for w in words:
        if len(w) >= 6:
            m = difflib.get_close_matches(w, list(flat), n=1, cutoff=0.85)
            if m:
                return flat[m[0]]
    return None


def _finish(tiers, tot, AB, H, BB, HP, SF, HR, D2, D3, K, PA):
    if not tot or not AB or not PA:
        return None
    obp_den = AB + BB + HP + SF
    return {"gold": tiers.get("Gold", 0) / tot,
            "silver": tiers.get("Silver", 0) / tot,
            "sub": sum(v for k, v in tiers.items() if k not in ("Gold", "Silver")) / tot,
            "ops": (H + BB + HP) / obp_den + (H + D2 + 2 * D3 + 3 * HR) / AB,
            "kpct": K / PA, "hrpa": HR / PA}


def file_feats(path):
    tiers = {}
    tot = AB = H = BB = HP = SF = HR = D2 = D3 = K = PA = 0
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                def gi(k):
                    try:
                        return int(r.get(k) or 0)
                    except ValueError:
                        return 0
                pa = gi("PA")
                tiers[r.get("Tier", "")] = tiers.get(r.get("Tier", ""), 0) + pa
                tot += pa; PA += pa
                AB += gi("AB"); H += gi("H"); BB += gi("BB"); HP += gi("HP")
                SF += gi("SF"); HR += gi("HR"); D2 += gi("2B_1"); D3 += gi("3B_1")
                K += gi("K")
    except Exception:
        return None
    return _finish(tiers, tot, AB, H, BB, HP, SF, HR, D2, D3, K, PA)


def seed_signatures():
    sigs = {}
    for f in glob.glob(os.path.join(SEED_DIR, "*.json")):
        try:
            s = json.load(open(f))
        except Exception:
            continue
        tiers = {}
        tot = AB = H = BB = HP = SF = HR = D2 = D3 = K = PA = 0
        for d in s.get("bat", {}).values():
            pa = d["PA"]
            tiers[d.get("tier", "")] = tiers.get(d.get("tier", ""), 0) + pa
            tot += pa; PA += pa
            AB += d["AB"]; H += d["H"]; BB += d["BB"]; HP += d["HP"]; SF += d["SF"]
            HR += d["HR"]; D2 += d["2B"]; D3 += d["3B"]; K += d["K"]
        ft = _finish(tiers, tot, AB, H, BB, HP, SF, HR, D2, D3, K, PA)
        if ft:
            sigs[s["format"]] = ft
    return sigs


def detect(path):
    """Returns (format or None, human-readable reason)."""
    tgt = file_feats(path)
    if tgt is None:
        return None, "could not read stats from the file"
    sigs = seed_signatures()
    if not sigs:
        return None, "no format signatures available (seeds/ missing)"
    scored = sorted(
        (math.sqrt(sum(W[k] * (sig[k] - tgt[k]) ** 2 for k in W)), fmt)
        for fmt, sig in sigs.items())
    d1, f1 = scored[0]
    d2, f2 = scored[1] if len(scored) > 1 else (99, "-")
    if d1 <= MAX_DIST and (d2 - d1) >= MIN_MARGIN:
        return f1, f"fingerprint match: {f1} (dist {d1:.3f}, next {f2} {d2:.3f})"
    return None, (f"ambiguous fingerprint: {f1} {d1:.3f} vs {f2} {d2:.3f} — "
                  f"type the tournament name with the file")
