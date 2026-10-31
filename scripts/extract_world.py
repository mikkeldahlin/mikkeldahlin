#!/usr/bin/env python3
"""Extract trainers.json and areas.json for the Reborn Field Guide.

Sources (treated as untrusted data, only parsed, never executed):
  --site  a clone of github.com/Andrew-Foote/reborn-db-site (trainer/*.html,
          trainers.html, area/*.html, areas.html)
  --pbs   the PBS folder of github.com/Andrew-Foote/reborn-db (trainertypes.txt
          and trainers.txt). Used only to split "Class Name" into class and
          name, and to read the section headings the game files group trainers
          under (Rivals, Major Battles, E4 / Champion, Postgame tiers, ...).

Usage (run extract_dex.py first: pokemon and move ids are taken from its
pokemon.json / moves.json so references match exactly):
  python3 -I scripts/extract_world.py --site /tmp/claude-0/src/reborn-db-site \
      --pbs /tmp/claude-0/src/reborn-db/PBS --out app/data [--dex app/data]

Output follows data/SCHEMA.md (areas.json, trainers.json), minified.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, OrderedDict, defaultdict
from fractions import Fraction

from bs4 import BeautifulSoup, NavigableString, Tag

DATA_URI = re.compile(r'data:image/[^"\']+')
WS = re.compile(r'\s+')


def soup_of(path):
    with open(path, encoding='utf-8') as fh:
        text = fh.read()
    # Drop the base64 sprites before parsing: faster and never stored.
    return BeautifulSoup(DATA_URI.sub('', text), 'lxml')


def clean(text):
    return WS.sub(' ', text or '').strip()


def href_slug(href, folder):
    """'/reborn-db-site/pokemon/geodude.html#alolan' -> ('geodude', 'alolan')."""
    if not href:
        return None, None
    m = re.search(r'/%s/([^/#?]+)\.html(?:#(.*))?$' % re.escape(folder), href)
    if not m:
        return None, None
    return m.group(1), (m.group(2) or None)


def area_id(href):
    slug, _ = href_slug(href, 'area')
    return slug


# ------------------------------------------------------- id resolving ----

class DexIds:
    """Map site links to the ids extract_dex.py wrote (pokemon.json, moves.json).

    Without those files, fall back to the site slugs: pokemon '<page>' or
    '<page>-<fragment>', moves the lowercased page name."""

    def __init__(self, dex_dir):
        self.move_by_code = {}
        self.pk = {}          # (page, formSlug) -> id
        self.pk_default = {}  # page -> id of its first (default) form
        self.misses = Counter()
        self.loaded = False
        mp = os.path.join(dex_dir, 'moves.json') if dex_dir else None
        pp = os.path.join(dex_dir, 'pokemon.json') if dex_dir else None
        if mp and pp and os.path.exists(mp) and os.path.exists(pp):
            with open(mp, encoding='utf-8') as fh:
                for m in json.load(fh):
                    if m.get('code'):
                        self.move_by_code[m['code']] = m['id']
            with open(pp, encoding='utf-8') as fh:
                for e in json.load(fh):
                    base = e.get('base') or e['id']
                    self.pk[(base, e.get('formSlug') or '')] = e['id']
                    if e['id'] == base or base not in self.pk_default:
                        self.pk_default[base] = e['id']
            self.loaded = True

    def move(self, href):
        code, _ = href_slug(href, 'move')
        if not code:
            return None
        if self.loaded:
            if code in self.move_by_code:
                return self.move_by_code[code]
            self.misses['move ' + code] += 1
        return code.lower()

    def pokemon(self, href):
        """-> (id, page, fragment)"""
        page, frag = href_slug(href, 'pokemon')
        if not page:
            return None, None, None
        frag = frag or ''
        if self.loaded:
            for key in ((page, frag), (page, slugify(frag))):
                if key in self.pk:
                    return self.pk[key], page, frag or None
            if not frag and page in self.pk_default:
                return self.pk_default[page], page, None
            if page in self.pk_default:
                self.misses['pokemon %s#%s -> default form' % (page, frag)] += 1
                return self.pk_default[page], page, frag or None
            self.misses['pokemon %s#%s' % (page, frag)] += 1
        return (page + '-' + frag if frag else page), page, frag or None


IDS = DexIds(None)


# ---------------------------------------------------------------- PBS ----

def read_trainertypes(pbs):
    types = {}
    with open(os.path.join(pbs, 'trainertypes.txt'), encoding='utf-8-sig') as fh:
        for line in fh:
            if line.startswith('#') or not line.strip():
                continue
            p = line.rstrip('\n').split(',')
            if len(p) < 3:
                continue
            types[p[1]] = {'class': p[2].strip(), 'music': (p[4] if len(p) > 4 else '').strip()}
    return types


SECTION_RE = re.compile(r'^#+\s*([A-Za-z][^#]*?)\s*#*\s*$')
TIER_RE = re.compile(r'^T\d')


def strip_comment(s):
    return re.split(r'\s+#', s, maxsplit=1)[0].strip()


def read_trainers_txt(pbs, ttypes):
    """Return trainer entries in file order with the section heading they sit under."""
    with open(os.path.join(pbs, 'trainers.txt'), encoding='utf-8-sig') as fh:
        lines = fh.read().split('\n')
    entries = []
    section = None
    tier = None
    in_header = True
    i = 0
    n = len(lines)

    def skip(i):
        while i < n and (not lines[i].strip() or lines[i].strip().startswith('#')):
            i += 1
        return i

    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith('#'):
            if in_header:  # the file opens with a commented list of categories
                if re.match(r'^#-+$', line):
                    in_header = False
                i += 1
                continue
            m = SECTION_RE.match(line)
            if m and not re.match(r'^[A-Z0-9]+,', m.group(1)):
                label = m.group(1).strip()
                if TIER_RE.match(label):
                    tier = label
                else:
                    section, tier = label, None
            i += 1
            continue
        in_header = False
        ttype = strip_comment(line)
        i = skip(i + 1)
        name_line = strip_comment(lines[i])
        i = skip(i + 1)
        count = int(lines[i].split(',')[0])
        i += 1
        k = 0
        while k < count and i < n:
            if lines[i].strip() and not lines[i].strip().startswith('#'):
                k += 1
            i += 1
        parts = name_line.split(',')
        entries.append({
            'type': ttype,
            'class': ttypes.get(ttype, {}).get('class'),
            'typeMusic': ttypes.get(ttype, {}).get('music'),
            'name': parts[0].strip(),
            'party': parts[1].strip() if len(parts) > 1 else None,
            'section': section,
            'tier': tier,
        })
    return entries


def slugify(s):
    s = s.lower().replace('?', '-qmark-').replace('é', 'e')
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


def align(site_list, entries):
    """Greedy in-order match of site trainer slugs to trainers.txt entries.

    The site lists trainers in trainers.txt order; a few file entries have
    no page (skipped). Match on slugify(class + ' ' + name)."""
    out = {}
    j = 0
    for slug in site_list:
        base = re.sub(r'-\d+$', '', slug)
        for jj in range(j, min(j + 80, len(entries))):
            e = entries[jj]
            if e['class'] is None:
                continue
            key = slugify(e['class'] + ' ' + e['name'])
            if key in (base, slug):
                out[slug] = e
                j = jj + 1
                break
    return out


# -------------------------------------------------------------- kinds ----

BOSS_MUSIC = {'Battle- Meteor Admin', 'Battle- Dramatic', 'Battle- ReverseGym',
              'Battle- Legendary', 'Battle- Umbral', 'Battle- Postgame',
              'RBY Battle- Champion'}
ELITE_MUSIC = {'Battle- Elite', 'Battle- Champion'}
BOSS_SECTIONS = {'Major Battles and Gym Leaders', 'Sidequest Fights',
                 'Postgame content', 'Tower Bosses', 'Boss Rush and Theme Teams',
                 'Developer Teams'}


def norm_music(m):
    return re.sub(r'\.ogg$', '', (m or '').strip())


def decide_kind(tclass, name, ptype, section, music, rival_types, elite_types, rival_names):
    """Return (kind, rule) from data on the site / in the game files."""
    if section is None and ptype == 'TEST':
        return 'regular', 'test trainer'
    if tclass and re.match(r'^(Anomaly|Anomalized|Umbral)\b', tclass):
        return 'boss', 'class Anomaly/Umbral'
    if ptype in rival_types:
        return 'rival', 'trainer type used in the Rivals section'
    if (tclass, name) in rival_names:
        return 'rival', 'class+name used in the Rivals section'
    if ptype in elite_types:
        return 'elite', 'trainer type used in the E4 / Champion section'
    if music in ELITE_MUSIC:
        return 'elite', 'class battle music ' + music
    if music == 'Battle- Gym':
        return 'gym', 'class battle music Battle- Gym'
    if music in BOSS_MUSIC:
        return 'boss', 'class battle music ' + music
    if section in BOSS_SECTIONS:
        return 'boss', 'section ' + section
    return 'regular', 'default'


# ----------------------------------------------------------- trainers ----

STAT_KEYS = {'hp': 'hp', 'attack': 'atk', 'defense': 'def', 'speed': 'spe',
             'special attack': 'spa', 'special defense': 'spd',
             'atk': 'atk', 'def': 'def', 'spe': 'spe', 'spa': 'spa', 'spd': 'spd'}


def int_or_none(s):
    s = clean(s)
    return int(s) if re.fullmatch(r'-?\d+', s) else None


def parse_header(s):
    info = {}
    table = s.find('table', class_='data-table')
    if not table:
        return info
    for th in table.find_all('th'):
        td = th.find_next_sibling('td')
        if td is not None:
            info[clean(th.get_text(' '))] = td
    return info


def parse_dialogue(td):
    pre = td.find('pre')
    if not pre:
        return []
    segs = []
    cur = None
    for node in pre.children:
        if isinstance(node, Tag) and node.name == 'i':
            cur = {'when': clean(node.get_text()).strip('()'), 'text': ''}
            segs.append(cur)
        else:
            txt = node if isinstance(node, NavigableString) else node.get_text()
            if cur is None:
                cur = {'when': None, 'text': ''}
                segs.append(cur)
            cur['text'] += str(txt)
    out = []
    for sg in segs:
        t = sg['text'].strip()
        if sg['when'] == 'none' or not t:
            continue
        out.append({'when': sg['when'], 'text': t})
    return out


def parse_locations(s, problems, slug):
    h = s.find('h2', string=re.compile(r'^\s*Locations\s*$'))
    if not h:
        return [], None
    table = h.parent.find('table')
    if not table:
        p = h.parent.find('p')
        return [], clean(p.get_text()) if p else None
    locs = []
    seen = set()
    for tr in table.find_all('tr')[1:]:
        tds = tr.find_all('td', recursive=False)
        if len(tds) < 4:
            problems.append('%s: odd location row' % slug)
            continue
        a = tds[0].find('a')
        area = area_id(a['href']) if a else None
        type_td = tds[2]
        type_text = clean(type_td.get_text(' '))
        partner = None
        if type_text.startswith('Partnered with'):
            battle = 'Partnered'
            pa = type_td.find('a')
            if pa:
                partner, _ = href_slug(pa.get('href'), 'trainer')
        elif type_text in ('Single', 'Double'):
            battle = type_text
        else:
            battle = type_text or None
        flags = [clean(x.get_text()) for x in tds[3].find_all(['abbr', 'span'])] or \
            [f for f in (clean(x) for x in tds[3].get_text('|').split('|')) if f and f != ',']
        flags = [f for f in flags if f and f != ',']
        dlg = parse_dialogue(tds[1])
        main = next((d['text'] for d in dlg if d['when'] == 'in battle, after defeat'), None)
        if main is None and dlg:
            main = dlg[0]['text']
        note_bits = []
        if partner or battle == 'Partnered':
            note_bits.append(type_text)
        note_bits += flags
        loc = OrderedDict()
        loc['area'] = area
        loc['areaName'] = re.sub(r'\s*\(\d+\)\s*$', '', clean(a.get_text())) if a else clean(tds[0].get_text())
        loc['note'] = '; '.join(note_bits) or None
        loc['dialogue'] = main
        if len(dlg) > 1:
            loc['dialogues'] = dlg
        loc['battle'] = battle
        if partner:
            loc['partner'] = partner
        if flags:
            loc['flags'] = flags
        key = json.dumps(loc, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        locs.append(loc)
    return locs, None


def parse_stats(table):
    res = {'stats': None, 'evs': None, 'ivs': None}
    if not table:
        return res
    rows = table.find_all('tr')
    if not rows:
        return res
    keys = []
    for th in rows[0].find_all('th'):
        if th.get('rowspan'):
            continue
        ab = th.find('abbr')
        label = clean(ab['title'] if ab and ab.get('title') else th.get_text()).lower()
        keys.append(STAT_KEYS.get(label, label))
    for tr in rows[1:]:
        th = tr.find('th')
        vals = [int_or_none(td.get_text()) for td in tr.find_all('td')]
        if len(vals) != len(keys):
            continue
        d = OrderedDict((k, v) for k, v in zip(keys, vals))
        d = OrderedDict((k, d.get(k)) for k in ('hp', 'atk', 'def', 'spa', 'spd', 'spe'))
        label = clean(th.get_text()).lower() if th else 'stats'
        if label == 'evs':
            res['evs'] = d
        elif label == 'ivs':
            res['ivs'] = d
        else:
            res['stats'] = d
    return res


GENDER_TOKENS = [('random gender', 'random'), ('same as player', 'same as player'),
                 ('genderless', 'genderless'), ('female', 'female'), ('male', 'male')]


def parse_member(div, problems, slug):
    fig = div.find('figure')
    a = fig.find('a') if fig else None
    pokemon, page, form = IDS.pokemon(a.get('href') if a else None)
    img = fig.find('img') if fig else None
    alt = img.get('alt', '') if img else ''
    cap = fig.find('figcaption') if fig else None
    info = div.select_one('div[style*="flex-direction: column"] > div')
    text = clean(info.get_text(' ')) if info else ''
    m = OrderedDict()
    m['pokemon'] = pokemon
    m['species'] = page
    m['form'] = form
    m['name'] = clean(cap.get_text()) if cap else None
    lv = re.search(r'Level (\d+)', text)
    m['level'] = int(lv.group(1)) if lv else None
    item_a = info.find('a', href=re.compile(r'/item/')) if info else None
    m['item'] = clean(item_a.get_text()) if item_a else None
    m['itemId'] = href_slug(item_a['href'], 'item')[0] if item_a else None
    abil = info.find_all('a', href=re.compile(r'/ability/')) if info else []
    m['ability'] = ' / '.join(clean(x.get_text()) for x in abil) or None
    m['abilityIds'] = [href_slug(x['href'], 'ability')[0] for x in abil]
    if len(abil) > 1:
        m['abilityRandom'] = True
    nat = re.search(r'(\w+) nature', text)
    m['nature'] = nat.group(1) if nat else None
    gender = None
    head = text.split('friendship')[0].lower()
    for tok, val in GENDER_TOKENS:
        if re.search(r'\b' + tok + r'\b', head):
            gender = val
            break
    m['gender'] = gender
    fr = re.search(r'(\d+) friendship', text)
    m['friendship'] = int(fr.group(1)) if fr else None
    m['shiny'] = bool(re.search(r'\bshiny\b', alt))
    moves, pps = [], []
    for li in div.select('ul.move-list li'):
        ma = li.find('a', href=re.compile(r'/move/'))
        if not ma:
            continue
        moves.append(IDS.move(ma['href']))
        pp = re.search(r'\((\d+) PP\)', li.get_text(' '))
        pps.append(int(pp.group(1)) if pp else None)
    m['moves'] = moves
    m['movePP'] = pps
    st = parse_stats(div.find('table', class_='stat-table'))
    m['evs'], m['ivs'], m['stats'] = st['evs'], st['ivs'], st['stats']
    if not pokemon:
        problems.append('%s: team member without pokemon link' % slug)
    return m


def parse_trainer(path, slug, problems):
    s = soup_of(path)
    h1 = s.find('h1')
    hdr = parse_header(s)
    t = OrderedDict()
    t['id'] = slug
    t['title'] = clean(h1.get_text()) if h1 else None
    full = clean(hdr['Name'].get_text()) if 'Name' in hdr else None
    t['fullName'] = full
    t['trainerGender'] = clean(hdr['Gender'].get_text()) if 'Gender' in hdr else None
    t['prize'] = int_or_none(hdr['Base prize money'].get_text()) if 'Base prize money' in hdr else None
    t['skill'] = int_or_none(hdr['Skill level'].get_text()) if 'Skill level' in hdr else None
    music = None
    for k, td in hdr.items():
        if k.startswith('Battle music'):
            music = clean(td.get_text())
    t['music'] = None if music in (None, 'Default') else music
    items = []
    if 'Items' in hdr:
        it = clean(hdr['Items'].get_text(' '))
        if it and it != 'None':
            items = [clean(x) for x in re.split(r',|\n', hdr['Items'].get_text('\n')) if clean(x)]
    t['items'] = items
    locs, locnote = parse_locations(s, problems, slug)
    t['locations'] = locs
    if locnote:
        t['locationNote'] = locnote
    team = []
    team_note = None
    tm = s.find('div', class_='trainer-team')
    if tm:
        for div in tm.find_all('div', recursive=False):
            team.append(parse_member(div, problems, slug))
    else:
        h = s.find('h2', string=re.compile(r'^\s*Team\s*$'))
        p = h.parent.find('p') if h else None
        team_note = clean(p.get_text()) if p else None
    t['team'] = team
    if team_note:
        t['teamNote'] = team_note
    return t


def site_trainer_order(site):
    s = soup_of(os.path.join(site, 'trainers.html'))
    order = []
    for cap in s.find_all('figcaption'):
        a = cap.find('a', href=re.compile(r'/trainer/'))
        if a:
            sl, _ = href_slug(a['href'], 'trainer')
            if sl and sl not in order:
                order.append(sl)
    return order


def build_trainers(site, pbs, problems):
    ttypes = read_trainertypes(pbs)
    entries = read_trainers_txt(pbs, ttypes)
    order = site_trainer_order(site)
    tdir = os.path.join(site, 'trainer')
    files = sorted(f[:-5] for f in os.listdir(tdir) if f.endswith('.html'))
    for f in files:
        if f not in order:
            problems.append('trainer page not in trainers.html list: ' + f)
    mapping = align(order + [f for f in files if f not in order], entries)
    rival_types = {e['type'] for e in entries if e['section'] == 'Rivals'}
    elite_types = {e['type'] for e in entries if e['section'] == 'E4 / Champion'}
    rival_names = {(e['class'], e['name']) for e in entries if e['section'] == 'Rivals'}

    trainers = []
    class_kinds = defaultdict(Counter)
    rules = Counter()
    for slug in files:
        t = parse_trainer(os.path.join(tdir, slug + '.html'), slug, problems)
        e = mapping.get(slug)
        full = t['fullName'] or ''
        tclass = e['class'] if e else None
        if tclass and full.startswith(tclass + ' '):
            name = full[len(tclass) + 1:]
        elif tclass and full == tclass:
            name = full
        else:
            if e:
                problems.append('%s: class %r is not a prefix of %r' % (slug, tclass, full))
            tclass, name = (tclass, full) if tclass else (None, full)
        music = norm_music(t['music'] or (e['typeMusic'] if e else None))
        section = e['section'] if e else None
        kind, rule = decide_kind(tclass, name, e['type'] if e else None, section, music,
                                 rival_types, elite_types, rival_names)
        rules[rule] += 1
        class_kinds[tclass or '(unknown)'][kind] += 1
        battles = [l['battle'] for l in t['locations'] if l.get('battle')]
        battle = None
        if battles:
            b = battles[0]
            battle = 'Double' if b == 'Partnered' else b
        out = OrderedDict()
        out['id'] = slug
        out['class'] = tclass
        out['name'] = name
        out['title'] = t['title']
        out['important'] = kind != 'regular'
        out['kind'] = kind
        out['battle'] = battle
        out['locations'] = t['locations']
        if 'locationNote' in t:
            out['locationNote'] = t['locationNote']
        out['items'] = t['items']
        out['team'] = t['team']
        if 'teamNote' in t:
            out['teamNote'] = t['teamNote']
        out['order'] = order.index(slug) if slug in order else None
        out['category'] = section
        out['tier'] = e['tier'] if e else None
        out['gender'] = t['trainerGender']
        out['prize'] = t['prize']
        out['skill'] = t['skill']
        out['music'] = t['music']
        trainers.append(out)
    trainers.sort(key=lambda x: (x['order'] is None, x['order'] if x['order'] is not None else 0, x['id']))
    unmatched = [s for s in files if s not in mapping]
    return trainers, class_kinds, rules, unmatched


# -------------------------------------------------------------- areas ----

def parse_rate(text):
    t = clean(text).replace('⁄', '/').rstrip('%').strip()
    if not t:
        return None, None
    try:
        total = sum(Fraction(p) for p in t.split())
    except (ValueError, ZeroDivisionError):
        return None, clean(text)
    return round(float(total), 2), clean(text)


def parse_levels(text):
    t = clean(text).replace('–', '-').replace(' ', '')
    m = re.fullmatch(r'(\d+)-(\d+)', t)
    if m and m.group(1) == m.group(2):
        return m.group(1)
    return t or None


def area_tree(site):
    s = soup_of(os.path.join(site, 'areas.html'))
    top = s.find('h1').find_next('ul')
    depth, ancestors = {}, {}

    def walk(ul, d, chain):
        for li in ul.find_all('li', recursive=False):
            links = [a for a in li.find_all('a', recursive=False) if '/area/' in (a.get('href') or '')]
            if not links:
                continue
            aid = area_id(links[-1]['href'])
            name = re.sub(r'\s*\(\d+\)\s*$', '', clean(links[-1].get_text()))
            depth[aid] = d
            ancestors[aid] = chain
            sub = li.find('ul', recursive=False)
            if sub:
                walk(sub, d + 1, chain + [(aid, name)])
    walk(top, 0, [])
    return depth, ancestors


def yes_no(td):
    t = clean(td.get_text()) if td is not None else ''
    return True if t == 'Yes' else False if t == 'No' else None


def none_or(td):
    t = clean(td.get_text()) if td is not None else ''
    return None if t in ('', 'None') else t


def parse_area(path, aid, problems):
    s = soup_of(path)
    h1 = clean(s.find('h1').get_text())
    name = re.sub(r'\s*\(\d+\)\s*$', '', h1)
    hdr = parse_header(s)

    def link_id(key):
        td = hdr.get(key)
        a = td.find('a', href=re.compile(r'/area/')) if td is not None else None
        return area_id(a['href']) if a else None

    def find(prefix):
        for k, v in hdr.items():
            if k.startswith(prefix):
                return v
        return None

    a = OrderedDict()
    a['id'] = aid
    a['name'] = name
    a['parent'] = link_id('Parent area')
    ch = hdr.get('Child areas')
    a['children'] = [area_id(x['href']) for x in ch.find_all('a', href=re.compile(r'/area/'))] if ch is not None else []
    a['region'] = None
    a['outdoor'] = yes_no(hdr.get('Outdoor map?'))
    a['fieldEffect'] = none_or(hdr.get('Base field effect'))
    w = none_or(hdr.get('Fixed weather'))
    a['weather'] = w.strip('"') if w else None
    a['encounters'] = []
    a['trainers'] = []
    a['image'] = None
    a['bicycle'] = yes_no(hdr.get('Bicycle usable?'))
    a['dark'] = yes_no(find('Dark area'))
    ua = find('Underwater area')
    sa = find('Surface area')
    a['underwater'] = area_id(ua.find('a')['href']) if ua is not None and ua.find('a') else None
    a['surface'] = area_id(sa.find('a')['href']) if sa is not None and sa.find('a') else None
    a['battleBackground'] = none_or(hdr.get('Battle background'))

    # step encounter rates per terrain
    br = s.find('h3', string=re.compile('Base encounter rates'))
    if br:
        t = br.find_next('table')
        rows = t.find_all('tr')
        heads = [clean(x.get_text()) for x in rows[0].find_all('th')]
        vals = [parse_rate(x.get_text())[0] for x in rows[1].find_all('td')]
        a['stepRates'] = OrderedDict((h, v) for h, v in zip(heads, vals))

    for t in s.find_all('table'):
        first = t.find('tr')
        heads = [clean(x.get_text()) for x in first.find_all('th')] if first else []
        if 'Encounter method' not in heads:
            continue
        idx = {h: i for i, h in enumerate(heads)}
        for tr in t.find_all('tr')[1:]:
            tds = tr.find_all('td', recursive=False)
            if len(tds) < 4:
                continue
            meth = clean(tds[idx['Encounter method']].get_text())
            mm = re.fullmatch(r'(.*?)\s*\((morning|day|night|dusk)\)', meth, re.I)
            time = None
            if mm:
                meth, time = mm.group(1), mm.group(2).capitalize()
            pa = tds[idx['Pokémon']].find('a', href=re.compile(r'/pokemon/'))
            pk, page, form = IDS.pokemon(pa['href']) if pa else (None, None, None)
            if not pk:
                problems.append('area %s: encounter row without pokemon' % aid)
            rate, rate_text = parse_rate(tds[idx['Rate']].get_text())
            enc = OrderedDict()
            enc['method'] = meth
            enc['time'] = time
            enc['pokemon'] = pk
            enc['species'] = page
            enc['form'] = form
            enc['levels'] = parse_levels(tds[idx['Level range']].get_text())
            enc['rate'] = rate
            a['encounters'].append(enc)

    sp = s.find('h2', string=re.compile(r'Special encounters'))
    if sp:
        special = []
        t = sp.find_next('table')
        for tr in t.find_all('tr')[1:]:
            tds = tr.find_all('td', recursive=False)
            if len(tds) < 3:
                continue
            pa = tds[0].find('a', href=re.compile(r'/pokemon/'))
            pk, page, form = IDS.pokemon(pa['href']) if pa else (None, None, None)
            lvtxt = clean(tds[2].get_text())
            lv = re.match(r'(\d+)', lvtxt)
            e = OrderedDict()
            e['pokemon'] = pk
            e['species'] = page
            e['form'] = form
            e['type'] = clean(tds[1].get_text()) or None
            e['level'] = int(lv.group(1)) if lv else None
            if 'egg' in lvtxt.lower():
                e['egg'] = True
            notes = clean(tds[3].get_text(' ')) if len(tds) > 3 else ''
            notes = re.sub(r'\s+([:,)])', r'\1', notes).replace('( ', '(')
            e['notes'] = notes or None
            special.append(e)
        ev = sp.find_next('small', class_='event-notes')
        a['special'] = special
        if ev and clean(ev.get_text()):
            a['specialNotes'] = clean(ev.get_text(' '))
    return a


def build_areas(site, problems):
    adir = os.path.join(site, 'area')
    ids = sorted((f[:-5] for f in os.listdir(adir) if f.endswith('.html')),
                 key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else 0, x))
    depth, ancestors = area_tree(site)
    areas = []
    for aid in ids:
        a = parse_area(os.path.join(adir, aid + '.html'), aid, problems)
        chain = ancestors.get(aid)
        if chain is None:
            problems.append('area %s not in areas.html tree' % aid)
            a['world'] = None
        else:
            # region: the first-level division under a root (e.g. 'Reborn City',
            # 'Aventurine Region'); world: the root itself ('Reborn', 'Intro').
            full = chain + [(aid, a['name'])]
            a['world'] = full[0][1]
            a['region'] = full[1][1] if len(full) > 1 else None
            a['depth'] = depth.get(aid)
        areas.append(a)
    return areas


# ---------------------------------------------------------- validation ----

def validate(trainers, areas, class_kinds, rules, unmatched, problems, site):
    area_ids = {a['id'] for a in areas}
    tids = {t['id'] for t in trainers}
    if IDS.loaded:
        pk_slugs = set(IDS.pk.values())
        mv_slugs = set(IDS.move_by_code.values())
    else:
        pk_slugs = {f[:-5] for f in os.listdir(os.path.join(site, 'pokemon')) if f.endswith('.html')}
        mv_slugs = {f[:-5].lower() for f in os.listdir(os.path.join(site, 'move')) if f.endswith('.html')}
    unresolved = Counter()
    for t in trainers:
        for l in t['locations']:
            if l['area'] not in area_ids:
                unresolved['trainer location area %s' % l['area']] += 1
            if l.get('partner') and l['partner'] not in tids:
                unresolved['partner trainer %s' % l['partner']] += 1
        for m in t['team']:
            if (m['pokemon'] if IDS.loaded else m['species']) not in pk_slugs:
                unresolved['pokemon %s' % m['pokemon']] += 1
            for mv in m['moves']:
                if mv not in mv_slugs:
                    unresolved['move %s' % mv] += 1
    for a in areas:
        for k in ('parent', 'underwater', 'surface'):
            if a.get(k) and a[k] not in area_ids:
                unresolved['area %s %s %s' % (a['id'], k, a[k])] += 1
        for c in a['children']:
            if c not in area_ids:
                unresolved['area child %s' % c] += 1
        for e in a['encounters'] + a.get('special', []):
            if (e['pokemon'] if IDS.loaded else e['species']) not in pk_slugs:
                unresolved['pokemon %s' % e['pokemon']] += 1

    p = lambda *x: print(*x, file=sys.stderr)
    p('== trainers: %d' % len(trainers))
    p('   per kind: %s' % dict(Counter(t['kind'] for t in trainers)))
    p('   important: %d' % sum(t['important'] for t in trainers))
    p('   kind rules used: %s' % dict(rules))
    p('   with no locations: %d' % sum(not t['locations'] for t in trainers))
    empty = [t['id'] for t in trainers if not t['team']]
    p('   empty teams (%d): %s' % (len(empty), ', '.join(empty)))
    p('   not matched to trainers.txt (class unknown) (%d): %s' % (len(unmatched), ', '.join(unmatched)))
    p('   team members: %d' % sum(len(t['team']) for t in trainers))
    p('== class -> kind mapping')
    for c in sorted(class_kinds):
        p('   %-32s %s' % (c, ', '.join('%s x%d' % kv for kv in class_kinds[c].most_common())))
    p('== areas: %d' % len(areas))
    p('   with wild encounters: %d (rows %d)' % (sum(bool(a['encounters']) for a in areas),
                                                 sum(len(a['encounters']) for a in areas)))
    p('   with special encounters: %d' % sum(bool(a.get('special')) for a in areas))
    p('   with trainers: %d' % sum(bool(a['trainers']) for a in areas))
    p('   with field effect: %d' % sum(bool(a['fieldEffect']) for a in areas))
    p('   regions: %s' % dict(Counter(a['region'] for a in areas).most_common()))
    p('   encounter methods: %s' % dict(Counter((e['method'], e['time']) for a in areas for e in a['encounters']).most_common()))
    p('== ids resolved against extract_dex output: %s' % IDS.loaded)
    for k, v in IDS.misses.most_common():
        p('   fallback: %s x%d' % (k, v))
    p('== unresolved references: %d' % sum(unresolved.values()))
    for k, v in unresolved.most_common():
        p('   %s x%d' % (k, v))
    p('== parse problems: %d' % len(problems))
    for x in problems[:50]:
        p('   ' + x)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--site', required=True, help='reborn-db-site clone')
    ap.add_argument('--pbs', required=True, help='reborn-db PBS folder')
    ap.add_argument('--out', required=True, help='output folder (app/data)')
    ap.add_argument('--dex', help='folder with pokemon.json + moves.json from extract_dex.py '
                    '(default: --out); ids fall back to site slugs if absent')
    args = ap.parse_args()
    global IDS
    IDS = DexIds(args.dex or args.out)
    if not IDS.loaded:
        print('WARNING: pokemon.json/moves.json not found; using raw site slugs', file=sys.stderr)
    problems = []
    trainers, class_kinds, rules, unmatched = build_trainers(args.site, args.pbs, problems)
    areas = build_areas(args.site, problems)
    by_area = defaultdict(list)
    for t in trainers:
        for l in t['locations']:
            if l['area'] and t['id'] not in by_area[l['area']]:
                by_area[l['area']].append(t['id'])
    for a in areas:
        a['trainers'] = by_area.get(a['id'], [])
    validate(trainers, areas, class_kinds, rules, unmatched, problems, args.site)
    os.makedirs(args.out, exist_ok=True)
    for name, data in (('trainers.json', trainers), ('areas.json', areas)):
        path = os.path.join(args.out, name)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(',', ':'))
        print('wrote %s (%d bytes)' % (path, os.path.getsize(path)), file=sys.stderr)


if __name__ == '__main__':
    main()
