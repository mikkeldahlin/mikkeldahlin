# Data contract (app/data/*.json)

All files are UTF-8 JSON and minified. IDs are lowercase kebab slugs, the same
slugs the reborn-db-site uses in its file names (`abra`, `tm01`, `anomaly-tapu-lele`).
Areas use their numeric map id as a string (`"180"`).
Type names are Title Case: `"Psychic"`.

Producers may add fields. They must not rename or remove the ones listed here.
Leave out unknown values or set them to `null`; never invent data.

## meta.json
```json
{ "game": "Pokémon Reborn", "version": "19.16", "generated": "ISO date",
  "sources": ["https://github.com/Andrew-Foote/reborn-db-site", "..."] }
```

## types.json
```json
{ "types": ["Normal", "Fire", "..."],
  "chart": { "Fire": { "Grass": 2, "Water": 0.5, "Rock": 0.5, "Fire": 0.5, "Dragon": 0.5, "Bug": 2, "Ice": 2, "Steel": 2 } } }
```
`chart[attacker][defender]` is the multiplier. Missing pairs mean 1.

## pokemon.json, an array
```json
{ "id": "abra", "form": null, "name": "Abra", "dex": 63,
  "types": ["Psychic"],
  "abilities": ["Synchronize", "Inner Focus"], "hiddenAbility": "Magic Guard",
  "stats": { "hp": 25, "atk": 20, "def": 15, "spa": 105, "spd": 55, "spe": 90 },
  "evYield": { "spa": 1 },
  "catchRate": 200, "genderRatio": "75% male, 25% female", "growthRate": "Medium Slow",
  "eggGroups": ["Human-Like"], "heldItems": [{ "item": "Twisted Spoon", "chance": 5 }],
  "evolutions": [{ "to": "kadabra", "method": "Level 16" }],
  "evolvesFrom": null,
  "moves": {
    "level": [{ "level": 1, "move": "teleport" }],
    "tm": ["tm04"],
    "tutor": ["move-slug"],
    "egg": ["move-slug"]
  },
  "locations": [{ "area": "103", "method": "Grass", "levels": "5-8", "rate": 20, "time": "Day" }],
  "special": ["free-text special encounter notes, e.g. gift or static"],
  "sprite": "data:image/png;base64,..."
}
```
Rules:
- A form is a separate entry, with `id` as `"<base>-<form-slug>"` and `form` set to the form name.
- Move references use move ids from `moves.json`. For TMs, store the move id and the TM number in `moves.tm` as `{ "tm": "TM04", "move": "calm-mind" }`.
- Keep `sprite` small (the front sprite only), or leave it out and put sprites in `sprites.json` as a map from `id` to data URI, so the main file stays light.

## moves.json, an array
```json
{ "id": "moonblast", "name": "Moonblast", "type": "Fairy", "category": "Special",
  "power": 95, "accuracy": 100, "pp": 15, "priority": 0, "target": "...",
  "description": "...", "tm": "TM??" | null }
```

## abilities.json, an array
`{ "id", "name", "description" }`

## items.json, an array
`{ "id", "name", "pocket", "buy", "sell", "description", "move": "move-id or null", "locations": [] }`
`locations` stays empty in phase 1. In phase 2 each entry is `{ "area": "180", "note": "hidden, behind the rock", "x": 12, "y": 9 }`.

## areas.json, an array
```json
{ "id": "180", "name": "Grand Stairway B3F", "parent": "177",
  "region": "Reborn City" | null,
  "outdoor": false, "fieldEffect": "Crystal Cavern" | null, "weather": null,
  "encounters": [
    { "method": "Grass" | "Cave" | "Surf" | "Old Rod" | "Good Rod" | "Super Rod" | "Rock Smash" | "Headbutt" | "...",
      "time": "Morning" | "Day" | "Night" | null,
      "pokemon": "abra", "levels": "5-8", "rate": 20 }
  ],
  "trainers": ["trainer-id", "..."],
  "image": { "bundle": "maps-03", "key": "180", "w": 640, "h": 480 } | null }
```

## trainers.json, an array
```json
{ "id": "anomaly-tapu-lele", "class": "Anomaly", "name": "Tapu Lele",
  "important": true, "kind": "gym" | "boss" | "rival" | "elite" | "regular",
  "battle": "Single" | "Double",
  "locations": [{ "area": "855", "note": "free text", "dialogue": "Lel lel!" }],
  "items": ["Full Restore"],
  "team": [
    { "pokemon": "clefable", "level": 125, "item": "Leftovers", "ability": "Unaware",
      "nature": "Calm", "gender": null, "shiny": false,
      "moves": ["stealth-rock", "moonblast", "cosmic-power", "wish"],
      "evs": { "hp": 252, "atk": 0, "def": 252, "spa": 0, "spd": 0, "spe": 4 },
      "ivs": { "hp": 31, "atk": 31, "def": 31, "spa": 31, "spd": 31, "spe": 31 },
      "stats": { "hp": 490, "atk": 196, "def": 305, "spa": 281, "spd": 294, "spe": 195 } }
  ],
  "order": 123 }
```
`important` and `kind` drive the star badges. Use the trainer class (Gym Leader, Elite Four, Meteor Admin and so on) and boss markers on the site. `order` is an approximate story order when one can be derived, otherwise `null`.

## regionmap.json
```json
{ "grid": { "w": 20, "h": 20 },
  "points": [{ "x": 7, "y": 14, "name": "Opal Ward", "sub": "Grand Hall", "areas": ["519"] }],
  "stitched": [{ "id": "reborn-city", "name": "Reborn City",
                 "bundle": "world", "key": "reborn-city", "w": 2048, "h": 1536, "scale": 0.25,
                 "areas": [{ "area": "142", "x": 0, "y": 0, "w": 640, "h": 480 }] }] }
```
`x`, `y`, `w` and `h` inside `stitched[].areas` are pixel boxes in the stitched image, so the front end can place layer markers per area.

## maps bundles (app/maps/<bundle>.json)
`{ "<key>": "data:image/webp;base64,..." }`. Keep each bundle under 8 MB and keep fewer than 60 bundle files in total.
