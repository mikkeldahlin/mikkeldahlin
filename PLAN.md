# Reborn Field Guide: plan

A companion tool for **Pokémon Reborn**, published as a claude.ai artifact.
It is built so a second game (Rejuvenation) can be added later as another
`app/data/<game>/` folder.

## Data source

- Main source: the static site of the **Pokémon Reborn Database** by Andrew Foote
  (`github.com/Andrew-Foote/reborn-db-site`). Its data was extracted from the game
  files of Reborn **v19.16**, and the author invites people to download and reuse it.
- Extra source: `github.com/Andrew-Foote/reborn-db` (`PBS/townmap.txt`,
  `PBS/connections.txt`, `PBS/types.txt`, `PBS/tm.txt`) for the region grid, map
  stitching and the type chart.
- Not used: BIGJRA's walkthrough, or anything derived from it. Its author asks that
  it not be fed to LLM tools.
- Blocked from this build environment: the fandom wikis and rebornevo.com. Item, TM
  and HM pickup locations live there. They come in phase 2, once those domains are
  allowed.

Local clones live in `/tmp/claude-0/src/` (not committed). Every extractor lives in
`scripts/`, reads from those clones and writes JSON to `app/data/` following
`data/SCHEMA.md`.

## Features

| # | Feature | Phase 1 (now) | Phase 2 |
|---|---------|---------------|---------|
| 1 | **Type matchups**: attack and defend calculator for dual types, a full 18×18 chart, ability notes (Levitate and others) | yes | Reborn field-effect modifiers |
| 2 | **Pokédex**: all species and forms, with stats, types, abilities, wild held items, EV yield, evolutions, level-up, TM, tutor and egg moves, and where to catch them | yes | |
| 3 | **Battles**: every trainer, with level, ability, item, nature, moves, EVs and IVs per Pokémon. Gym leaders, bosses and rivals get a star, and battles are grouped by location | yes | progress checkboxes |
| 4 | **Areas**: wild encounters by method and time, field effect, trainers there, rendered map | yes | items and TMs per area |
| 5 | **World map**: region grid from the town map, plus stitched outdoor maps. Layer toggles for wild encounters, trainers, gyms and bosses, field effects, and items/TMs/HMs (phase 2) | yes, except items | item and pickup layer |

## Work split (sub-agents)

- **A, dex data**: `scripts/extract_dex.py` writes `pokemon.json`, `moves.json`,
  `abilities.json`, `items.json` and `types.json`.
- **B, battle and area data**: `scripts/extract_world.py` writes `areas.json` and
  `trainers.json`.
- **C, maps**: `scripts/build_maps.py` writes `regionmap.json` and stitched or
  downscaled map images, bundled as `app/maps/*.json` (base64 WebP). Bundling
  keeps the artifact under the 511-file limit.
- **D, front end**: `app/index.html`, one page with vanilla JS and no build step.
  It fetches `data/*.json` and works against fixtures until the real data lands.

## Integration

The lead agent runs the extractors, validates the output against the schema,
commits, and publishes `app/index.html` with `app/data/**` and `app/maps/**` as
artifact files.

## Phase 3: Live link to the running game (local only)

**Goal:** when the game runs on the same laptop, the guide follows it.
- Trainers and items tick themselves off as you beat or collect them.
- The current battle pops up: the trainer, their team, the active Pokémon's types and weaknesses, and the field effect.
- The map shows where the player is.

**Why it is feasible:** Reborn 19.5 runs on mkxp-z with plain Ruby scripts. At startup it loads every `./Data/Mods/*.rb` file (see `Scripts/idk.rb`, `startup`). A mod can read the game state (`$game_map`, `$game_player`, `$game_self_switches`, the active `PokeBattle_Battle`, `$fefieldeffect`) and write it to a JSON file.

**The artifact cannot do this.** It runs in a locked-down frame on claude.ai, with no access to local files or localhost. The live link therefore works only with the **local copy** of the page, served from the laptop. The published artifact stays as it is, and simply never shows live mode.

**Pieces:**
1. `bridge/mod/RebornFieldGuideLink.rb` is the game mod. It goes in `<game>/Data/Mods/`.
   - It writes `<game>/RFGLink/state.json` atomically (write a temp file, then rename).
   - Writes are throttled. It writes when the map or position changes, and on battle start, each battle turn and battle end.
   - It also writes when a self switch or progress flag changes.
   - It is read-only towards the game: it never changes game state, and on any error it logs to `RFGLink/error.log` and disables itself.
2. `bridge/serve.py` is a stdlib-only local server.
   - It serves `app/`, and `/live/state.json` from the game folder (`--game <path>`).
   - It binds to 127.0.0.1 only.
3. **Live mode in `app/index.html`.**
   - The page probes `live/state.json`. It gets a 404 in the artifact, where live mode stays hidden.
   - **Map:** a player marker on the map, with an auto-follow option.
   - **Auto-tick:** items and trainers are ticked off using self switches from `events.json` (map id + event id).
   - **Battle panel:** the opponent's trainer, team and active Pokémon, type matchups against the player's moves, and the current field effect, linked to the Fields page.
4. **`bridge/simulate.py`** writes a fake `state.json` sequence, so live mode can be tested without the game.
5. **`LOCAL_SETUP.md`** gives step-by-step setup for Windows, macOS and Linux.

**Limits:**
- The live link can't be fully tested without running the game on the laptop.
- The first real run may need small fixes to hook names.
