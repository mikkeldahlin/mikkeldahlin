# Live link contract

How the game mod, the local server and the page talk to each other.

```
Reborn (mkxp-z) + Data/Mods/RebornFieldGuideLink.rb
   │  writes <game>/RFGLink/state.json  (atomic: state.json.tmp → rename)
   ▼
bridge/serve.py  (127.0.0.1:8742)
   ├─ GET /live/state.json   the file, plus battle.trainer[].id      (404 = no live link)
   ├─ GET /live/stream       Server-Sent Events: "state", "progress"
   ├─ GET/PUT/PATCH /api/progress   ticks saved in <data>/progress.json
   └─ GET /api/info
   ▲
app/index.html (local copy only; the claude.ai artifact gets a 404 and hides live mode)
```

## state.json (v1)

UTF-8 JSON, one object, rewritten in full each time.

```json
{
  "v": 1, "game": "reborn", "gameVersion": "e19.16", "mod": "1.0.0",
  "t": 1793457237465,          // unix ms when written (wall clock, not the in-game clock)
  "seq": 812,                  // +1 per write since the game started (resets on restart)
  "player": { "map": 29, "x": 12, "y": 40, "dir": 2, "name": "Ana", "badges": 2, "money": 4120 },
  "party": [ Pokemon, ... ],
  "battle": null | Battle,
  "progress": {
    "selfSwitches": { "10": { "9": "A", "12": "AB" } },   // only ON self switches: map → event → letters
    "switchesOn": [172, 2017],                             // ids of game switches that are ON
    "switchesHash": "8f3a01c2",                            // FNV-1a of switchesOn, cheap change check
    "variables": { "107": 25 }                             // game variables that are non-zero integers
  }
}
```

- `player.dir`: 2 down, 4 left, 6 right, 8 up. `player.map` is the map id = the area id in `areas.json`.
- The game version string comes from the game's own `GAMEVERSION` constant ("e19.16" in the 19.5 download).
- `progress` can be `null` for a moment at the title screen.

**Pokemon** (party member)

```json
{ "species": "PIKACHU", "speciesName": "Pikachu", "dex": 25, "form": 0, "name": "Sparky",
  "level": 22, "hp": 47, "totalhp": 47, "status": null | "SLEEP" | "POISON" | "BURN" | "PARALYSIS" | "FROZEN",
  "ability": "STATIC", "abilityName": "Static", "item": "LIGHTBALL", "itemName": "Light Ball",
  "types": ["ELECTRIC"],
  "moves": [ { "id": "THUNDERBOLT", "name": "Thunderbolt", "pp": 15, "totalpp": 15, "type": "ELECTRIC" } ] }
```
An egg is `{ "egg": true }`.

**Battle**

```json
{ "id": 3,                                   // +1 per battle this session
  "phase": "start" | "command" | "endround",
  "wild": false, "double": false, "turn": 2,
  "trainer": null | [ { "class": "TechNerd", "className": "Techie", "name": "Naoman",
                        "nameKey": "Naoman", "party": 0, "key": "TECHNERD|Naoman|0",
                        "id": "techie-naoman-1" } ],          // "id" is added by serve.py
  "field": null | { "id": 17, "const": "FACTORYF", "name": "Factory Field", "layers": [17] },
  "weather": null | "SUNNYDAY" | "RAINDANCE" | "SANDSTORM" | "HAIL" | "SHADOWSKY" | "STRONGWINDS",
  "weatherTurns": 5, "trickRoom": 0,
  "opponents":    [ Battler, ... ],          // active foes (index 1, 3)
  "player":       [ Battler, ... ],          // active own side (index 0, 2); "ally": true = partner's
  "opponentParty":[ { "slot": 0, "species": "ZORUA", "dex": 570, "form": 0, "level": 34, "fainted": false } ] }
```

**Battler**: `index, partyIndex, species, speciesName, dex, form, name, level, hpPct, fainted,
status, types, ability, abilityName, stages, item, itemName, moves, ally`, and for the player's side
also `hp, totalhp`.
- `types` are the battler's *current* types (Soak, Roost, Color Change... included).
- `stages`: only non-zero stat stages, keys `atk def spe spa spd acc eva`, values -6..6.
- Opponents: `item` is `null` and `moves` lists only the moves seen this battle
  (`"movesRevealed": true`), because the game does not show you more. The guide has the trainer's
  full team from `trainers.json`. (The mod constant `SHOW_HIDDEN_INFO` turns this off.)
- Illusion: an opponent under Illusion is reported as the Pokémon it is disguised as, with its
  types, and `ability: null`.

### When the mod writes

| trigger | when |
|---|---|
| map / position / facing changed | at most every 250 ms, on the map |
| self switches, switches, variables, party, money, badges | checked once per second on the map; written only if changed |
| heartbeat | every 5 s on the map, even if nothing changed |
| battle start | `PokeBattle_Battle#pbStartBattle` (phase `start`, before send-out) |
| each turn | start of `pbCommandPhase` (phase `command`, the state you choose your move in) and after `pbEndOfRoundPhase` (phase `endround`) |
| battle end | when `pbStartBattle` returns (`battle: null`) |

No writes happen in menus, the bag, the Pokémon screen, or while the window is unfocused and the
game is paused. So "t is old" with `battle: null` means paused or closed, not stuck.

### Hooks used (Reborn 19.5 scripts)

| hook | file | how |
|---|---|---|
| `Scene_Map#update` | Scene_Map.rb | prepended module, calls `super` first |
| `PokeBattle_Battle#pbStartBattle`, `#pbCommandPhase`, `#pbEndOfRoundPhase` | PokeBattle_Battle.rb | prepended module, `super` |
| `Game_SelfSwitches#[]=`, `Game_Switches#[]=`, `Game_Variables#[]=` | Game_Data.rb | prepended module; only sets a "changed" flag |
| `pbLoadTrainer` (top level) | PokemonTrainers.rb | alias chain; remembers which party id built the opponent |

Prepending (`Module#prepend` + `super`) is used for class methods instead of `alias`. It chains the
same way and never replaces the game's method. It also survives an F12 soft reset, when the game
re-defines its methods and then loads Data/Mods again. It is idempotent too.

## Mapping game symbols to guide ids

| state.json | guide data | rule |
|---|---|---|
| move `id` (`THUNDERBOLT`) | `moves.json` | match `code` (exact for all 771 moves) |
| species | `pokemon.json` | `dex` is the game's species id = national dex number; `form` 0 = the base entry. For form > 0, pick the entry of that dex whose types match `types` |
| `abilityName` / `itemName` | `abilities.json` / `items.json` | `slug(name)`, as in index.html (`Light Ball` → `light-ball`); covers all 258 abilities and 853 items. Fallback: the symbol with non-alphanumerics removed, compared with `id` likewise |
| `types` (`ELECTRIC`) | `types.json` | Title Case (`Electric`); `QMARKS` is `???` |
| `field.id` | `fields.json` | `number` (Electric Terrain = 1, Factory Field = 17); or `field.name` = `name` |
| battle `trainer[].key` | `eventflags.json` `_trainers` | `TRAINERTYPE|name|party` → trainer id; serve.py fills `trainer[].id` |
| `player.map` | `areas.json` | `id` = `String(map)` |

## eventflags.json

Built by `scripts/extract_eventflags.py` from the map files. For every item, hidden item, gift and
trainer event in `events.json`, it says which flag means "done":

```json
{ "10": { "9": "A", "12": "A" },          // self switch letter ON
  "8":  { "3": { "switch": 2014 } },      // game switch ON (story battles)
  "17": { "24": { "var": 107, "min": 25 } },  // story variable ≥ min
  "_trainers": { "TECHNERD|Naoman|0": "techie-naoman-1" },
  "_meta": { ... } }
```
Done means: `progress.selfSwitches[area][ev]` contains the letter, or the switch id is in
`progress.switchesOn`, or `progress.variables[var] >= min`. Keys starting with `_` are not areas.

## Progress API (serve.py)

```
GET   /api/progress  → { "v": 1, "ticks": { "<key>": { "on": true, "src": "user" | "game", "t": ms } }, "updated": ms }
PUT   /api/progress  ← the whole object (replaces)
PATCH /api/progress  ← { "<key>": { "on": bool, "src": "user", "t": ms } | null, ... }   (null deletes)
GET   /api/info      → { "app": "reborn-field-guide", "version", "live": bool, "game", "dataDir", "autoTick", "port" }
```
- **Keys** (same as `Progress` in app/index.html): `beat:<trainerId>` for trainers;
  `pick:<areaId>:<ev>` for map item events; `pick:<itemId>:<locationIndex>` for wiki location rows.
- Writes need `Content-Type: application/json`, a body under 5 MB, Host `127.0.0.1:<port>` or
  `localhost:<port>`, and a matching `Origin` if one is sent. Anything else gets 403, 413 or 415.
- Storage: `progress.json` in the data folder, written to a temp file, then fsync, then
  `os.replace`. The previous version stays as `progress.json.bak`, which is read if the main file
  is damaged.
  Data folder: `%APPDATA%\RebornFieldGuide` (Windows), `~/Library/Application Support/RebornFieldGuide`
  (macOS), `$XDG_DATA_HOME/RebornFieldGuide` or `~/.local/share/RebornFieldGuide` (Linux), or `--data`.
- **Game auto-ticks**: whenever state.json changes, the server works out which keys the game says
  are done (via eventflags.json + events.json). It sets them to `{"on": true, "src": "game"}`
  unless the key's current entry has `src: "user"`. So an untick by hand sticks, and the game
  never unticks anything. This runs even when no page is open.
- `/live/stream` (SSE) sends `event: state` with the state JSON on every change, and
  `event: progress` with the full progress object when ticks change. It also sends a keep-alive
  comment every 15 s.

## Testing without the game

`python3 bridge/simulate.py --out <dir>/RFGLink` writes a real-data sequence: walking, an item ball,
a trainer battle and a warp. Then run `python3 bridge/serve.py --game <dir>`.
