#!/usr/bin/env python3
"""Build the world-map data and map image bundles for the Reborn Field Guide.

Usage (run isolated, inputs are untrusted):

    python3 -I scripts/build_maps.py \
        --db   /tmp/claude-0/src/reborn-db \
        --site /tmp/claude-0/src/reborn-db-site \
        --out  app \
        [--links app/data/links.json] \
        [--report /path/report.json] [--preview-dir /path/previews] [--jobs 4]

Reads
  <db>/PBS/townmap.txt      Essentials town map points
  <db>/PBS/connections.txt  Essentials map edge connections
  <db>/PBS/metadata.txt     per-map MapPosition (region,x,y)
  <site>/area/<id>.html     area name, parent, "Outdoor map?"
  <site>/img/area/<id>.png  rendered map (32 px tiles)
  --links links.json        optional: edge warps between outdoor maps (see
                            "warp stitching" below) join more maps into composites

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


# ---------------------------------------------------------------- warp stitching
# Reborn City's wards and many towns are joined by warp events near a map edge
# (step off the edge, land near the neighbour's opposite edge) rather than by
# seamless connections.  Their maps usually carry a margin that repeats the
# neighbour's scenery, so the warp pair gives an offset that the pixels in that
# shared margin can confirm.  Each accepted pair is cut at a seam between the two
# warp lines, so neither map's margin is drawn over the other's playable area.
WARP_MARGIN = 20        # tiles: a warp run this close to a border is an edge exit
WARP_MAX_DIFF = 15.0    # mean abs RGB difference in the shared margin to accept
WARP_SEARCH = 2         # tiles searched around the warp-derived offset
WARP_MIN_TILES = 20     # opaque tiles both maps must share to compare pixels
OVERLAP_TOL = 2         # tiles of overlap tolerated between clipped maps
SCORE_FACTOR = 4        # downscale used for pixel comparison (8 px per tile)
SIDES = ("L", "R", "T", "B")
OPP = {"L": "R", "R": "L", "T": "B", "B": "T"}
SIDE_NAME = {"left": "L", "right": "R", "top": "T", "bottom": "B"}


def area_tree(areas):
    """top[m]: the root's child on m's parent chain (the place, e.g. Reborn City);
    era[m]: True for story variants filed under a "Neo ..." area."""
    top, era = {}, {}
    for m in areas:
        chain, cur, hops = [m], areas[m]["parent"], 0
        while cur and cur in areas and hops < 30:
            chain.append(cur)
            cur, hops = areas[cur]["parent"], hops + 1
        top[m] = chain[-2] if len(chain) >= 2 else chain[-1]
        era[m] = any(areas[c]["name"].startswith("Neo ") for c in chain)
    return top, era


def warp_candidates(links, areas, dims, top, era, log):
    """Edge-like warps between two outdoor maps of the same place and era.

    links.json marks warps on the last tile row/column as kind "edge"; the wards'
    exits sit on the inner edge of a scenery margin and come through as "door"
    (without a door/cave hint), so both kinds are tried and judged by geometry:
    the run must lie within WARP_MARGIN of one border of A and land within
    WARP_MARGIN + 2 of the opposite border of B.  d is B's origin in A's tile
    space, taking the run's middle tile (the one tx,ty belongs to) to coincide
    with the landing tile.
    """
    sizes = links.get("_meta", {}).get("mapSizes", {})
    skipped = collections.Counter()

    def usable(m):
        a = areas.get(m)
        if not a or not a["outdoor"] or m not in dims or a["name"] == "REMOVED":
            return False
        if m in sizes and tuple(sizes[m]) != tuple(dims[m]):
            skipped[f"size mismatch {m}"] += 1
            return False
        return True

    out = []
    for a, lst in links.items():
        if a == "_meta" or not isinstance(lst, list) or not usable(a):
            continue
        W, H = dims[a]
        for l in lst:
            b = str(l.get("to"))
            if l.get("kind") not in ("edge", "door") or b == a or not usable(b):
                continue
            if l.get("tx") is None or l.get("ty") is None or l.get("npc"):
                continue
            if l.get("hint") in ("door", "cave"):
                continue
            if top.get(a) != top.get(b) or era.get(a) != era.get(b):
                skipped["different place or era"] += 1
                continue
            x, y, w, h, tx, ty = (int(l[k]) for k in ("x", "y", "w", "h", "tx", "ty"))
            wb, hb = dims[b]
            dist = {"L": x, "R": W - x - w, "T": y, "B": H - y - h}
            land = {"L": wb - 1 - tx, "R": tx, "T": hb - 1 - ty, "B": ty}
            opts = [(dist[s] + land[s], s) for s in SIDES
                    if dist[s] <= WARP_MARGIN and land[s] <= WARP_MARGIN + 2
                    and (h >= w if s in "LR" else w >= h)]
            if not opts:
                skipped["not at a border"] += 1
                continue
            side = min(opts)[1]
            mx, my = x + w // 2, y + h // 2
            out.append({"a": a, "b": b, "side": side, "d": (mx - tx, my - ty),
                        "line": x if side in "LR" else y, "len": max(w, h),
                        "cond": bool(l.get("conditional")), "kind": l["kind"]})
    for k, v in sorted(skipped.items()):
        log.append(f"warp links skipped ({k}): {v}")
    return out


class PixelScorer:
    """Mean abs RGB difference where two maps overlap at a tile offset."""

    def __init__(self, img_dir):
        self.img_dir, self.cache = img_dir, {}

    def arr(self, m):
        if m not in self.cache:
            with Image.open(os.path.join(self.img_dir, m + ".png")) as im:
                self.cache[m] = np.asarray(shrink(im, SCORE_FACTOR), dtype=np.int16)
        return self.cache[m]

    def score(self, a, b, d, win=None):
        """(score or None, shared opaque tiles) with B's origin at d in A's tiles,
        optionally only inside win = (x0, y0, x1, y1) in A's tiles."""
        p = TILE // SCORE_FACTOR
        A, B = self.arr(a), self.arr(b)
        ha, wa = A.shape[:2]
        hb, wb = B.shape[:2]
        dx, dy = d[0] * p, d[1] * p
        x0, x1 = max(0, dx), min(wa, dx + wb)
        y0, y1 = max(0, dy), min(ha, dy + hb)
        if win:
            x0, y0 = max(x0, win[0] * p), max(y0, win[1] * p)
            x1, y1 = min(x1, win[2] * p), min(y1, win[3] * p)
        if x1 <= x0 or y1 <= y0:
            return None, 0
        xa = A[y0:y1, x0:x1]
        xb = B[y0 - dy:y1 - dy, x0 - dx:x1 - dx]
        m = (xa[..., 3] > 0) & (xb[..., 3] > 0)
        n = int(m.sum()) // (p * p)
        if n < WARP_MIN_TILES:
            return None, n
        return float(np.abs(xa[m][:, :3] - xb[m][:, :3]).mean()), n


def warp_pairs(cands, scorer, log):
    """Collapse warp links per map pair, vote on the offset, check pixels.

    Returns accepted pairs {a, b, d (B origin in A tiles), side (B seen from A),
    linesA, linesB, cond, n, recip, score, verified} and rejected descriptions.
    """
    group = collections.defaultdict(list)
    for c in cands:
        a, b = sorted((c["a"], c["b"]), key=int)
        group[(a, b)].append(c)
    pairs, rejected = [], []
    for (a, b), cs in sorted(group.items(), key=lambda kv: (int(kv[0][0]), int(kv[0][1]))):
        votes = collections.Counter()
        weight = collections.Counter()
        sides = collections.Counter()
        lines = {a: [], b: []}
        for c in cs:
            fwd = c["a"] == a
            d = c["d"] if fwd else (-c["d"][0], -c["d"][1])
            side = c["side"] if fwd else OPP[c["side"]]
            votes[d] += 1
            weight[d] += (0 if c["cond"] else 1000) + c["len"]
            sides[side] += 1
            lines[c["a"]].append(c["line"])
        d = max(votes, key=lambda k: (votes[k], weight[k], k))
        side = max(sides, key=lambda s: (sides[s], s))
        recip = bool(lines[a]) and bool(lines[b])
        cond = all(c["cond"] for c in cs)
        desc = f"{a}-{b}"
        if len(votes) > 1:
            log.append(f"warp {desc}: offsets disagree {dict(votes)}; majority {d}")
        if len(sides) > 1:
            log.append(f"warp {desc}: sides disagree {dict(sides)}; using {side}")
        stated, n = scorer.score(a, b, d)
        best, bd = stated, d
        if stated is not None:
            for i in range(-WARP_SEARCH, WARP_SEARCH + 1):
                for j in range(-WARP_SEARCH, WARP_SEARCH + 1):
                    if i == 0 and j == 0:
                        continue
                    s, _ = scorer.score(a, b, (d[0] + i, d[1] + j))
                    if s is not None and s < best - 0.5:
                        best, bd = s, (d[0] + i, d[1] + j)
        rec = {"a": a, "b": b, "side": side, "lines": lines, "cond": cond, "n": len(cs),
               "recip": recip, "statedOffset": list(d), "statedScore": stated}
        if best is None:
            near = all(abs(k[0] - d[0]) <= 1 and abs(k[1] - d[1]) <= 1 for k in votes)
            if recip and near:
                rec.update(d=d, score=None, verified=False)
                log.append(f"warp {desc}: margins too thin to compare pixels; "
                           f"accepted on reciprocal links at {d}")
                pairs.append(rec)
            else:
                rejected.append(f"{desc}: one-way warp and no shared margin to verify")
            continue
        if best > WARP_MAX_DIFF:
            rejected.append(f"{desc}: margins do not match (diff {stated:.1f}, "
                            f"best {best:.1f} at {list(bd)})")
            continue
        if bd != d:
            log.append(f"warp {desc}: pixels move offset {d} -> {bd} "
                       f"(diff {stated:.1f} -> {best:.1f})")
        rec.update(d=bd, score=round(best, 2), verified=True)
        pairs.append(rec)
    return pairs, rejected


def seam_index(p, d):
    """Seam along the pair's axis, in A's tiles: A keeps tiles >= seam for L/T,
    tiles < seam for R/B; B keeps the rest.  Median of both maps' warp lines."""
    side, a, b = p["side"], p["a"], p["b"]
    k = 0 if side in "LR" else 1
    near_side = side in "LT"
    cand = [la if near_side else la + 1 for la in p["lines"][a]]
    cand += [lb + 1 + d[k] if near_side else lb + d[k] for lb in p["lines"][b]]
    cand.sort()
    return cand[(len(cand) - 1) // 2]


SEAM_SPAN = 0.4         # cut only maps that share at least this much of a side


def apply_seam(clip, pos, p, dims):
    """Cut A and B at the pair's seam (clip boxes are [l,t,r,b] in own tiles).

    The cut runs along a whole side, so it is made only when the two maps face
    each other along at least SEAM_SPAN of the shorter side; a warp at a corner
    leaves both maps whole (the merge then accepts their overlap only where the
    scenery matches).  A side cut by several seams keeps the most of the map, so
    seams that disagree by a tile or two leave a small overlap, never a gap.
    Uncut sides stay None until the cluster is drawn.  Returns True when cut."""
    a, b, side = p["a"], p["b"], p["side"]
    d = (pos[b][0] - pos[a][0], pos[b][1] - pos[a][1])
    ra, rb = vis_rect(a, pos, clip, dims), vis_rect(b, pos, clip, dims)
    k = 1 if side in "LR" else 0  # axis along the seam
    span = min(ra[k + 2], rb[k + 2]) - max(ra[k], rb[k])
    if span < SEAM_SPAN * min(ra[k + 2] - ra[k], rb[k + 2] - rb[k]):
        return False
    s = seam_index(p, d)
    ca = clip.setdefault(a, [None] * 4)
    cb = clip.setdefault(b, [None] * 4)

    def cut(c, i, v, keep_more):
        c[i] = v if c[i] is None else keep_more(c[i], v)

    i, j = {"L": (0, 2), "R": (2, 0), "T": (1, 3), "B": (3, 1)}[side]
    cut(ca, i, s, min if i < 2 else max)
    cut(cb, j, s - d[k ^ 1], min if j < 2 else max)
    return True


def full_clip(m, clip, dims):
    """[l, t, r, b] in m's tiles, uncut sides filled in."""
    c = clip.get(m) or [None] * 4
    full = (0, 0, *dims[m])
    return [full[i] if c[i] is None else c[i] for i in range(4)]


def vis_rect(m, pos, clip, dims):
    l, t, r, b = full_clip(m, clip, dims)
    x, y = pos[m]
    return x + l, y + t, x + r, y + b


def merge_warp_clusters(base, pairs, dims, areas, joined, scorer, log):
    """Kruskal-style merge of the connection clusters (kept rigid) along warp pairs.

    Pairs go in order: unconditional first, then verified, then best pixel match.
    A pair is refused when it would put two story variants (same name and size,
    not joined by a seamless connection) into one cluster, or when, after cutting
    at the seam, two maps would overlap by more than OVERLAP_TOL tiles where
    their pixels differ (an overlap that repeats the same scenery is allowed:
    either map draws it the same).
    """
    comps, of = {}, {}
    for i, cl in enumerate(base):
        comps[i] = {"members": dict(cl["members"]), "clip": {}, "warps": [],
                    "edges": list(cl["edges"]), "bases": [i]}
        for m in cl["members"]:
            of[m] = i
    nxt = [len(base)]

    def comp_of(m):
        if m not in of:
            comps[nxt[0]] = {"members": {m: (0, 0)}, "clip": {}, "warps": [], "edges": [],
                             "bases": []}
            of[m] = nxt[0]
            nxt[0] += 1
        return of[m]

    def variant(m1, m2):
        a1, a2 = areas.get(m1, {}), areas.get(m2, {})
        return (a1.get("name") == a2.get("name") and dims[m1] == dims[m2]
                and frozenset((m1, m2)) not in joined)

    rejected = []
    order = sorted(pairs, key=lambda p: (p["cond"], not p["verified"],
                                         p["score"] if p["score"] is not None else 99,
                                         -p["n"], int(p["a"]), int(p["b"])))
    for p in order:
        a, b = p["a"], p["b"]
        ca, cb = comp_of(a), comp_of(b)
        A, B = comps[ca], comps[cb]
        desc = f"{a} {areas[a]['name']} -> {b} {areas[b]['name']}"
        ax, ay = A["members"][a]
        if ca == cb:
            bx, by = A["members"][b]
            if abs(bx - ax - p["d"][0]) > 1 or abs(by - ay - p["d"][1]) > 1:
                rejected.append(f"{desc}: already placed at {(bx - ax, by - ay)}, "
                                f"warp says {tuple(p['d'])}")
                continue
            cut = apply_seam(A["clip"], A["members"], p, dims)
            A["warps"].append(p)
            log.append(f"warp {a}-{b}: already placed consistently"
                       + ("; cut at the seam" if cut else ""))
            continue
        bx0, by0 = B["members"][b]
        sx, sy = ax + p["d"][0] - bx0, ay + p["d"][1] - by0
        moved = {m: (x + sx, y + sy) for m, (x, y) in B["members"].items()}
        clash = [(m1, m2) for m1 in A["members"] for m2 in moved if variant(m1, m2)]
        if clash:
            rejected.append(f"{desc}: story variants {clash[0][0]}/{clash[0][1]} "
                            f"({areas[clash[0][0]]['name']}) would share a cluster")
            continue
        pos = {**A["members"], **moved}
        clip = {k: list(v) for k, v in {**A["clip"], **B["clip"]}.items()}
        if not apply_seam(clip, pos, p, dims):
            log.append(f"warp {a}-{b}: maps meet at a corner, not cut")
        bad = None
        for m1 in A["members"]:
            r1 = vis_rect(m1, pos, clip, dims)
            if r1[2] <= r1[0] or r1[3] <= r1[1]:
                bad = f"{m1} clipped away"
                break
            for m2 in moved:
                r2 = vis_rect(m2, pos, clip, dims)
                if r2[2] <= r2[0] or r2[3] <= r2[1]:
                    bad = f"{m2} clipped away"
                    break
                ox = min(r1[2], r2[2]) - max(r1[0], r2[0])
                oy = min(r1[3], r2[3]) - max(r1[1], r2[1])
                if ox > OVERLAP_TOL and oy > OVERLAP_TOL:
                    o = (pos[m2][0] - pos[m1][0], pos[m2][1] - pos[m1][1])
                    win = (max(r1[0], r2[0]) - pos[m1][0], max(r1[1], r2[1]) - pos[m1][1],
                           min(r1[2], r2[2]) - pos[m1][0], min(r1[3], r2[3]) - pos[m1][1])
                    sc, _n = scorer.score(m1, m2, o, win)
                    if sc is not None and sc <= WARP_MAX_DIFF:
                        log.append(f"warp {a}-{b}: {m1}/{m2} share {ox}x{oy} tiles of "
                                   f"matching scenery (diff {sc:.1f}), kept")
                        continue
                    bad = (f"would overlap {m1} {areas[m1]['name']} / {m2} "
                           f"{areas[m2]['name']} by {ox}x{oy} tiles"
                           + (f" (diff {sc:.1f})" if sc is not None else ""))
                    break
            if bad:
                break
        if bad:
            rejected.append(f"{desc}: {bad}")
            continue
        A["members"] = pos
        A["clip"] = clip
        A["warps"] += B["warps"] + [p]
        A["edges"] += B["edges"]
        A["bases"] += B["bases"]
        for m in moved:
            of[m] = ca
        del comps[cb]
        log.append(f"warp placed {desc} at {tuple(p['d'])}"
                   + (f" (diff {p['score']})" if p["score"] is not None else " (unverified)"))
    return [c for c in comps.values() if len(c["members"]) > 1], rejected


def warp_cluster_name(members, areas, dims, top):
    """Most prominent (largest) maps first; prefix the shared place when the
    cluster spans three or more differently named maps."""
    size = collections.Counter()
    for m in members:
        size[areas[m]["name"]] += dims[m][0] * dims[m][1]
    names = [n for n, _ in sorted(size.items(), key=lambda kv: (-kv[1], kv[0]))]
    label = " / ".join(names[:3]) + (" …" if len(names) > 3 else "")
    tops = {top.get(m) for m in members}
    place = areas.get(next(iter(tops)), {}).get("name") if len(tops) == 1 else None
    if place and len(names) >= 3 and place not in names:
        return f"{place} — {label}", place
    return label, None


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
    ap.add_argument("--links", help="links.json (map exits); also stitch outdoor maps "
                    "joined by edge warps, checked against their shared margins")
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

    top, _era = area_tree(areas)
    if args.links:
        if np is None:
            sys.exit("--links needs numpy (pixel checks of warp seams)")
        with open(args.links, encoding="utf-8") as f:
            links = json.load(f)
        wlog = []
        cands = warp_candidates(links, areas, dims, top, _era, wlog)
        scorer = PixelScorer(img_dir)
        pairs, prej = warp_pairs(cands, scorer, wlog)
        joined = {frozenset((c["a"], c["b"])) for c in conns}
        clusters, mrej = merge_warp_clusters(clusters, pairs, dims, areas, joined, scorer, wlog)
        report["warp"] = {
            "candidates": len(cands), "pairs": len(pairs),
            "rejectedPairs": prej, "rejectedPlacements": mrej, "log": wlog,
            "accepted": [{k: p[k] for k in ("a", "b", "side", "d", "score", "verified",
                                            "cond", "n", "recip")} for p in pairs],
        }
    else:
        for cl in clusters:
            cl.update(clip={}, warps=[])

    world = Bundler(maps_dir, "world")
    stitched = []
    used_ids = {}
    for cl in sorted(clusters, key=lambda c: -len(c["members"])):
        members = sorted(cl["members"], key=int)
        place = None
        if cl["warps"]:
            name, place = warp_cluster_name(members, areas, dims, top)
        else:
            name = cluster_name(members, areas)
        cid = slug(place or name.replace(" …", "")) or "cluster"
        # context: first ancestor (site area tree) outside the cluster's own names
        own = {areas[m]["name"] for m in members if m in areas}
        ctx, cur, hops = None, areas.get(members[0], {}).get("parent"), 0
        while cur and cur in areas and hops < 20:
            if areas[cur]["name"] not in own:
                ctx = areas[cur]["name"]
                break
            cur, hops = areas[cur]["parent"], hops + 1
        if cl["warps"] and ctx and ctx.startswith("Neo ") and ctx not in name:
            name = f"{name} ({ctx})"  # say which story variant this is
        if cid in used_ids:
            prev = used_ids[cid]
            if ctx and ctx != prev:
                name = f"{name} ({ctx})"
            else:
                name = f"{name} (maps {members[0]}–{members[-1]})"
            cid = f"{cid}-{members[0]}"
        else:
            used_ids[cid] = ctx
        # visible part of each map (warp seams cut the margins) in cluster tiles
        rects = {m: vis_rect(m, cl["members"], cl["clip"], dims) for m in members}
        ox = min(r[0] for r in rects.values())
        oy = min(r[1] for r in rects.values())
        tw = max(r[2] for r in rects.values()) - ox
        th = max(r[3] for r in rects.values()) - oy
        f = factor  # same scale as the rest unless this cluster is too big for it
        while max(tw, th) * TILE / f > COMPOSITE_MAX and f < TILE:
            f *= 2
        tpx = TILE // f
        canvas = Image.new("RGBA", (tw * tpx, th * tpx), (0, 0, 0, 0))
        boxes = []
        for m in members:
            x, y = cl["members"][m]
            clip = full_clip(m, cl["clip"], dims)
            if clip == [0, 0, *dims[m]]:
                clip = None
            with Image.open(os.path.join(img_dir, m + ".png")) as im:
                if clip:
                    l, t, r, b = clip
                    with im.crop((l * TILE, t * TILE, r * TILE, b * TILE)) as part:
                        s = shrink(part, f)
                else:
                    s = shrink(im, f)
            vx, vy = rects[m][0] - ox, rects[m][1] - oy
            canvas.alpha_composite(s, (vx * tpx, vy * tpx))
            box = {"area": m, "x": vx * tpx, "y": vy * tpx, "w": s.size[0], "h": s.size[1],
                   "tx": x - ox, "ty": y - oy}
            if clip:
                box["clip"] = clip
            boxes.append(box)
        if args.preview_dir:
            os.makedirs(args.preview_dir, exist_ok=True)
            pv = canvas.copy()
            pv.thumbnail((1600, 1600))
            pv.save(os.path.join(args.preview_dir, cid + ".png"))
        bundle = world.add(cid, to_webp(canvas))
        grid = order_ids([m for m in members if m in pos_of])
        entry = {
            "id": cid, "name": name, "context": ctx, "bundle": bundle, "key": cid,
            "w": canvas.size[0], "h": canvas.size[1], "scale": 1 / f,
            "outdoor": any(areas.get(m, {}).get("outdoor") for m in members),
            "gridPoints": sorted({pos_of[m] for m in grid}),
            "areas": boxes,
        }
        if cl["warps"]:
            entry["via"] = "warps" if not cl["edges"] else "connections+warps"
        stitched.append(entry)
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
    if args.links:
        regionmap["notes"] += [
            "stitched[] with via=warps also joins outdoor maps linked by edge warps (links.json), "
            "placed by the warp and checked against the scenery margin both maps share; "
            "areas[].clip=[l,t,r,b] (tiles of that map) is the part drawn, cut at the seam, "
            "so tx/ty (the map's origin) can lie outside the composite",
            "stitched[].scale can be smaller for a cluster too big for the common scale",
        ]
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
        print(f"  {s['id']}: {len(s['areas'])} maps, {s['w']}x{s['h']}"
              + (f" @{s['scale']}" if s["scale"] != cscale else "")
              + (f" [{s['via']}]" if s.get("via") else ""))
    if "warp" in report:
        w = report["warp"]
        print(f"warps: {w['candidates']} links, {len(w['accepted'])} pairs accepted, "
              f"{len(w['rejectedPairs'])} pairs rejected, "
              f"{len(w['rejectedPlacements'])} placements rejected")
        for p in w["rejectedPairs"] + w["rejectedPlacements"]:
            print("  rejected " + p)
    print(f"bundles: {len(report['bundles'])}, {report['totalBundleBytes'] / 1e6:.1f} MB")
    print(f"problems: {len(report['problems'])}")
    for p in report["problems"]:
        print("  " + p)


if __name__ == "__main__":
    main()
