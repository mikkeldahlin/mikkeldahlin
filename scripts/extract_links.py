#!/usr/bin/env python3
"""Extract map-to-map links (edges, seamless connections, doors) for the Reborn Field Guide.

Usage (run isolated: the game files are untrusted binary data):

    python3 -I scripts/extract_links.py \
        --game  /tmp/claude-0/game/merged/Data \
        --conns /tmp/claude-0/src/reborn-db/PBS/connections.txt \
        --areas app/data/areas.json \
        --images app/data/area_images.json \
        --out   app/data/links.json \
        [--debug-dir /tmp/claude-0/debug --maps-dir app/maps --spot 285,20,52]

Reads RPG Maker XP data (Pokémon Reborn v19.5, core + patch merged) with a
pure-Python Marshal reader (scripts/links_marshal.py) that never instantiates
Ruby classes.

Where warps come from
  * Event command 201 "Transfer Player" on any page of a map event.
    designation 0 = constant map/x/y; designation 1 = game variables, resolved
    when the same command list sets them with "Control Variables" (122) to a
    constant (map) or to the player's own x/y (position-preserving swap).
  * Event command 117 "Call Common Event" whose common event holds a 201.
  * Script commands (355/655) are scanned for `$game_temp.player_new_map_id = N`
    style assignments and `pbTransferPlayer(...)`-like calls with literal ids.
  Autorun (3) and parallel (4) pages are cutscene / controller logic whose event
  tile means nothing, so they are counted but not emitted.  Same-map warps
  (puzzle teleporters, rock climbs) are counted and dropped.

Output: app/data/links.json (front-end contract):
  { "<areaId>": [ {x,y,w,h,to,toName,tx,ty,kind,side,hint,conditional[,npc]} ],
    "_meta": { source, mapSizes: { "<areaId>": [w,h] }, stats } }
"""
import argparse
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import links_marshal as M  # noqa: E402

SOURCE = "Pokémon Reborn v19.5 game data (rebornevo.com official download)"
SIDE_FROM_PBS = {"N": "top", "North": "top", "S": "bottom", "South": "bottom",
                 "E": "right", "East": "right", "W": "left", "West": "left"}
OPPOSITE = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}

CAVE_RE = re.compile(r"cave|cavern|tunnel|grotto|mine\b|mines\b|underground|chasm|pit\b|"
                     r"crevasse|hollow|lair|catacomb|sewer", re.I)
FLOOR_RE = re.compile(r"\b(?:B?\d+F|\d+(?:st|nd|rd|th) Floor|Basement|Roof(?:top)?|Lobby|"
                      r"Upper|Lower|Ground Floor)\b", re.I)
BUILDING_RE = re.compile(r"house|center|centre|mart|gym|store|shop|lab\b|laboratory|hotel|"
                         r"apartment|office|building|room|hall|station|cafe|café|restaurant|"
                         r"school|academy|mansion|manor|tower|club|hospital|library|museum|"
                         r"factory|plant|hq|headquarters|inn\b|bar\b|garage|warehouse|"
                         r"theater|theatre|casino|arcade|condo|residence|home|cottage|cabin|"
                         r"church|temple|shrine|chapel|castle|palace|interior|inside", re.I)
SCRIPT_MAP_RE = re.compile(r"player_new_map_id\s*=\s*(\d+)")
SCRIPT_X_RE = re.compile(r"player_new_x\s*=\s*(\d+)")
SCRIPT_Y_RE = re.compile(r"player_new_y\s*=\s*(\d+)")
SCRIPT_CALL_RE = re.compile(r"pb(?:TransferPlayer|Warp\w*|Teleport\w*)\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")


def text(v):
    if isinstance(v, M.RStr):
        return v.text
    return v if isinstance(v, str) else ""


def floor_base(name):
    return FLOOR_RE.sub("", name).strip(" -:")


def load_mapinfos(path):
    infos = M.load_path(path)
    out = {}
    for mid, mi in infos.items():
        if isinstance(mid, int) and isinstance(mi, M.RObj):
            out[mid] = {"name": text(mi.get("name")), "parent": mi.get("parent_id") or 0}
    return out


def load_common_events(path):
    """{ce_id: [(map, x, y, resolved_by)]} for common events that transfer the player."""
    ces = M.load_path(path)
    out = {}
    for ce in ces if isinstance(ces, list) else []:
        if not isinstance(ce, M.RObj):
            continue
        targets, _unres = scan_list(ce.get("list") or [], None)
        if targets:
            out[ce.get("id")] = targets
    return out


def scan_list(cmds, common):
    """Return ([(map, x, y, how)], unresolved_count) for one event command list.

    `how` is "201", "201var", "script" or "common".  x/y may be None, or the
    string "player" when the target keeps the player's own coordinates.
    """
    targets = []
    unresolved = 0
    var = {}
    script_buf = []

    def flush_script():
        if not script_buf:
            return
        s = "\n".join(script_buf)
        script_buf.clear()
        for m in SCRIPT_CALL_RE.finditer(s):
            targets.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), "script"))
        mm = SCRIPT_MAP_RE.search(s)
        if mm:
            mx, my = SCRIPT_X_RE.search(s), SCRIPT_Y_RE.search(s)
            targets.append((int(mm.group(1)), int(mx.group(1)) if mx else None,
                            int(my.group(1)) if my else None, "script"))

    for cmd in cmds:
        if not isinstance(cmd, M.RObj):
            continue
        code = cmd.get("code")
        p = cmd.get("parameters") or []
        if code == 655:
            if p:
                script_buf.append(text(p[0]))
            continue
        flush_script()
        if code == 355 and p:
            script_buf.append(text(p[0]))
        elif code == 122 and len(p) >= 5:
            # [first, last, op, operand_type, ...]; only "set" is tracked
            first, last, op, kind = p[0], p[1], p[2], p[3]
            val = None
            if op == 0 and kind == 0:
                val = p[4]
            elif op == 0 and kind == 6 and len(p) >= 6 and p[4] in (-1, 0) and p[5] in (0, 1):
                val = ("player", p[5])
            for v in range(first, (last or first) + 1):
                var[v] = val
        elif code == 201 and len(p) >= 4:
            if p[0] == 0:
                targets.append((p[1], p[2], p[3], "201"))
            else:
                mv, xv, yv = var.get(p[1]), var.get(p[2]), var.get(p[3])
                if isinstance(mv, int):
                    def coord(v):
                        if isinstance(v, int):
                            return v
                        if isinstance(v, tuple):
                            return "player"
                        return None
                    targets.append((mv, coord(xv), coord(yv), "201var"))
                else:
                    unresolved += 1
        elif code == 117 and common is not None and p:
            for (m, x, y, _how) in common.get(p[0], []):
                targets.append((m, x, y, "common"))
    flush_script()
    return targets, unresolved


def border_side(x, y, w, h, tx, ty, tw, th):
    """Side of the map border this tile is on (within 1 tile), or None."""
    d = {"left": x, "right": w - 1 - x, "top": y, "bottom": h - 1 - y}
    near = [s for s, v in d.items() if v <= 1]
    if not near:
        return None
    if len(near) == 1:
        return near[0]
    # corner: prefer the side whose opposite border the target tile is on
    if isinstance(tx, int) and isinstance(ty, int) and tw and th:
        td = {"right": tx, "left": tw - 1 - tx, "bottom": ty, "top": th - 1 - ty}
        near.sort(key=lambda s: (td[s], d[s]))
        return near[0]
    near.sort(key=lambda s: d[s])
    return near[0]


def guess_hint(src_name, dst_name, ev_name, gfx_name, scripts, src_outdoor, dst_outdoor):
    evn, gfx = ev_name.lower(), gfx_name.lower()
    if "ladder" in evn or "stair" in evn or "climb" in gfx and "rock" not in evn:
        return "stairs"
    if "door" in gfx or "door" in evn:
        return "door"
    if "pbcaveentrance" in scripts or "pbcaveexit" in scripts:
        return "cave"
    if CAVE_RE.search(dst_name) and not CAVE_RE.search(src_name):
        return "cave"
    if CAVE_RE.search(src_name) and not CAVE_RE.search(dst_name) and dst_outdoor:
        return "cave"
    if (FLOOR_RE.search(src_name) or FLOOR_RE.search(dst_name)) and \
            floor_base(src_name).lower() == floor_base(dst_name).lower() and src_name != dst_name:
        return "stairs"
    if src_outdoor and not dst_outdoor and BUILDING_RE.search(dst_name):
        return "door"
    if dst_outdoor and not src_outdoor and BUILDING_RE.search(src_name):
        return "door"
    if CAVE_RE.search(src_name) and CAVE_RE.search(dst_name):
        return "cave"
    if src_outdoor != dst_outdoor:  # entering / leaving a building
        return "door"
    return None


def merge_runs(items, key_fn, horizontal_ok=True, vertical_ok=True):
    """Merge 1x1 tiles with equal key into straight contiguous runs.

    items: list of dicts with x,y (w=h=1). Returns new list of dicts.
    """
    groups = collections.defaultdict(list)
    for it in items:
        groups[key_fn(it)].append(it)
    out = []
    for _k, its in groups.items():
        cells = {(i["x"], i["y"]): i for i in its}
        used = set()
        # try horizontal runs first when allowed, else vertical
        for (x, y) in sorted(cells, key=lambda c: (c[1], c[0])):
            if (x, y) in used:
                continue
            run = [(x, y)]
            if horizontal_ok:
                nx = x + 1
                while (nx, y) in cells and (nx, y) not in used:
                    run.append((nx, y))
                    nx += 1
            if len(run) == 1 and vertical_ok:
                ny = y + 1
                while (x, ny) in cells and (x, ny) not in used:
                    run.append((x, ny))
                    ny += 1
            used.update(run)
            xs = [c[0] for c in run]
            ys = [c[1] for c in run]
            mid = cells[run[len(run) // 2]]
            first = cells[run[0]]
            link = dict(mid)
            link["x"], link["y"] = min(xs), min(ys)
            link["w"], link["h"] = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
            link["conditional"] = any(cells[c]["conditional"] for c in run)
            link["npc"] = all(cells[c].get("npc") for c in run)
            link["hint"] = first["hint"] if all(cells[c]["hint"] == first["hint"] for c in run) \
                else mid["hint"]
            out.append(link)
    return out


def parse_connections(path):
    conns, problems = [], []
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        lines = f.read().splitlines()
    comment = None
    for lineno, line in enumerate(lines, 1):
        s = line.strip()
        if s.startswith("#"):
            comment = s[1:].strip()
            continue
        s = s.split("#", 1)[0].strip()
        if not s:
            continue
        fl = [x.strip() for x in s.split(",")]
        try:
            conns.append({"a": int(fl[0]), "sa": SIDE_FROM_PBS[fl[1]], "oa": int(fl[2]),
                          "b": int(fl[3]), "sb": SIDE_FROM_PBS[fl[4]], "ob": int(fl[5]),
                          "line": lineno, "comment": comment})
        except (IndexError, KeyError, ValueError):
            problems.append(f"connections.txt:{lineno}: unparsed: {s}")
        comment = None
    return conns, problems


def edge_point(side, off, w, h):
    return {"top": (off, 0), "bottom": (off, h), "left": (0, off), "right": (w, off)}[side]


def connection_links(c, sizes):
    """Links on A's border (and B's) for one connections.txt record."""
    out = []
    (wa, ha), (wb, hb) = sizes[c["a"]], sizes[c["b"]]
    pa = edge_point(c["sa"], c["oa"], wa, ha)
    pb = edge_point(c["sb"], c["ob"], wb, hb)
    dx, dy = pa[0] - pb[0], pa[1] - pb[1]  # B origin in A tile space
    for src, dst, side, (ox, oy), (sw, sh), (tw, th) in (
            (c["a"], c["b"], c["sa"], (dx, dy), (wa, ha), (wb, hb)),
            (c["b"], c["a"], c["sb"], (-dx, -dy), (wb, hb), (wa, ha))):
        if side in ("top", "bottom"):
            lo, hi = max(0, ox), min(sw, ox + tw)
            if hi <= lo:
                continue
            y = 0 if side == "top" else sh - 1
            mid = (lo + hi - 1) // 2
            tx, ty = mid - ox, (y - 1 if side == "top" else y + 1) - oy
            out.append((src, {"x": lo, "y": y, "w": hi - lo, "h": 1, "to": dst,
                              "tx": tx, "ty": ty, "side": side}))
        else:
            lo, hi = max(0, oy), min(sh, oy + th)
            if hi <= lo:
                continue
            x = 0 if side == "left" else sw - 1
            mid = (lo + hi - 1) // 2
            tx, ty = (x - 1 if side == "left" else x + 1) - ox, mid - oy
            out.append((src, {"x": x, "y": lo, "w": 1, "h": hi - lo, "to": dst,
                              "tx": tx, "ty": ty, "side": side}))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True, help="dir with Map###.rxdata, MapInfos.rxdata, CommonEvents.rxdata")
    ap.add_argument("--conns", required=True)
    ap.add_argument("--areas", required=True)
    ap.add_argument("--images", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--debug-dir")
    ap.add_argument("--maps-dir", help="app/maps (image bundles) for --spot renders")
    ap.add_argument("--spot", default="", help="comma-separated area ids to render with link boxes")
    args = ap.parse_args()

    infos = load_mapinfos(os.path.join(args.game, "MapInfos.rxdata"))
    common = load_common_events(os.path.join(args.game, "CommonEvents.rxdata"))
    with open(args.areas, encoding="utf-8") as f:
        areas = {a["id"]: a for a in json.load(f)}
    with open(args.images, encoding="utf-8") as f:
        images = json.load(f)

    def name_of(mid):
        a = areas.get(str(mid))
        if a and a.get("name"):
            return a["name"]
        return infos.get(mid, {}).get("name")

    def outdoor(mid):
        a = areas.get(str(mid))
        return bool(a and a.get("outdoor"))

    # ---------------------------------------------------------- read maps
    maps, sizes = {}, {}
    st = collections.Counter()
    for mid in sorted(infos):
        path = os.path.join(args.game, f"Map{mid:03d}.rxdata")
        if not os.path.exists(path):
            st["mapinfo_without_file"] += 1
            continue
        try:
            m = M.load_path(path)
        except M.MarshalError as e:
            print(f"Map{mid:03d}: {e}", file=sys.stderr)
            st["unreadable_maps"] += 1
            continue
        w, h = m.get("width"), m.get("height")
        if not (isinstance(w, int) and isinstance(h, int)):
            dims = M.table_dims(m.get("data"))
            w, h = (dims[0], dims[1]) if dims else (0, 0)
        sizes[mid] = (w, h)
        maps[mid] = m

    # ---------------------------------------------------------- event warps
    raw = collections.defaultdict(list)  # src -> tile links (1x1)
    for mid, m in maps.items():
        w, h = sizes[mid]
        events = m.get("events") or {}
        for _eid, ev in sorted(events.items(), key=lambda kv: kv[0] if isinstance(kv[0], int) else 0):
            if not isinstance(ev, M.RObj):
                continue
            ex, ey = ev.get("x"), ev.get("y")
            ev_name = text(ev.get("name"))
            found = []  # (target tuple, page)
            for pg in ev.get("pages") or []:
                if not isinstance(pg, M.RObj):
                    continue
                cmds = pg.get("list") or []
                targets, unres = scan_list(cmds, common)
                st["unresolved_variable_warps"] += unres
                trig = pg.get("trigger")
                for t in targets:
                    st["found_" + t[3]] += 1
                    if trig in (3, 4):
                        st["skipped_autorun_parallel"] += 1
                        continue
                    found.append((t, pg))
            if not found:
                continue
            distinct = {t[0] for t, _ in found if t[0] != mid}
            seen = set()
            for (tmap, tx, ty, how), pg in found:
                if tmap == mid:
                    st["skipped_same_map"] += 1
                    continue
                if tmap not in infos:
                    st["skipped_target_not_in_mapinfos"] += 1
                    continue
                if tx == "player":
                    tx = ex
                if ty == "player":
                    ty = ey
                key = (tmap,)
                if key in seen:
                    continue
                seen.add(key)
                g = pg.get("graphic")
                gname = text(g.get("character_name")) if isinstance(g, M.RObj) else ""
                gtile = (g.get("tile_id") or 0) if isinstance(g, M.RObj) else 0
                scripts = " ".join(text((c.get("parameters") or [""])[0]).lower()
                                   for c in pg.get("list") or []
                                   if isinstance(c, M.RObj) and c.get("code") in (355, 655))
                trig = pg.get("trigger")
                tw, th = sizes.get(tmap, (0, 0))
                side = border_side(ex, ey, w, h, tx, ty, tw, th)
                has_gfx = bool(gname) or gtile > 0
                is_npc = trig == 0 and bool(re.match(r"(?:trchar|npc|trainer|pkmn|follower)", gname, re.I))
                if side and not has_gfx and trig in (1, 2):
                    kind = "edge"
                    hint = None
                else:
                    kind, side = "door", None
                    hint = guess_hint(name_of(mid) or "", name_of(tmap) or "", ev_name, gname,
                                      scripts, outdoor(mid), outdoor(tmap))
                link = {"x": ex, "y": ey, "w": 1, "h": 1, "to": tmap,
                        "tx": tx if isinstance(tx, int) else None,
                        "ty": ty if isinstance(ty, int) else None,
                        "kind": kind, "side": side, "hint": hint,
                        "conditional": len(distinct) > 1, "npc": is_npc, "how": how}
                raw[mid].append(link)

    # ---------------------------------------------------------- merge runs
    links = collections.defaultdict(list)
    for mid, items in raw.items():
        edges = [i for i in items if i["kind"] == "edge"]
        doors = [i for i in items if i["kind"] == "door"]
        for side in ("left", "right", "top", "bottom"):
            sel = [i for i in edges if i["side"] == side]
            horiz = side in ("top", "bottom")
            links[mid] += merge_runs(sel, lambda i: i["to"], horizontal_ok=horiz, vertical_ok=not horiz)
        links[mid] += merge_runs(doors, lambda i: (i["to"], i["hint"], i["npc"]))

    # ---------------------------------------------------------- connections.txt
    conns, problems = parse_connections(args.conns)
    conn_report = []
    for c in conns:
        bad = [x for x in (c["a"], c["b"]) if x not in sizes]
        if bad:
            conn_report.append(f"line {c['line']}: map(s) {bad} missing in v19.5, skipped")
            continue
        if c["comment"]:
            names = re.findall(r"(.+?) \((\d+)\)", c["comment"].replace(" - ", "|").replace("|", " "))
            for nm, nid in names:
                nid = int(nid)
                if nid in infos and infos[nid]["name"].strip() != nm.strip():
                    conn_report.append(f"line {c['line']}: comment says {nid}={nm.strip()!r}, "
                                       f"MapInfos says {infos[nid]['name']!r}")
        if OPPOSITE[c["sa"]] != c["sb"]:
            conn_report.append(f"line {c['line']}: sides {c['sa']}/{c['sb']} not opposite")
        cl = connection_links(c, sizes)
        if len(cl) < 2:
            st["connection_sides_without_overlap"] += 2 - len(cl)
        for src, link in cl:
            link.update({"kind": "connection", "hint": None, "conditional": False})
            links[src].append(link)
            st["connection_links"] += 1

    # ---------------------------------------------------------- finalize
    out = {}
    kinds = collections.Counter()
    for mid in sorted(links):
        seen, final = set(), []
        for l in sorted(links[mid], key=lambda l: (l["kind"], l["y"], l["x"], l["to"])):
            k = (l["x"], l["y"], l["w"], l["h"], l["to"], l["kind"])
            if k in seen:
                continue
            seen.add(k)
            rec = {"x": l["x"], "y": l["y"], "w": l["w"], "h": l["h"], "to": str(l["to"]),
                   "toName": name_of(l["to"]), "tx": l.get("tx"), "ty": l.get("ty"),
                   "kind": l["kind"], "side": l.get("side"), "hint": l.get("hint"),
                   "conditional": bool(l.get("conditional"))}
            if l.get("npc"):
                rec["npc"] = True
            final.append(rec)
            kinds[l["kind"]] += 1
        if final:
            out[str(mid)] = final

    meta = {"source": SOURCE,
            "mapSizes": {str(k): list(v) for k, v in sorted(sizes.items())},
            "stats": {"links": dict(kinds), **{k: v for k, v in sorted(st.items())}}}
    out["_meta"] = meta
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    # ---------------------------------------------------------- report
    print(f"maps read: {len(maps)} / MapInfos {len(infos)}; areas.json {len(areas)}")
    print("links per kind:", dict(kinds), " total", sum(kinds.values()))
    print("maps with >=1 link:", len(out) - 1)
    print("warp sources:", {k: v for k, v in st.items() if k.startswith("found_")})
    for k in ("unresolved_variable_warps", "skipped_autorun_parallel", "skipped_same_map",
              "skipped_target_not_in_mapinfos", "connection_sides_without_overlap", "mapinfo_without_file", "unreadable_maps"):
        print(f"  {k}: {st[k]}")
    print("hints:", dict(collections.Counter(l["hint"] for v in out.values() if isinstance(v, list) for l in v)))
    print("conditional links:", sum(l["conditional"] for v in out.values() if isinstance(v, list) for l in v))
    no_area = sorted({l["to"] for v in out.values() if isinstance(v, list) for l in v if l["to"] not in areas})
    print("targets without an area in areas.json:", no_area)
    print("source maps without an area:", sorted(k for k in out if k != "_meta" and k not in areas))
    print("connections.txt:", len(conns), "records;", *problems, sep=" ")
    for r in conn_report:
        print("  ", r)

    name_mm = [(mid, infos[mid]["name"], areas[str(mid)]["name"]) for mid in sorted(infos)
               if str(mid) in areas and infos[mid]["name"].strip() != (areas[str(mid)]["name"] or "").strip()]
    print(f"name mismatches MapInfos(v19.5) vs areas.json: {len(name_mm)}")
    for mid, a, b in name_mm[:60]:
        print(f"   {mid}: {a!r} vs {b!r}")
    size_mm = []
    for mid, (w, h) in sorted(sizes.items()):
        im = images.get(str(mid))
        if im and list(im.get("tiles") or []) != [w, h]:
            size_mm.append((mid, [w, h], im.get("tiles"), str(mid) in out))
    print(f"size mismatches v19.5 vs area_images tiles: {len(size_mm)} "
          f"({sum(1 for s in size_mm if s[3])} of them have links)")
    for mid, a, b, has in size_mm:
        print(f"   {mid} {name_of(mid)!r}: v19.5 {a} vs image {b}{'  [has links]' if has else ''}")
    no_img = [mid for mid in sizes if str(mid) not in images]
    print(f"maps without area image: {len(no_img)} ({sum(1 for m in no_img if str(m) in out)} with links)")

    if args.debug_dir and args.maps_dir and args.spot:
        render_spots(args, out, images)


def render_spots(args, out, images):
    import base64
    import io
    from PIL import Image, ImageDraw
    os.makedirs(args.debug_dir, exist_ok=True)
    bundles = {}
    colors = {"edge": (255, 0, 0), "connection": (0, 160, 255), "door": (255, 230, 0)}
    for aid in [s.strip() for s in args.spot.split(",") if s.strip()]:
        im = images.get(aid)
        if not im:
            print(f"spot {aid}: no image")
            continue
        b = im["bundle"]
        if b not in bundles:
            with open(os.path.join(args.maps_dir, b + ".json"), encoding="utf-8") as f:
                bundles[b] = json.load(f)
        uri = bundles[b][im["key"]]
        img = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))).convert("RGB")
        px = img.width / im["tiles"][0]
        if px < 16:
            img = img.resize((int(img.width * 16 / px), int(img.height * 16 / px)), Image.NEAREST)
            px = 16
        d = ImageDraw.Draw(img)
        for l in out.get(aid, []):
            x0, y0 = l["x"] * px, l["y"] * px
            d.rectangle([x0, y0, x0 + l["w"] * px - 1, y0 + l["h"] * px - 1],
                        outline=colors[l["kind"]], width=2)
            d.text((x0 + 2, y0 + 1), l["to"], fill=colors[l["kind"]])
        p = os.path.join(args.debug_dir, f"links_{aid}.png")
        img.save(p)
        print(f"spot {aid} ({len(out.get(aid, []))} links) -> {p}")


if __name__ == "__main__":
    main()
