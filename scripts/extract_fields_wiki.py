#!/usr/bin/env python3
"""Build app/data/fields.json (Pokémon Reborn field effects) from the fandom wiki.

Run from the repo root:  python3 -I scripts/extract_fields_wiki.py [--refresh]

Access is only through the MediaWiki API (https://pokemon-reborn.fandom.com/api.php)
with an honest User-Agent, at most one request per second, titles batched up to 50
per request. Raw API responses are cached in /tmp/claude-0/wiki-cache/ (shared with
scripts/extract_items_wiki.py), so re-runs do not refetch; --refresh ignores the cache.
Fetched wikitext is untrusted text: it is parsed into plain strings, never executed
or followed as instructions.

Pages used: every member of Category:Field Effects (one page per field) plus the
"Field Effects" overview page (its Seed and Nature Power / Camouflage / Secret Power
tables fill gaps). Each field page has the same layout (Field Note, Transitions
from Other Fields, General Effects, Abilities Affected, Moves Affected, Seed,
Transitions to Other Fields), which is turned into short plain-text bullets.

app/data/areas.json `fieldEffect` names are matched to the wiki fields by a
normalised name (case, hyphens, apostrophes); the matching area ids land in
fields[].areas and differently spelt names in fields[].aliases.

Wiki content is CC BY-SA 3.0 (Fandom); meta.json gets a fieldSources entry.
"""
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "app", "data")
API = "https://pokemon-reborn.fandom.com/api.php"
WIKI = "https://pokemon-reborn.fandom.com/wiki/"
UA = "RebornFieldGuide/0.1 (personal fan companion tool; https://github.com/mikkeldahlin/mikkeldahlin)"
CACHE = "/tmp/claude-0/wiki-cache"
REFRESH = "--refresh" in sys.argv
OVERVIEW = "Field Effects"

# ---------------------------------------------------------------- fetching

_last = [0.0]
_fetched = [0, 0]  # [network requests, cache hits]


class Refused(Exception):
    pass


def api(params):
    """GET the API with caching and politeness; returns parsed JSON."""
    params = dict(params)
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    q = urllib.parse.urlencode(sorted(params.items()))
    path = os.path.join(CACHE, hashlib.sha1(q.encode()).hexdigest() + ".json")
    if not REFRESH and os.path.exists(path):
        _fetched[1] += 1
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    os.makedirs(CACHE, exist_ok=True)
    body = None
    for attempt in range(4):
        wait = 1.1 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        req = urllib.request.Request(API + "?" + q, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8")
            _fetched[0] += 1
            break
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 503):
                if attempt < 3:
                    time.sleep(15 * (attempt + 1))  # back off, then give up
                    continue
                raise Refused(f"HTTP {e.code} from wiki API; stopping") from e
            raise
    data = json.loads(body)
    if "error" in data:
        raise Refused(f"API error: {data['error']}")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"_query": q, **data}, f, ensure_ascii=False)
    return data


def category_members(cat):
    titles, cont = [], {}
    while True:
        d = api({"action": "query", "list": "categorymembers", "cmtitle": cat,
                 "cmlimit": "500", "cmnamespace": "0", **cont})
        titles += [p["title"] for p in d["query"]["categorymembers"]]
        if "continue" not in d:
            return titles
        cont = dict(d["continue"])


def fetch_pages(titles):
    pages = {}
    for i in range(0, len(titles), 50):
        d = api({"action": "query", "prop": "revisions|categories",
                 "rvprop": "content|timestamp", "rvslots": "main", "cllimit": "max",
                 "titles": "|".join(titles[i:i + 50])})
        for p in d["query"]["pages"]:
            rev = (p.get("revisions") or [{}])[0]
            pages[p["title"]] = {
                "text": rev.get("slots", {}).get("main", {}).get("content", ""),
                "ts": rev.get("timestamp"),
                "cats": [c["title"] for c in p.get("categories", [])],
            }
    return pages


# ---------------------------------------------------------------- wikitext helpers

SIMPLE_TPL = {"m", "a", "i", "p", "type", "item", "pokemon", "pokémon", "tt", "bigic"}


def clean(s):
    """Wikitext -> plain text (templates reduced to their display text)."""
    if not s:
        return ""
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"\[\[\s*(?:File|Image):[^\[\]]*(?:\[\[[^\]]*\]\][^\[\]]*)*\]\]", "", s, flags=re.I)
    for _ in range(20):
        m = re.search(r"\{\{([^{}]*)\}\}", s)
        if not m:
            break
        parts = m.group(1).split("|")
        name = parts[0].strip().lower().replace("_", " ")
        if name == "color" and len(parts) >= 3:
            rep = parts[2]
        elif name in SIMPLE_TPL and len(parts) >= 2:
            rep = parts[1]
        else:
            rep = ""
        s = s[:m.start()] + rep + s[m.end():]
    s = re.sub(r"\[\[([^\]|]*)\|([^\]]*)\]\]", r"\2", s)
    s = re.sub(r"\[\[([^\]]*)\]\]", lambda m: m.group(1).lstrip("#"), s)
    s = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", s)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("'''", "").replace("''", "")
    s = s.replace("’", "'")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,.;:])", r"\1", s)
    return s


def tpl_args(s, names):
    """First positional arg of every {{name|arg}} in s, in order."""
    out = []
    for m in re.finditer(r"\{\{\s*([^{}|]+?)\s*\|([^{}|]*)", s or ""):
        if m.group(1).strip().lower() in names:
            v = clean(m.group(2))
            if v and v not in out:
                out.append(v)
    return out


def sections(text, level):
    """[(heading, body)] split at headings of exactly `level`; the first entry
    (heading None) is the text before the first such heading."""
    pat = re.compile(r"^(={%d})\s*([^=].*?)\s*\1\s*$" % level, re.M)
    out, pos, head = [], 0, None
    for m in pat.finditer(text):
        out.append((head, text[pos:m.start()]))
        head, pos = clean(m.group(2)), m.end()
    out.append((head, text[pos:]))
    return out


def chart_moves(block):
    names = []
    for m in re.finditer(r"\{\{\s*FieldChartEntry\s*\|([^{}]*)\}\}", block):
        mv = re.search(r"(?:^|\|)\s*move\s*=\s*([^|]*)", m.group(1))
        if mv:
            n = clean(mv.group(1))
            if n and n not in names:
                names.append(n)
    return names


def items(body):
    """Split a section body into units:
    {"kind": "bullet"|"prose"|"moves"|"table", "raw": wikitext, "depth": n,
     "moves": [names from a FieldMoves chart attached to it], "rows": [...]}."""
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = re.sub(r"<gallery.*?</gallery>", "", body, flags=re.S | re.I)
    body = re.sub(r"\[\[\s*(?:File|Image):[^\]]*\]\]", "", body, flags=re.I)
    # drop collapsible spoiler/guide blocks' markup but keep their text lines
    lines = body.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i].rstrip()
        s = line.strip()
        if re.match(r"\{\{\s*FieldMoves", s, re.I):
            j, closes, buf = i + 1, 0, []
            while j < len(lines) and closes < 2:
                if lines[j].strip() == "|}":
                    closes += 1
                else:
                    buf.append(lines[j])
                j += 1
            mv = chart_moves("\n".join(buf))
            prev = out[-1] if out else None
            if prev and prev["kind"] in ("bullet", "prose") and not prev["moves"] \
                    and prev.get("open", True):
                prev["moves"] = mv
            else:
                out.append({"kind": "moves", "raw": "", "depth": 0, "moves": mv})
            i = j
            continue
        if s.startswith("{|"):
            j, buf = i + 1, [s]
            while j < len(lines) and lines[j].strip() != "|}":
                buf.append(lines[j])
                j += 1
            if "wikitable" in s:
                out.append({"kind": "table", "raw": "\n".join(buf), "depth": 0,
                            "moves": [], "rows": table_rows("\n".join(buf[1:]))})
            i = j + 1
            continue
        if s.startswith("*") or s.startswith("#"):
            depth = len(re.match(r"[*#]+", s).group(0))
            out.append({"kind": "bullet", "raw": s[depth:].strip(), "depth": depth,
                        "moves": []})
        elif s.startswith(":") and out:
            out[-1]["raw"] += " " + s.lstrip(":").strip()
        elif not s or re.fullmatch(r"(<[^>]*>\s*)+|<br\s*/?>|\{\{Field Effects Color[^}]*\}\}"
                                    r"|\[\[\s*Category:[^\]]*\]\]|\{\{[^{}|]*\}\}", s, re.I):
            if out:
                out[-1]["open"] = False if not s else out[-1].get("open", True)
            if not s and out and out[-1]["kind"] == "prose":
                out[-1]["closed"] = True
        else:
            prev = out[-1] if out else None
            if prev and prev["kind"] == "prose" and not prev.get("closed") and not prev["moves"]:
                prev["raw"] += " " + s
            else:
                out.append({"kind": "prose", "raw": s, "depth": 0, "moves": []})
        i += 1
    for it in out:
        it["text"] = clean(it["raw"])
    return [it for it in out if it["text"] or it["moves"] or it.get("rows")]


def table_rows(body):
    rows = []
    for chunk in re.split(r"^\|-.*$", body, flags=re.M):
        cells = []
        for line in chunk.split("\n"):
            if line.startswith("!"):
                cells = None
                break
            if line.startswith("|") and not line.startswith("|+"):
                for c in line[1:].split("||"):
                    parts = c.split("|", 1)
                    if len(parts) == 2 and re.search(r"(style|scope|rowspan|colspan|align)\s*=", parts[0]):
                        c = parts[1]
                    cells.append(c)
            elif cells and line.strip():
                cells[-1] += "\n" + line
        if cells:
            rows.append(cells)
    return rows


def with_moves(text, moves):
    if not moves:
        return text
    t = text.rstrip()
    if t.endswith(":"):
        return t + " " + ", ".join(moves)
    return (t + " " if t else "") + "(" + ", ".join(moves) + ")" if t else ", ".join(moves)


# ---------------------------------------------------------------- field parsing

def slug(s):
    s = s.lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("é", "e"))


MULT = re.compile(r"(\d+(?:\.\d+)?)\s*[x×]|[x×]\s*(\d+(?:\.\d+)?)")


def mults(text):
    return [float(a or b) for a, b in MULT.findall(text)]


def boost_group(text, default):
    """Classify a power/damage statement as a boost or a weakening."""
    t = text.lower()
    ms = mults(t)
    weak = any(m < 1 for m in ms) or re.search(
        r"\b(weaken|weaker|halved|reduced? (?:base )?power|fail|lose|loses|no longer|cannot|can't|"
        r"is disabled|are disabled|prevent)", t)
    strong = any(m > 1 for m in ms) and re.search(r"power|damage|boost", t)
    if ms and all(m < 1 for m in ms) and re.search(r"power|damage", t):
        return "Weakened"
    if strong:
        return "Type & move boosts"
    if weak and re.search(r"-type moves|moves\b", t) and not re.search(r"pokémon (?:are|is) prevented", t):
        return "Weakened"
    return default


def heading_phrase(h):
    """'Moves that have 1.5x base power' -> '1.5x base power'."""
    h = re.sub(r"^moves\s+(?:that\s+)?(?:have\s+)?", "", h, flags=re.I).strip()
    return h[:1].upper() + h[1:] if h else h


def parse_effects(text, title):
    eff = []

    def add(group, t, sub=None):
        t = re.sub(r"\s+", " ", t).strip()
        if not t:
            return
        e = {"group": group, "text": t}
        if sub:
            e["section"] = sub
        if all(not (x["text"] == t and x["group"] == group) for x in eff):
            eff.append(e)

    secs = dict((h, b) for h, b in sections(text, 2) if h)
    order = [h for h, _ in sections(text, 2) if h]

    # extra mechanic sections before General Effects (Crystal Types, Stages, Chess Pieces)
    for h in order:
        if h in ("Field Note", "Transitions from Other Fields", "General Effects",
                 "Abilities Affected", "Moves Affected", "Seed", "Transitions to Other Fields",
                 "Trivia"):
            continue
        group = "Field changes" if h == "Stages" else "Other"
        for it in items(secs[h]):
            if it["kind"] == "table":
                for r in it["rows"]:
                    cells = [clean(c) for c in r]
                    if len(cells) >= 3:
                        add(group, f"{cells[0]} ({cells[1]}): {cells[2]}", h)
                    elif cells:
                        add(group, ": ".join(c for c in cells if c), h)
            else:
                add(group, with_moves(it["text"], it["moves"]), h)

    # General Effects (with optional ===subsections===)
    for sub, body in sections(secs.get("General Effects", ""), 3):
        for it in items(body):
            t = with_moves(it["text"], it["moves"])
            if sub and re.search(r"type chart|physical and special|inverted", sub, re.I):
                g = "Other"
            elif re.match(r"\s*\{\{\s*i\s*\|", it["raw"]):
                g = "Items"
            else:
                g = boost_group(t, "Other")
            add(g, t, sub)

    for it in items(secs.get("Abilities Affected", "")):
        add("Abilities", with_moves(it["text"], it["moves"]))

    for sub, body in sections(secs.get("Moves Affected", ""), 3):
        its = items(body)
        for it in its:
            if it["kind"] == "moves" or (it["moves"] and it["kind"] == "prose"
                                          and it["text"].endswith(":")):
                lead = it["text"].rstrip(":") if it["text"] else heading_phrase(sub or "")
                if not lead:
                    lead = "Affected moves"
                g = boost_group(lead, "Move effects") if mults(lead) else "Move effects"
                add(g, f"{lead}: {', '.join(it['moves'])}", sub)
            else:
                t = with_moves(it["text"], it["moves"])
                if sub and re.match(r"moves (?:that )?(?:have )?\d", sub, re.I):
                    g = boost_group(sub, "Type & move boosts")
                elif re.match(r"(the following moves|moves that) (?:now )?have [\d.]+x", t, re.I):
                    g = boost_group(t, "Type & move boosts")
                else:
                    g = "Move effects"
                add(g, t, sub if sub not in ("Other moves",) else None)
    return eff


def parse_transitions_to(body, field_titles, self_title):
    changes, terminated, notes = [], [], []
    for it in items(body):
        raw = it["raw"]
        txt = with_moves(it["text"], it["moves"])
        targets = []
        for m in re.finditer(r"\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]", raw):
            t = m.group(1).strip()
            k = field_titles.get(norm(t))
            if k and k != self_title and k not in targets:
                targets.append(k)
        moves = it["moves"] or tpl_args(re.split(r"\b(?:in the absence|unless|without|except)\b", raw)[0], {"m"})
        abil = tpl_args(raw, {"a"})
        if re.search(r"terminat", raw, re.I) and not targets:
            terminated += [m for m in moves if m not in terminated]
            notes.append(txt)
            continue
        if not targets:
            notes.append(txt)
            continue
        by = ", ".join(moves + abil) or txt
        for t in targets:
            changes.append({"to": slug(t), "name": t, "by": by, "text": txt})
    return changes, terminated, notes


def parse_transitions_from(body, field_titles, self_title):
    creation, creation_text, frm = [], [], []
    its = items(body)
    i = 0
    while i < len(its):
        it = its[i]
        group = [it]
        j = i + 1
        while j < len(its) and its[j]["kind"] == "bullet" and its[j]["depth"] > it["depth"]:
            group.append(its[j])
            j += 1
        raw = " ".join(g["raw"] for g in group)
        srcs = []
        for m in re.finditer(r"\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]", it["raw"]):
            k = field_titles.get(norm(m.group(1).strip()))
            if k and k != self_title and k not in srcs:
                srcs.append(k)
        txt = with_moves(it["text"], it["moves"])
        if srcs and not re.search(r"\bgenerated\b", it["raw"], re.I):
            for s in srcs:
                frm.append({"from": slug(s), "name": s, "text": txt})
        else:
            names = []
            for g in group:
                names += [n for n in (g["moves"] + tpl_args(g["raw"], {"m", "a"})) if n not in names]
            # held items that only extend duration / block generation are not creators
            creation += [n for n in names if n not in creation]
            if len(group) > 1:
                creation_text += [with_moves(g["text"], g["moves"]) for g in group[1:]]
            else:
                creation_text.append(txt)
        i = j
    return creation, creation_text, frm


def field_note(text):
    sec = dict((h, b) for h, b in sections(text, 2) if h).get("Field Note", "")
    out = {}
    for key, lab in (("location", "Location"), ("available", "Earliest available")):
        m = re.search(r"'''%s'''\s*:\s*(.*?)(?:<br\s*/?>|\n)" % re.escape(lab), sec)
        if m:
            out[key] = clean(m.group(1))
    return out


def overview_tables(text):
    """From the 'Field Effects' page: seed text and Nature Power/Camouflage/Secret Power per field."""
    seed, common = {}, {}
    for m in re.finditer(r"^\{\|(.*?)^\|\}", text, re.S | re.M):
        for r in table_rows(m.group(1)):
            cells = [c.strip() for c in r]
            # rows start with the field number, except rowspan continuation rows
            if len(cells) >= 2 and re.fullmatch(r"\d+|-", clean(cells[0])):
                cells = cells[1:]
            fm = re.search(r"\[\[([^\]|]+)", cells[0]) if cells else None
            if not fm:
                continue
            key = norm(fm.group(1))
            stage = clean(re.sub(r"\[\[[^\]]*\]\]", "", cells[0]))
            if len(cells) == 3 and "Seed" in cells[1]:
                seed[key] = {"seed": clean(cells[1]), "text": clean(cells[2])}
            elif len(cells) == 4:
                entry = {"naturePower": clean(cells[1]), "camouflage": clean(cells[2]),
                         "secretPower": clean(cells[3])}
                if stage:
                    entry["stage"] = stage
                common.setdefault(key, []).append(entry)
    return seed, common


def summary_for(title, text, cat, num, quote):
    intro = re.search(r"^'''%s'''.*$" % re.escape(title), text, re.M)
    parts = []
    kind = f"{cat} Field" if cat else "Field"
    lead = f"{'An' if kind[0] in 'AEIOU' else 'A'} {kind}"
    if num:
        lead += f" (Field Notes no. {num})"
    parts.append(lead + ".")
    if quote:
        parts.append(f"Battle message: “{quote}”")
    return " ".join(parts)


def parse_field(title, page, field_titles, ov_seed, ov_common):
    text = page["text"]
    quote = None
    q = re.search(r"<blockquote>(.*?)</blockquote>", text, re.S)
    if q:
        quote = clean(q.group(1))
    cat = num = None
    m = re.search(r"categorized as an? (\w+) Field", text)
    if m:
        cat = m.group(1)
    m = re.search(r"designated number (\d+)", text)
    if m:
        num = int(m.group(1))
    if not cat:
        for c in page["cats"]:
            n = c.split(":", 1)[-1]
            if n in ("Elemental", "Magical", "Synthetic", "Telluric"):
                cat = n
    secs = dict((h, b) for h, b in sections(text, 2) if h)

    effects = parse_effects(text, title)
    changes, terminated, to_notes = parse_transitions_to(
        secs.get("Transitions to Other Fields", ""), field_titles, title)
    for n in to_notes:
        effects.append({"group": "Field changes", "text": n})
    creation, creation_text, frm = parse_transitions_from(
        secs.get("Transitions from Other Fields", ""), field_titles, title)

    seed_items = items(secs.get("Seed", ""))
    seeds = " ".join(with_moves(i["text"], i["moves"]) for i in seed_items) or None
    ovs = ov_seed.get(norm(title))
    if not seeds and ovs:
        seeds = f"The {ovs['seed']} {ovs['text'][0].lower()}{ovs['text'][1:]}"
    seed_item = None
    m = re.search(r"\[\[((?:Elemental|Magical|Synthetic|Telluric) Seed)\]\]", secs.get("Seed", ""))
    if m:
        seed_item = m.group(1)
    elif ovs:
        seed_item = ovs["seed"]

    entry = {
        "id": slug(title), "name": title,
        "summary": summary_for(title, text, cat, num, quote),
        "category": cat, "number": num, "message": quote,
        "effects": effects,
        "seeds": seeds, "seedItem": seed_item,
        "changes": changes,
        "terminatedBy": terminated,
        "creation": creation,
        "creationNotes": creation_text,
        "from": frm,
        "fieldNote": field_note(text) or None,
        "common": ov_common.get(norm(title)),
        "areas": [],
        "source": title, "url": WIKI + urllib.parse.quote(title.replace(" ", "_"), safe="'()-_,"),
        "revision": page.get("ts"),
    }
    return entry


# ---------------------------------------------------------------- main

def main():
    members = sorted(set(category_members("Category:Field Effects")))
    if not members:
        raise SystemExit("Category:Field Effects returned no pages")
    pages = fetch_pages([OVERVIEW] + [t for t in members if t != OVERVIEW])
    ov = pages.get(OVERVIEW, {}).get("text", "")
    ov_seed, ov_common = overview_tables(ov)
    # fields listed in the overview's tables but missing from the category
    # (e.g. Mirror Arena has no category tag): fetch them too
    listed = []
    for m in re.finditer(r"^\{\|(.*?)^\|\}", ov, re.S | re.M):
        for lm in re.finditer(r"\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]", m.group(1)):
            t = lm.group(1).strip()
            if t not in pages and t not in listed and not t.startswith(("Category:", "File:")) \
                    and not re.search(r"Seed$|\(type\)", t):
                listed.append(t)
    if listed:
        pages.update(fetch_pages(listed))

    def is_field(t, p):
        return t != OVERVIEW and p["text"] and (
            "Category:Field Effects" in p["cats"]
            or re.search(r"is an? \[\[Field Effects\|Field\]\] in Pokémon Reborn", p["text"]))
    field_pages = {t: p for t, p in pages.items() if is_field(t, p)}
    missing_cat = sorted(t for t, p in field_pages.items() if "Category:Field Effects" not in p["cats"])
    if missing_cat:
        print(f"field pages without Category:Field Effects (found via overview): {missing_cat}")
    field_titles = {norm(t): t for t in field_pages}
    # links like [[Super-Heated Field|...]] may use other spellings: add simple aliases
    for t in list(field_pages):
        field_titles.setdefault(norm(t.replace(" Field", "")), t)

    fields = [parse_field(t, field_pages[t], field_titles, ov_seed, ov_common)
              for t in field_pages]
    fields.sort(key=lambda f: (f["number"] or 999, f["name"]))
    by_norm = {norm(f["name"]): f for f in fields}

    # areas.json fieldEffect names
    with open(os.path.join(DATA, "areas.json"), encoding="utf-8") as fh:
        areas = json.load(fh)
    unmatched = {}
    for a in areas:
        n = a.get("fieldEffect")
        if not n:
            continue
        f = by_norm.get(norm(n)) or by_norm.get(norm(field_titles.get(norm(n), "")))
        if not f:
            unmatched.setdefault(n, []).append(a["id"])
            continue
        f["areas"].append(a["id"])
        if n != f["name"]:
            f.setdefault("aliases", [])
            if n not in f["aliases"]:
                f["aliases"].append(n)
    for f in fields:
        f["areas"] = sorted(set(f["areas"]), key=lambda x: (len(x), x))
        for k in ("terminatedBy", "creation", "creationNotes", "from", "changes"):
            if not f[k]:
                f[k] = [] if k in ("changes", "creation") else None
        for k in [k for k, v in f.items() if v is None]:
            del f[k]

    out = os.path.join(DATA, "fields.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(fields, fh, ensure_ascii=False, separators=(",", ":"))

    mp = os.path.join(DATA, "meta.json")
    with open(mp, encoding="utf-8") as fh:
        meta = json.load(fh)
    revs = sorted(p["ts"] for p in pages.values() if p.get("ts"))
    meta["fieldSources"] = {
        "name": "Pokémon Reborn Wiki (Fandom)",
        "url": "https://pokemon-reborn.fandom.com/wiki/Field_Effects",
        "license": "CC BY-SA 3.0",
        "licenseUrl": "https://creativecommons.org/licenses/by-sa/3.0/",
        "attribution": "Field effect descriptions are adapted (condensed to plain-text bullets) from "
                       "the Pokémon Reborn Wiki on Fandom (pokemon-reborn.fandom.com), by its "
                       "contributors, under CC BY-SA 3.0. Each field names its wiki page in "
                       "'source' and 'url'.",
        "fetched": datetime.date.today().isoformat(),
        "latestRevision": revs[-1] if revs else None,
        "pages": len(field_pages) + (1 if ov else 0),
        "script": "scripts/extract_fields_wiki.py",
        "caveat": "The wiki is community-maintained and may describe a different game version "
                  "than the v19.16 game data; effects are taken as written.",
    }
    meta.setdefault("counts", {})["fields"] = len(fields)
    with open(mp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, separators=(",", ":"))

    groups = {}
    for f in fields:
        for e in f["effects"]:
            groups[e["group"]] = groups.get(e["group"], 0) + 1
    print(f"fields: {len(fields)}  effects: {sum(groups.values())}  groups: {groups}")
    print(f"requests: {_fetched[0]} network, {_fetched[1]} cached")
    print(f"area field names unmatched: {unmatched or 'none'}")
    unused = [f["name"] for f in fields if not f["areas"]]
    print(f"fields with no areas: {unused}")
    print(f"wrote {out} ({os.path.getsize(out)} bytes)")


if __name__ == "__main__":
    try:
        main()
    except Refused as e:
        raise SystemExit(f"wiki refused: {e}")
