#!/usr/bin/env python3
"""Work out which switch marks each item / trainer event on a map as "done".

Usage (run isolated: the game files are untrusted binary data):

    python3 -I scripts/extract_eventflags.py \
        --game     /tmp/claude-0/game/merged/Data \
        --events   app/data/events.json \
        --out      app/data/eventflags.json \
        [--pbs /tmp/claude-0/src/reborn-db/PBS --trainers app/data/trainers.json]

How Reborn marks an event as done (checked on every page that holds the call
events.json was built from: pbItemBall / pbReceiveItem / pbTrainerBattle...):

  self   the page turns a self switch ON after the call (event command 123
         ["A", 0], usually nested inside the Conditional Branch "script" that
         makes the call), and a later page of the same event is conditioned on
         that self switch.  Item balls: page 1 "self switch A" has no graphic.
         Trainers: page 1 "self switch A" is the post-battle chatter.
  switch the page turns a game switch ON after the call (command 121 [id, id, 0])
         and a later page is conditioned on that game switch (story battles).

The game keeps self switches as $game_self_switches[[map_id, event_id, "A"]],
so the live link reports them per map and event, and the guide looks the event
up here.

Output (minified):
  { "<areaId>": { "<ev>": "A" | {"switch": 2014} },
    "_trainers": { "<TRAINERTYPE>|<name>|<party>": "<trainers.json id>" },   (with --pbs)
    "_meta": { source, rules, stats } }

"_trainers" maps the opponent the live link reports in battle (trainer type
constant upper-cased, the internal trainer name, the party id) to a trainer id.
Keys that start with "_" are not areas.
"""
import argparse
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import links_marshal as M  # noqa: E402
from extract_events import CALL_RE, text  # noqa: E402

SOURCE = "Pokémon Reborn v19.5 game data (Map###.rxdata event pages)"
RULES = {
    "self": "command 123 [letter, 0] (self switch ON) after the item/trainer call on the same page, "
            "and a later page conditioned on that self switch",
    "switch": "command 121 [id, id, 0] (game switch ON) after the call, and a later page "
              "conditioned on that game switch",
    "script": "pbSetSelfSwitch(@event_id|<ev>,\"X\",true) / $game_self_switches[[..,..,\"X\"]]=true "
              "in a script after the call counts as command 123",
    "fallback": "no explicit ON after the call: the only later page conditioned on a self switch "
                "(marked 'inferred' in stats)",
    "value": "\"A\".. \"D\" = self switch letter; {\"switch\": n} = game switch id",
}
SS_SCRIPT_RE = re.compile(
    r"pbSetSelfSwitch\(\s*(@event_id|\d+)\s*,\s*[\"'](\w)[\"']\s*,\s*true\s*\)"
    r"|\$game_self_switches\[\[\s*[^,\]]+,\s*(@event_id|\d+)\s*,\s*[\"'](\w)[\"']\s*\]\]\s*=\s*true")


def cond_of(pg):
    c = pg.get("condition")
    if not isinstance(c, M.RObj):
        return None, set()
    ss = text(c.get("self_switch_ch")) if c.get("self_switch_valid") else None
    sw = set()
    for k in ("switch1", "switch2"):
        if c.get(k + "_valid"):
            sw.add(c.get(k + "_id"))
    return ss, sw


def scan_page(pg, eid):
    """Return (has_call, [self letters set after the call], [switch ids set after the call])."""
    cmds = [c for c in (pg.get("list") or []) if isinstance(c, M.RObj)]
    call_at, letters, switches = None, [], []

    def script_hits(s):
        out = []
        for m in SS_SCRIPT_RE.finditer(s):
            who, letter = (m.group(1), m.group(2)) if m.group(2) else (m.group(3), m.group(4))
            if who == "@event_id" or (who.isdigit() and int(who) == eid):
                out.append(letter)
        return out

    for i, c in enumerate(cmds):
        code, p = c.get("code"), c.get("parameters") or []
        s = None
        if code in (355, 655) and p:
            s = text(p[0])
        elif code == 111 and len(p) > 1 and p[0] == 12:
            s = text(p[1])
        if s is not None and call_at is None and CALL_RE.search(s):
            call_at = i
            # a self switch set later in the same script block
            letters += script_hits(s[CALL_RE.search(s).end():])
            continue
        if call_at is None:
            continue
        if code == 123 and len(p) >= 2 and p[1] == 0:
            letters.append(text(p[0]))
        elif code == 121 and len(p) >= 3 and p[2] == 0:
            a, b = p[0], p[1]
            if isinstance(a, int) and isinstance(b, int) and b - a < 50:
                switches += list(range(a, b + 1))
        elif s is not None:
            letters += script_hits(s)
    return call_at is not None, letters, switches


def flag_for(ev, eid, st):
    pages = [pg for pg in (ev.get("pages") or []) if isinstance(pg, M.RObj)]
    conds = [cond_of(pg) for pg in pages]
    fallback = None
    for pi, pg in enumerate(pages):
        has, letters, switches = scan_page(pg, eid)
        if not has:
            continue
        later_ss = {conds[j][0] for j in range(pi + 1, len(pages)) if conds[j][0]}
        later_sw = set().union(*[conds[j][1] for j in range(pi + 1, len(pages))]) if pi + 1 < len(pages) else set()
        for L in letters:
            if L in later_ss:
                st["self"] += 1
                return L
        for s in switches:
            if s in later_sw:
                st["switch"] += 1
                return {"switch": s}
        if fallback is None:
            if letters:
                fallback = ("self_unverified", letters[0])
            elif len(later_ss) == 1:
                fallback = ("inferred", next(iter(later_ss)))
    if fallback:
        st[fallback[0]] += 1
        return fallback[1]
    return None


def trainer_keys(pbs, trainers_path):
    from extract_events import TrainerIndex
    with open(trainers_path, encoding="utf-8") as f:
        tj = json.load(f)
    TI = TrainerIndex(tj, pbs)
    return {f"{T}|{N}|{P}": slug for (T, N, P), slug in sorted(TI.exact.items(), key=lambda kv: kv[1])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True, help="folder with Map###.rxdata")
    ap.add_argument("--events", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pbs", help="reborn-db PBS folder (adds the _trainers lookup)")
    ap.add_argument("--trainers", default="app/data/trainers.json")
    args = ap.parse_args()

    with open(args.events, encoding="utf-8") as f:
        events = json.load(f)
    st = collections.Counter()
    unresolved = collections.Counter()
    out = {}
    for area, recs in sorted(events.items(), key=lambda kv: (not kv[0].isdigit(), int(kv[0]) if kv[0].isdigit() else 0)):
        if area.startswith("_") or not isinstance(recs, list):
            continue
        path = os.path.join(args.game, f"Map{int(area):03d}.rxdata")
        if not os.path.exists(path):
            st["missing_map"] += 1
            continue
        evs = M.load_path(path).get("events") or {}
        flags = {}
        for r in recs:
            ev = evs.get(r["ev"])
            if not isinstance(ev, M.RObj):
                st["missing_event"] += 1
                continue
            st["events_" + r["kind"]] += 1
            fl = flag_for(ev, r["ev"], st)
            if fl is None:
                unresolved[r["kind"]] += 1
                continue
            flags[str(r["ev"])] = fl
            st["resolved_" + r["kind"]] += 1
        if flags:
            out[area] = flags
    if args.pbs:
        out["_trainers"] = trainer_keys(args.pbs, args.trainers)
        st["trainer_keys"] = len(out["_trainers"])
    stats = dict(sorted(st.items()))
    stats["unresolved"] = dict(unresolved)
    out["_meta"] = {"source": SOURCE, "rules": RULES, "stats": stats}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps(stats, indent=1))
    letters = collections.Counter(v if isinstance(v, str) else "switch"
                                  for k, a in out.items() if not k.startswith("_") for v in a.values())
    print("flag values:", dict(letters))


if __name__ == "__main__":
    main()
