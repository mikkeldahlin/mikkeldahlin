#!/usr/bin/env python3
"""Extract Pokédex data (pokemon, moves, abilities, items, types) for the
Reborn Field Guide from a local clone of the reborn-db-site static site
(data from Pokémon Reborn v19.16) plus the reborn-db PBS files.

Output follows data/SCHEMA.md and is written minified to --out:
  types.json, pokemon.json, sprites.json, moves.json, abilities.json,
  items.json, meta.json

Usage (run with -I, sources are untrusted data):
  python3 -I scripts/extract_dex.py \
      --site /tmp/claude-0/src/reborn-db-site \
      --pbs  /tmp/claude-0/src/reborn-db/PBS \
      --out  app/data

Needs beautifulsoup4 + lxml + Pillow.
"""

import argparse
import base64
import datetime
import glob
import io
import json
import os
import re
import sys
import unicodedata
from collections import Counter, OrderedDict

from bs4 import BeautifulSoup
from PIL import Image

TYPES = ["Normal", "Fire", "Water", "Electric", "Grass", "Ice", "Fighting",
         "Poison", "Ground", "Flying", "Psychic", "Bug", "Rock", "Ghost",
         "Dragon", "Dark", "Steel", "Fairy"]
TYPE_BY_UPPER = {t.upper(): t for t in TYPES}

STAT_KEYS = {"HP": "hp", "Attack": "atk", "Defense": "def", "Speed": "spe",
             "Special Attack": "spa", "Special Defense": "spd"}
STAT_ORDER = ["hp", "atk", "def", "spa", "spd", "spe"]

WARN = []


def warn(msg):
    WARN.append(msg)


# ----------------------------------------------------------------- helpers

def slugify(name):
    """Same convention the site uses for item/ability file names:
    ASCII-fold, lowercase, every run of non-alphanumerics -> '-'."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s


def text(el):
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def to_int(s):
    s = clean(s).replace(",", "")
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return None


def href_tail(a, kind):
    """'/reborn-db-site/move/TELEPORT.html' -> 'TELEPORT' (kind='move')."""
    if a is None:
        return None
    h = a.get("href") or ""
    m = re.search(r"/%s/([^/#?]+)\.html" % kind, h)
    return m.group(1) if m else None


def pokemon_link(a):
    """-> (page, formslug) from a pokemon/<page>.html#<slug> link."""
    h = a.get("href") or ""
    m = re.search(r"/pokemon/([^/#?]+)\.html(?:#(.*))?$", h)
    if not m:
        return None
    return m.group(1), (m.group(2) or "")


def type_name(s):
    s = clean(s).upper()
    return TYPE_BY_UPPER.get(s)


def parse_rate(s):
    """'8 1&frasl;3 %' (already unescaped to '8 1⁄3 %') -> 8.33"""
    s = clean(s).replace("%", "").replace("⁄", "/").strip()
    m = re.fullmatch(r"(?:(\d+)\s+)?(\d+)/(\d+)", s)
    if m:
        whole = int(m.group(1) or 0)
        return round(whole + int(m.group(2)) / int(m.group(3)), 2)
    m = re.fullmatch(r"\d+(?:\.\d+)?", s)
    if m:
        v = float(s)
        return int(v) if v.is_integer() else round(v, 2)
    return None


def soup_of(path):
    with open(path, encoding="utf-8") as f:
        return BeautifulSoup(f.read(), "lxml")


def th_map(table):
    """Map header-cell text -> following <td> for label/value tables."""
    out = {}
    if table is None:
        return out
    for th in table.find_all("th"):
        td = th.find_next_sibling("td")
        if td is not None:
            out.setdefault(text(th), td)
    return out


# ---------------------------------------------------------------- types.json

STANDARD_CHART = {
    # attacker: {defender: multiplier} -- the standard Gen 6+ chart, used
    # only to VERIFY the PBS data (differences are reported, not applied).
    "Normal": {"Rock": .5, "Ghost": 0, "Steel": .5},
    "Fire": {"Fire": .5, "Water": .5, "Grass": 2, "Ice": 2, "Bug": 2, "Rock": .5, "Dragon": .5, "Steel": 2},
    "Water": {"Fire": 2, "Water": .5, "Grass": .5, "Ground": 2, "Rock": 2, "Dragon": .5},
    "Electric": {"Water": 2, "Electric": .5, "Grass": .5, "Ground": 0, "Flying": 2, "Dragon": .5},
    "Grass": {"Fire": .5, "Water": 2, "Grass": .5, "Poison": .5, "Ground": 2, "Flying": .5, "Bug": .5, "Rock": 2, "Dragon": .5, "Steel": .5},
    "Ice": {"Fire": .5, "Water": .5, "Grass": 2, "Ice": .5, "Ground": 2, "Flying": 2, "Dragon": 2, "Steel": .5},
    "Fighting": {"Normal": 2, "Ice": 2, "Poison": .5, "Flying": .5, "Psychic": .5, "Bug": .5, "Rock": 2, "Ghost": 0, "Dark": 2, "Steel": 2, "Fairy": .5},
    "Poison": {"Grass": 2, "Poison": .5, "Ground": .5, "Rock": .5, "Ghost": .5, "Steel": 0, "Fairy": 2},
    "Ground": {"Fire": 2, "Electric": 2, "Grass": .5, "Poison": 2, "Flying": 0, "Bug": .5, "Rock": 2, "Steel": 2},
    "Flying": {"Electric": .5, "Grass": 2, "Fighting": 2, "Bug": 2, "Rock": .5, "Steel": .5},
    "Psychic": {"Fighting": 2, "Poison": 2, "Psychic": .5, "Dark": 0, "Steel": .5},
    "Bug": {"Fire": .5, "Grass": 2, "Fighting": .5, "Poison": .5, "Flying": .5, "Psychic": 2, "Ghost": .5, "Dark": 2, "Steel": .5, "Fairy": .5},
    "Rock": {"Fire": 2, "Ice": 2, "Fighting": .5, "Ground": .5, "Flying": 2, "Bug": 2, "Steel": .5},
    "Ghost": {"Normal": 0, "Psychic": 2, "Ghost": 2, "Dark": .5},
    "Dragon": {"Dragon": 2, "Steel": .5, "Fairy": 0},
    "Dark": {"Fighting": .5, "Psychic": 2, "Ghost": 2, "Dark": .5, "Fairy": .5},
    "Steel": {"Fire": .5, "Water": .5, "Electric": .5, "Ice": 2, "Rock": 2, "Steel": .5, "Fairy": 2},
    "Fairy": {"Fire": .5, "Fighting": 2, "Poison": .5, "Dragon": 2, "Dark": 2, "Steel": .5},
}


def build_types(pbs_dir):
    path = os.path.join(pbs_dir, "types.txt")
    blocks, cur = [], None
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                cur = {}
                blocks.append(cur)
                continue
            if "=" in line and cur is not None:
                k, v = line.split("=", 1)
                cur[k.strip()] = v.strip()
    internal = {b["InternalName"]: b["Name"] for b in blocks if "InternalName" in b}
    chart = {t: {} for t in TYPES}
    skipped = []
    for b in blocks:
        dname = b.get("Name")
        if dname not in TYPES:
            skipped.append(dname)
            continue
        for key, mult in (("Weaknesses", 2), ("Resistances", 0.5), ("Immunities", 0)):
            for atk in filter(None, (x.strip() for x in b.get(key, "").split(","))):
                aname = internal.get(atk)
                if aname not in TYPES:
                    warn("types.txt: %s lists unknown attacker %s" % (dname, atk))
                    continue
                chart[aname][dname] = mult
    missing = [t for t in TYPES if t not in {b.get("Name") for b in blocks}]
    if missing:
        warn("types.txt is missing types: %s" % missing)
    diffs = []
    for a in TYPES:
        for d in TYPES:
            got = chart[a].get(d, 1)
            exp = STANDARD_CHART[a].get(d, 1)
            if got != exp:
                diffs.append("%s -> %s: PBS %s, standard %s" % (a, d, got, exp))
    chart = {a: {d: (int(v) if float(v).is_integer() else v) for d, v in sorted(m.items(), key=lambda kv: TYPES.index(kv[0]))}
             for a, m in chart.items()}
    return {"types": TYPES, "chart": chart}, diffs, skipped


# ---------------------------------------------------------------- moves

def build_moves(site):
    moves = []
    code_to_id = {}
    for path in sorted(glob.glob(os.path.join(site, "move", "*.html"))):
        code = os.path.basename(path)[:-5]
        soup = soup_of(path)
        sec = soup.find("section")
        name = text(sec.find("h1"))
        tbl = th_map(sec.find("table"))
        tcell = tbl.get("Type")
        mtype = None
        if tcell is not None:
            icon = tcell.find(class_="type-icon")
            raw = text(icon) if icon else text(tcell)
            mtype = type_name(raw)
            if mtype is None and raw.upper() == "QMARKS":
                mtype = "???"
                warn("move %s: type QMARKS (the '???' type) kept as '???'" % code)
            elif mtype is None:
                warn("move %s: type %r is not one of the 18 types; kept as-is" % (code, raw))
                mtype = raw.title() if raw else None
        desc = None
        for h2 in sec.find_all("h2"):
            if text(h2) == "In-game description":
                p = h2.find_next_sibling("p")
                desc = text(p) or None
        chance = clean(tbl["Additional effect chance"].get_text()) if "Additional effect chance" in tbl else ""
        chance = to_int(chance.replace("%", ""))
        m = OrderedDict()
        m["id"] = slugify(name)
        m["code"] = code
        m["name"] = name
        m["type"] = mtype
        m["category"] = text(tbl.get("Damage class")) or None
        m["power"] = to_int(text(tbl.get("Power")))
        m["accuracy"] = to_int(text(tbl.get("Accuracy")))
        m["pp"] = to_int(text(tbl.get("PP")))
        m["priority"] = to_int(text(tbl.get("Priority")))
        m["target"] = text(tbl.get("Target")) or None
        m["effectChance"] = chance if chance else None
        m["effect"] = text(tbl.get("Additional effect description")) or None
        m["description"] = desc
        m["tm"] = None
        moves.append(m)
        code_to_id[code] = m["id"]
    ids = Counter(m["id"] for m in moves)
    for k, n in ids.items():
        if n > 1:
            warn("duplicate move id %s (%d moves); disambiguating with code" % (k, n))
    # Reborn has per-type/per-variant internal copies of some moves (Hidden
    # Power, Judgment, Multi-Attack, ...). The copy whose code matches the
    # name keeps the plain id; the others get "<id>-<code suffix>".
    for m in moves:
        if ids[m["id"]] > 1:
            base = m["id"].replace("-", "").upper()
            if m["code"] == base:
                continue
            suffix = m["code"][len(base):] if m["code"].startswith(base) else m["code"]
            m["id"] = m["id"] + "-" + suffix.lower()
            m["variantOf"] = slugify(m["name"])
            code_to_id[m["code"]] = m["id"]
    return moves, code_to_id


# ---------------------------------------------------------------- abilities

def build_abilities(site):
    out = []
    for path in sorted(glob.glob(os.path.join(site, "ability", "*.html"))):
        aid = os.path.basename(path)[:-5]
        sec = soup_of(path).find("section")
        name = text(sec.find("h1"))
        desc = None
        for h2 in sec.find_all("h2"):
            if text(h2) == "Description":
                desc = text(h2.find_next_sibling("p")) or None
        if slugify(name) != aid:
            warn("ability slug %s != slugify(%r)" % (aid, name))
        out.append(OrderedDict([("id", aid), ("name", name), ("description", desc)]))
    return out


# ---------------------------------------------------------------- items

def build_items(site, code_to_id):
    out = []
    for path in sorted(glob.glob(os.path.join(site, "item", "*.html"))):
        iid = os.path.basename(path)[:-5]
        sec = soup_of(path).find("section")
        name = text(sec.find("h1"))
        tbl = th_map(sec.find("table"))
        desc = None
        for h2 in sec.find_all("h2"):
            if text(h2) == "Description":
                desc = text(h2.find_next_sibling("p")) or None
        move = None
        if "Move taught" in tbl:
            code = href_tail(tbl["Move taught"].find("a"), "move")
            move = code_to_id.get(code)
            if move is None:
                warn("item %s teaches unknown move %s" % (iid, code))
        it = OrderedDict()
        it["id"] = iid
        it["name"] = name
        it["pocket"] = text(tbl.get("Bag pocket")) or None
        it["buy"] = to_int(text(tbl.get("Buy price")))
        it["sell"] = to_int(text(tbl.get("Sell price")))
        it["description"] = desc
        it["move"] = move
        it["locations"] = []
        out.append(it)
    return out


# ---------------------------------------------------------------- pokemon

def find_h3(container, title):
    for h3 in container.find_all("h3"):
        if text(h3) == title:
            return h3
    return None


def h3_section(container, title):
    h3 = find_h3(container, title)
    return h3.parent if h3 is not None else None


def parse_header(sec):
    """Species-level table right under the <h1>."""
    tbl = sec.find("table", class_="data-table")
    m = th_map(tbl)
    eggs = []
    if "Egg groups" in m:
        eggs = [text(a) for a in m["Egg groups"].find_all("a")]
    return {
        "dex": to_int(text(m.get("Pokédex number"))),
        "genderRatio": text(m.get("Gender ratio")) or None,
        "growthRate": text(m.get("Growth rate")) or None,
        "category": text(m.get("Category")) or None,
        "eggSteps": to_int(text(m.get("Steps to hatch"))),
        "baseExp": to_int(text(m.get("Base EXP yield"))),
        "baseFriendship": to_int(text(m.get("Base friendship"))),
        "eggGroups": eggs,
    }


def parse_evo(container):
    """-> (evolvesFrom link, [(to link, method)]) relative to the bold node."""
    sec = h3_section(container, "Evolution")
    if sec is None:
        return None, []
    bold = None
    for b in sec.find_all("b"):
        a = b.find("a", href=re.compile(r"/pokemon/"))
        if a is not None and b.find_parent(class_="evo-arrow") is None:
            bold = b
            break
    if bold is None:
        return None, []
    line = bold.find_parent("div", class_="evo-line")
    # predecessor: the node of the evo-line that owns the evo-col we sit in
    prev = None
    col = line.find_parent("div", class_="evo-col") if line else None
    if col is not None:
        pline = col.find_parent("div", class_="evo-line")
        if pline is not None:
            for child in pline.find_all(recursive=False):
                if child.name == "a":
                    prev = pokemon_link(child)
                    break
                if child.name == "b" and child.find("a"):
                    prev = pokemon_link(child.find("a"))
                    break
    nxt = []
    mycol = None
    for child in line.find_all(recursive=False):
        if child.name == "div" and "evo-col" in (child.get("class") or []):
            mycol = child
            break
    if mycol is not None:
        for cl in mycol.find_all("div", class_="evo-line", recursive=False):
            arrow = cl.find("div", class_="evo-arrow", recursive=False)
            method = None
            if arrow is not None:
                small = arrow.find("small")
                method = clean((small if small else arrow).get_text("")).replace("→", "").strip()
                method = re.sub(r"\s*\bOR\b\s*", " OR ", method).strip() or None
            target = None
            for child in cl.find_all(recursive=False):
                if child.name == "a":
                    target = pokemon_link(child)
                    break
                if child.name == "b" and child.find("a"):
                    target = pokemon_link(child.find("a"))
                    break
            if target:
                nxt.append((target, method))
    return prev, nxt


def parse_moves(container, code_to_id, unknown):
    sec = h3_section(container, "Moves")
    res = OrderedDict([("level", []), ("tm", []), ("tutor", []), ("egg", []),
                       ("evolution", []), ("prevo", [])])
    if sec is None:
        return res

    def mid(a):
        code = href_tail(a, "move")
        if code is None:
            return None
        if code not in code_to_id:
            unknown[code] += 1
            return None
        return code_to_id[code]

    for h4 in sec.find_all("h4"):
        title = text(h4)
        tbl = h4.find_next_sibling("table")
        if tbl is None:
            continue
        rows = [tr for tr in tbl.find_all("tr") if tr.find("td")]
        for tr in rows:
            tds = tr.find_all("td")
            if title == "Learnt by levelling up":
                lvl = to_int(text(tds[0]))
                mv = mid(tds[1].find("a"))
                if mv:
                    res["level"].append(OrderedDict([("level", lvl if lvl is not None else text(tds[0])), ("move", mv)]))
            elif title == "Learnt from TMs/TMXs":
                tm = text(tds[0])
                mv = mid(tds[1].find("a"))
                if mv:
                    res["tm"].append(OrderedDict([("tm", tm), ("move", mv)]))
            elif title in ("Learnt from Move Tutors", "Egg Moves", "Learnt on evolution"):
                mv = mid(tds[0].find("a"))
                key = {"Learnt from Move Tutors": "tutor", "Egg Moves": "egg",
                       "Learnt on evolution": "evolution"}[title]
                if mv:
                    res[key].append(mv)
            elif title == "Learnt from a pre-evolution":
                pre = pokemon_link(tds[0].find("a")) if tds[0].find("a") else None
                mv = mid(tds[1].find("a"))
                if mv:
                    res["prevo"].append({"from": pre, "move": mv, "method": text(tds[2]) if len(tds) > 2 else None})
            else:
                warn("unknown move section %r" % title)
                break
    return res


def parse_encounters(container):
    sec = h3_section(container, "Encounter rates")
    out = []
    if sec is None:
        return out
    tbl = sec.find("table")
    if tbl is None:
        return out
    for tr in tbl.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        area = href_tail(tds[0].find("a"), "area") or text(tds[0])
        method = text(tds[2])
        time = None
        m = re.fullmatch(r"(.*?)\s*\((morning|day|night)\)", method, re.I)
        if m:
            method, time = m.group(1), m.group(2).title()
        levels = text(tds[3]).replace("–", "-").replace(" ", "")
        rate = parse_rate(tds[4].get_text())
        if rate is None:
            warn("unparsed rate %r" % tds[4].get_text())
        out.append(OrderedDict([("area", area), ("areaName", text(tds[1]) or None),
                                ("method", method), ("levels", levels or None),
                                ("rate", rate), ("time", time)]))
    return out


def parse_special(container, code_to_id):
    sec = h3_section(container, "Special encounters")
    notes, detail = [], []
    if sec is None:
        return notes, detail
    tbl = sec.find("table")
    for tr in tbl.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue
        a = tds[0].find("a", href=re.compile(r"/area/"))
        place = text(tds[0])
        area = href_tail(a, "area") if a else None
        area_name = event = None
        m = re.fullmatch(r"\d+\s*\((.*?)(?:;\s*event\s*(\d+))?\)", place)
        if m:
            area_name, event = m.group(1), (int(m.group(2)) if m.group(2) else None)
        kind = text(tds[1]) or None
        lvl_txt = text(tds[2])
        level = to_int(lvl_txt)
        attrs = []
        moves = []
        for div in tds[3].find_all("div", recursive=False):
            b = div.find("b")
            if b is None:
                continue
            key = text(b)
            if key == "Moves":
                for ma in div.find_all("a", href=re.compile(r"/move/")):
                    code = href_tail(ma, "move")
                    if code in code_to_id:
                        moves.append(code_to_id[code])
                val = ", ".join(text(x) for x in div.find_all("a")) or text(div)[len(key):].lstrip(": ")
            else:
                val = text(div)
                val = val[len(key):].lstrip(" :") if val.startswith(key) else val
            attrs.append((key, val))
        d = OrderedDict()
        d["area"] = area
        d["areaName"] = area_name if area else place
        if event is not None:
            d["event"] = event
        d["type"] = kind
        d["level"] = level if level is not None else (None if lvl_txt in ("", "None") else lvl_txt)
        if moves:
            d["moves"] = moves
        if attrs:
            d["attributes"] = OrderedDict((k, v) for k, v in attrs if k != "Moves")
            if not d["attributes"]:
                del d["attributes"]
        detail.append(d)
        # human readable note
        where = (area_name or place) if area else place
        s = "%s%s" % ((kind or "encounter").capitalize(), (" (Lv. %s)" % d["level"]) if d["level"] is not None else "")
        s += " at %s" % where
        if area:
            s += " [area %s]" % area
        extra = ["%s: %s" % (k, v) for k, v in attrs]
        if extra:
            s += " - " + "; ".join(extra)
        notes.append(s)
    return notes, detail


def parse_form(container, header, ctx):
    f = OrderedDict()
    # sprite: first figure img with a data URI
    sprite = None
    for fig in container.find_all("figure"):
        img = fig.find("img")
        if img is not None and (img.get("src") or "").startswith("data:image"):
            sprite = img["src"]
        break
    # type table
    types = []
    abilities, hidden = [], None
    info = {}
    for tbl in container.find_all("table", class_="data-table"):
        firstth = tbl.find("th")
        lab = text(firstth)
        if lab == "Type" and not types:
            td = firstth.find_next_sibling("td")
            for icon in td.find_all(class_="type-icon"):
                t = type_name(text(icon))
                if t is None:
                    warn("%s: unknown type %r" % (ctx["label"], text(icon)))
                else:
                    types.append(t)
        elif lab == "Abilities" and not abilities:
            kind = None
            for tr in tbl.find_all("tr"):
                th = tr.find("th")
                if th is not None and text(th) in ("Regular Abilities", "Regular Ability", "Hidden Ability", "Hidden Abilities"):
                    kind = text(th)
                for a in tr.find_all("a", href=re.compile(r"/ability/")):
                    aid = href_tail(a, "ability")
                    if kind and kind.startswith("Hidden"):
                        if hidden is None:
                            hidden = aid
                        else:
                            warn("%s: more than one hidden ability" % ctx["label"])
                    else:
                        abilities.append(aid)
        elif not info and any(text(th) == "Catch Rate" for th in tbl.find_all("th")):
            info = th_map(tbl)
    held = []
    if "Wild held items" in info:
        td = info["Wild held items"]
        for a in td.find_all("a", href=re.compile(r"/item/")):
            nxt = a.next_sibling
            chance = None
            if nxt is not None and isinstance(nxt, str):
                m = re.match(r"\s*\((\d+(?:\.\d+)?)%\)", nxt)
                if m:
                    chance = float(m.group(1))
                    chance = int(chance) if chance.is_integer() else chance
            held.append(OrderedDict([("item", text(a)), ("id", href_tail(a, "item")), ("chance", chance)]))
    # stats
    stats, evs = OrderedDict(), OrderedDict()
    ssec = h3_section(container, "Stats")
    if ssec is not None:
        tbl = ssec.find("table")
        rows = tbl.find_all("tr")
        heads = [text(th) for th in rows[0].find_all("th")][1:]
        for tr in rows[1:]:
            lab = text(tr.find("th"))
            vals = [to_int(text(td)) for td in tr.find_all("td")]
            d = {}
            for h, v in zip(heads, vals):
                if h in STAT_KEYS:
                    d[STAT_KEYS[h]] = v
            if lab == "Base stats":
                stats = OrderedDict((k, d.get(k)) for k in STAT_ORDER)
            elif lab == "EV yields":
                evs = OrderedDict((k, d[k]) for k in STAT_ORDER if d.get(k))
    f["types"] = types
    f["abilities"] = abilities
    f["hiddenAbility"] = hidden
    f["stats"] = stats
    f["evYield"] = evs
    f["catchRate"] = to_int(text(info.get("Catch Rate")))
    f["heldItems"] = held
    f["height"] = text(info.get("Height")) or None
    f["weight"] = text(info.get("Weight")) or None
    f["dexEntry"] = text(info.get("Pokédex entry")) or None
    mm = info.get("Mega Evolution method")
    f["megaMethod"] = clean(mm.get_text("")) if mm is not None else None
    f["megaStone"] = None
    if mm is not None and mm.find("a"):
        # the site links mega stones under /move/ by mistake; use the name
        f["megaStone"] = slugify(text(mm.find("a")))
    prev, nxt = parse_evo(container)
    f["_evo"] = (prev, nxt)
    f["moves"] = parse_moves(container, ctx["code_to_id"], ctx["unknown_moves"])
    f["locations"] = parse_encounters(container)
    f["special"], f["specialDetail"] = parse_special(container, ctx["code_to_id"])
    f["_sprite"] = sprite
    return f


def build_pokemon(site, code_to_id):
    entries = []
    link_map = {}   # (page, slug) -> id
    unknown_moves = Counter()
    for path in sorted(glob.glob(os.path.join(site, "pokemon", "*.html"))):
        page = os.path.basename(path)[:-5]
        soup = soup_of(path)
        h1 = soup.find("h1")
        sec = h1.parent
        species = text(h1)
        header = parse_header(sec)
        tabs = []
        ul = sec.find("ul", class_="tabs")
        if ul is not None:
            for a in ul.find_all("a", class_="tab"):
                slug = (a.get("data-tab-target") or "")[len("hash-tab-"):]
                tabs.append((slug, text(a)))
        forms = []
        if tabs:
            seen = set()
            targets = sec.find("div", class_="tab-targets")
            for i, (slug, fname) in enumerate(tabs):
                if slug in seen:
                    warn("%s: duplicate form tab %r (%s) skipped" % (page, slug, fname))
                    continue
                seen.add(slug)
                cont = targets.find("section", id="hash-tab-" + slug, recursive=False)
                if cont is None:
                    warn("%s: no section for tab %r" % (page, slug))
                    continue
                forms.append((i, slug, fname, cont))
        else:
            forms.append((0, "", None, sec))
        for i, slug, fname, cont in forms:
            if i == 0:
                pid = page
                form = None if (fname in (None, "Normal")) else fname
            else:
                pid = "%s-%s" % (page, slug)
                form = fname
            label = pid
            f = parse_form(cont, header, {"label": label, "code_to_id": code_to_id,
                                         "unknown_moves": unknown_moves})
            e = OrderedDict()
            e["id"] = pid
            e["base"] = page
            e["formSlug"] = slug
            e["form"] = form
            e["name"] = species
            e["dex"] = header["dex"]
            e["types"] = f["types"]
            e["abilities"] = f["abilities"]
            e["hiddenAbility"] = f["hiddenAbility"]
            e["stats"] = f["stats"]
            e["evYield"] = f["evYield"]
            e["catchRate"] = f["catchRate"]
            e["genderRatio"] = header["genderRatio"]
            e["growthRate"] = header["growthRate"]
            e["eggGroups"] = header["eggGroups"]
            e["heldItems"] = f["heldItems"]
            e["evolutions"] = []
            e["evolvesFrom"] = None
            e["moves"] = f["moves"]
            e["locations"] = f["locations"]
            e["special"] = f["special"]
            e["specialDetail"] = f["specialDetail"]
            e["category"] = header["category"]
            e["height"] = f["height"]
            e["weight"] = f["weight"]
            e["baseExp"] = header["baseExp"]
            e["eggSteps"] = header["eggSteps"]
            e["baseFriendship"] = header["baseFriendship"]
            e["dexEntry"] = f["dexEntry"]
            if f["megaMethod"]:
                e["megaMethod"] = f["megaMethod"]
                e["megaStone"] = f["megaStone"]
            e["_evo"] = f["_evo"]
            e["_sprite"] = f["_sprite"]
            entries.append(e)
            link_map[(page, slug)] = pid
            if i == 0:
                link_map[(page, "")] = pid
    # resolve evolution + prevo links
    aliases = {}
    for (page, slug), pid in link_map.items():
        alias = "%s-%s" % (page, slug) if slug else page
        if alias != pid:
            aliases[alias] = pid

    def resolve(link, who):
        if link is None:
            return None
        if link in link_map:
            return link_map[link]
        alt = (link[0], slugify(link[1]))   # some links use the form name
        if alt in link_map:
            return link_map[alt]
        if (link[0], "") in link_map:
            warn("%s: link %s#%s not a known form, using default form" % (who, link[0], link[1]))
            return link_map[(link[0], "")]
        warn("%s: unresolved pokemon link %s#%s" % (who, link[0], link[1]))
        return None

    for e in entries:
        prev, nxt = e.pop("_evo")
        e["evolvesFrom"] = resolve(prev, e["id"])
        e["evolutions"] = [OrderedDict([("to", resolve(t, e["id"])), ("method", m)]) for t, m in nxt]
        for p in e["moves"]["prevo"]:
            p["from"] = resolve(p["from"], e["id"])
        # drop empty optional move lists to keep the file small
        for k in ("evolution", "prevo"):
            if not e["moves"][k]:
                del e["moves"][k]
        if not e["specialDetail"]:
            del e["specialDetail"]
    entries.sort(key=lambda e: (e["dex"] if e["dex"] is not None else 10 ** 6))  # stable: forms stay in tab order
    return entries, aliases, unknown_moves


# ---------------------------------------------------------------- sprites

def is_pixel_doubled(im):
    w, h = im.size
    if w % 2 or h % 2:
        return False
    small = im.resize((w // 2, h // 2), Image.NEAREST)
    return small.resize((w, h), Image.NEAREST).tobytes() == im.tobytes()


def compress_sprite(uri, stats):
    m = re.match(r"data:image/(\w+);base64,(.*)$", uri, re.S)
    if not m:
        return uri
    raw = base64.b64decode(m.group(2))
    im = Image.open(io.BytesIO(raw))
    im.load()
    im = im.convert("RGBA")
    # most sprites are 96px art pixel-doubled to 192px: halve those losslessly
    if is_pixel_doubled(im):
        im = im.resize((im.size[0] // 2, im.size[1] // 2), Image.NEAREST)
        stats["halved"] += 1
    else:
        stats["kept_size"] += 1
    # lossless only: pick the smaller of optimized PNG and lossless WebP
    cands = []
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    cands.append(("png", buf.getvalue()))
    buf = io.BytesIO()
    im.save(buf, "WEBP", lossless=True, quality=100, method=6)
    cands.append(("webp", buf.getvalue()))
    fmt, data = min(cands, key=lambda c: len(c[1]))
    stats[fmt] += 1
    return "data:image/%s;base64,%s" % (fmt, base64.b64encode(data).decode())


# ---------------------------------------------------------------- PBS cross-check

def pbs_crosscheck(pbs_dir, pokemon):
    """Compare base stats / types of default forms against PBS pokemon.txt
    (possibly an older game version). Only reports, never changes data."""
    path = os.path.join(pbs_dir, "pokemon.txt")
    if not os.path.exists(path):
        return ["pokemon.txt not found"]
    recs, cur = {}, None
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.rstrip("\n")
            m = re.match(r"\[(\d+)\]", line)
            if m:
                cur = recs.setdefault(int(m.group(1)), {})
                continue
            if "=" in line and cur is not None:
                k, v = line.split("=", 1)
                cur[k] = v
    out, missing = [], []
    by_dex = {}
    for p in pokemon:
        if p["formSlug"] in ("", "normal") or p["id"] == p["base"]:
            by_dex.setdefault(p["dex"], p)
    for dex, r in recs.items():
        p = by_dex.get(dex)
        if p is None:
            missing.append("#%d %s" % (dex, r.get("Name")))
            continue
        bs = [int(x) for x in r.get("BaseStats", "").split(",") if x.strip()]
        if len(bs) == 6:
            pb = {"hp": bs[0], "atk": bs[1], "def": bs[2], "spe": bs[3], "spa": bs[4], "spd": bs[5]}
            if any(p["stats"].get(k) != v for k, v in pb.items()):
                out.append("%s: base stats site %s vs PBS %s" % (p["id"], [p["stats"].get(k) for k in STAT_ORDER], [pb[k] for k in STAT_ORDER]))
        ts = [TYPE_BY_UPPER.get(r.get(k, "")) for k in ("Type1", "Type2") if r.get(k)]
        ts = list(OrderedDict.fromkeys(ts))
        if ts and ts != p["types"]:
            out.append("%s: types site %s vs PBS %s" % (p["id"], p["types"], ts))
    if missing:
        out.append("%d PBS species have no site entry: %s ... %s" % (len(missing), missing[0], missing[-1]))
    return out


# ---------------------------------------------------------------- main

def dump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    return os.path.getsize(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True, help="reborn-db-site clone")
    ap.add_argument("--pbs", required=True, help="reborn-db PBS directory")
    ap.add_argument("--out", required=True, help="output dir (app/data)")
    ap.add_argument("--no-sprites", action="store_true", help="skip sprites.json")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    print("types ...", flush=True)
    types, type_diffs, type_skipped = build_types(args.pbs)
    print("moves ...", flush=True)
    moves, code_to_id = build_moves(args.site)
    print("abilities ...", flush=True)
    abilities = build_abilities(args.site)
    print("items ...", flush=True)
    items = build_items(args.site, code_to_id)
    # moves.tm from TM/TMX/HM items
    move_by_id = {m["id"]: m for m in moves}
    for it in items:
        if it["move"] and re.fullmatch(r"(tm|tmx|hm)\d+", it["id"]):
            mv = move_by_id[it["move"]]
            label = it["name"]
            if mv["tm"] and mv["tm"] != label:
                mv.setdefault("tmAlso", []).append(label)
                warn("move %s taught by both %s and %s" % (mv["id"], mv["tm"], label))
            else:
                mv["tm"] = label
    print("pokemon ...", flush=True)
    pokemon, aliases, unknown_moves = build_pokemon(args.site, code_to_id)

    sprites = {}
    sstats = Counter()
    for p in pokemon:
        uri = p.pop("_sprite")
        if uri and not args.no_sprites:
            sprites[p["id"]] = compress_sprite(uri, sstats)
        elif not uri:
            warn("%s: no sprite" % p["id"])

    # mega "stone" that is really a move (Rayquaza: Dragon Ascent)
    item_ids = {i["id"] for i in items}
    for p in pokemon:
        if p.get("megaStone") and p["megaStone"] not in item_ids and p["megaStone"] in move_by_id:
            p["megaMove"] = p.pop("megaStone")

    # ------------------------------------------------ validation
    errors = []
    ids = Counter(p["id"] for p in pokemon)
    errors += ["duplicate pokemon id %s" % k for k, n in ids.items() if n > 1]
    ab_ids = {a["id"] for a in abilities}
    mv_ids = set(move_by_id)
    it_ids = {i["id"] for i in items}
    pk_ids = set(ids)
    for p in pokemon:
        if not p["types"] or any(t not in TYPES for t in p["types"]):
            errors.append("%s: bad types %s" % (p["id"], p["types"]))
        for a in p["abilities"] + ([p["hiddenAbility"]] if p["hiddenAbility"] else []):
            if a not in ab_ids:
                errors.append("%s: unknown ability %s" % (p["id"], a))
        if not p["abilities"]:
            errors.append("%s: no abilities" % p["id"])
        if len(p["stats"]) != 6 or any(v is None for v in p["stats"].values()):
            errors.append("%s: incomplete stats %s" % (p["id"], p["stats"]))
        if p["dex"] is None:
            errors.append("%s: no dex number" % p["id"])
        mvs = p["moves"]
        refs = [x["move"] for x in mvs["level"]] + [x["move"] for x in mvs["tm"]] + mvs["tutor"] + mvs["egg"] \
            + mvs.get("evolution", []) + [x["move"] for x in mvs.get("prevo", [])]
        for r in refs:
            if r not in mv_ids:
                errors.append("%s: unknown move %s" % (p["id"], r))
        for h in p["heldItems"]:
            if h["id"] not in it_ids:
                errors.append("%s: unknown held item %s" % (p["id"], h["id"]))
        for ev in p["evolutions"]:
            if ev["to"] not in pk_ids:
                errors.append("%s: evolution to unknown %s" % (p["id"], ev["to"]))
        if p.get("megaStone") and p["megaStone"] not in it_ids:
            errors.append("%s: unknown mega stone %s" % (p["id"], p["megaStone"]))
        if p["catchRate"] is None:
            errors.append("%s: no catch rate" % p["id"])
        if p["evolvesFrom"] is not None and p["evolvesFrom"] not in pk_ids:
            errors.append("%s: evolvesFrom unknown %s" % (p["id"], p["evolvesFrom"]))
        for loc in p["locations"]:
            if not re.fullmatch(r"\d+", loc["area"] or ""):
                errors.append("%s: non-numeric area %r" % (p["id"], loc["area"]))
    for m in moves:
        if m["type"] not in TYPES and m["type"] != "???":
            warn("move %s has non-standard type %r" % (m["id"], m["type"]))
    for it in items:
        if it["move"] and it["move"] not in mv_ids:
            errors.append("item %s: unknown move %s" % (it["id"], it["move"]))
    for code, n in unknown_moves.items():
        errors.append("move code %s referenced %d times but has no move page" % (code, n))

    crosscheck = pbs_crosscheck(args.pbs, pokemon)

    meta = OrderedDict()
    meta["game"] = "Pokémon Reborn"
    meta["version"] = "19.16"
    meta["generated"] = datetime.date.today().isoformat()
    meta["sources"] = ["https://github.com/Andrew-Foote/reborn-db-site",
                       "https://github.com/Andrew-Foote/reborn-db (PBS/types.txt)"]
    meta["counts"] = OrderedDict([("pokemon", len(pokemon)), ("species", len({p["base"] for p in pokemon})),
                                  ("moves", len(moves)), ("abilities", len(abilities)),
                                  ("items", len(items)), ("sprites", len(sprites))])
    meta["pokemonAliases"] = OrderedDict(sorted(aliases.items()))
    meta["notes"] = [
        "pokemon ids: first form tab of a page uses the page slug; other forms are <page>-<form-slug>. "
        "pokemonAliases maps site links like 'muk-normal' or 'aegislash-shield' to the entry id.",
        "move ids are kebab slugs of the move name (site convention for items/abilities); "
        "moves[].code is the site's move file name (e.g. CALMMIND).",
        "locations[].rate is a percentage; the site's fractional rates are rounded to 2 decimals.",
    ]
    if type_diffs:
        meta["typeChartDifferences"] = type_diffs

    sizes = OrderedDict()
    sizes["types.json"] = dump(os.path.join(args.out, "types.json"), types)
    sizes["moves.json"] = dump(os.path.join(args.out, "moves.json"), moves)
    sizes["abilities.json"] = dump(os.path.join(args.out, "abilities.json"), abilities)
    sizes["items.json"] = dump(os.path.join(args.out, "items.json"), items)
    sizes["pokemon.json"] = dump(os.path.join(args.out, "pokemon.json"), pokemon)
    if not args.no_sprites:
        sizes["sprites.json"] = dump(os.path.join(args.out, "sprites.json"), sprites)
    sizes["meta.json"] = dump(os.path.join(args.out, "meta.json"), meta)

    # ------------------------------------------------ summary
    print("\n== summary")
    for k, v in sizes.items():
        print("  %-15s %8.1f KB" % (k, v / 1024))
    for k, v in meta["counts"].items():
        print("  %-10s %d" % (k, v))
    print("  sprite encodings: %s" % dict(sstats))
    print("  forms: %d entries with form != null" % sum(1 for p in pokemon if p["form"]))
    print("  with wild locations: %d, with special encounters: %d" % (
        sum(1 for p in pokemon if p["locations"]), sum(1 for p in pokemon if p["special"])))
    print("  methods: %s" % dict(Counter(l["method"] for p in pokemon for l in p["locations"])))
    print("\n== type chart vs standard Gen 6+ chart: %s" % ("identical" if not type_diffs else "%d differences" % len(type_diffs)))
    for d in type_diffs:
        print("  " + d)
    if type_skipped:
        print("  (non-standard types in types.txt not in the 18-type chart: %s)" % type_skipped)
    print("\n== PBS cross-check (PBS may be an older version): %d differences" % len(crosscheck))
    for d in crosscheck[:40]:
        print("  " + d)
    if len(crosscheck) > 40:
        print("  ... %d more" % (len(crosscheck) - 40))
    print("\n== warnings: %d" % len(WARN))
    for w in WARN[:60]:
        print("  " + w)
    if len(WARN) > 60:
        print("  ... %d more" % (len(WARN) - 60))
    print("\n== validation errors: %d" % len(errors))
    for e in errors[:80]:
        print("  " + e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
