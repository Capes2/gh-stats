#!/usr/bin/env python3
"""Gold Heads Community Data — dashboard bake.

Reads validated tournament exports (community/corpus/ plus, optionally, Nick's
own Input/ exports), aggregates per-card stats per tournament format, and bakes
a single self-contained site/index.html with one tab per format.

Design notes:
  * stdlib only — runs anywhere python3 runs.
  * Per-file parse results are cached in community/cache/<sha>.json keyed by
    file content hash, so re-bakes only parse NEW files.
  * Published stats are descriptive only (observed results). No projections,
    valuations, or pricing.

Usage:
    python3 bake_community.py                  # bake corpus/ only
    python3 bake_community.py --input ../Input # also fold in local Input exports
    python3 bake_community.py --only-format earlygold   # parse one format (chunked runs)
    python3 bake_community.py --render-only    # skip parsing, render from cache
"""
import argparse, csv, glob, hashlib, html, json, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS_DIR = os.path.join(HERE, "corpus")
CACHE_DIR = os.path.join(HERE, "cache")
SITE_DIR = os.path.join(HERE, "site")
SEED_DIR = os.path.join(HERE, "seeds")
MANIFEST = os.path.join(HERE, "manifest.csv")

LEGACY_RE = re.compile(r"^([a-z0-9]+)_(\d+)(?:_tourn_export)?\.csv$")
COMM_RE = re.compile(r"^([a-z0-9]+)_(\d{4}-\d{2}-\d{2})_([a-z0-9\-]+?)(?:_([a-z0-9\-]+))?\.csv$")

TOP_N = 250          # rows rendered per table (stated in the UI, not silent)
MIN_PA_DEFAULT = 30  # default UI filter values
MIN_IP_DEFAULT = 10

# legacy filename typos folded into their real format
FMT_ALIAS = {"goldheart": "goldenheart", "livegold": "livegolddaily",
             "sundayhifgc": "goldceiling"}

FMT_LABELS = {
    "earlygold": "Early Gold", "livegolddaily": "Live Gold Daily",
    "goldenheart": "Golden Heart", "lgretro": "LG Retro",
    "goldenchild": "Golden Child", "goldslots": "Gold Slots",
    "goldcap": "Gold Cap", "goldquick": "Gold Quick",
    "goldrush": "Gold Rush", "goldceiling": "Gold Ceiling",
    "sandlot": "Sporer's Sandlot", "goldfather": "GoldFather",
}

BAT_KEYS = ["G", "PA", "AB", "H", "2B", "3B", "HR", "R", "RBI", "BB", "HP",
            "SF", "K", "SB", "CS", "wRAA", "WAR"]
PIT_KEYS = ["G", "GS", "W", "L", "IP3", "BF", "oAB", "oH", "oHR", "ER", "oBB",
            "oK", "WAR"]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def ip_to_thirds(v):
    """OOTP IP: '123.2' = 123 innings + 2 thirds."""
    try:
        if "." in v:
            whole, frac = v.split(".", 1)
            return int(whole) * 3 + int(frac[0])
        return int(v) * 3
    except (ValueError, IndexError):
        return 0


def parse_export(path):
    """Extract per-card batting/pitching count stats from one export."""
    def gi(r, k):
        try:
            return int(r.get(k) or 0)
        except ValueError:
            return 0

    def gf(r, k):
        try:
            return float(r.get(k) or 0)
        except ValueError:
            return 0.0

    bat, pit = {}, {}
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            cid = r.get("CID") or ""
            title = r.get("Title") or r.get("Name") or "?"
            key = f"{cid}|{title}"
            name = r.get("Name") or "?"
            tier = r.get("Tier") or ""
            year = r.get("CYear") or ""
            val = r.get("VAL") or ""
            pos = r.get("POS") or ""
            if gi(r, "PA") > 0:
                d = bat.setdefault(key, {"name": name, "tier": tier, "year": year,
                                         "val": val, "pos": pos,
                                         **{k: 0 for k in BAT_KEYS}})
                d["G"] += gi(r, "G"); d["PA"] += gi(r, "PA"); d["AB"] += gi(r, "AB")
                d["H"] += gi(r, "H"); d["2B"] += gi(r, "2B_1"); d["3B"] += gi(r, "3B_1")
                d["HR"] += gi(r, "HR"); d["R"] += gi(r, "R"); d["RBI"] += gi(r, "RBI")
                d["BB"] += gi(r, "BB"); d["HP"] += gi(r, "HP"); d["SF"] += gi(r, "SF")
                d["K"] += gi(r, "K"); d["SB"] += gi(r, "SB"); d["CS"] += gi(r, "CS")
                d["wRAA"] += gf(r, "wRAA"); d["WAR"] += gf(r, "WAR")
            if ip_to_thirds(r.get("IP") or "0") > 0:
                d = pit.setdefault(key, {"name": name, "tier": tier, "year": year,
                                         "val": val, "pos": pos,
                                         **{k: 0 for k in PIT_KEYS}})
                d["G"] += gi(r, "G_1"); d["GS"] += gi(r, "GS_1")
                d["W"] += gi(r, "W"); d["L"] += gi(r, "L")
                d["IP3"] += ip_to_thirds(r.get("IP") or "0"); d["BF"] += gi(r, "BF")
                d["oAB"] += gi(r, "AB_1")
                d["oH"] += gi(r, "1B_2") + gi(r, "2B_2") + gi(r, "3B_2") + gi(r, "HR_1")
                d["oHR"] += gi(r, "HR_1"); d["ER"] += gi(r, "ER")
                d["oBB"] += gi(r, "BB_1"); d["oK"] += gi(r, "K_1")
                d["WAR"] += gf(r, "WAR_1")
    return {"bat": bat, "pit": pit}


def collect_files(input_dir, only_format):
    """Yield (format, path, source) for corpus + optional Input files."""
    out = []
    if os.path.isdir(CORPUS_DIR):
        for fmt in sorted(os.listdir(CORPUS_DIR)):
            fdir = os.path.join(CORPUS_DIR, fmt)
            if not os.path.isdir(fdir):
                continue
            for p in sorted(glob.glob(os.path.join(fdir, "*.csv"))):
                out.append((fmt, p, "community"))
    if input_dir:
        for p in sorted(glob.glob(os.path.join(input_dir, "*.csv"))):
            m = LEGACY_RE.match(os.path.basename(p).lower()) or \
                COMM_RE.match(os.path.basename(p).lower())
            if m:
                out.append((m.group(1), p, "nick"))
    if only_format:
        out = [x for x in out if x[0] == only_format]
    return out


def load_or_parse(fmt, path, stats):
    sha = sha256(path)
    cpath = os.path.join(CACHE_DIR, sha + ".json")
    if os.path.exists(cpath):
        with open(cpath) as f:
            data = json.load(f)
        stats["cached"] += 1
    else:
        data = parse_export(path)
        data["_meta"] = {"file": os.path.basename(path), "format": fmt}
        with open(cpath, "w") as f:
            json.dump(data, f)
        stats["parsed"] += 1
    return sha, data


def merge(agg, data):
    for side in ("bat", "pit"):
        keys = BAT_KEYS if side == "bat" else PIT_KEYS
        dst = agg[side]
        for key, row in data[side].items():
            if key not in dst:
                dst[key] = dict(row)
            else:
                d = dst[key]
                for k in keys:
                    d[k] += row[k]
    return agg


def finalize_format(agg, n_files, managers):
    """Compute rate stats and build render-ready row lists."""
    bat_rows = []
    for d in agg["bat"].values():
        ab, pa = d["AB"], d["PA"]
        if pa == 0:
            continue
        obp_den = ab + d["BB"] + d["HP"] + d["SF"]
        singles = d["H"] - d["2B"] - d["3B"] - d["HR"]
        tb = singles + 2 * d["2B"] + 3 * d["3B"] + 4 * d["HR"]
        bat_rows.append({
            "n": d["name"], "t": d["tier"], "y": d["year"], "v": d["val"], "p": d["pos"],
            "G": d["G"], "PA": pa,
            "AVG": round(d["H"] / ab, 3) if ab else 0,
            "OBP": round((d["H"] + d["BB"] + d["HP"]) / obp_den, 3) if obp_den else 0,
            "SLG": round(tb / ab, 3) if ab else 0,
            "HR": d["HR"], "R": d["R"], "RBI": d["RBI"], "SB": d["SB"],
            "BBp": round(100 * d["BB"] / pa, 1), "Kp": round(100 * d["K"] / pa, 1),
            "wRAA": round(d["wRAA"], 1), "WAR": round(d["WAR"], 1),
        })
    for r in bat_rows:
        r["OPS"] = round(r["OBP"] + r["SLG"], 3)

    # league pitching baseline for a per-format FIP constant
    lg_ip3 = sum(d["IP3"] for d in agg["pit"].values())
    lg_er = sum(d["ER"] for d in agg["pit"].values())
    lg_hr = sum(d["oHR"] for d in agg["pit"].values())
    lg_bb = sum(d["oBB"] for d in agg["pit"].values())
    lg_k = sum(d["oK"] for d in agg["pit"].values())
    lg_ip = lg_ip3 / 3 or 1
    lg_era = 9 * lg_er / lg_ip
    fip_c = lg_era - (13 * lg_hr + 3 * lg_bb - 2 * lg_k) / lg_ip

    pit_rows = []
    for d in agg["pit"].values():
        ip = d["IP3"] / 3
        if ip == 0:
            continue
        pit_rows.append({
            "n": d["name"], "t": d["tier"], "y": d["year"], "v": d["val"], "p": d["pos"],
            "G": d["G"], "GS": d["GS"], "W": d["W"], "L": d["L"],
            "IP": round(ip, 1),
            "ERA": round(9 * d["ER"] / ip, 2),
            "WHIP": round((d["oBB"] + d["oH"]) / ip, 2),
            "K9": round(9 * d["oK"] / ip, 1), "BB9": round(9 * d["oBB"] / ip, 1),
            "HR9": round(9 * d["oHR"] / ip, 2),
            "oAVG": round(d["oH"] / d["oAB"], 3) if d["oAB"] else 0,
            "FIP": round((13 * d["oHR"] + 3 * d["oBB"] - 2 * d["oK"]) / ip + fip_c, 2),
            "WAR": round(d["WAR"], 1),
        })
    bat_rows.sort(key=lambda r: -r["PA"])
    pit_rows.sort(key=lambda r: -r["IP"])
    return {"bat": bat_rows[:TOP_N * 4], "pit": pit_rows[:TOP_N * 4],
            "files": n_files, "managers": sorted(managers),
            "bat_total": len(bat_rows), "pit_total": len(pit_rows),
            "fip_c": round(fip_c, 2)}


def render(formats, out_path):
    order = sorted(formats, key=lambda f: -formats[f]["files"])
    payload = {
        "baked": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "formats": {f: formats[f] for f in order},
        "labels": {f: FMT_LABELS.get(f, f.title()) for f in order},
        "order": order, "topN": TOP_N,
        "minPA": MIN_PA_DEFAULT, "minIP": MIN_IP_DEFAULT,
    }
    tpl = HTML_TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(tpl)
    size = os.path.getsize(out_path) / 1e6
    print(f"Baked {out_path} ({size:.1f} MB, {len(order)} format tabs)")
    if size > 15:
        print("WARNING: page is large — consider lowering TOP_N")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gold Heads — Community Tournament Stats</title>
<style>
:root{--bg:#141210;--panel:#1d1a16;--panel2:#242019;--gold:#d4af37;--gold2:#f0d78c;
--tx:#e8e2d4;--dim:#9a917e;--line:#38321f;--accent:#c9a227}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:14px/1.45 -apple-system,'Segoe UI',Roboto,sans-serif}
header{padding:18px 22px 10px;border-bottom:1px solid var(--line);
background:linear-gradient(180deg,#1c1815,#141210)}
h1{font-size:20px;color:var(--gold2);letter-spacing:.5px}
h1 span{color:var(--dim);font-weight:400;font-size:14px;margin-left:10px}
.meta{color:var(--dim);font-size:12px;margin-top:4px}
nav{display:flex;flex-wrap:wrap;gap:6px;padding:12px 22px;border-bottom:1px solid var(--line)}
nav button{background:var(--panel);color:var(--tx);border:1px solid var(--line);
border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px}
nav button.on{background:var(--gold);color:#191510;font-weight:600;border-color:var(--gold)}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:12px 22px}
.bar input[type=text]{background:var(--panel);border:1px solid var(--line);color:var(--tx);
border-radius:6px;padding:7px 10px;width:220px}
.bar label{color:var(--dim);font-size:12px}
.bar input[type=number]{width:70px;background:var(--panel);border:1px solid var(--line);
color:var(--tx);border-radius:6px;padding:6px}
.toggle button{background:var(--panel);color:var(--tx);border:1px solid var(--line);
padding:6px 14px;cursor:pointer}
.toggle button:first-child{border-radius:6px 0 0 6px}
.toggle button:last-child{border-radius:0 6px 6px 0}
.toggle button.on{background:var(--accent);color:#191510;font-weight:600}
.wrap{padding:0 22px 30px;overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:900px}
th,td{padding:6px 9px;text-align:right;white-space:nowrap}
th{position:sticky;top:0;background:var(--gold);color:#191510;font-size:12px;
cursor:pointer;user-select:none}
th:first-child,td:first-child{text-align:left}
th:nth-child(2),td:nth-child(2){text-align:left}
tbody tr:nth-child(odd){background:var(--panel)}
tbody tr:hover{background:var(--panel2)}
td.nm{color:var(--gold2);font-weight:600}
td .chip{color:var(--dim);font-size:11px;margin-left:6px}
.note{color:var(--dim);font-size:12px;padding:8px 22px}
footer{color:var(--dim);font-size:12px;padding:14px 22px;border-top:1px solid var(--line)}
</style></head><body>
<header><h1>GOLD HEADS <span>Community Tournament Stats</span></h1>
<div class="meta" id="meta"></div></header>
<nav id="tabs"></nav>
<div class="bar">
  <div class="toggle"><button id="bBat" class="on">Batting</button><button id="bPit">Pitching</button></div>
  <input type="text" id="q" placeholder="Search player...">
  <label id="minLab">Min PA <input type="number" id="minV" value="30"></label>
  <span class="note" id="capNote"></span>
</div>
<div class="wrap"><table id="tbl"><thead></thead><tbody></tbody></table></div>
<footer>Gold Heads Community Data Project · observed tournament results only ·
contribute your completed-tournament exports to keep this growing</footer>
<script>
const D=__DATA__;
let fmt=D.order[0],side='bat',sortK=null,sortDir=-1;
const BATCOLS=[['n','Player'],['card','Card'],['G','G'],['PA','PA'],['AVG','AVG'],['OBP','OBP'],['SLG','SLG'],['OPS','OPS'],['HR','HR'],['R','R'],['RBI','RBI'],['SB','SB'],['BBp','BB%'],['Kp','K%'],['wRAA','wRAA'],['WAR','WAR']];
const PITCOLS=[['n','Player'],['card','Card'],['G','G'],['GS','GS'],['W','W'],['L','L'],['IP','IP'],['ERA','ERA'],['WHIP','WHIP'],['K9','K/9'],['BB9','BB/9'],['HR9','HR/9'],['oAVG','oAVG'],['FIP','FIP'],['WAR','WAR']];
const ASC=new Set(['ERA','WHIP','BB9','HR9','oAVG','FIP','Kp']);
function tabs(){const n=document.getElementById('tabs');n.innerHTML='';
 D.order.forEach(f=>{const b=document.createElement('button');
 b.textContent=D.labels[f]+' ('+D.formats[f].files+')';
 b.className=f===fmt?'on':'';b.onclick=()=>{fmt=f;sortK=null;draw();tabs();};n.appendChild(b);});}
function meta(){const F=D.formats[fmt];
 document.getElementById('meta').textContent=
 'Baked '+D.baked+' · '+F.files+' tournament exports combined for '+D.labels[fmt]+
 ' · contributors: '+(F.managers.join(', ')||'—');}
function draw(){meta();
 const F=D.formats[fmt],rows=F[side],cols=side==='bat'?BATCOLS:PITCOLS;
 const q=document.getElementById('q').value.toLowerCase();
 const minv=+document.getElementById('minV').value||0;
 document.getElementById('minLab').firstChild.textContent=side==='bat'?'Min PA ':'Min IP ';
 let out=rows.filter(r=>(side==='bat'?r.PA:r.IP)>=minv&&(!q||r.n.toLowerCase().includes(q)));
 const total=out.length;
 let k=sortK||(side==='bat'?'PA':'IP');
 let dir=sortK?sortDir:-1;
 out.sort((a,b)=>{const x=a[k],y=b[k];return (x<y?-1:x>y?1:0)*(-dir);});
 if(ASC.has(k)&&!sortK)out.reverse();
 out=out.slice(0,D.topN);
 document.getElementById('capNote').textContent=
  total>D.topN?('showing top '+D.topN+' of '+total+' (of '+(side==='bat'?F.bat_total:F.pit_total)+' total players)'):
  (total+' players');
 const th=document.querySelector('#tbl thead');
 th.innerHTML='<tr>'+cols.map(c=>'<th data-k="'+c[0]+'">'+c[1]+(sortK===c[0]?(sortDir<0?' ▼':' ▲'):'')+'</th>').join('')+'</tr>';
 th.querySelectorAll('th').forEach(el=>el.onclick=()=>{const kk=el.dataset.k;
  if(kk==='n'||kk==='card')return;
  if(sortK===kk)sortDir*=-1;else{sortK=kk;sortDir=ASC.has(kk)?1:-1;}draw();});
 const tb=document.querySelector('#tbl tbody');
 tb.innerHTML=out.map(r=>'<tr>'+cols.map(c=>{
  if(c[0]==='n')return '<td class="nm">'+esc(r.n)+'</td>';
  if(c[0]==='card')return '<td>'+esc(r.t)+' '+esc(r.v)+'<span class="chip">'+esc(r.p)+' · '+esc(r.y)+'</span></td>';
  let v=r[c[0]];
  if(['AVG','OBP','SLG','OPS','oAVG'].includes(c[0]))v=(''+v.toFixed(3)).replace(/^0\./,'.');
  return '<td>'+v+'</td>';}).join('')+'</tr>').join('');}
function esc(s){return (''+s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
document.getElementById('bBat').onclick=()=>{side='bat';sortK=null;
 document.getElementById('bBat').className='on';document.getElementById('bPit').className='';
 document.getElementById('minV').value=D.minPA;draw();};
document.getElementById('bPit').onclick=()=>{side='pit';sortK=null;
 document.getElementById('bPit').className='on';document.getElementById('bBat').className='';
 document.getElementById('minV').value=D.minIP;draw();};
document.getElementById('q').oninput=draw;
document.getElementById('minV').oninput=draw;
tabs();draw();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="also include local Input/ exports (Nick)")
    ap.add_argument("--only-format", help="parse just one format (chunked runs)")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--export-seed", action="store_true",
                    help="write per-format aggregate seed files to seeds/ "
                         "(small, repo-committable stand-ins for a large local corpus)")
    ap.add_argument("--out", default=os.path.join(SITE_DIR, "index.html"))
    args = ap.parse_args()

    os.makedirs(CACHE_DIR, exist_ok=True)
    stats = {"parsed": 0, "cached": 0}
    per_fmt = {}

    if not args.render_only:
        files = collect_files(args.input, args.only_format)
        print(f"{len(files)} export files to fold in "
              f"({'format=' + args.only_format if args.only_format else 'all formats'})")
        for fmt, path, source in files:
            try:
                load_or_parse(fmt, path, stats)
            except Exception as e:
                print(f"  SKIP {os.path.basename(path)}: {e}")
        print(f"parsed {stats['parsed']} new, {stats['cached']} from cache")
        if args.only_format:
            print("chunk done — run again for other formats, then --render-only")
            return 0

    # render from ALL cache entries
    managers_by_fmt, files_by_fmt, agg_by_fmt = {}, {}, {}
    # manifest gives manager attribution for community files
    mgr_lookup = {}
    if os.path.exists(MANIFEST):
        with open(MANIFEST, newline="") as f:
            for m in csv.DictReader(f):
                mgr_lookup[m["file"]] = m.get("manager", "")
    for cpath in glob.glob(os.path.join(CACHE_DIR, "*.json")):
        with open(cpath) as f:
            data = json.load(f)
        meta = data.get("_meta", {})
        fmt = meta.get("format")
        if not fmt:
            continue
        fmt = FMT_ALIAS.get(fmt, fmt)
        agg = agg_by_fmt.setdefault(fmt, {"bat": {}, "pit": {}})
        merge(agg, data)
        files_by_fmt[fmt] = files_by_fmt.get(fmt, 0) + 1
        mgr = mgr_lookup.get(meta.get("file", ""), "") or "nick"
        managers_by_fmt.setdefault(fmt, set()).add(mgr)

    if args.export_seed:
        # Write mergeable per-format aggregates (raw counts, not rates).
        os.makedirs(SEED_DIR, exist_ok=True)
        for fmt, agg in agg_by_fmt.items():
            seed = {"format": fmt, "files": files_by_fmt[fmt],
                    "managers": sorted(managers_by_fmt[fmt]),
                    "bat": agg["bat"], "pit": agg["pit"]}
            spath = os.path.join(SEED_DIR, fmt + ".json")
            with open(spath, "w") as f:
                json.dump(seed, f, separators=(",", ":"))
            print(f"  seed {fmt}.json: {os.path.getsize(spath)/1e6:.2f} MB "
                  f"({files_by_fmt[fmt]} files)")
        return 0

    # fold in seed files (pre-aggregated corpora, e.g. Nick's historical Input/)
    for spath in glob.glob(os.path.join(SEED_DIR, "*.json")):
        with open(spath) as f:
            seed = json.load(f)
        fmt = FMT_ALIAS.get(seed["format"], seed["format"])
        agg = agg_by_fmt.setdefault(fmt, {"bat": {}, "pit": {}})
        merge(agg, seed)
        files_by_fmt[fmt] = files_by_fmt.get(fmt, 0) + seed.get("files", 0)
        managers_by_fmt.setdefault(fmt, set()).update(seed.get("managers", []))

    if not agg_by_fmt:
        print("No parsed data in cache — run without --render-only first.")
        return 1
    for fmt, agg in agg_by_fmt.items():
        per_fmt[fmt] = finalize_format(agg, files_by_fmt[fmt], managers_by_fmt[fmt])
        print(f"  {fmt}: {files_by_fmt[fmt]} files, "
              f"{per_fmt[fmt]['bat_total']} batters, {per_fmt[fmt]['pit_total']} pitchers")
    render(per_fmt, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
