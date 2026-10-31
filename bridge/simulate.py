#!/usr/bin/env python3
"""Write a fake but realistic RFGLink/state.json sequence, to test live mode without the game.

    python3 bridge/simulate.py --out /tmp/x/RFGLink            # real time, ~4 frames/s
    python3 bridge/simulate.py --out /tmp/x/RFGLink --fast     # no delays
    python3 bridge/simulate.py --out /tmp/x/RFGLink --loop     # repeat forever
    python3 bridge/serve.py --game /tmp/x                      # serve it (game dir = parent of RFGLink)

The scenario uses real data from app/data: the player walks across a real map
(default 10, Blacksteam Factory 1F) from a real warp tile, picks up a real
item ball (its self switch goes ON as in the game, per eventflags.json),
walks up to a real trainer, battles that trainer's real team from
trainers.json on the map's field effect (a few turns, fainting, switching,
weather), wins (the trainer's self switch goes ON), then takes a real warp to
the next map and walks a few steps. Shape: bridge/STATE_FORMAT.md.
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve  # noqa: E402

DATA = os.path.join(serve.DEFAULT_APP, "data")
FIELD_CONSTS = ("NONE ELECTRICT GRASSYT MISTYT DARKCRYSTALC CHESSB BIGTOPA BURNINGF SWAMPF RAINBOWF "
                "CORROSIVEF CORROSIVEMISTF DESERTF ICYF ROCKYF FORESTF SUPERHEATEDF FACTORYF "
                "SHORTCIRCUITF WASTELAND ASHENB WATERS UNDERWATER CAVE GLITCHF CRYSTALC MURKWATERS "
                "MOUNTAIN SNOWYM HOLYF MIRRORA FAIRYTALEF DRAGONSD FLOWERGARDENF STARLIGHTA NEWW "
                "INVERSEF PSYCHICT INDOORA INDOORB INDOORC CITY CITYNEW").split()


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


def sym(s):
    """Game-style constant from a name or slug: 'Light Ball' / 'light-ball' -> LIGHTBALL."""
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper()) or None


class World:
    def __init__(self):
        self.areas = {a["id"]: a for a in load("areas.json")}
        self.events = load("events.json")
        self.flags = load("eventflags.json")
        self.links = load("links.json")
        self.trainers = {t["id"]: t for t in load("trainers.json")}
        self.pokemon = {p["id"]: p for p in load("pokemon.json")}
        self.moves = {m["id"]: m for m in load("moves.json")}
        self.abilities = {a["id"]: a for a in load("abilities.json")}
        self.items = {i["id"]: i for i in load("items.json")}
        self.fields = {f["name"]: f for f in load("fields.json")}
        self.tkey = {v: k for k, v in (self.flags.get("_trainers") or {}).items()}

    def move(self, mid, pp=None):
        m = self.moves.get(mid)
        if not m:
            return None
        h = {"id": m.get("code") or sym(mid), "name": m["name"]}
        if pp is not None:
            h["pp"], h["totalpp"] = pp, m.get("pp")
        h["type"] = sym(m.get("type"))
        return h

    def mon_types(self, pid):
        p = self.pokemon.get(pid) or {}
        return [sym(t) for t in p.get("types") or []]

    def field(self, area):
        name = (self.areas.get(area) or {}).get("fieldEffect")
        f = self.fields.get(name) if name else None
        if not f:
            return None
        n = f.get("number")
        return {"id": n, "const": FIELD_CONSTS[n] if isinstance(n, int) and n < len(FIELD_CONSTS) else None,
                "name": f["name"], "layers": [n]}


class Sim:
    def __init__(self, world, out, interval, fast):
        self.w = world
        self.out = out
        self.interval = 0 if fast else interval
        self.seq = 0
        self.self_sw = {}
        self.sw_on = []
        self.vars = {}
        self.player = None
        self.party = []
        self.battle = None
        self.frames = 0

    # -- output
    def progress(self):
        on = sorted(set(self.sw_on))
        s = ",".join(str(i) for i in on)
        h = 0x811c9dc5
        for b in s.encode():
            h = ((h ^ b) * 0x01000193) & 0xffffffff
        ss = {m: {e: "".join(sorted(l)) for e, l in sorted(evs.items(), key=lambda kv: int(kv[0]))}
              for m, evs in sorted(self.self_sw.items(), key=lambda kv: int(kv[0]))}
        return {"selfSwitches": ss, "switchesOn": on, "switchesHash": "%08x" % h, "variables": dict(self.vars)}

    def emit(self, pause=1.0):
        self.seq += 1
        doc = {"v": 1, "game": "reborn", "gameVersion": "e19.16", "mod": "simulate.py",
               "t": serve.now_ms(), "seq": self.seq, "player": self.player, "party": self.party,
               "battle": self.battle, "progress": self.progress()}
        serve.atomic_write(os.path.join(self.out, "state.json"),
                           json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                           backup=False)
        self.frames += 1
        if self.interval:
            time.sleep(self.interval * pause)

    # -- world actions
    def set_flag(self, area, ev, flag):
        if isinstance(flag, str):
            self.self_sw.setdefault(area, {}).setdefault(str(ev), set()).add(flag)
        elif "switch" in flag:
            self.sw_on.append(flag["switch"])
        elif "var" in flag:
            self.vars[str(flag["var"])] = max(self.vars.get(str(flag["var"]), 0), flag["min"])

    def walk_to(self, x, y):
        p = self.player
        while (p["x"], p["y"]) != (x, y):
            if p["x"] != x:
                d = 1 if x > p["x"] else -1
                p["x"] += d
                p["dir"] = 6 if d > 0 else 4
            else:
                d = 1 if y > p["y"] else -1
                p["y"] += d
                p["dir"] = 2 if d > 0 else 8
            self.emit()

    def face(self, x, y):
        p = self.player
        dx, dy = x - p["x"], y - p["y"]
        p["dir"] = (6 if dx > 0 else 4) if abs(dx) >= abs(dy) else (2 if dy > 0 else 8)
        self.emit()

    def warp(self, area, x, y):
        self.player.update(map=int(area), x=x, y=y)
        self.emit(2)

    # -- party / battle shapes (same keys as the mod)
    def make_party(self):
        out = []
        for pid, lvl, item in (("pikachu", 22, "light-ball"), ("growlithe", 21, None), ("marill", 20, "leftovers")):
            p = self.w.pokemon.get(pid)
            if not p:
                continue
            lm = [m["move"] for m in p["moves"]["level"] if m["level"] <= lvl][-4:]
            moves = [mv for mv in (self.w.move(m, (self.w.moves.get(m) or {}).get("pp")) for m in lm) if mv]
            ab = p["abilities"][0] if p.get("abilities") else None
            hp = 2 * p["stats"]["hp"] * lvl // 100 + lvl + 10
            it = self.w.items.get(item) if item else None
            out.append({"species": sym(p["name"]), "speciesName": p["name"], "dex": p["dex"], "form": 0,
                        "name": p["name"], "level": lvl, "hp": hp, "totalhp": hp, "status": None,
                        "ability": sym(ab), "abilityName": (self.w.abilities.get(ab) or {}).get("name"),
                        "item": sym(it["name"]) if it else None, "itemName": it["name"] if it else None,
                        "types": self.w.mon_types(pid), "moves": moves})
        self.party = out

    def battler(self, mon, idx, opposing, party_index=0, hp_pct=100, revealed=()):
        sp = mon.get("species") or mon.get("pokemon")
        p = self.w.pokemon.get(mon.get("pokemon") or sp) or {}
        h = {"index": idx, "partyIndex": party_index, "species": sym(p.get("name") or sp),
             "speciesName": p.get("name"), "dex": p.get("dex"), "form": 0,
             "name": mon.get("name") or p.get("name"), "level": mon.get("level"),
             "hpPct": hp_pct, "fainted": hp_pct <= 0, "status": None,
             "types": self.w.mon_types(mon.get("pokemon") or sp),
             "ability": sym(mon.get("ability")), "abilityName": mon.get("ability"),
             "stages": {}, "item": None, "itemName": None}
        if opposing:
            h["ally"] = False
            h["moves"] = [mv for mv in (self.w.move(m) for m in revealed) if mv]
            h["movesRevealed"] = True
        else:
            h["ally"] = False
        return h

    def player_battler(self, i=0):
        m = self.party[i]
        b = {k: m[k] for k in ("species", "speciesName", "dex", "form", "name", "level", "status",
                               "types", "ability", "abilityName", "item", "itemName")}
        b.update(index=0, partyIndex=i, hpPct=max(0, -(-m["hp"] * 100 // m["totalhp"])), fainted=m["hp"] <= 0,
                 stages={}, ally=False, hp=m["hp"], totalhp=m["totalhp"], moves=m["moves"])
        return b

    def fight(self, tid, area, battle_no):
        t = self.w.trainers[tid]
        key = self.w.tkey.get(tid)
        cls, name_key, party = (key.split("|") + [None, None, None])[:3] if key else (sym(t["class"]), t["name"], None)
        team = t.get("team") or []
        opp_party = [{"slot": i, "species": sym((self.w.pokemon.get(m["pokemon"]) or {}).get("name") or m["pokemon"]),
                      "dex": (self.w.pokemon.get(m["pokemon"]) or {}).get("dex"), "form": 0, "level": m.get("level"), "fainted": False} for i, m in enumerate(team)]
        b = {"id": battle_no, "phase": "start", "wild": False,
             "trainer": [{"class": cls, "className": t["class"], "name": t["name"], "nameKey": name_key,
                          "party": int(party) if party is not None else None, "key": key}],
             "double": t.get("battle") == "Double", "turn": 0, "field": self.w.field(area),
             "weather": None, "weatherTurns": 0, "trickRoom": 0,
             "opponents": [], "opponentParty": opp_party, "player": []}
        self.battle = b
        self.emit(2)
        turn = 0
        for i, mon in enumerate(team[:3]):
            revealed = []
            for hp in (100, 45, 0):
                turn += 1
                b["turn"] = turn
                b["phase"] = "command"
                b["opponents"] = [self.battler(mon, 1, True, i, hp, revealed)]
                b["player"] = [self.player_battler(0)]
                self.emit(2)
                if hp == 0:
                    break
                if mon.get("moves"):
                    mv = mon["moves"][min(len(revealed), len(mon["moves"]) - 1)]
                    if mv not in revealed:
                        revealed.append(mv)
                self.party[0]["hp"] = max(1, self.party[0]["hp"] - 9)
                self.party[0]["moves"][0]["pp"] = max(0, (self.party[0]["moves"][0].get("pp") or 1) - 1)
                if turn == 2:
                    b["weather"], b["weatherTurns"] = "RAINDANCE", 5
                b["phase"] = "endround"
                b["opponents"] = [self.battler(mon, 1, True, i, hp // 2 if hp > 45 else 0, revealed)]
                b["player"] = [self.player_battler(0)]
                self.emit()
            opp_party[i]["fainted"] = True
            b["opponentParty"] = opp_party
        for op in opp_party:
            op["fainted"] = True
        b["phase"] = "endround"
        self.emit()
        self.battle = None
        self.emit(2)

    def run(self, start):
        w = self.w
        area = str(start)
        evs = w.events.get(area) or []
        fl = w.flags.get(area) or {}
        item = next((e for e in evs if e["kind"] in ("item", "hidden") and str(e["ev"]) in fl), None)
        trainer = next((e for e in evs if e["kind"] == "trainer" and e.get("trainer") in w.trainers
                        and str(e["ev"]) in fl and isinstance(fl[str(e["ev"])], str)), None)
        links = [l for l in w.links.get(area) or [] if isinstance(l, dict)]
        if not links or not (item or trainer):
            raise SystemExit("area %s has no warp or no item/trainer event to simulate" % area)
        entry = links[0]
        exit_ = next((l for l in links if l is not entry and l.get("to") in w.events), links[-1])
        self.player = {"map": int(area), "x": entry["x"], "y": entry["y"] + 1, "dir": 2,
                       "name": "Sim", "badges": 2, "money": 4120}
        self.make_party()
        self.emit(2)
        if item:
            self.walk_to(item["x"], item["y"] + 1)
            self.face(item["x"], item["y"])
            self.set_flag(area, item["ev"], fl[str(item["ev"])])
            self.player["money"] += 0
            self.emit(2)
            print("picked up %s (map %s event %s)" % (item.get("item"), area, item["ev"]))
        if trainer:
            self.walk_to(trainer["x"], trainer["y"] + 1)
            self.face(trainer["x"], trainer["y"])
            print("battle vs %s" % trainer["trainer"])
            self.fight(trainer["trainer"], area, 1)
            self.set_flag(area, trainer["ev"], fl[str(trainer["ev"])])
            self.player["money"] += 840
            self.emit(2)
        self.walk_to(exit_["x"], exit_["y"])
        print("warp to %s %s" % (exit_["to"], exit_.get("toName") or ""))
        self.warp(exit_["to"], exit_["tx"], exit_["ty"])
        for _ in range(4):
            self.player["y"] += 1
            self.player["dir"] = 2
            self.emit()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, help="the RFGLink folder to write state.json into")
    ap.add_argument("--start", default="10", help="map id to start on (default 10)")
    ap.add_argument("--interval", type=float, default=0.25, help="seconds between frames")
    ap.add_argument("--fast", action="store_true", help="no delays")
    ap.add_argument("--loop", action="store_true", help="repeat until Ctrl+C")
    args = ap.parse_args()
    out = serve.clean_path(args.out)
    os.makedirs(out, exist_ok=True)
    world = World()
    try:
        while True:
            sim = Sim(world, out, args.interval, args.fast)
            sim.run(args.start)
            print("wrote %d frames to %s" % (sim.frames, os.path.join(out, "state.json")))
            if not args.loop:
                break
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
