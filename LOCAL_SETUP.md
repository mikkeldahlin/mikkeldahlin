# Running the Reborn Field Guide on your own computer

The local copy does everything the online version does. It also:

- saves your ticks (items picked up, trainers beaten) in a file on your computer, so they survive
  browser cleanups and work in any browser;
- can **follow your game live**: the map shows where you are, items and trainers tick themselves
  off as you collect or beat them, and the current battle pops up with the trainer, their team,
  type matchups and the field effect.

The live link needs a small, read-only mod in the game's `Data\Mods` folder. It only reads the
game and writes one file, `RFGLink\state.json`, next to the game. It never changes your save,
your party or anything else, and has no cheats. Your saves live elsewhere and are not touched
(on Windows: `%USERPROFILE%\Saved Games\Pokemon Reborn`).

> The online (claude.ai) version cannot connect to your game. Browsers do not let a web page read
> files on your computer, so live mode only appears in the local copy.

---

## Windows (main path)

### 1. Get the guide

**Either** download the ZIP: open
<https://github.com/mikkeldahlin/reborn-field-guide/archive/refs/heads/main.zip>, save it, then
right-click it and choose **Extract All…**. For example, extract it to `C:\RebornFieldGuide`.

**Or**, with Git installed:
```bat
git clone -b main https://github.com/mikkeldahlin/reborn-field-guide.git C:\RebornFieldGuide
```

### 2. Install Python 3 (once)

Download Python 3.9 or newer from <https://www.python.org/downloads/> and run the installer.
**On the first screen, tick "Add python.exe to PATH"**, then click *Install Now*. Nothing else is
needed: the guide uses only Python's standard library.

### 3. Get Pokémon Reborn

Download Reborn from the official site, <https://www.rebornevo.com/>, and extract it, for example
to `C:\Games\Pokemon Reborn`. The **game folder** is the one that contains `Game.exe` and a `Data`
folder. Paths with spaces are fine.

### 4. Start the guide

Open the guide folder and **double-click `bridge\RebornFieldGuide.bat`**.

- The first time, a black window asks for your Reborn folder. **Drag the game folder into that
  window and press Enter**, or just press Enter to use the guide without the live link.
- If the mod is not installed yet, it asks *"Copy it there now? [Y/n]"*. Press Enter to say yes.
  This creates `Data\Mods` in the game folder and copies `RebornFieldGuideLink.rb` into it.
- Your browser opens on **http://127.0.0.1:8742/**. Keep the black window open while you use the
  guide, and close it (or press Ctrl+C) to stop.

The game folder is remembered. After the first time, a double-click is all you need.

**Install the mod by hand instead (optional).** Drop the game folder onto `bridge\install_mod.bat`,
or double-click that file and paste the path. Or do it manually: create `Data\Mods` inside the
game folder and copy `bridge\mod\RebornFieldGuideLink.rb` into it.

**Desktop shortcut.** Right-click `bridge\RebornFieldGuide.bat`, choose *Show more options* →
*Send to* → *Desktop (create shortcut)*. You can rename the shortcut "Reborn Field Guide".

**Your ticks are saved in** `%APPDATA%\RebornFieldGuide\progress.json`, with the previous version
kept as `progress.json.bak`. The remembered game folder is in `config.json` in the same place.
To back up your progress, copy that folder.

### 5. Check that the live link works

1. Start Reborn (or restart it if it was already open: mods load at startup) and load your save.
2. Walk a step. A folder `RFGLink` with `state.json` appears in the game folder.
3. In the guide, the **Live** badge lights up and the map follows you.
4. In the black window you will see lines like `game: ticked 1 (pick:10:9)` as you collect items
   and beat trainers.

---

## macOS

1. Install Python 3.9+ from <https://www.python.org/downloads/>. macOS may already include one; check with `python3 --version`.
2. Get the guide (ZIP link above, then double-click it to extract), or run
   `git clone -b main https://github.com/mikkeldahlin/reborn-field-guide.git ~/RebornFieldGuide`.
3. Find the game folder, the one that contains `Data`. For the Mac app bundle, right-click
   `Reborn.app` → *Show Package Contents* → `Contents/Resources`. (Different builds can differ:
   look for the folder with `Data`.)
4. In Terminal:
   ```sh
   cd ~/RebornFieldGuide
   python3 bridge/run.py --game "/path/to/the/game folder"
   ```
   Answer *Y* to copy the mod. Next time `python3 bridge/run.py` is enough.
5. Shortcut (optional): create a file `Reborn Field Guide.command` on the Desktop that contains
   `cd ~/RebornFieldGuide && python3 bridge/run.py`, then run `chmod +x` on it in Terminal.
   Double-click it to start the guide.

Ticks are saved in `~/Library/Application Support/RebornFieldGuide/`.

## Linux

```sh
git clone -b main https://github.com/mikkeldahlin/reborn-field-guide.git ~/RebornFieldGuide
cd ~/RebornFieldGuide
python3 bridge/run.py --game "$HOME/Games/Pokemon Reborn"     # the folder that contains Data/
```
Shortcut (optional): create `~/.local/share/applications/reborn-field-guide.desktop` with
```
[Desktop Entry]
Type=Application
Name=Reborn Field Guide
Exec=sh -c 'cd ~/RebornFieldGuide && python3 bridge/run.py'
Terminal=true
```
Ticks are saved in `$XDG_DATA_HOME/RebornFieldGuide/` (normally `~/.local/share/RebornFieldGuide/`).

---

## Options

`run.py` takes these (on Windows they work as `RebornFieldGuide.bat --option`):

| option | |
|---|---|
| `--game PATH` | use (and remember) this game folder |
| `--forget-game` | stop using the live link |
| `--port N` | another port if 8742 is taken; the next free port is chosen automatically |
| `--data DIR` | keep progress.json somewhere else, such as a synced folder |
| `--install-mod` | copy or update the mod without asking |
| `--no-browser` | don't open the browser |
| `--no-autotick` | show the live position and battle, but don't tick anything automatically |

The server alone is `python bridge/serve.py --game "<game folder>" [--port 8742] [--data DIR]`.
It prints the URL to open.

The guide never unticks anything you ticked by hand. If you untick something the game ticked, it
stays unticked.

## Testing without the game

```sh
python bridge/simulate.py --out "%TEMP%\rfg\RFGLink"      # Windows (cmd); macOS/Linux: --out /tmp/rfg/RFGLink
python bridge/serve.py --game "%TEMP%\rfg"
```
This replays a walk across Blacksteam Factory 1F: an item ball, a battle against Techie Naoman's
real team, then a warp. Use `--loop` to repeat it and `--fast` to skip the delays.

## Uninstall

- **Mod:** delete `Data\Mods\RebornFieldGuideLink.rb` from the game folder. You can also delete the
  `RFGLink` folder next to the game.
- **Guide:** delete the guide folder. Delete `%APPDATA%\RebornFieldGuide` too, unless you want to
  keep your ticks.

## Troubleshooting

- **The black window says Python was not found.** Install Python from python.org and tick *Add
  python.exe to PATH*. If the Microsoft Store opens instead, turn off the `python.exe` "App
  execution alias": Settings → Apps → Advanced app settings → App execution aliases.
- **No `RFGLink` folder appears.** Check the mod is at `<game>\Data\Mods\RebornFieldGuideLink.rb`,
  not in a sub-folder, and that the file name ends in `.rb` (Windows can hide extensions). Then
  restart the game and walk a step on the map.
- **`RFGLink\error.log` exists.** The mod hit something it did not expect and **switched itself
  off** for that session. The game keeps running normally. Please send that file along with a bug
  report. It is plain text and contains no personal data beyond the game's file paths.
- **The Live badge stays off.** Check the black window shows the right `game folder:`, and that
  `live state:` points to a file that exists. Then open `http://127.0.0.1:8742/live/state.json`:
  you should see JSON, not "no live state".
- **Firewall prompts.** None are needed: the server only listens on `127.0.0.1`, which only your
  own computer can reach. If Windows asks anyway, you can click *Cancel*.
- **Port in use.** Another program is using 8742. The launcher picks the next free port and prints it.
- **The position lags or the battle panel looks a turn behind.** The mod writes at most 4 times a
  second on the map, and once per battle phase: at command selection and at end of round. Pause
  menus and the bag do not update it.
