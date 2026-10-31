#!/usr/bin/env python3
"""Local server for the Reborn Field Guide (Python 3.9+, standard library only).

    python3 bridge/serve.py [--game "C:\\Games\\Pokemon Reborn"] [--port 8742] [--data DIR]

Serves app/ on http://127.0.0.1:<port>/ and adds:

  GET   /live/state.json   the game's <game>/RFGLink/state.json (written by the
                           mod bridge/mod/RebornFieldGuideLink.rb), with
                           battle.trainer[].id filled in; 404 when missing
  GET   /live/stream       Server-Sent Events: "state" whenever state.json changes,
                           "progress" whenever saved ticks change
  GET   /api/info          {app, version, live, game, dataDir, autoTick, port}
  GET   /api/progress      {"v":1, "ticks": {key: {on, src, t}}, "updated": ms}
  PUT   /api/progress      replace the whole object
  PATCH /api/progress      merge {key: {on, src, t} | null}  (null removes a key)

Ticks are stored in <data>/progress.json (atomic write: temp file, fsync,
os.replace; the previous version is kept as progress.json.bak). The default
data folder is per user: %APPDATA%\\RebornFieldGuide on Windows,
~/Library/Application Support/RebornFieldGuide on macOS,
$XDG_DATA_HOME/RebornFieldGuide (or ~/.local/share/RebornFieldGuide) on Linux.

Game auto-ticks: while state.json changes, the server compares its
progress.selfSwitches / switchesOn / variables with app/data/eventflags.json
and app/data/events.json and merges {"on": true, "src": "game"} for item
pickups ("pick:<areaId>:<ev>") and beaten trainers ("beat:<trainerId>").
It never changes a key whose last writer was the user.

Security: binds 127.0.0.1 only; every request must come from 127.0.0.1 with
Host "127.0.0.1:<port>" or "localhost:<port>" (blocks DNS rebinding); writes
must be application/json under 5 MB and, when the browser sends an Origin,
the Origin must be this server.
"""
import argparse
import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time
import urllib.parse

APP_NAME = "reborn-field-guide"
VERSION = "1.0.0"
DEFAULT_PORT = 8742
MAX_BODY = 5 * 1024 * 1024
MAX_KEYS = 200000
KEY_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,200}$")
POLL = 0.25
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_APP = os.path.join(REPO, "app")


def now_ms():
    return int(time.time() * 1000)


def default_data_dir():
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "RebornFieldGuide")


def clean_path(p):
    """Accept paths pasted or dragged in from Explorer/Finder: quotes, ~, trailing slashes."""
    if p is None:
        return None
    p = p.strip().strip('"').strip("'").strip()
    if not p:
        return None
    return os.path.normpath(os.path.expanduser(p))


def _replace(src, dst, tries=20):
    """os.replace with retries: on Windows a reader holding dst open makes it fail briefly."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)


def atomic_write(path, data, backup=True):
    """Write bytes to path atomically (temp file in the same folder + os.replace).

    With backup, the current file is first copied to <path>.bak (also atomically)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    if backup and os.path.exists(path):
        with open(path, "rb") as f:
            old = f.read()
        tmpb = path + ".bak.tmp"
        with open(tmpb, "wb") as f:
            f.write(old)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmpb, path + ".bak")
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def read_bytes(path, tries=10):
    """Read a file another process replaces atomically; retry briefly on Windows locks."""
    for i in range(tries):
        try:
            with open(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            if i >= 3:
                return None
            time.sleep(0.02)  # the mod may be between delete and rename
        except PermissionError:
            time.sleep(0.03)
    return None


# ------------------------------------------------------------------ progress --

class BadRequest(ValueError):
    pass


def valid_entry(k, v):
    if not isinstance(k, str) or not KEY_RE.match(k):
        raise BadRequest("bad key: %r" % (k,))
    if not isinstance(v, dict):
        raise BadRequest("entry for %s must be an object" % k)
    on = v.get("on")
    if not isinstance(on, bool):
        raise BadRequest("%s.on must be true or false" % k)
    src = v.get("src", "user")
    if src not in ("user", "game"):
        raise BadRequest("%s.src must be 'user' or 'game'" % k)
    t = v.get("t", now_ms())
    if not isinstance(t, (int, float)) or isinstance(t, bool):
        raise BadRequest("%s.t must be a number" % k)
    return {"on": on, "src": src, "t": int(t)}


class ProgressStore:
    def __init__(self, data_dir):
        self.dir = data_dir
        self.path = os.path.join(data_dir, "progress.json")
        self.lock = threading.RLock()
        self.version = 0
        self.obj = self._load()

    @staticmethod
    def empty():
        return {"v": 1, "ticks": {}, "updated": 0}

    def _parse(self, raw):
        o = json.loads(raw.decode("utf-8"))
        if not isinstance(o, dict) or not isinstance(o.get("ticks"), dict):
            raise ValueError("not a progress object")
        ticks = {}
        for k, v in o["ticks"].items():
            try:
                ticks[k] = valid_entry(k, v)
            except BadRequest:
                continue
        return {"v": 1, "ticks": ticks, "updated": int(o.get("updated") or 0)}

    def _load(self):
        for p in (self.path, self.path + ".bak"):
            raw = read_bytes(p) if os.path.exists(p) else None
            if raw is None:
                continue
            try:
                o = self._parse(raw)
                if p != self.path:
                    sys.stderr.write("progress.json was unreadable; restored from progress.json.bak\n")
                return o
            except (ValueError, UnicodeDecodeError):
                sys.stderr.write("could not read %s, ignoring it\n" % p)
        return self.empty()

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.obj))

    def _save(self):
        self.obj["updated"] = now_ms()
        data = json.dumps(self.obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        atomic_write(self.path, data, backup=True)
        self.version += 1

    def put(self, body):
        if not isinstance(body, dict) or not isinstance(body.get("ticks"), dict):
            raise BadRequest("expected {\"v\":1,\"ticks\":{...}}")
        if len(body["ticks"]) > MAX_KEYS:
            raise BadRequest("too many keys")
        ticks = {k: valid_entry(k, v) for k, v in body["ticks"].items()}
        with self.lock:
            old = self.obj
            self.obj = {"v": 1, "ticks": ticks, "updated": old.get("updated", 0)}
            try:
                self._save()
            except Exception:
                self.obj = old
                raise
            return self.get()

    def patch(self, body):
        if isinstance(body, dict) and isinstance(body.get("ticks"), dict) and "v" in body:
            body = body["ticks"]
        if not isinstance(body, dict):
            raise BadRequest("expected {key: {on, src, t}}")
        changes = {k: (None if v is None else valid_entry(k, v)) for k, v in body.items()}
        with self.lock:
            old = json.loads(json.dumps(self.obj))
            for k, v in changes.items():
                if v is None:
                    self.obj["ticks"].pop(k, None)
                else:
                    self.obj["ticks"][k] = v
            if len(self.obj["ticks"]) > MAX_KEYS:
                self.obj = old
                raise BadRequest("too many keys")
            try:
                self._save()
            except Exception:
                self.obj = old
                raise
            return self.get()

    def merge_game(self, keys):
        """Tick keys for the game; never touches a key the user set. Returns keys changed."""
        changed = []
        with self.lock:
            t = now_ms()
            for k in keys:
                cur = self.obj["ticks"].get(k)
                if cur is None or (cur["src"] == "game" and not cur["on"]):
                    self.obj["ticks"][k] = {"on": True, "src": "game", "t": t}
                    changed.append(k)
            if changed:
                self._save()
        return changed


# --------------------------------------------------------------- game link --

class GameFlags:
    """eventflags.json + events.json -> which progress keys a state.json implies."""

    def __init__(self, app_dir):
        self.rules = []   # (area, ev, flag, [keys])
        self.trainer_keys = {}
        self.ok = False
        fl_path = os.path.join(app_dir, "data", "eventflags.json")
        ev_path = os.path.join(app_dir, "data", "events.json")
        try:
            with open(fl_path, encoding="utf-8") as f:
                flags = json.load(f)
            with open(ev_path, encoding="utf-8") as f:
                events = json.load(f)
        except (OSError, ValueError) as e:
            sys.stderr.write("auto-tick off (%s)\n" % e)
            return
        self.trainer_keys = {k.upper(): v for k, v in (flags.get("_trainers") or {}).items()}
        for area, recs in events.items():
            if area.startswith("_") or not isinstance(recs, list):
                continue
            af = flags.get(area) or {}
            for r in recs:
                flag = af.get(str(r.get("ev")))
                if flag is None:
                    continue
                if r.get("kind") == "trainer":
                    keys = ["beat:" + t for t in (r.get("trainers") or ([r["trainer"]] if r.get("trainer") else []))]
                else:
                    keys = ["pick:%s:%s" % (area, r["ev"])]
                if keys:
                    self.rules.append((area, str(r["ev"]), flag, keys))
        self.ok = bool(self.rules)

    def done_keys(self, state):
        pr = (state or {}).get("progress")
        if not isinstance(pr, dict):
            return []
        ss = pr.get("selfSwitches") or {}
        on = set(pr.get("switchesOn") or [])
        var = pr.get("variables") or {}
        out = []
        for area, ev, flag, keys in self.rules:
            if isinstance(flag, str):
                hit = flag in ((ss.get(area) or {}).get(ev) or "")
            elif "switch" in flag:
                hit = flag["switch"] in on
            elif "var" in flag:
                v = var.get(str(flag["var"]))
                hit = isinstance(v, (int, float)) and v >= flag.get("min", 0)
            else:
                hit = False
            if hit:
                out.extend(keys)
        return out

    def trainer_id(self, key):
        return self.trainer_keys.get(str(key or "").upper())


class GameWatcher(threading.Thread):
    """Polls <game>/RFGLink/state.json; keeps the last good copy; auto-ticks."""

    def __init__(self, state_path, flags, store, autotick=True):
        super().__init__(daemon=True)
        self.state_path = state_path
        self.flags = flags
        self.store = store
        self.autotick = autotick and flags is not None and flags.ok
        self.lock = threading.Lock()
        self.mtime = None
        self.state = None
        self.raw = None
        self.version = 0
        self.stop_ev = threading.Event()

    def augment(self, st):
        b = st.get("battle")
        if isinstance(b, dict) and isinstance(b.get("trainer"), list) and self.flags:
            for t in b["trainer"]:
                if isinstance(t, dict) and t.get("key") and "id" not in t:
                    t["id"] = self.flags.trainer_id(t["key"])
        return st

    def poll_once(self):
        if not self.state_path:
            return
        try:
            m = os.stat(self.state_path).st_mtime_ns
        except OSError:
            return
        if m == self.mtime:
            return
        raw = read_bytes(self.state_path)
        if raw is None:
            return
        try:
            st = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return  # half-written by a non-atomic writer; try next poll
        if not isinstance(st, dict):
            return
        st = self.augment(st)
        with self.lock:
            self.mtime = m
            self.state = st
            self.raw = json.dumps(st, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.version += 1
        if self.autotick:
            try:
                ch = self.store.merge_game(self.flags.done_keys(st))
                if ch:
                    sys.stdout.write("game: ticked %d (%s%s)\n" % (len(ch), ", ".join(ch[:3]),
                                                                   ", ..." if len(ch) > 3 else ""))
            except OSError as e:
                sys.stderr.write("could not save auto-ticks: %s\n" % e)

    def current(self):
        """(raw bytes or None, version). None when the file is gone."""
        if not self.state_path or not os.path.exists(self.state_path):
            # the mod may be mid delete+rename on Windows: give it a moment
            time.sleep(0.05)
            if not self.state_path or not os.path.exists(self.state_path):
                return None, self.version
        self.poll_once()
        with self.lock:
            return self.raw, self.version

    def run(self):
        while not self.stop_ev.is_set():
            try:
                self.poll_once()
            except Exception as e:  # keep watching whatever happens
                sys.stderr.write("watcher: %s\n" % e)
            self.stop_ev.wait(POLL)


# ------------------------------------------------------------------- HTTP --

class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "RebornFieldGuide/" + VERSION
    protocol_version = "HTTP/1.1"
    # fixed types: on Windows the registry can map .js/.json to odd types
    extensions_map = dict(http.server.SimpleHTTPRequestHandler.extensions_map, **{
        ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
        ".webp": "image/webp", ".png": "image/png", ".md": "text/plain; charset=utf-8",
    })

    def list_directory(self, path):
        self.send_error(404, "not found")
        return None

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    # -- guards
    def _allowed(self):
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return False
        port = self.server.server_address[1]
        hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
        if (self.headers.get("Host") or "").lower() not in hosts:
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in {"http://" + h for h in hosts}:
            return False
        return True

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        elif not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, code, msg):
        if self.command not in ("GET", "HEAD"):
            self.close_connection = True  # an unread request body must not leak into the next request
        self._send(code, {"error": msg})

    def _body_json(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._err(415, "Content-Type must be application/json")
            return None
        try:
            n = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            n = -1
        if n < 0:
            self._err(411, "Content-Length required")
            return None
        if n > MAX_BODY:
            self.close_connection = True
            self._err(413, "body too large (limit 5 MB)")
            return None
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._err(400, "body is not valid JSON")
            return None

    # -- routing
    def _route(self):
        return urllib.parse.urlsplit(self.path).path

    def do_GET(self):
        if not self._allowed():
            return self._err(403, "forbidden")
        p = self._route()
        if p == "/live/state.json":
            raw, _ = self.server.watcher.current() if self.server.watcher else (None, 0)
            if raw is None:
                return self._err(404, "no live state (is the game running with the mod?)")
            return self._send(200, raw)
        if p == "/live/stream":
            return self._stream()
        if p == "/api/info":
            return self._send(200, self.server.info())
        if p == "/api/progress":
            return self._send(200, self.server.store.get())
        if p.startswith("/api/") or p.startswith("/live/"):
            return self._err(404, "not found")
        return super().do_GET()

    def do_HEAD(self):
        if not self._allowed():
            return self._err(403, "forbidden")
        return super().do_HEAD()

    def do_PUT(self):
        self._write("put")

    def do_PATCH(self):
        self._write("patch")

    def do_POST(self):
        if not self._allowed():
            return self._err(403, "forbidden")
        self._err(405, "use PUT or PATCH")

    def do_DELETE(self):
        self.do_POST()

    def do_OPTIONS(self):
        # no CORS: cross-origin pages get nothing
        if not self._allowed():
            return self._err(403, "forbidden")
        self._send(204, b"", extra={"Allow": "GET, HEAD, PUT, PATCH"})

    def _write(self, how):
        if not self._allowed():
            return self._err(403, "forbidden")
        if self._route() != "/api/progress":
            return self._err(404, "not found")
        body = self._body_json()
        if body is None:
            return
        try:
            out = self.server.store.put(body) if how == "put" else self.server.store.patch(body)
        except BadRequest as e:
            return self._err(400, str(e))
        except OSError as e:
            return self._err(500, "could not save progress: %s" % e)
        self._send(200, out)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        w = self.server.watcher
        st_ver, pr_ver, last_beat = -1, self.server.store.version, time.time()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            while not self.server.stopping.is_set():
                sent = False
                if w:
                    raw, v = w.current()
                    if raw is not None and v != st_ver:
                        st_ver = v
                        self.wfile.write(b"event: state\ndata: " + raw + b"\n\n")
                        sent = True
                if self.server.store.version != pr_ver:
                    pr_ver = self.server.store.version
                    data = json.dumps(self.server.store.get(), separators=(",", ":")).encode("utf-8")
                    self.wfile.write(b"event: progress\ndata: " + data + b"\n\n")
                    sent = True
                if not sent and time.time() - last_beat > 15:
                    self.wfile.write(b": keep-alive\n\n")
                    sent = True
                if sent:
                    last_beat = time.time()
                    self.wfile.flush()
                time.sleep(POLL)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def end_headers(self):
        p = self._route() if hasattr(self, "path") else ""
        if not (p.startswith("/api/") or p.startswith("/live/")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # POSIX: SO_REUSEADDR only lets us rebind past TIME_WAIT after a restart, so the
    # app keeps its port (and the browser keeps its per-origin settings).
    # Windows: SO_REUSEADDR would let another process bind the same port, so leave it
    # off there; Windows does not block a listener on TIME_WAIT anyway.
    allow_reuse_address = os.name != "nt"

    def __init__(self, port, app_dir, data_dir, game_dir=None, autotick=True, verbose=False):
        self.app_dir = os.path.abspath(app_dir)
        self.data_dir = os.path.abspath(data_dir)
        self.game_dir = os.path.abspath(game_dir) if game_dir else None
        self.verbose = verbose
        self.stopping = threading.Event()
        os.makedirs(self.data_dir, exist_ok=True)
        self.store = ProgressStore(self.data_dir)
        state_path = os.path.join(self.game_dir, "RFGLink", "state.json") if self.game_dir else None
        self.flags = GameFlags(self.app_dir) if self.game_dir else None
        self.watcher = GameWatcher(state_path, self.flags, self.store, autotick) if self.game_dir else None
        app = self.app_dir

        def make(*a, **kw):
            return Handler(*a, directory=app, **kw)
        super().__init__(("127.0.0.1", port), make)
        if self.watcher:
            self.watcher.start()

    def info(self):
        sp = self.watcher.state_path if self.watcher else None
        return {"app": APP_NAME, "version": VERSION,
                "live": bool(sp and os.path.exists(sp)),
                "game": self.game_dir, "dataDir": self.data_dir,
                "autoTick": bool(self.watcher and self.watcher.autotick),
                "port": self.server_address[1]}

    def shutdown(self):
        self.stopping.set()
        if self.watcher:
            self.watcher.stop_ev.set()
        super().shutdown()


def build_parser():
    ap = argparse.ArgumentParser(description="Serve the Reborn Field Guide locally (127.0.0.1 only).")
    ap.add_argument("--game", help="Pokémon Reborn game folder (the one with Game.exe and Data\\)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--data", help="folder for progress.json (default: %s)" % default_data_dir())
    ap.add_argument("--app", default=DEFAULT_APP, help="folder to serve (default: app/ in this repo)")
    ap.add_argument("--no-autotick", action="store_true", help="do not tick items/trainers from the game")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    return ap


def make_server(args):
    game = clean_path(args.game)
    if game and not os.path.isdir(game):
        raise SystemExit("game folder not found: %s" % game)
    data = clean_path(args.data) or default_data_dir()
    return Server(args.port, clean_path(args.app) or DEFAULT_APP, data, game,
                  autotick=not args.no_autotick, verbose=args.verbose)


def describe(srv):
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    lines = ["Reborn Field Guide is running at  %s" % url,
             "  progress file: %s" % srv.store.path]
    if srv.game_dir:
        lines.append("  game folder:   %s" % srv.game_dir)
        lines.append("  live state:    %s%s" % (srv.watcher.state_path,
                                               "" if os.path.exists(srv.watcher.state_path)
                                               else "  (not there yet: start the game with the mod)"))
        if not srv.watcher.autotick:
            lines.append("  auto-tick:     off")
    else:
        lines.append("  live link:     off (pass --game \"<Reborn folder>\")")
    lines.append("Press Ctrl+C to stop.")
    return url, "\n".join(lines)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        srv = make_server(args)
    except OSError as e:
        raise SystemExit("could not start on port %d: %s (is it already running? try --port %d)"
                         % (args.port, e, args.port + 1))
    url, text = describe(srv)
    print(text, flush=True)
    try:
        srv.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        srv.stopping.set()
        srv.server_close()


if __name__ == "__main__":
    main()
