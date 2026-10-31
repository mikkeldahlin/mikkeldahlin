#!/usr/bin/env python3
"""One-command launcher for the Reborn Field Guide (Python 3.9+, standard library only).

    python3 bridge/run.py                     # Windows: double-click bridge\\RebornFieldGuide.bat
    python3 bridge/run.py --game "C:\\Games\\Pokemon Reborn"

Starts the local server (bridge/serve.py) on http://127.0.0.1:8742/ and opens
your default browser on it. The game folder is remembered in
<data dir>/config.json after the first run, so next time no arguments are
needed. If the live-link mod is missing from <game>\\Data\\Mods, it offers to
copy it there (it asks first; it never touches your save files).

Options:
  --game PATH     Reborn game folder (the one that contains Data\\)
  --forget-game   clear the remembered game folder (guide only, no live link)
  --port N        port (default 8742; the next free one is used if it is taken)
  --data DIR      where progress.json and config.json live
  --no-browser    do not open the browser
  --install-mod   copy/update the mod without asking
"""
import argparse
import json
import os
import shutil
import sys
import threading
import urllib.request
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve  # noqa: E402

MOD_NAME = "RebornFieldGuideLink.rb"
MOD_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mod", MOD_NAME)


def load_config(data_dir):
    try:
        with open(os.path.join(data_dir, "config.json"), encoding="utf-8") as f:
            c = json.load(f)
            return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(data_dir, cfg):
    data = json.dumps(cfg, indent=1, ensure_ascii=False).encode("utf-8")
    serve.atomic_write(os.path.join(data_dir, "config.json"), data, backup=False)


def interactive():
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def ask(prompt):
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return ""


def looks_like_game(path):
    return bool(path) and os.path.isdir(os.path.join(path, "Data"))


def mod_status(game):
    dst = os.path.join(game, "Data", "Mods", MOD_NAME)
    if not os.path.exists(dst):
        return "missing", dst
    try:
        with open(dst, "rb") as a, open(MOD_SRC, "rb") as b:
            return ("ok" if a.read() == b.read() else "different"), dst
    except OSError:
        return "different", dst


def install_mod(game):
    mods = os.path.join(game, "Data", "Mods")
    os.makedirs(mods, exist_ok=True)
    dst = os.path.join(mods, MOD_NAME)
    shutil.copyfile(MOD_SRC, dst)
    return dst


def already_running(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/info" % port, timeout=1) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == serve.APP_NAME
    except Exception:
        return False


def pick_game(args, cfg):
    if args.forget_game:
        cfg.pop("game", None)
        return None
    game = serve.clean_path(args.game) or serve.clean_path(cfg.get("game"))
    if game and not looks_like_game(game):
        print("The game folder %s has no Data folder." % game)
        game = None
    if not game and interactive() and not cfg.get("askedGame"):
        print("Live link (optional): the guide can follow your game while you play.")
        print("Drag your Pokemon Reborn folder (the one with Game.exe) into this window and press")
        print("Enter, or just press Enter to use the guide without it.")
        while True:
            g = serve.clean_path(ask("Reborn folder: "))
            if not g:
                cfg["askedGame"] = True
                break
            if looks_like_game(g):
                game = g
                break
            print("  %s does not look like the game folder (no Data folder inside). Try again." % g)
    if game:
        cfg["game"] = game
    return game


def check_mod(game, args):
    st, dst = mod_status(game)
    if st == "ok":
        return
    what = "is not installed" if st == "missing" else "is an older/different version"
    if args.install_mod:
        print("Installed the live-link mod: %s" % install_mod(game))
        return
    if interactive():
        a = ask("The live-link mod %s in\n  %s\nCopy it there now? [Y/n] " % (what, os.path.dirname(dst)))
        if a.strip().lower() in ("", "y", "yes", "j", "ja"):
            print("Installed the live-link mod: %s" % install_mod(game))
            print("(Restart the game if it is running, so it loads the mod.)")
            return
    print("Note: the live-link mod %s. Copy\n  %s\ninto\n  %s" % (what, MOD_SRC, os.path.dirname(dst)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Start the Reborn Field Guide and open it in the browser.")
    ap.add_argument("--game")
    ap.add_argument("--forget-game", action="store_true")
    ap.add_argument("--port", type=int)
    ap.add_argument("--data")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--install-mod", action="store_true")
    ap.add_argument("--no-autotick", action="store_true")
    args = ap.parse_args(argv)

    data = serve.clean_path(args.data) or serve.default_data_dir()
    os.makedirs(data, exist_ok=True)
    cfg = load_config(data)
    game = pick_game(args, cfg)
    port = args.port or int(cfg.get("port") or serve.DEFAULT_PORT)
    save_config(data, cfg)
    if game:
        check_mod(game, args)

    srv = None
    for p in range(port, port + 10):
        try:
            srv = serve.Server(p, serve.DEFAULT_APP, data, game, autotick=not args.no_autotick)
            break
        except OSError:
            if already_running(p):
                url = "http://127.0.0.1:%d/" % p
                print("The guide is already running at %s - opening it." % url)
                if not args.no_browser:
                    webbrowser.open(url)
                return
    if srv is None:
        raise SystemExit("No free port between %d and %d." % (port, port + 9))
    url, text = serve.describe(srv)
    print(text, flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        srv.stopping.set()
        srv.server_close()


if __name__ == "__main__":
    main()
