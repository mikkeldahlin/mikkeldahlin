#!/usr/bin/env python3
"""Extract item balls, hidden items, NPC gifts and trainers placed on each map.

Usage (run isolated: the game files are untrusted binary data):

    python3 -I scripts/extract_events.py \
        --game     /tmp/claude-0/game/merged/Data \
        --pbs      /tmp/claude-0/src/reborn-db/PBS \
        --items    app/data/items.json \
        --trainers app/data/trainers.json \
        --areas    app/data/areas.json \
        --images   app/data/area_images.json \
        --out      app/data/events.json \
        [--maps-dir app/maps --debug-dir /tmp/claude-0/debug --spot 19,209]

Reads RPG Maker XP data (Pokémon Reborn v19.5, core + patch merged) with the
pure-Python Marshal reader in scripts/links_marshal.py (never instantiates Ruby
classes).  PBS trainers.txt / trainertypes.txt / items.txt (reborn-db, v19.16)
map game symbols to the site slugs used in items.json and trainers.json.

Patterns used by Reborn (every event page is scanned; script text comes from
Script commands 355+655 and Conditional Branch "script" commands 111/[12]):
  item   Kernel.pbItemBall(PBItems::X[,qty]) on an event with an item-ball
         graphic (itemball*, rby_pokeball, manaphy_gem).  Event name "Item".
  hidden the same call on an event with no graphic (or the "sparkle" graphic);
         Reborn names these events "HiddenItem".
  gift   Kernel.pbReceiveItem(PBItems::X[,qty]) on an action-button page that
         shows an NPC graphic.
  trainer pbTrainerBattle / pbTrainerBattle100(PBTrainers::T,"Name",_I(..),
         double, party, ...) and pbDoubleTrainerBattle[100](T1,"N1",p1,_I(..),
         T2,"N2",p2,_I(..)).  Reborn names regular trainer events "Trainer(N)"
         (N = sight range).
Autorun (3) and parallel (4) pages are cutscene logic whose tile means nothing:
they are counted but not emitted.  So are events outside the map bounds.

Output (minified): { "<areaId>": [record...], "_meta": {source, patterns, stats} }
  record = { ev, x, y, kind, name, item, qty, trainer, trainers, raw,
             conditional[, double, alt, when] }
  item/qty    items.json id and quantity (qty null when it is a game variable)
  trainer     trainers.json id of the (first) opponent; trainers = every opponent
              id of that battle (two for double battles)
  alt         other item / trainer ids the same event can give on other pages
              or branches (e.g. a rival's team chosen by starter)
  conditional the page has a game switch/variable condition, the call sits in a
              nested branch, or the event has alternatives
  when        names of the game switches the page requires (from System.rxdata)
"""
import argparse
import collections
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import links_marshal as M  # noqa: E402
from extract_world import align, read_trainers_txt, read_trainertypes  # noqa: E402

SOURCE = "Pokémon Reborn v19.5 game data"
CALL_RE = re.compile(r"\b(?:Kernel\.)?(pbItemBall|pbReceiveItem|pbTrainerBattle100|pbTrainerBattle|"
                     r"pbDoubleTrainerBattle100|pbDoubleTrainerBattle)\s*\(")
BALL_GFX_RE = re.compile(r"itemball|pokeball|_gem$", re.I)
PATTERNS = {
    "item": "Kernel.pbItemBall(PBItems::X[,qty]) on an event with an item-ball graphic "
            "(itemball*, rby_pokeball, manaphy_gem); event name 'Item'",
    "hidden": "Kernel.pbItemBall(::PBItems::X[,qty]) on an event with no graphic (or 'sparkle'); "
              "event name 'HiddenItem'",
    "gift": "Kernel.pbReceiveItem(PBItems::X[,qty]) on an action-button page with an NPC graphic",
    "trainer": "pbTrainerBattle[100](PBTrainers::T,\"Name\",_I(..),double,party,..) / "
               "pbDoubleTrainerBattle[100](T1,\"N1\",p1,_I(..),T2,\"N2\",p2,_I(..)); "
               "event name 'Trainer(N)' for regular trainers",
    "scriptSources": "Script 355+655 and Conditional Branch script (111, type 12)",
    "skipped": "autorun/parallel pages, events outside the map; pbPickBerry berry trees, "
               "PokemonBag.pbStoreItem and pbReceiveItem on non-NPC pages are not emitted",
    "trainerMatch": "(trainer type, name, party) against PBS trainers.txt aligned to trainers.json "
                    "ids (site slug suffix = party+1); fallbacks: unique (type,name), then unique "
                    "(class,name,party) / (name,party)",
}


def text(v):
    if isinstance(v, M.RStr):
        return v.text
    return v if isinstance(v, str) else ""


# ------------------------------------------------------------ ruby args --

def split_args(s, start):
    """Split the argument list of a call whose '(' ends at s[start-1].

    Returns (args, end) with args as raw stripped strings; handles nested
    brackets and "..."/'...' literals with backslash escapes.
    """
    args, cur, depth, i, q = [], [], 0, start, None
    while i < len(s):
        ch = s[i]
        if q:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 2
                continue
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            if depth == 0:
                args.append("".join(cur).strip())
                return [a for a in args if a != ""] if args != [""] else [], i
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    args.append("".join(cur).strip())
    return args, i


def const_sym(a, mod):
    m = re.fullmatch(r"(?:::)?(?:%s::|:)(\w+)" % mod, a or "")
    return m.group(1) if m else None


def str_lit(a):
    m = re.fullmatch(r'"((?:[^"\\]|\\.)*)"', a or "")
    return m.group(1).replace('\\"', '"') if m else None


def int_lit(a):
    return int(a) if a and re.fullmatch(r"\d+", a) else None


# ------------------------------------------------------------- lookups --

class ItemIndex:
    def __init__(self, items_json, pbs):
        self.ids = {i["id"] for i in items_json}
        self.by_name = {i["name"].lower(): i["id"] for i in items_json}
        self.sym_name = {}
        with open(os.path.join(pbs, "items.txt"), encoding="utf-8-sig", errors="replace") as f:
            for row in csv.reader(f):
                if len(row) > 2 and not row[0].startswith("#"):
                    self.sym_name[row[1].strip()] = row[2].strip()

    def resolve(self, sym):
        n = self.sym_name.get(sym)
        if n is None:
            n = sym
        r = self.by_name.get(n.lower())
        if r:
            return r
        slug = re.sub(r"[^a-z0-9]+", "-", n.lower().replace("é", "e")).strip("-")
        return slug if slug in self.ids else None


class TrainerIndex:
    def __init__(self, trainers_json, pbs):
        ttypes = read_trainertypes(pbs)
        self.tclass = {k.upper(): v["class"] for k, v in ttypes.items()}
        entries = read_trainers_txt(pbs, ttypes)
        mapping = align([t["id"] for t in trainers_json], entries)
        self.exact, tn, cnp, np_ = {}, collections.defaultdict(set), collections.defaultdict(set), \
            collections.defaultdict(set)
        for slug, e in mapping.items():
            party = int(e["party"] or 0)
            self.exact[(e["type"].upper(), e["name"], party)] = slug
            tn[(e["type"].upper(), e["name"])].add(slug)
            cnp[(e["class"], e["name"], party)].add(slug)
            np_[(e["name"], party)].add(slug)
        self.tn, self.cnp, self.np = tn, cnp, np_

    def resolve(self, ttype, name, party):
        """Return (slug or None, how)."""
        T = ttype.upper()
        p = party if isinstance(party, int) else 0
        s = self.exact.get((T, name, p))
        if s:
            return s, "exact"
        c = self.tn.get((T, name), set())
        if len(c) == 1:
            return next(iter(c)), "type+name"
        cls = self.tclass.get(T)
        c = self.cnp.get((cls, name, p), set()) if cls else set()
        if len(c) == 1:
            return next(iter(c)), "class+name+party"
        c = self.np.get((name, p), set())
        if len(c) == 1:
            return next(iter(c)), "name+party"
        return None, None


# ------------------------------------------------------------- scanning --

def page_scripts(pg):
    """Yield (script_text, indent) for every script-bearing command on a page."""
    buf, ind = [], 0
    for c in pg.get("list") or []:
        if not isinstance(c, M.RObj):
            continue
        code, p = c.get("code"), c.get("parameters") or []
        if code == 655:
            if p:
                buf.append(text(p[0]))
            continue
        if buf:
            yield "\n".join(buf), ind
            buf = []
        if code == 355 and p:
            buf, ind = [text(p[0])], c.get("indent") or 0
        elif code == 111 and len(p) > 1 and p[0] == 12:
            yield text(p[1]), c.get("indent") or 0
    if buf:
        yield "\n".join(buf), ind


def parse_calls(s):
    """Yield dicts for item / gift / trainer calls in one script string."""
    for m in CALL_RE.finditer(s):
        fn = m.group(1)
        args, _ = split_args(s, m.end())
        if fn in ("pbItemBall", "pbReceiveItem"):
            sym = const_sym(args[0] if args else "", "PBItems")
            qty = 1 if len(args) < 2 else int_lit(args[1])
            yield {"fn": fn, "sym": sym, "qty": qty, "rawargs": ",".join(args)}
        elif fn.startswith("pbTrainerBattle"):
            opp = [(const_sym(args[0] if args else "", "PBTrainers"), str_lit(args[1] if len(args) > 1 else ""),
                    int_lit(args[4]) if len(args) > 4 else 0, args[:2] + args[4:5])]
            double = len(args) > 3 and args[3] == "true"
            yield {"fn": fn, "opp": opp, "double": double}
        else:
            opp = []
            for k in (0, 4):
                a = args[k:k + 3]
                if len(a) == 3:
                    opp.append((const_sym(a[0], "PBTrainers"), str_lit(a[1]), int_lit(a[2]), a))
            yield {"fn": fn, "opp": opp, "double": True}


def page_cond(pg, switch_names):
    c = pg.get("condition")
    if not isinstance(c, M.RObj):
        return False, []
    when = []
    for k in ("switch1", "switch2"):
        if c.get(k + "_valid"):
            sid = c.get(k + "_id")
            nm = switch_names.get(sid) or f"switch {sid}"
            when.append(nm.strip())
    if c.get("variable_valid"):
        vid = c.get("variable_id")
        when.append(f"variable {vid} >= {c.get('variable_value')}")
    return bool(when), when


def load_switch_names(path):
    if not os.path.exists(path):
        return {}
    sysd = M.load_path(path)
    sw = sysd.get("switches") if isinstance(sysd, M.RObj) else None
    return {i: text(n) for i, n in enumerate(sw or []) if text(n).strip()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True)
    ap.add_argument("--pbs", required=True)
    ap.add_argument("--items", required=True)
    ap.add_argument("--trainers", required=True)
    ap.add_argument("--areas", required=True)
    ap.add_argument("--images", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--maps-dir")
    ap.add_argument("--debug-dir")
    ap.add_argument("--spot", default="")
    args = ap.parse_args()

    def jload(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    items_json, trainers_json = jload(args.items), jload(args.trainers)
    areas = {a["id"]: a for a in jload(args.areas)}
    images = jload(args.images)
    II, TI = ItemIndex(items_json, args.pbs), TrainerIndex(trainers_json, args.pbs)
    switch_names = load_switch_names(os.path.join(args.game, "System.rxdata"))
    infos = M.load_path(os.path.join(args.game, "MapInfos.rxdata"))

    st = collections.Counter()
    unmatched_tr = collections.Counter()
    unmatched_it = collections.Counter()
    tr_how = collections.Counter()
    out = {}
    for mid in sorted(k for k in infos if isinstance(k, int)):
        path = os.path.join(args.game, f"Map{mid:03d}.rxdata")
        if not os.path.exists(path):
            continue
        try:
            m = M.load_path(path)
        except M.MarshalError as e:
            print(f"Map{mid:03d}: {e}", file=sys.stderr)
            st["unreadable_maps"] += 1
            continue
        w, h = m.get("width"), m.get("height")
        recs = []
        events = m.get("events") or {}
        for eid in sorted(k for k in events if isinstance(k, int)):
            ev = events[eid]
            if not isinstance(ev, M.RObj):
                continue
            ex, ey, name = ev.get("x"), ev.get("y"), text(ev.get("name")).strip()
            found = collections.OrderedDict()  # kind -> list of hits
            for pg in ev.get("pages") or []:
                if not isinstance(pg, M.RObj):
                    continue
                trig = pg.get("trigger")
                g = pg.get("graphic")
                gname = text(g.get("character_name")) if isinstance(g, M.RObj) else ""
                gtile = (g.get("tile_id") or 0) if isinstance(g, M.RObj) else 0
                cond, when = page_cond(pg, switch_names)
                for s, indent in page_scripts(pg):
                    for call in parse_calls(s):
                        fn = call["fn"]
                        st["calls_" + fn] += 1
                        if trig in (3, 4):
                            st["skipped_autorun_parallel_" + ("item" if "Item" in fn else "trainer")] += 1
                            continue
                        if not (isinstance(ex, int) and isinstance(ey, int) and 0 <= ex < w and 0 <= ey < h):
                            st["skipped_outside_map"] += 1
                            continue
                        hit = {"cond": cond or indent > 0, "when": when}
                        if fn == "pbItemBall":
                            vis = bool(BALL_GFX_RE.search(gname)) or gtile > 0
                            kind = "item" if vis else "hidden"
                        elif fn == "pbReceiveItem":
                            if trig != 0 or not gname or BALL_GFX_RE.search(gname):
                                st["skipped_gift_not_npc"] += 1
                                continue
                            kind = "gift"
                        else:
                            kind = "trainer"
                        if kind == "trainer":
                            ids, raws = [], []
                            for (T, N, P, rawa) in call["opp"]:
                                tid = None
                                if T and N is not None:
                                    tid, how = TI.resolve(T, N, P)
                                    if tid:
                                        tr_how[how] += 1
                                if not tid:
                                    raws.append(",".join(x for x in (T or rawa[0] if rawa else "?",
                                                                        N if N is not None else (rawa[1] if len(rawa) > 1 else "?"),
                                                                        str(P) if P is not None else "?")))
                                    unmatched_tr[raws[-1]] += 1
                                ids.append(tid)
                            hit.update(key=tuple(ids) + tuple(raws), ids=ids, raws=raws, double=call["double"])
                        else:
                            iid = II.resolve(call["sym"]) if call["sym"] else None
                            if not iid:
                                unmatched_it[call["sym"] or call["rawargs"]] += 1
                            hit.update(key=iid or call["sym"] or call["rawargs"], item=iid, qty=call["qty"],
                                       raw=None if iid else (call["sym"] or call["rawargs"]))
                        found.setdefault(kind, []).append(hit)
            for kind, hits in found.items():
                keys = list(collections.OrderedDict.fromkeys(hh["key"] for hh in hits))
                first = hits[0]
                rec = {"ev": eid, "x": ex, "y": ey, "kind": kind, "name": name}
                if kind == "trainer":
                    ids = [i for i in first["ids"] if i]
                    rec.update(item=None, qty=None, trainer=ids[0] if ids else None,
                               trainers=ids, raw=";".join(first["raws"]) or None)
                    if first["double"]:
                        rec["double"] = True
                    alt = []
                    for hh in hits:
                        if hh["key"] != first["key"]:
                            alt += [i for i in hh["ids"] if i and i not in ids and i not in alt]
                else:
                    rec.update(item=first["item"], qty=first["qty"], trainer=None, trainers=[], raw=first["raw"])
                    alt = []
                    for hh in hits:
                        if hh["key"] != first["key"] and hh["item"] and hh["item"] != first["item"] \
                                and hh["item"] not in alt:
                            alt.append(hh["item"])
                rec["conditional"] = bool(len(keys) > 1 or any(hh["cond"] for hh in hits))
                if alt:
                    rec["alt"] = alt
                when = sorted({x for hh in hits for x in hh["when"]})
                if when:
                    rec["when"] = when
                recs.append(rec)
        if recs:
            out[str(mid)] = recs

    # ---------------------------------------------------------- stats
    kinds = collections.Counter(r["kind"] for v in out.values() for r in v)
    itrecs = [r for v in out.values() for r in v if r["kind"] != "trainer"]
    trrecs = [r for v in out.values() for r in v if r["kind"] == "trainer"]
    it_ok = sum(1 for r in itrecs if r["item"])
    tr_ok = sum(1 for r in trrecs if r["trainer"] and not r["raw"])
    tr_any = sum(1 for r in trrecs if r["trainer"])
    covered = {i for r in trrecs for i in r["trainers"] + r.get("alt", [])}
    stats = {"records": dict(kinds), "maps": len(out),
             "itemIdsResolved": f"{it_ok}/{len(itrecs)}",
             "trainerRecordsFullyResolved": f"{tr_ok}/{len(trrecs)}",
             "trainerRecordsWithAnId": f"{tr_any}/{len(trrecs)}",
             "trainerMatchHow": dict(tr_how),
             "distinctTrainerIdsPlaced": f"{len(covered)}/{len(trainers_json)}",
             "conditional": sum(r["conditional"] for v in out.values() for r in v),
             "mapsNotInAreas": sorted((k for k in out if k not in areas), key=int),
             **dict(sorted(st.items()))}
    out["_meta"] = {"source": SOURCE, "patterns": PATTERNS, "stats": stats}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    # ---------------------------------------------------------- report
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print("unmatched trainer opponents:", unmatched_tr.most_common(30))
    print("unmatched item symbols:", unmatched_it.most_common(20))
    report_wiki(out, items_json, areas)
    placed = {i for v in out.values() if isinstance(v, list) for r in v for i in r["trainers"] + r.get("alt", [])}
    imp = [t for t in trainers_json if t.get("important") and t["id"] not in placed]
    print(f"important trainers not placed (autorun cutscene battles etc.): {len(imp)} of "
          f"{sum(1 for t in trainers_json if t.get('important'))}; e.g.", [t["id"] for t in imp[:8]])
    report_sizes(out, images)
    if args.debug_dir and args.maps_dir and args.spot:
        render_spots(args, out, images)


def report_wiki(out, items_json, areas):
    """Compare item(+hidden) pickups with items.json wiki locations, grouped by area name.

    Wiki rows often use a parent map id ("Tourmaline Desert" 148) while the
    balls sit on child maps with the same name, so areas are compared by name.
    """
    def group(aid):
        a = areas.get(aid)
        return (a or {}).get("name") or aid

    wiki_items, game_items = collections.defaultdict(set), collections.defaultdict(set)
    wiki_n, game_n = collections.Counter(), collections.Counter()
    for it in items_json:
        for loc in it.get("locations") or []:
            note = (loc.get("note") or "").lower()
            if not loc.get("area") or re.search(r"received|given|gift|purchase|buy|sold|shop|mart|reward|trade", note):
                continue
            if re.fullmatch(r"available (after|during) [^.]*\.", note):
                continue  # shop-stock style rows ("Available after Badge 08.")
            g = group(loc["area"])
            wiki_items[g].add(it["id"])
            wiki_n[g] += 1
    for aid, recs in out.items():
        if aid == "_meta":
            continue
        for r in recs:
            if r["kind"] in ("item", "hidden") and r["item"]:
                g = group(aid)
                game_items[g].add(r["item"])
                game_n[g] += 1
    both = set(wiki_items) & set(game_items)
    wp = sum(len(v) for v in wiki_items.values())
    hit = sum(len(wiki_items[g] & game_items.get(g, set())) for g in wiki_items)
    print(f"wiki check (by area name): wiki groups {len(wiki_items)}, game groups {len(game_items)}, both {len(both)}; "
          f"wiki (area,item) pairs found among game balls: {hit}/{wp} ({100 * hit / max(wp, 1):.0f}%)")
    gaps = sorted(both, key=lambda g: -abs(wiki_n[g] - game_n[g]))[:10]
    print("  largest count gaps (wiki vs game):", "; ".join(f"{g}: {wiki_n[g]} vs {game_n[g]}" for g in gaps))
    print("  wiki-only:", "; ".join(f"{g} ({wiki_n[g]})" for g in sorted(set(wiki_items) - set(game_items),
                                                                    key=lambda g: -wiki_n[g])[:10]))
    return {"wikiPairsFound": f"{hit}/{wp}"}


def report_sizes(out, images):
    outside = []
    for aid, recs in out.items():
        if aid == "_meta":
            continue
        im = images.get(aid)
        if not im:
            continue
        tw, th = im["tiles"]
        outside += [(aid, r["ev"], r["x"], r["y"]) for r in recs if r["x"] >= tw or r["y"] >= th]
    no_img = [a for a in out if a != "_meta" and a not in images]
    print(f"records outside the v19.16 image tiles: {len(outside)} {outside[:10]}; maps with records but no image: {no_img}")


def render_spots(args, out, images):
    import base64
    import io
    from PIL import Image, ImageDraw
    os.makedirs(args.debug_dir, exist_ok=True)
    colors = {"item": (255, 40, 40), "hidden": (255, 0, 255), "gift": (0, 220, 0), "trainer": (0, 150, 255)}
    bundles = {}
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
        px = 32 * im["scale"]
        if px < 24:
            f = 24 / px
            img = img.resize((int(img.width * f), int(img.height * f)), Image.NEAREST)
            px = 24
        d = ImageDraw.Draw(img)
        for r in out.get(aid, []):
            x0, y0 = r["x"] * px, r["y"] * px
            d.rectangle([x0, y0, x0 + px - 1, y0 + px - 1], outline=colors[r["kind"]], width=2)
            label = (r["item"] or r["raw"] or "?") if r["kind"] != "trainer" else (r["trainer"] or r["raw"] or "?")
            d.text((x0 + 1, y0 + px), f"{r['ev']}:{label}"[:22], fill=colors[r["kind"]])
        p = os.path.join(args.debug_dir, f"events_{aid}.png")
        img.save(p)
        print(f"spot {aid} ({len(out.get(aid, []))} records) -> {p}")


if __name__ == "__main__":
    main()
