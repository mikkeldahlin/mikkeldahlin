#!/usr/bin/env python3
"""Build the world-map data and map image bundles for the Reborn Field Guide.

Usage (run isolated, inputs are untrusted):

    python3 -I scripts/build_maps.py \
        --db   /tmp/claude-0/src/reborn-db \
        --site /tmp/claude-0/src/reborn-db-site \
        --out  app \
        [--report /path/report.json] [--preview-dir /path/previews] [--jobs 4]

Reads
  <db>/PBS/townmap.txt      Essentials town map points
  <db>/PBS/connections.txt  Essentials map edge connections
  <db>/PBS/metadata.txt     per-map MapPosition (region,x,y)
  <site>/area/<id>.html     area name, parent, "Outdoor map?"
  <site>/img/area/<id>.png  rendered map (32 px tiles)

Writes
  <out>/data/regionmap.json   grid, points, stitched composites (data/SCHEMA.md)
  <out>/data/area_images.json { areaId: {bundle, key, w, h, scale, tiles} }
  <out>/maps/maps-NN.json     { key: data:image/webp;base64,... } per-area images
  <out>/maps/world-NN.json    { key: data:image/webp;base64,... } stitched composites

Connection semantics follow Essentials' MapFactoryHelper.getMapConnections:
each record "A,SideA,offA,B,SideB,offB" names a point on each map's edge
(N/S: (offset, 0|height), E/W: (0|width, offset), in tiles) and those two
points coincide.  So B's origin in A's tile space is pointA - pointB.
"""
import argparse
import base64
import collections
import concurrent.futures as cf
import hashlib
import html
import io
import json
import os
import re
import sys

from PIL import Image

try:
    import numpy as np
except ImportError:  # seam diagnostics are skipped without numpy
    np = None

Image.MAX_IMAGE_PIXELS = 400_000_000  # largest map is ~96 Mpx

TILE = 32
BUNDLE_LIMIT = 7_600_000      # bytes per bundle file (< 8 MB)
COMPOSITE_MAX = 4096          # long side of the biggest composite
SMALL_MAP_TILES = 1500        # maps with <= this many tiles get 50 % instead of 25 %
AREA_LONG_MAX = 2700          # cap long side of a per-area image
WEBP_Q = 70


# ---------------------------------------------------------------- parsing
def read_text(path):
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def parse_csv_fields(s):
    """Split an Essentials PBS CSV value, honouring double quotes."""
    out, cur, q = [], [], False
    for ch in s:
        if ch == '"':
            q = not q
        elif ch == "," and not q:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return out


def parse_townmap(path):
    regions = []
    cur = None
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[(\d+)\]$", line)
        if m:
            cur = {"region": int(m.group(1)), "points": []}
            regions.append(cur)
            continue
        if cur is None or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k == "Name":
            cur["name"] = parse_csv_fields(v)[0]
        elif k == "Filename":
            cur["filename"] = v.strip()
        elif k == "Point":
            f = parse_csv_fields(v) + [""] * 8
            def num(x):
                return int(x) if re.fullmatch(r"-?\d+", x or "") else None
            cur["points"].append({
                "x": num(f[0]), "y": num(f[1]), "name": f[2] or None, "sub": f[3] or None,
                "healMap": num(f[4]), "healX": num(f[5]), "healY": num(f[6]),
                "switch": num(f[7]),
            })
    return regions


def parse_connections(path):
    conns = []
    for lineno, line in enumerate(read_text(path).splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        f = [x.strip() for x in line.split(",")]
        if len(f) < 6:
            continue
        side = {"N": "N", "North": "N", "S": "S", "South": "S",
                "E": "E", "East": "E", "W": "W", "West": "W"}
        try:
            conns.append({"a": str(int(f[0])), "sa": side[f[1]], "oa": int(f[2]),
                          "b": str(int(f[3])), "sb": side[f[4]], "ob": int(f[5]),
                          "line": lineno})
        except (KeyError, ValueError):
            print(f"connections.txt:{lineno}: unparsed: {line}", file=sys.stderr)
    return conns


def parse_metadata(path):
    txt = read_text(path)
    secs = re.split(r"^\[(\d+)\]\s*$", txt, flags=re.M)
    meta = {}
    for i in range(1, len(secs), 2):
        mid = str(int(secs[i]))
        e = {}
        for k, v in re.findall(r"^(\w+)\s*=\s*(.*?)\s*$", secs[i + 1], re.M):
            e[k] = v
        meta[mid] = e
    return meta


def parse_site_areas(site):
    areas = {}
    adir = os.path.join(site, "area")
    for fn in os.listdir(adir):
        m = re.fullmatch(r"(\d+)\.html", fn)
        if not m:
            continue
        aid = m.group(1)
        h = read_text(os.path.join(adir, fn))
        t = re.search(r"<h1>(.*?)</h1>", h, re.S)
        name = html.unescape(t.group(1).strip()) if t else aid
        base = re.sub(r"\s*\(\d+\)$", "", name).strip()
        od = re.search(r"Outdoor map\?</th>\s*<td>\s*(\w+)", h)
        par = re.search(r'Parent area</th>\s*<td>\s*(?:<a href="[^"]*/(\d+)\.html")?', h)
        areas[aid] = {"name": base, "outdoor": bool(od and od.group(1) == "Yes"),
                      "parent": par.group(1) if par and par.group(1) else None}
    return areas


# ---------------------------------------------------------------- images
def to_webp(im, q=WEBP_Q):
    b = io.BytesIO()
    im.save(b, "WEBP", quality=q, method=6, alpha_quality=80)
    return b.getvalue()


def shrink(im, factor):
    """Box-filter downscale by an integer factor, premultiplied alpha."""
    if factor == 1:
        return im.convert("RGBA")
    pm = im.convert("RGBa")
    return pm.reduce(factor).convert("RGBA")


def area_factor(w, h):
    tiles = (w // TILE) * (h // TILE)
    f = 2 if tiles <= SMALL_MAP_TILES else 4
    while max(w, h) / f > AREA_LONG_MAX:
        f *= 2
    return f


def encode_area(args):
    path, aid = args
    with Image.open(path) as im:
        im.load()
        w, h = im.size
        alpha_max = im.getchannel("A").getextrema()[1] if im.mode == "RGBA" else 255
        if alpha_max == 0:
            return aid, w, h, None, None, None
        f = area_factor(w, h)
        s = shrink(im, f)
    data = to_webp(s)
    return aid, w, h, f, s.size, data


def file_hash(path):
    hsh = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hsh.update(chunk)
    return hsh.hexdigest()


# ---------------------------------------------------------------- stitching
def edge_point(side, off, w, h):
    if side == "N":
        return off, 0
    if side == "S":
        return off, h
    if side == "W":
        return 0, off
    return w, off  # E


def build_clusters(conns, dims):
    """Union connected maps; place them in tile space via BFS.

    Returns (clusters, problems). Each cluster: {members: {id: (tx,ty)}, edges: [conn]}.
    """
    problems = []
    adj = collections.defaultdict(list)
    for c in conns:
        if c["a"] not in dims or c["b"] not in dims:
            problems.append(f"line {c['line']}: map without image ({c['a']}->{c['b']}), skipped")
            continue
        wa, ha = dims[c["a"]]
        wb, hb = dims[c["b"]]
        pa = edge_point(c["sa"], c["oa"], wa, ha)
        pb = edge_point(c["sb"], c["ob"], wb, hb)
        d = (pa[0] - pb[0], pa[1] - pb[1])  # B origin relative to A
        opp = {"N": "S", "S": "N", "E": "W", "W": "E"}
        if opp[c["sa"]] != c["sb"]:
            problems.append(f"line {c['line']}: sides {c['sa']}/{c['sb']} not opposite")
        adj[c["a"]].append((c["b"], d, c))
        adj[c["b"]].append((c["a"], (-d[0], -d[1]), c))
    seen = set()
    clusters = []
    for start in sorted(adj, key=int):
        if start in seen:
            continue
        pos = {start: (0, 0)}
        edges = []
        queue = [start]
        seen.add(start)
        while queue:
            u = queue.pop(0)
            for v, d, c in adj[u]:
                want = (pos[u][0] + d[0], pos[u][1] + d[1])
                if c not in edges:
                    edges.append(c)
                if v in pos:
                    if pos[v] != want:
                        problems.append(
                            f"line {c['line']}: inconsistent offset for {v}: {pos[v]} vs {want}")
                    continue
                pos[v] = want
                seen.add(v)
                queue.append(v)
        mx = min(p[0] for p in pos.values())
        my = min(p[1] for p in pos.values())
        pos = {k: (x - mx, y - my) for k, (x, y) in pos.items()}
        clusters.append({"members": pos, "edges": edges})
    # overlap check (tile boxes)
    for cl in clusters:
        ids = sorted(cl["members"], key=int)
        for i, a in enumerate(ids):
            ax, ay = cl["members"][a]
            aw, ah = dims[a]
            for b in ids[i + 1:]:
                bx, by = cl["members"][b]
                bw, bh = dims[b]
                ox = min(ax + aw, bx + bw) - max(ax, bx)
                oy = min(ay + ah, by + bh) - max(ay, by)
                if ox > 0 and oy > 0:
                    problems.append(f"overlap {a}/{b}: {ox}x{oy} tiles")
    return clusters, problems


def seam_check(c, dims, img_dir, search=6):
    """Compare pixel rows across a seam for the stated offset and nearby shifts.

    Returns dict with the stated-offset score, the best shift (tiles) and its score.
    Score = mean abs RGB difference between the two facing edge pixel lines over
    the overlapping span (lower = better continuity).
    """
    if np is None:
        return None
    a, b = c["a"], c["b"]
    wa, ha = dims[a]
    wb, hb = dims[b]
    pa = edge_point(c["sa"], c["oa"], wa, ha)
    pb = edge_point(c["sb"], c["ob"], wb, hb)
    dx, dy = pa[0] - pb[0], pa[1] - pb[1]
    with Image.open(os.path.join(img_dir, a + ".png")) as ia, \
            Image.open(os.path.join(img_dir, b + ".png")) as ib:
        A = np.asarray(ia.convert("RGBA"), dtype=np.int16)
        B = np.asarray(ib.convert("RGBA"), dtype=np.int16)
    horiz = c["sa"] in "NS"
    if c["sa"] == "N":
        la, lb = A[0], B[-1]
    elif c["sa"] == "S":
        la, lb = A[-1], B[0]
    elif c["sa"] == "W":
        la, lb = A[:, 0], B[:, -1]
    else:
        la, lb = A[:, -1], B[:, 0]
    shift0 = dx if horiz else dy  # B start along A's edge, tiles

    def score(shift):
        s0 = shift * TILE
        lo, hi = max(0, s0), min(len(la), s0 + len(lb))
        if hi - lo < 4 * TILE:
            return None
        xa, xb = la[lo:hi], lb[lo - s0:hi - s0]
        m = (xa[:, 3] > 0) & (xb[:, 3] > 0)
        if m.sum() < 2 * TILE:
            return None
        return float(np.abs(xa[m, :3] - xb[m, :3]).mean())

    stated = score(shift0)
    best = (stated, 0)
    if stated is None:  # corner-only link (diagonal neighbour): nothing to compare
        return {"stated": None, "bestShift": 0, "bestScore": None}
    for k in range(-search, search + 1):
        if k == 0:
            continue
        s = score(shift0 + k)
        if s is not None and (best[0] is None or s < best[0] - 0.5):
            best = (s, k)
    return {"stated": stated, "bestShift": best[1], "bestScore": best[0]}


def cluster_name(members, areas):
    cnt = collections.Counter(areas[m]["name"] for m in members if m in areas)
    names = [n for n, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))]
    return " / ".join(names[:3]) + (" …" if len(names) > 3 else "")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ---------------------------------------------------------------- bundling
class Bundler:
    def __init__(self, out_dir, prefix):
        self.out_dir, self.prefix = out_dir, prefix
        self.n, self.cur, self.size, self.files = 0, {}, 2, []

    def add(self, key, webp):
        uri = "data:image/webp;base64," + base64.b64encode(webp).decode("ascii")
        extra = len(uri) + len(key) + 6
        if self.cur and self.size + extra > BUNDLE_LIMIT:
            self.flush()
        self.cur[key] = uri
        self.size += extra
        return f"{self.prefix}-{self.n + 1:02d}"

    def flush(self):
        if not self.cur:
            return
        self.n += 1
        name = f"{self.prefix}-{self.n:02d}"
        path = os.path.join(self.out_dir, name + ".json")
        with open(path, "w", encoding="ascii") as f:
            json.dump(self.cur, f, separators=(",", ":"))
        self.files.append((name, os.path.getsize(path), len(self.cur)))
        self.cur, self.size = {}, 2


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--site", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report")
    ap.add_argument("--preview-dir")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    args = ap.parse_args()

    pbs = os.path.join(args.db, "PBS")
    img_dir = os.path.join(args.site, "img", "area")
    data_dir = os.path.join(args.out, "data")
    maps_dir = os.path.join(args.out, "maps")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(maps_dir, exist_ok=True)
    for fn in os.listdir(maps_dir):  # remove stale bundles of ours
        if re.fullmatch(r"(maps|world)-\d+\.json", fn):
            os.remove(os.path.join(maps_dir, fn))

    areas = parse_site_areas(args.site)
    meta = parse_metadata(os.path.join(pbs, "metadata.txt"))
    regions = parse_townmap(os.path.join(pbs, "townmap.txt"))
    conns = parse_connections(os.path.join(pbs, "connections.txt"))
    report = {"problems": []}

    # image dims (tiles)
    img_ids = sorted((fn[:-4] for fn in os.listdir(img_dir)
                      if re.fullmatch(r"\d+\.png", fn)), key=int)
    px_dims, dims = {}, {}
    for aid in img_ids:
        with Image.open(os.path.join(img_dir, aid + ".png")) as im:
            w, h = im.size
        px_dims[aid] = (w, h)
        dims[aid] = (w // TILE, h // TILE)
        if w % TILE or h % TILE:
            report["problems"].append(f"{aid}: size {w}x{h} not a multiple of {TILE}")

    # ---------------- region grid / points
    region = regions[0]
    pos_of = {}
    by_pos = collections.defaultdict(list)
    for mid, e in meta.items():
        if "MapPosition" not in e or mid == "0":
            continue
        f = [int(x) for x in re.findall(r"-?\d+", e["MapPosition"])[:3]]
        if len(f) == 3 and f[0] == region["region"]:
            pos_of[mid] = (f[1], f[2])
            by_pos[(f[1], f[2])].append(mid)

    def order_ids(ids):
        return sorted(set(ids), key=lambda m: (not areas.get(m, {}).get("outdoor", False), int(m)))

    raw_pts = [p for p in region["points"] if p["x"] is not None and p["y"] is not None]
    gw = max(p["x"] for p in raw_pts) + 1
    gh = max(p["y"] for p in raw_pts) + 1
    merged = collections.OrderedDict()
    for p in raw_pts:  # Essentials shows the first matching point; keep others as "also"
        k = (p["x"], p["y"])
        if k not in merged:
            merged[k] = dict(p, also=[])
        else:
            q = merged[k]
            alt = {"name": p["name"], "sub": p["sub"]}
            if (p["name"], p["sub"]) != (q["name"], q["sub"]) and alt not in q["also"]:
                q["also"].append(alt)
            if q["healMap"] is None and p["healMap"] is not None:
                q.update(healMap=p["healMap"], healX=p["healX"], healY=p["healY"])

    base_names = collections.defaultdict(list)
    for mid, a in areas.items():
        base_names[a["name"].lower()].append(mid)

    points, unmatched = [], []
    for (x, y), p in merged.items():
        ids = list(by_pos.get((x, y), []))
        how = "position" if ids else None
        if p["healMap"] is not None and str(p["healMap"]) in areas:
            ids.append(str(p["healMap"]))
            how = how or "heal"
        if not ids:
            # fall back to area names: sub-name first, then the location name
            names = [p["sub"], p["name"]] + [a["sub"] for a in p["also"]] + [a["name"] for a in p["also"]]
            names = [n.lower() for n in names if n]
            for nm in names:  # exact area name
                if base_names.get(nm):
                    ids, how = list(base_names[nm]), "name"
                    break
            if not ids:
                for nm in names:  # area name with a floor/part suffix ("Ametrine Mountain 1F")
                    ids = [m for bn, ms in base_names.items() if bn.startswith(nm + " ") for m in ms]
                    if ids:
                        how = "name-prefix"
                        break
        pt = {"x": x, "y": y, "name": p["name"], "sub": p["sub"],
              "areas": order_ids(ids), "match": how,
              "heal": p["healMap"] is not None}
        if p["healMap"] is not None:
            pt["healArea"] = str(p["healMap"])
            pt["healTile"] = [p["healX"], p["healY"]]
        if p["switch"] is not None:
            pt["switch"] = p["switch"]
        if p["also"]:
            pt["also"] = p["also"]
        points.append(pt)
        if not ids:
            unmatched.append(f"{x},{y} {p['name']}" + (f" / {p['sub']}" if p["sub"] else ""))
    # map positions with no town-map point (from metadata, not invented)
    extra = []
    for (x, y), ids in sorted(by_pos.items()):
        if (x, y) in merged or not (0 <= x < gw and 0 <= y < gh):
            continue
        ids = order_ids(ids)
        extra.append({"x": x, "y": y, "name": areas.get(ids[0], {}).get("name"), "sub": None,
                      "areas": ids, "match": "position", "heal": False, "townmap": False})
    points.extend(extra)
    report["unmatchedPoints"] = unmatched
    report["metadataOnlyPoints"] = [f"{p['x']},{p['y']} {p['name']}" for p in extra]

    # ---------------- clusters
    clusters, probs = build_clusters(conns, dims)
    report["problems"] += probs
    seams = []
    for cl in clusters:
        for c in cl["edges"]:
            sc = seam_check(c, dims, img_dir)
            if sc:
                sc.update(line=c["line"], a=c["a"], b=c["b"], side=c["sa"])
                seams.append(sc)
                if sc["bestShift"] and sc["bestScore"] < 0.6 * sc["stated"]:
                    report["problems"].append(
                        f"seam line {c['line']} {c['a']}-{c['b']}: stated diff {sc['stated']}, "
                        f"shift {sc['bestShift']} tiles gives {sc['bestScore']:.1f}")
    report["seams"] = seams

    big = 0
    for cl in clusters:
        w = max(x + dims[m][0] for m, (x, y) in cl["members"].items())
        h = max(y + dims[m][1] for m, (x, y) in cl["members"].items())
        cl["tiles"] = (w, h)
        big = max(big, w * TILE, h * TILE)
    factor = 1
    while big / factor > COMPOSITE_MAX and factor < TILE:
        factor *= 2
    cscale = 1 / factor
    report["compositeScale"] = cscale

    world = Bundler(maps_dir, "world")
    stitched = []
    used_ids = {}
    for cl in sorted(clusters, key=lambda c: -len(c["members"])):
        members = sorted(cl["members"], key=int)
        name = cluster_name(members, areas)
        cid = slug(name.replace(" …", "")) or "cluster"
        # context: first ancestor (site area tree) outside the cluster's own names
        own = {areas[m]["name"] for m in members if m in areas}
        ctx, cur, hops = None, areas.get(members[0], {}).get("parent"), 0
        while cur and cur in areas and hops < 20:
            if areas[cur]["name"] not in own:
                ctx = areas[cur]["name"]
                break
            cur, hops = areas[cur]["parent"], hops + 1
        if cid in used_ids:
            prev = used_ids[cid]
            if ctx and ctx != prev:
                name = f"{name} ({ctx})"
            else:
                name = f"{name} (maps {members[0]}–{members[-1]})"
            cid = f"{cid}-{members[0]}"
        else:
            used_ids[cid] = ctx
        tw, th = cl["tiles"]
        tpx = TILE // factor
        canvas = Image.new("RGBA", (tw * tpx, th * tpx), (0, 0, 0, 0))
        boxes = []
        for m in members:
            x, y = cl["members"][m]
            with Image.open(os.path.join(img_dir, m + ".png")) as im:
                s = shrink(im, factor)
            canvas.alpha_composite(s, (x * tpx, y * tpx))
            boxes.append({"area": m, "x": x * tpx, "y": y * tpx, "w": s.size[0], "h": s.size[1],
                          "tx": x, "ty": y})
        if args.preview_dir:
            os.makedirs(args.preview_dir, exist_ok=True)
            pv = canvas.copy()
            pv.thumbnail((1600, 1600))
            pv.save(os.path.join(args.preview_dir, cid + ".png"))
        bundle = world.add(cid, to_webp(canvas))
        grid = order_ids([m for m in members if m in pos_of])
        stitched.append({
            "id": cid, "name": name, "context": ctx, "bundle": bundle, "key": cid,
            "w": canvas.size[0], "h": canvas.size[1], "scale": cscale,
            "outdoor": any(areas.get(m, {}).get("outdoor") for m in members),
            "gridPoints": sorted({pos_of[m] for m in grid}),
            "areas": boxes,
        })
    world.flush()

    regionmap = {
        "grid": {"w": gw, "h": gh},
        "name": region.get("name"),
        "tile": TILE,
        "points": points,
        "stitched": stitched,
        "areaPositions": {m: list(p) for m, p in sorted(pos_of.items(), key=lambda kv: int(kv[0]))},
        "notes": [
            "grid size = max town-map point coordinate + 1 (no map.png available)",
            "points[].areas: maps whose metadata MapPosition is this cell (match=position), "
            "plus the heal map; otherwise maps whose name equals the point's sub-name/name (match=name)",
            "points with townmap=false come only from metadata MapPosition",
            "stitched[].areas[].tx/ty are tile offsets; x/y/w/h are pixels in the composite",
        ],
    }
    with open(os.path.join(data_dir, "regionmap.json"), "w", encoding="utf-8") as f:
        json.dump(regionmap, f, ensure_ascii=False, separators=(",", ":"))

    # ---------------- per-area images
    hashes = {aid: file_hash(os.path.join(img_dir, aid + ".png")) for aid in img_ids}
    first_of = {}
    todo = []
    for aid in img_ids:
        hs = hashes[aid]
        if hs in first_of:
            continue
        first_of[hs] = aid
        todo.append((os.path.join(img_dir, aid + ".png"), aid))
    results = {}
    with cf.ProcessPoolExecutor(max_workers=args.jobs) as ex:
        for r in ex.map(encode_area, todo, chunksize=4):
            results[r[0]] = r
    bund = Bundler(maps_dir, "maps")
    key_info = {}
    for aid in sorted(results, key=int):
        _, w, h, f, size, data = results[aid]
        if data is None:
            key_info[aid] = None
            continue
        b = bund.add(aid, data)
        key_info[aid] = {"bundle": b, "key": aid, "w": size[0], "h": size[1],
                         "scale": 1 / f, "tiles": [w // TILE, h // TILE]}
    bund.flush()
    area_images = {}
    empty = []
    for aid in img_ids:
        info = key_info.get(first_of[hashes[aid]])
        if info is None:
            empty.append(aid)
        area_images[aid] = info
    with open(os.path.join(data_dir, "area_images.json"), "w", encoding="utf-8") as f:
        json.dump(area_images, f, separators=(",", ":"))

    report["emptyImages"] = empty
    report["duplicates"] = {aid: first_of[hashes[aid]] for aid in img_ids
                            if first_of[hashes[aid]] != aid}
    report["bundles"] = world.files + bund.files
    report["totalBundleBytes"] = sum(s for _, s, _ in report["bundles"])
    report["clusters"] = [(s["id"], s["name"], len(s["areas"]), s["w"], s["h"]) for s in stitched]
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"points: {len(points)} ({len(unmatched)} unmatched, {len(extra)} metadata-only)")
    print(f"clusters: {len(stitched)}, composite scale {cscale}")
    for s in stitched:
        print(f"  {s['id']}: {len(s['areas'])} maps, {s['w']}x{s['h']}")
    print(f"bundles: {len(report['bundles'])}, {report['totalBundleBytes'] / 1e6:.1f} MB")
    print(f"problems: {len(report['problems'])}")
    for p in report["problems"]:
        print("  " + p)


if __name__ == "__main__":
    main()
