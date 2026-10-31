#!/usr/bin/env python3
"""Fill items[].locations in app/data/items.json from the Pokémon Reborn fandom wiki.

Run from the repo root:  python3 -I scripts/extract_items_wiki.py [--refresh] [--report]

Access is only through the MediaWiki API (https://pokemon-reborn.fandom.com/api.php)
with an honest User-Agent, at most one request per second, titles batched 50 per
request. Every raw API response is cached in /tmp/claude-0/wiki-cache/ so re-runs
do not refetch (pass --refresh to ignore the cache). Fetched wikitext is treated as
untrusted text: it is parsed, never executed or followed as instructions.

Sources, in order of preference (structured first):
  1. {{Itemlist|<item>|<where>|display=...}} rows in the "Items" sections of
     location pages. One location row per bullet.
  2. {{Shop|<shop>}} / {{Shopitem|<item>|<price>|<currency>}} blocks on location
     pages ("Sold at ..." rows).
  3. "Item | Available | Location | Guide" tables on item pages and on the
     "... Locations" summary pages (TM Locations, Key Item Locations, Battle Item
     Locations, Mega Stone Locations, Z Crystal Locations, Memory Locations, Mint
     Locations, ...), the TM/Mega Stone/Z Crystal pages' '''Location''' /
     '''Available''' lines, Reborn Items' Location column and Fossil Pokemon's
     fossil table. When such a row repeats an Itemlist row (same item, same
     location page) it only adds its "available after ..." text to that row;
     otherwise it becomes its own row with a short excerpt of the wiki guide.
  4. The "Pickup Table" page is written to app/data/pickup.json.

Location names are mapped to areas.json ids by normalised name; when a name has
several areas the root-most one (preferring present-day Reborn over Neo Reborn
City) is used, and a floor prefix such as "B2F - ..." picks the matching child
area. Names that cannot be mapped confidently keep area = null.

Wiki content is CC BY-SA 3.0 (Fandom); meta.json gets an itemSources entry with
the attribution.
"""
import collections
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
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
REPORT = "--report" in sys.argv

# ---------------------------------------------------------------- fetching

_last = [0.0]


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


def all_titles():
    titles, cont = [], {}
    while True:
        d = api({"action": "query", "list": "allpages", "aplimit": "500",
                 "apfilterredir": "nonredirects", **cont})
        titles += [p["title"] for p in d["query"]["allpages"]]
        if "continue" not in d:
            return titles
        cont = {k: v for k, v in d["continue"].items()}


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

def find_templates(text, names):
    """Yield (start, end, name, params) for top-level-or-nested templates named in names."""
    names = {n.lower() for n in names}
    for m in re.finditer(r"\{\{\s*([^{}|\n]+?)\s*(?=\||\}\})", text):
        name = m.group(1).strip().replace("_", " ").lower()
        if name not in names:
            continue
        end = match_braces(text, m.start())
        if end < 0:
            continue
        inner = text[m.start() + 2:end - 2]
        yield m.start(), end, name, split_top(inner)[1:]


def match_braces(text, i):
    depth, j = 0, i
    while j < len(text):
        if text.startswith("{{", j):
            depth += 1
            j += 2
        elif text.startswith("}}", j):
            depth -= 1
            j += 2
            if depth == 0:
                return j
        else:
            j += 1
    return -1


def split_top(s, sep="|"):
    """Split on sep outside of {{ }} and [[ ]]."""
    out, cur, d1, d2, i = [], [], 0, 0, 0
    while i < len(s):
        two = s[i:i + 2]
        if two == "{{":
            d1 += 1; cur.append(two); i += 2; continue
        if two == "}}":
            d1 -= 1; cur.append(two); i += 2; continue
        if two == "[[":
            d2 += 1; cur.append(two); i += 2; continue
        if two == "]]":
            d2 -= 1; cur.append(two); i += 2; continue
        if s[i] == sep and d1 <= 0 and d2 <= 0:
            out.append("".join(cur)); cur = []; i += 1; continue
        cur.append(s[i]); i += 1
    out.append("".join(cur))
    return out


def params_dict(params):
    pos, named = [], {}
    for p in params:
        m = re.match(r"\s*([A-Za-z_][\w ]*?)\s*=(.*)$", p, re.S)
        if m and "[[" not in m.group(1):
            named[m.group(1).strip().lower()] = m.group(2).strip()
        else:
            pos.append(p.strip())
    return pos, named


SIMPLE_TPL = {"blink", "i", "m", "p", "a", "tt", "item", "pokémon", "pokemon", "type"}


def clean(s):
    """Wikitext -> plain text."""
    if not s:
        return ""
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    # files / images
    while True:
        m = re.search(r"\[\[\s*(File|Image):", s, re.I)
        if not m:
            break
        depth, j = 0, m.start()
        while j < len(s):
            if s.startswith("[[", j):
                depth += 1; j += 2
            elif s.startswith("]]", j):
                depth -= 1; j += 2
                if depth == 0:
                    break
            else:
                j += 1
        s = s[:m.start()] + s[j:]
    # templates, innermost first
    for _ in range(10):
        m = None
        for m in re.finditer(r"\{\{([^{}]*)\}\}", s):
            break
        if not m:
            break
        parts = m.group(1).split("|")
        name = parts[0].strip().lower().replace("_", " ")
        if name in ("pdollar",):
            rep = "$"
        elif name == "color" and len(parts) >= 3:
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
    s = re.sub(r"__\w+__", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def first_link(s):
    """(target, display) of the first [[link]] in s, or (None, clean(s))."""
    m = None
    for m in re.finditer(r"\[\[([^\]|]*)(?:\|([^\]]*))?\]\]", s or ""):
        if not re.match(r"\s*(File|Image):", m.group(1), re.I):
            break
    else:
        m = None
    if not m:
        return None, clean(s)
    return m.group(1).strip(), clean(m.group(2) if m.group(2) is not None else m.group(1))


def sections(text):
    """{heading: body} for every ==heading== (all levels); body runs to the next heading of the same or higher level."""
    heads = [(m.start(), m.end(), len(m.group(1)), m.group(2).strip())
             for m in re.finditer(r"^(={2,6})\s*(.*?)\s*\1\s*$", text, re.M)]
    out = {}
    for i, (s, e, lvl, h) in enumerate(heads):
        end = len(text)
        for s2, _, lvl2, _ in heads[i + 1:]:
            if lvl2 <= lvl:
                end = s2
                break
        out.setdefault(clean(h), text[e:end])
    return out


def excerpt(wikitext, limit=240):
    """Short prose excerpt: first sentences of cleaned text, skipping tables."""
    t = re.sub(r"\{\|.*?\n\|\}", " ", wikitext or "", flags=re.S)
    t = re.sub(r"^\s*=+.*?=+\s*$", " ", t, flags=re.M)
    t = clean(t)
    if len(t) <= limit:
        return t
    cut = t[:limit]
    dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if dot > limit * 0.4:
        return cut[:dot + 1]
    return cut.rsplit(" ", 1)[0] + "…"


def parse_tables(text):
    """Yield (headers, rows) for each {| wikitable |}; rows are lists of raw cell wikitext."""
    for m in re.finditer(r"^\{\|(.*?)^\|\}", text, re.S | re.M):
        body = m.group(1)
        chunks = re.split(r"^\|-.*$", body, flags=re.M)
        headers, rows = [], []
        for ci, chunk in enumerate(chunks):
            cells, is_head = [], False
            for line in chunk.split("\n"):
                if line.startswith("!"):
                    is_head = True
                    for c in re.split(r"!!", line[1:]):
                        cells.append(cell_content(c))
                elif line.startswith("|") and not line.startswith("|+"):
                    for c in line[1:].split("||"):
                        cells.append(cell_content(c))
                elif cells and line.strip():
                    cells[-1] += "\n" + line
            if is_head and not headers:
                headers = [clean(c) for c in cells]
            elif cells:
                rows.append(cells)
        yield headers, rows


def cell_content(c):
    parts = split_top(c, "|")
    if len(parts) >= 2 and re.search(r"(style|align|scope|class|colspan|rowspan|width)\s*=", parts[0]):
        return "|".join(parts[1:]).strip()
    return c.strip()


# ---------------------------------------------------------------- names

def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", s)


# wiki spelling -> items.json name, where the two differ
ITEM_ALIASES = {
    "alolaraichium z": "Aloraichium-Z",
    "solgalium z": "Solganium-Z",
}


class Items:
    def __init__(self, items):
        self.items = items
        self.by_norm = {}
        for it in items:
            self.by_norm.setdefault(norm(it["name"]), it)
            self.by_norm.setdefault(norm(it["name"].strip("'")), it)

    def find(self, name):
        if not name:
            return None
        name = re.sub(r"\s*[x×]\s*\d+\s*$", "", name.strip())
        name = ITEM_ALIASES.get(name.lower(), name)
        m = re.match(r"^(TMX?\d+|HM\d+)\b", name.strip(), re.I)
        if m:
            code = m.group(1).upper()
            if re.match(r"TM\d$", code):
                code = "TM0" + code[2:]
            return self.by_norm.get(norm(code))
        return self.by_norm.get(norm(name))


# wiki page / location text -> exact areas.json name (only where the wiki's name differs)
AREA_ALIASES = {
    "obsidia department store": "Department Store",
    "obsidia department store penthouse": "Department Store Penthouse",
    "celestine mountain": "Celestinine Mountain",
    "chrysolia sauna": "Chrysolia Spring Sauna",
    "glitch world": "Glitch City",  # Terra's gym inside the Agate Circus computer
}


class Areas:
    def __init__(self, areas):
        self.areas = [a for a in areas if a["name"] != "REMOVED"]
        self.byid = {a["id"]: a for a in areas}
        self.by_norm = collections.defaultdict(list)
        for a in self.areas:
            self.by_norm[norm(a["name"])].append(a)
        self.kids = collections.defaultdict(list)
        for a in areas:
            if a.get("parent"):
                self.kids[a["parent"]].append(a)

    def descendants(self, aid):
        out, stack = [], list(self.kids.get(aid, []))
        while stack:
            a = stack.pop()
            out.append(a)
            stack.extend(self.kids.get(a["id"], []))
        return out

    def candidates(self, name, prefix=True):
        n = name.strip()
        alias = AREA_ALIASES.get(n.lower())
        hit = list(self.by_norm.get(norm(alias or n), []))
        if prefix:
            # "Citrine Mountain" -> also its floors "Citrine Mountain 1F/2F/..." (pick() takes the root-most)
            pat = re.compile(re.escape(norm(alias or n)) + r"(b?\d+f|peak)$")
            hit += [a for a in self.areas if pat.match(norm(a["name"]))]
        if not hit:
            m = re.match(r"^(.*?)\s*\((.*)\)$", n)
            if m:
                a_c = self.candidates(m.group(1))
                b_area = self.pick(self.candidates(m.group(2)))
                if b_area:
                    desc = {d["id"] for d in self.descendants(b_area["id"])}
                    inside = [a for a in a_c if a["id"] in desc]
                    hit = inside or [b_area]
                else:
                    hit = a_c
        return hit

    def pick(self, cands):
        """Root-most area among same-named candidates; None if still ambiguous."""
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        ids = {a["id"] for a in cands}
        roots = [a for a in cands if a.get("parent") not in ids]
        # a root whose own ancestors contain another candidate is not a root either
        def anc(a):
            seen, p = set(), a.get("parent")
            while p and p not in seen:
                seen.add(p)
                yield p
                p = self.byid.get(p, {}).get("parent")
        roots = [a for a in roots if not any(p in ids for p in anc(a))]
        if len(roots) > 1:
            present = [a for a in roots if a.get("region") != "Neo Reborn City" and a.get("world") == "Reborn"]
            if present:
                roots = present
        if len(roots) > 1:
            mind = min(a.get("depth") or 0 for a in roots)
            roots = [a for a in roots if (a.get("depth") or 0) == mind]
        if len(roots) > 1:
            # several same-named sibling maps (e.g. parts of one route): take the one
            # with the most descendants, then the lowest id
            roots.sort(key=lambda a: (-len(self.descendants(a["id"])), int(a["id"])))
        return roots[0]

    def resolve(self, page, text=None, floor=None):
        """Map (wiki location page, displayed location text, optional floor/sub-place) -> area id or None."""
        base = self.pick(self.candidates(page)) if page else None
        if not floor and text and page:
            fm = re.search(r"\(Floor (\d+)\)$", text)
            if fm:
                floor = f"{fm.group(1)}F"
            elif norm(text).startswith(norm(page)) and norm(text) != norm(page) and not self.candidates(text, prefix=False):
                rest = text.strip()[len(page.strip()):].strip()
                if rest and norm(page + rest) == norm(text):
                    floor = rest
        if floor and (base or page):
            pool = []
            for b in (self.candidates(page) if page else []):
                pool += [b] + self.descendants(b["id"])
            want = [norm(f"{page} {floor}")] + [norm(f"{b['name']} {floor}") for b in self.candidates(page)] + [norm(floor)]
            for w in want:
                hit = [a for a in pool if norm(a["name"]) == w]
                if hit:
                    return self.pick(hit)["id"]
            # "B2F Backroom" -> try just the leading floor token
            m = re.match(r"^(B?\d+F)\b", floor)
            if m and m.group(1) != floor:
                return self.resolve(page, text, m.group(1))
        if text and page and norm(text) != norm(page):
            hit = self.pick(self.candidates(text))
            if hit and base and (hit["id"] == base["id"] or hit in self.descendants(base["id"])):
                return hit["id"]
            if hit and not base:
                return hit["id"]
        if base:
            return base["id"]
        if text:
            hit = self.pick(self.candidates(text))
            if hit:
                return hit["id"]
        return None


# ---------------------------------------------------------------- extraction

FLOOR_RE = re.compile(r"^\s*([A-Z0-9][^\[\]{}*|\n]{0,38}?)\s+-\s+(.*)$", re.S)
FLOOR_COMMA_RE = re.compile(r"^\s*(B?\d+F)\s*[,:]\s+(.*)$", re.S)
GUIDE_LINK_RE = re.compile(r"\[\[[^\]|]*\|\s*Guide\s*\]\]", re.I)


def split_bullets(desc):
    desc = desc.strip()
    if desc.startswith("*") or "\n*" in desc:
        out = [b.strip() for b in re.split(r"(?:^|\n)\s*\*+", desc) if b.strip()]
        return out
    return [desc] if desc else [""]


def itemlist_rows(title, text, items, stats):
    rows = []
    for _, _, _, params in find_templates(text, {"itemlist"}):
        pos, named = params_dict(params)
        if not pos:
            continue
        icon = clean(pos[0])
        desc = pos[1] if len(pos) > 1 else ""
        display = named.get("display")
        it = items.find(icon)
        if display is not None:
            dtext = re.sub(r"\s*[x×]\s*\d+\s*$", "", clean(display))
            dit = items.find(dtext)
            if dit:
                it = dit
            elif it is None or norm(dtext) != norm(it["name"]):
                # icon is reused for something that is not this item (stickers, field notes, "any mint")
                stats["itemlist_skipped"][f"{icon} / {dtext}"] += 1
                continue
        if it is None:
            stats["itemlist_unknown_item"][icon] += 1
            continue
        for b in split_bullets(desc):
            floor = None
            b = GUIDE_LINK_RE.sub("", b)
            m = FLOOR_RE.match(b) or FLOOR_COMMA_RE.match(b)
            if m and len(m.group(1)) <= 38:
                floor, b = m.group(1).strip(), m.group(2)
            note = clean(b)
            rows.append({"item": it["id"], "page": title, "floor": floor, "areaName":
                         f"{title} {floor}" if floor and re.match(r"^B?\d+F\b|^Peak$", floor) else
                         (f"{title} ({floor})" if floor else title),
                         "note": note, "source": title, "kind": "itemlist"})
    return rows


def shop_rows(title, text, items, stats):
    rows = []
    heads = [(m.start(), len(m.group(1)), clean(m.group(2)))
             for m in re.finditer(r"^(={2,6})\s*(.*?)\s*\1\s*$", text, re.M)]
    shops = list(find_templates(text, {"shop"}))
    for i, (s, e, _, params) in enumerate(shops):
        # a sub-heading such as "Floor 9: Evolution Stones" says where in the building the shop is
        label = floor = None
        above = [h for h in heads if h[0] < s]
        if above and above[-1][1] >= 3 and ":" in above[-1][2]:
            label = above[-1][2].split(":")[0].strip()
            fm = re.match(r"^Floor (\d+)$", label)
            floor = f"{fm.group(1)}F" if fm else ("Penthouse" if label.endswith("Penthouse") else None)
        end = shops[i + 1][0] if i + 1 < len(shops) else len(text)
        foot = re.search(r"\{\{\s*shopfooter", text[e:end], re.I)
        block = text[e:e + foot.start()] if foot else text[e:end]
        pos, _ = params_dict(params)
        sname = clean(pos[0].replace("\n", " ")) if pos else "Shop"
        m = re.match(r"^(.*?)\s*\((.*)\)\s*$", sname)
        shop, stage = (m.group(1), m.group(2)) if m else (sname, None)
        for _, _, _, sp in find_templates(block, {"shopitem"}):
            spos, _ = params_dict(sp)
            if not spos:
                continue
            it = items.find(clean(spos[0]))
            if not it:
                stats["shop_unknown_item"][clean(spos[0])] += 1
                continue
            price = clean(spos[1]) if len(spos) > 1 else ""
            cur = clean(spos[2]) if len(spos) > 2 else ""
            rows.append({"item": it["id"], "page": title, "shop": shop, "stage": stage,
                         "price": price, "currency": cur, "label": label, "floor": floor})
    # merge stock stages of the same shop
    merged = collections.OrderedDict()
    for r in rows:
        k = (r["item"], r["shop"], r["price"], r["currency"], r["label"], r["floor"])
        merged.setdefault(k, []).append(r["stage"])
    out = []
    for (iid, shop, price, cur, label, floor), stages in merged.items():
        cost = ""
        if price:
            cost = (f" for ${price}" if cur.lower() in ("dollars", "dollar", "") else f" for {price} {cur}")
        st = [s for s in dict.fromkeys(stages) if s]
        where = f"{shop}, {label}" if label else shop
        note = f"Sold at {where}{cost}" + (f" ({'; '.join(st)})" if st else "")
        area_name = f"{title} {floor}" if floor else title
        out.append({"item": iid, "page": title, "floor": floor, "areaName": area_name,
                    "note": note, "source": title, "kind": "shop"})
    return out


ITEM_HEADS = {"item", "tm", "mega stone", "memory", "z crystal", "fossil"}


def guide_text(pages, here, link):
    """Excerpt of the guide section a Guide-column link points at."""
    if not link:
        return ""
    target, _ = first_link(link)
    if target is None:
        return ""
    page, _, anchor = target.partition("#")
    page = page.strip() or here
    p = pages.get(page)
    if not p:
        return ""
    secs = sections(p["text"])
    if anchor:
        body = secs.get(clean(anchor.replace("_", " ")))
        if body is None:
            return ""
        return excerpt(body)
    body = secs.get("Guide")
    return excerpt(body) if body else ""


def table_rows(pages, items, stats):
    rows = []
    for title, p in pages.items():
        text = p["text"]
        # 1) Item / Available / Location / Guide style tables
        for headers, trows in parse_tables(text):
            hl = [h.lower() for h in headers]
            if not hl or "location" not in hl:
                continue
            if hl[0] not in ITEM_HEADS:
                continue
            if "available" not in hl and not (hl[0] == "item" and "function" in hl):
                continue
            iloc = hl.index("location")
            iav = hl.index("available") if "available" in hl else None
            ig = hl.index("guide") if "guide" in hl else None
            for cells in trows:
                if len(cells) == len(headers) + 1 and not clean(cells[0]):
                    cells = cells[1:]  # icon column under a colspan=2 "Item" header
                if len(cells) <= iloc:
                    continue
                tgt, disp = first_link(cells[0])
                name = disp
                it = items.find(name) or (items.find(tgt.split("#")[0]) if tgt and tgt.split("#")[0] else None)
                if it is None and tgt and tgt.startswith("#"):
                    it = items.find(title) if items.find(name) is None and norm(name) == norm(title) else None
                if it is None:
                    stats["table_unknown_item"][f"{title}: {name}"] += 1
                    continue
                loc_raw = cells[iloc]
                ltgt, ldisp = first_link(loc_raw)
                if not ldisp or ldisp.lower() == "guide":
                    continue  # "[[Item|Guide]]": the location lives on the item's own page
                if ltgt and ltgt.split("#")[0].strip() == "Pickup Table":
                    lpage, ldisp = "Pickup Table", "Pickup"
                else:
                    lpage = (ltgt.split("#")[0].strip() if ltgt else ldisp)
                avail = clean(cells[iav]) if iav is not None and len(cells) > iav else ""
                guide = guide_text(pages, title, cells[ig]) if ig is not None and len(cells) > ig else ""
                rows.append({"item": it["id"], "page": lpage, "areaName": ldisp,
                             "available": avail, "guide": guide, "source": title,
                             "own": norm(title) == norm(it["name"]) or bool(re.match(r"^TMX?\d+ ", title))})
        # 2) single-item pages: '''Location''': [[X]]  '''Available''': ...
        loc = re.search(r"'''Location'''\s*:\s*(.+)", text)
        if loc and not text.lstrip().startswith("{|"):
            it = items.find(title)
            if it:
                av = re.search(r"'''Available'''\s*:\s*(.+)", text)
                secs = sections(text)
                guide = excerpt(secs.get("Guide", "")) if secs.get("Guide") else ""
                for part in re.split(r"\s*(?:,|<br\s*/?>|\band\b)\s*", loc.group(1)):
                    ltgt, ldisp = first_link(part)
                    if not ldisp:
                        continue
                    rows.append({"item": it["id"], "page": ltgt.split("#")[0].strip() if ltgt else ldisp,
                                 "areaName": ldisp, "available": clean(av.group(1)) if av else "",
                                 "guide": guide, "source": title, "own": True})
        # 3) {{Item template|obtained_from=...}}
        for _, _, _, params in find_templates(text, {"item template"}):
            _, named = params_dict(params)
            of = named.get("obtained_from")
            it = items.find(title)
            if of and it:
                ltgt, ldisp = first_link(of)
                rows.append({"item": it["id"], "page": ltgt.split("#")[0].strip() if ltgt else ldisp,
                             "areaName": clean(of), "available": "", "guide": "",
                             "source": title, "own": True})
    return rows


def avail_phrase(av):
    av = av.strip()
    if not av:
        return ""
    return av[0].lower() + av[1:] if re.match(r"^(After|During|Before)\b", av) else av


def build(pages, items, areas, stats):
    loc_rows = []
    for title, p in pages.items():
        if re.search(r"\{\{\s*itemlist", p["text"], re.I):
            loc_rows += itemlist_rows(title, p["text"], items, stats)
        if re.search(r"\{\{\s*shop\s*\|", p["text"], re.I):
            loc_rows += shop_rows(title, p["text"], items, stats)
    trows = table_rows(pages, items, stats)

    # dedupe table rows: same item + location page + availability; prefer the item's own page
    best = collections.OrderedDict()
    for r in sorted(trows, key=lambda r: (not r["own"], not r["guide"])):
        k = (r["item"], norm(r["page"]), norm(r["areaName"]), norm(r["available"]))
        best.setdefault(k, r)
    trows = list(best.values())

    by_item_page = collections.defaultdict(list)
    shops_by = collections.defaultdict(list)
    for r in loc_rows:
        if r["kind"] == "itemlist":
            by_item_page[(r["item"], norm(r["page"]))].append(r)
        else:
            shops_by[(r["item"], norm(r["page"]))].append(r)
    t_by = collections.defaultdict(list)
    for r in trows:
        t_by[(r["item"], norm(r["page"]))].append(r)

    extra = []
    for k, rs in t_by.items():
        targets = by_item_page.get(k)
        if targets:
            avs = {avail_phrase(r["available"]) for r in rs if r["available"]}
            if len(avs) == 1:
                av = avs.pop()
                for t in targets:
                    if not t["note"]:
                        g = [r["guide"] for r in rs if r["guide"]]
                        if g:
                            t["note"] = g[0]
                    if av and av.lower() not in t["note"].lower():
                        t["note"] = (t["note"].rstrip(". ") + f"; available {av}").lstrip("; ")
            stats["table_merged"] += len(rs)
            continue
        if k in shops_by:
            # a purchase the location page lists as a shop: fold the guide/availability into that row
            for r in rs:
                fl = norm(r["areaName"])[len(norm(r["page"])):]
                tg = [t for t in shops_by[k] if not fl or norm(t["floor"] or "") == fl] or shops_by[k]
                add = " ".join(b for b in [r["guide"], f"Available {avail_phrase(r['available'])}." if r["available"] else ""] if b)
                for t in tg:
                    if add and add not in t["note"]:
                        t["note"] = t["note"].rstrip(".") + ". " + add
            stats["table_merged"] += len(rs)
            continue
        for r in rs:
            bits = [r["guide"]] if r["guide"] else []
            if r["available"]:
                bits.append(f"Available {avail_phrase(r['available'])}.")
            extra.append({"item": r["item"], "page": r["page"], "floor": None,
                          "areaName": r["areaName"], "note": " ".join(bits).strip(),
                          "source": r["source"], "kind": "table"})
    stats["table_added"] = len(extra)

    out = collections.defaultdict(list)
    seen = set()
    for r in loc_rows + extra:
        page = r["page"]
        if page == "Pickup Table":
            aid = None
        else:
            aid = areas.resolve(page, r["areaName"], r.get("floor"))
        if aid is None:
            stats["unmapped_names"][r["areaName"] if page == r["areaName"] else f"{r['areaName']} [{page}]"] += 1
        row = {"area": aid, "areaName": r["areaName"], "note": r["note"], "source": r["source"]}
        key = (r["item"], aid, r["areaName"], r["note"], r["source"])
        if key in seen:
            continue
        seen.add(key)
        out[r["item"]].append(row)
    return out


def pickup_table(pages, items):
    p = pages.get("Pickup Table")
    if not p:
        return None
    for headers, rows in parse_tables(p["text"]):
        rates = [h.rstrip("%") for h in headers[1:]]
        if not rates or not all(re.match(r"^\d+$", r) for r in rates):
            continue
        levels = []
        raw = re.search(r"^\{\|.*?^\|\}", p["text"], re.S | re.M).group(0)
        # header cells "! 1-10" open each data row in this table
        for chunk in re.split(r"^\|-.*$", raw, flags=re.M)[1:]:
            lm = re.search(r"^!\s*([^\n|]+)$", chunk, re.M)
            if not lm:
                continue
            cells = [cell_content(l[1:]) for l in chunk.split("\n") if l.startswith("|") and not l.startswith("|}")]
            entries = []
            for rate, c in zip(rates, cells):
                name = clean(c)
                it = items.find(name)
                entries.append({"item": it["id"] if it else None, "name": name, "chance": int(rate)})
            levels.append({"levels": clean(lm.group(1)), "items": entries})
        intro = re.sub(r"\s*[^.]*:\s*$", "", excerpt(p["text"].split("{|")[0]))  # drop "The new table is as follows:"
        return {
            "source": "Pickup Table",
            "url": WIKI + "Pickup_Table",
            "revision": p["ts"],
            "license": "CC BY-SA 3.0 (Pokémon Reborn Wiki, Fandom)",
            "note": intro,
            "rates": [int(r) for r in rates],
            "levels": levels,
        }
    return None


# ---------------------------------------------------------------- main

def main():
    with open(os.path.join(DATA, "items.json"), encoding="utf-8") as f:
        items_list = json.load(f)
    with open(os.path.join(DATA, "areas.json"), encoding="utf-8") as f:
        areas_list = json.load(f)
    items, areas = Items(items_list), Areas(areas_list)

    try:
        titles = all_titles()
        pages = fetch_pages(titles)
    except Refused as e:
        sys.exit(f"stopped: {e}")

    stats = collections.defaultdict(collections.Counter)
    stats["table_merged"] = 0
    locs = build(pages, items, areas, stats)

    # TM numbers on the wiki vs the v19.16 move data
    tm_mismatch = []
    moves_path = os.path.join(DATA, "moves.json")
    move_names = {}
    if os.path.exists(moves_path):
        with open(moves_path, encoding="utf-8") as f:
            move_names = {m["id"]: m["name"] for m in json.load(f)}
    for t in titles:
        m = re.match(r"^(TMX?\d+) (.+)$", t)
        if m:
            it = items.find(m.group(1))
            if it and it.get("move") and norm(move_names.get(it["move"], it["move"])) != norm(m.group(2)):
                tm_mismatch.append(f"{t} (data: {move_names.get(it['move'], it['move'])})")

    for it in items_list:
        it["locations"] = locs.get(it["id"], [])
    with open(os.path.join(DATA, "items.json"), "w", encoding="utf-8") as f:
        json.dump(items_list, f, ensure_ascii=False, separators=(",", ":"))

    pick = pickup_table(pages, items)
    if pick:
        with open(os.path.join(DATA, "pickup.json"), "w", encoding="utf-8") as f:
            json.dump(pick, f, ensure_ascii=False, separators=(",", ":"))

    meta_path = os.path.join(DATA, "meta.json")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    newest = max((p["ts"] for p in pages.values() if p["ts"]), default=None)
    meta["itemSources"] = {
        "name": "Pokémon Reborn Wiki (Fandom)",
        "url": "https://pokemon-reborn.fandom.com/",
        "license": "CC BY-SA 3.0",
        "licenseUrl": "https://creativecommons.org/licenses/by-sa/3.0/",
        "attribution": "Item, TM and key item locations and the Pickup table are adapted from the Pokémon Reborn Wiki on Fandom (pokemon-reborn.fandom.com), by its contributors, under CC BY-SA 3.0. Each location row names the wiki page it came from in 'source'.",
        "fetched": time.strftime("%Y-%m-%d"),
        "latestRevision": newest,
        "pages": len(pages),
        "script": "scripts/extract_items_wiki.py",
        "caveat": "The wiki is community-maintained and may describe a different game version than the v19.16 game data; rows are taken as written.",
    }
    if tm_mismatch:
        meta["itemSources"]["tmMismatches"] = tm_mismatch
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))

    rows = [r for v in locs.values() for r in v]
    mapped = sum(1 for r in rows if r["area"])
    print(f"pages fetched: {len(pages)}")
    print(f"items with >=1 location: {sum(1 for it in items_list if it['locations'])} / {len(items_list)}")
    print(f"location rows: {len(rows)}  mapped: {mapped}  unmapped: {len(rows) - mapped}")
    print(f"table rows merged into itemlist rows: {stats['table_merged']}  added: {stats['table_added']}")
    if pick:
        print(f"pickup.json: {len(pick['levels'])} level bands")
    if tm_mismatch:
        print("TM page name vs data move mismatch:", "; ".join(tm_mismatch))
    print("top unmapped:", stats["unmapped_names"].most_common(40))
    if REPORT:
        for k in ("itemlist_unknown_item", "itemlist_skipped", "shop_unknown_item", "table_unknown_item"):
            print(f"{k}: {sum(stats[k].values())}", stats[k].most_common(60))


if __name__ == "__main__":
    main()
