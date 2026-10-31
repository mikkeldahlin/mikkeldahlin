"""Tests for bridge/serve.py (stdlib unittest).

    python3 -m unittest discover -s bridge/test -v
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import serve  # noqa: E402


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rfg-test ")  # space in the path on purpose
        self.app = os.path.join(self.tmp, "app")
        self.data = os.path.join(self.tmp, "App Data", "RebornFieldGuide")
        self.game = os.path.join(self.tmp, "Pokemon Reborn")
        os.makedirs(os.path.join(self.game, "Data"))
        write_json(os.path.join(self.app, "data", "events.json"), {
            "10": [{"ev": 9, "x": 36, "y": 3, "kind": "item", "item": "super-potion"},
                   {"ev": 12, "x": 30, "y": 7, "kind": "trainer", "trainer": "techie-naoman-1",
                    "trainers": ["techie-naoman-1"]},
                   {"ev": 20, "x": 1, "y": 1, "kind": "hidden", "item": "revive"}],
            "17": [{"ev": 24, "x": 5, "y": 5, "kind": "trainer", "trainer": "victoria-5", "trainers": ["victoria-5"]}],
            "8": [{"ev": 3, "x": 9, "y": 7, "kind": "trainer", "trainer": "a-1", "trainers": ["a-1", "b-1"]}],
            "_meta": {}})
        write_json(os.path.join(self.app, "data", "eventflags.json"), {
            "10": {"9": "A", "12": "A", "20": "A"}, "17": {"24": {"var": 107, "min": 25}},
            "8": {"3": {"switch": 2014}},
            "_trainers": {"TECHNERD|Naoman|0": "techie-naoman-1"}, "_meta": {}})
        with open(os.path.join(self.app, "index.html"), "w") as f:
            f.write("<!doctype html><title>t</title>")
        self.srv = serve.Server(0, self.app, self.data, self.game)
        self.port = self.srv.server_address[1]
        self.th = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.th.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, method, path, body=None, headers=None, host=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host if host is not None else "127.0.0.1:%d" % self.port}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if data is not None:
            h["Content-Length"] = str(len(data))
        h.update(headers or {})
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        c.endheaders()
        if data:
            c.send(data)
        r = c.getresponse()
        out = r.read()
        c.close()
        try:
            j = json.loads(out.decode()) if out else None
        except ValueError:
            j = None
        return r.status, j, r

    def state(self, progress=None, battle=None):
        write_json(os.path.join(self.game, "RFGLink", "state.json"), {
            "v": 1, "game": "reborn", "t": 1, "seq": 1,
            "player": {"map": 10, "x": 1, "y": 2, "dir": 2}, "party": [], "battle": battle,
            "progress": progress or {"selfSwitches": {}, "switchesOn": [], "variables": {}}})

    def wait_for(self, cond, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False


class TestProgressAPI(ServerCase):
    def test_initial_empty(self):
        st, j, _ = self.req("GET", "/api/progress")
        self.assertEqual(st, 200)
        self.assertEqual(j, {"v": 1, "ticks": {}, "updated": 0})

    def test_patch_merges_and_persists(self):
        st, j, _ = self.req("PATCH", "/api/progress", {"beat:techie-naoman-1": {"on": True, "src": "user", "t": 5}})
        self.assertEqual(st, 200)
        st, j, _ = self.req("PATCH", "/api/progress", {"pick:10:9": {"on": True, "src": "user", "t": 6}})
        self.assertEqual(set(j["ticks"]), {"beat:techie-naoman-1", "pick:10:9"})
        self.assertGreater(j["updated"], 0)
        with open(os.path.join(self.data, "progress.json"), encoding="utf-8") as f:
            disk = json.load(f)
        self.assertEqual(disk["ticks"]["pick:10:9"], {"on": True, "src": "user", "t": 6})
        # null removes; wrapped form {"v":1,"ticks":{...}} also accepted
        st, j, _ = self.req("PATCH", "/api/progress", {"v": 1, "ticks": {"pick:10:9": None}})
        self.assertEqual(set(j["ticks"]), {"beat:techie-naoman-1"})
        # a fresh store reads it back
        self.assertEqual(set(serve.ProgressStore(self.data).get()["ticks"]), {"beat:techie-naoman-1"})

    def test_put_replaces(self):
        self.req("PATCH", "/api/progress", {"pick:10:9": {"on": True}})
        st, j, _ = self.req("PUT", "/api/progress", {"v": 1, "ticks": {"pick:wiki-potion:0": {"on": True, "src": "user", "t": 1}}})
        self.assertEqual(st, 200)
        self.assertEqual(list(j["ticks"]), ["pick:wiki-potion:0"])

    def test_validation(self):
        st, _, _ = self.req("PATCH", "/api/progress", {"pick:1:2": {"on": "yes"}})
        self.assertEqual(st, 400)
        st, _, _ = self.req("PATCH", "/api/progress", {"bad key with spaces": {"on": True}})
        self.assertEqual(st, 400)
        st, _, _ = self.req("PATCH", "/api/progress", {"pick:1:2": {"on": True, "src": "hacker"}})
        self.assertEqual(st, 400)
        st, _, _ = self.req("PUT", "/api/progress", {"ticks": []})
        self.assertEqual(st, 400)
        st, _, _ = self.req("PATCH", "/api/progress", raw=b"{not json", headers={"Content-Type": "application/json"})
        self.assertEqual(st, 400)
        st, _, _ = self.req("PATCH", "/api/progress", raw=b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(st, 415)
        self.assertEqual(self.req("GET", "/api/progress")[1]["ticks"], {})

    def test_body_limit(self):
        # the server answers from the headers alone, before any of the body is sent
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest("PATCH", "/api/progress", skip_host=True)
        c.putheader("Host", "127.0.0.1:%d" % self.port)
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", str(serve.MAX_BODY + 1))
        c.endheaders()
        r = c.getresponse()
        self.assertEqual(r.status, 413)
        c.close()
        # just under the limit is fine
        ok = {"pick:1:%d" % i: {"on": True, "src": "user", "t": 1} for i in range(20000)}
        self.assertEqual(self.req("PUT", "/api/progress", {"v": 1, "ticks": ok})[0], 200)

    def test_methods(self):
        self.assertEqual(self.req("POST", "/api/progress", {"a": 1})[0], 405)
        self.assertEqual(self.req("PATCH", "/api/other", {})[0], 404)
        self.assertEqual(self.req("GET", "/api/nope")[0], 404)


class TestHostGuard(ServerCase):
    def test_allowed_hosts(self):
        self.assertEqual(self.req("GET", "/api/info")[0], 200)
        self.assertEqual(self.req("GET", "/api/info", host="localhost:%d" % self.port)[0], 200)

    def test_rebinding_hosts_blocked(self):
        for h in ("evil.example:%d" % self.port, "127.0.0.1", "localhost", "127.0.0.1:1",
                  "attacker.127.0.0.1.nip.io:%d" % self.port, ""):
            st, _, _ = self.req("GET", "/api/progress", host=h)
            self.assertEqual(st, 403, h)
            st, _, _ = self.req("PATCH", "/api/progress", {"pick:1:1": {"on": True}}, host=h)
            self.assertEqual(st, 403, h)
            st, _, _ = self.req("GET", "/index.html", host=h)
            self.assertEqual(st, 403, h)
        self.assertEqual(self.req("GET", "/api/progress")[1]["ticks"], {})

    def test_cross_origin_write_blocked(self):
        st, _, _ = self.req("PATCH", "/api/progress", {"pick:1:1": {"on": True}},
                            headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        st, _, _ = self.req("PATCH", "/api/progress", {"pick:1:1": {"on": True}},
                            headers={"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(st, 200)

    def test_no_cors_headers(self):
        st, _, r = self.req("OPTIONS", "/api/progress", headers={"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rfg-aw ")
        self.p = os.path.join(self.tmp, "sub dir", "progress.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_backup_rolls(self):
        serve.atomic_write(self.p, b"one")
        self.assertFalse(os.path.exists(self.p + ".bak"))
        serve.atomic_write(self.p, b"two")
        serve.atomic_write(self.p, b"three")
        with open(self.p, "rb") as f:
            self.assertEqual(f.read(), b"three")
        with open(self.p + ".bak", "rb") as f:
            self.assertEqual(f.read(), b"two")
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.p))), ["progress.json", "progress.json.bak"])

    def test_failed_replace_keeps_old_file(self):
        serve.atomic_write(self.p, b"good")
        real = os.replace

        def flaky(src, dst):
            if dst == self.p:
                raise OSError("disk full")
            return real(src, dst)
        with mock.patch("serve.os.replace", side_effect=flaky):
            with self.assertRaises(OSError):
                serve.atomic_write(self.p, b"bad")
        with open(self.p, "rb") as f:
            self.assertEqual(f.read(), b"good")
        self.assertFalse([n for n in os.listdir(os.path.dirname(self.p)) if n.endswith(".tmp")])

    def test_windows_lock_is_retried(self):
        real = os.replace
        calls = {"n": 0}

        def locked_twice(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError("in use by another process")
            return real(src, dst)
        with mock.patch("serve.os.replace", side_effect=locked_twice), mock.patch("serve.time.sleep"):
            serve.atomic_write(self.p, b"ok", backup=False)
        with open(self.p, "rb") as f:
            self.assertEqual(f.read(), b"ok")

    def test_store_rolls_back_on_save_failure(self):
        st = serve.ProgressStore(self.tmp)
        st.patch({"pick:1:1": {"on": True}})
        with mock.patch("serve.atomic_write", side_effect=OSError("nope")):
            with self.assertRaises(OSError):
                st.patch({"pick:1:2": {"on": True}})
        self.assertEqual(list(st.get()["ticks"]), ["pick:1:1"])

    def test_corrupt_file_recovers_from_bak(self):
        st = serve.ProgressStore(self.tmp)
        st.patch({"pick:1:1": {"on": True}})
        st.patch({"pick:1:2": {"on": True}})
        with open(os.path.join(self.tmp, "progress.json"), "wb") as f:
            f.write(b"{\"v\":1,\"ticks\":")  # truncated
        again = serve.ProgressStore(self.tmp)
        self.assertEqual(list(again.get()["ticks"]), ["pick:1:1"])


class TestLive(ServerCase):
    def test_state_404_then_200(self):
        st, j, r = self.req("GET", "/live/state.json")
        self.assertEqual(st, 404)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertFalse(self.req("GET", "/api/info")[1]["live"])
        self.state(battle={"trainer": [{"class": "TechNerd", "name": "Naoman", "key": "TECHNERD|Naoman|0"}]})
        st, j, r = self.req("GET", "/live/state.json")
        self.assertEqual(st, 200)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertEqual(j["battle"]["trainer"][0]["id"], "techie-naoman-1")
        info = self.req("GET", "/api/info")[1]
        self.assertTrue(info["live"])
        self.assertEqual(info["app"], "reborn-field-guide")
        self.assertEqual(info["game"], os.path.abspath(self.game))
        self.assertTrue(info["autoTick"])

    def test_autotick_respects_user(self):
        # the user had unticked the trainer by hand and ticked a hidden item
        self.req("PATCH", "/api/progress", {"beat:techie-naoman-1": {"on": False, "src": "user", "t": 1},
                                            "pick:10:20": {"on": True, "src": "user", "t": 2}})
        self.state({"selfSwitches": {"10": {"9": "A", "12": "A", "20": "B"}, "11": {"1": "A"}},
                    "switchesOn": [2014], "variables": {"107": 26}})
        ok = self.wait_for(lambda: "pick:10:9" in self.req("GET", "/api/progress")[1]["ticks"])
        self.assertTrue(ok)
        t = self.req("GET", "/api/progress")[1]["ticks"]
        self.assertEqual(t["pick:10:9"]["src"], "game")
        self.assertEqual(t["beat:techie-naoman-1"], {"on": False, "src": "user", "t": 1})  # untouched
        self.assertEqual(t["pick:10:20"]["src"], "user")                                 # untouched
        self.assertTrue(t["beat:victoria-5"]["on"])                                       # var >= min
        self.assertTrue(t["beat:a-1"]["on"] and t["beat:b-1"]["on"])                       # game switch, both opponents
        # the game never unticks: switches going off later changes nothing
        self.state({"selfSwitches": {}, "switchesOn": [], "variables": {}})
        time.sleep(0.6)
        self.assertTrue(self.req("GET", "/api/progress")[1]["ticks"]["pick:10:9"]["on"])

    def test_variable_below_min_not_ticked(self):
        self.state({"selfSwitches": {}, "switchesOn": [], "variables": {"107": 24}})
        time.sleep(0.6)
        self.assertNotIn("beat:victoria-5", self.req("GET", "/api/progress")[1]["ticks"])

    def test_stream_pushes_state(self):
        self.state()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/live/stream")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("text/event-stream"))
        buf = b""
        end = time.time() + 3
        while b"event: state" not in buf and time.time() < end:
            buf += r.fp.readline()
        self.assertIn(b"event: state", buf)
        c.close()


class TestStatic(ServerCase):
    def test_index_and_traversal(self):
        st, _, r = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("text/html"))
        st, _, _ = self.req("GET", "/../../etc/passwd")
        self.assertEqual(st, 404)
        st, _, _ = self.req("GET", "/data/")  # no directory listings
        self.assertEqual(st, 404)


class TestPaths(unittest.TestCase):
    def test_clean_path(self):
        self.assertEqual(serve.clean_path('  "C:\\Games\\Pokemon Reborn\\"  '),
                         os.path.normpath("C:\\Games\\Pokemon Reborn\\"))
        self.assertIsNone(serve.clean_path('""'))

    def test_default_data_dir(self):
        with mock.patch.object(serve.sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {"APPDATA": "C:\\Users\\Ana\\AppData\\Roaming"}):
            self.assertEqual(serve.default_data_dir(), os.path.join("C:\\Users\\Ana\\AppData\\Roaming", "RebornFieldGuide"))
        with mock.patch.object(serve.sys, "platform", "linux"), mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/x"}):
            self.assertEqual(serve.default_data_dir(), os.path.join("/x", "RebornFieldGuide"))
        with mock.patch.object(serve.sys, "platform", "darwin"):
            self.assertTrue(serve.default_data_dir().endswith(os.path.join("Library", "Application Support", "RebornFieldGuide")))


if __name__ == "__main__":
    unittest.main()


class TestRunHelpers(unittest.TestCase):
    def setUp(self):
        import run
        self.run = run
        self.tmp = tempfile.mkdtemp(prefix="rfg-run ")
        self.game = os.path.join(self.tmp, "Pokemon Reborn")
        os.makedirs(os.path.join(self.game, "Data"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mod_install_and_status(self):
        self.assertEqual(self.run.mod_status(self.game)[0], "missing")
        dst = self.run.install_mod(self.game)
        self.assertEqual(dst, os.path.join(self.game, "Data", "Mods", "RebornFieldGuideLink.rb"))
        self.assertEqual(self.run.mod_status(self.game)[0], "ok")
        with open(dst, "a") as f:
            f.write("# changed\n")
        self.assertEqual(self.run.mod_status(self.game)[0], "different")

    def test_config_remembers_game(self):
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        args = mock.Mock(game='"%s"' % self.game, forget_game=False)
        cfg = {}
        with mock.patch.object(self.run, "interactive", return_value=False):
            self.assertEqual(self.run.pick_game(args, cfg), os.path.normpath(self.game))
        self.run.save_config(data, cfg)
        cfg2 = self.run.load_config(data)
        args2 = mock.Mock(game=None, forget_game=False)
        with mock.patch.object(self.run, "interactive", return_value=False):
            self.assertEqual(self.run.pick_game(args2, cfg2), os.path.normpath(self.game))
        self.assertIsNone(self.run.pick_game(mock.Mock(game=None, forget_game=True), cfg2))
        self.assertNotIn("game", cfg2)
