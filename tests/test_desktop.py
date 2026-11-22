"""A desktop app (Tauri 2) in code and review: what the repository is (skills/code/desktop.py), how fx sets up its UI
check, and its front end in Chromium with Tauri's IPC answered by mocks (factory-ui.js, factory-desktop.js).

Standard library only; the browser part runs with Node and the factory's Playwright when they are there. The real app,
built for Windows and driven in WebView2, is not part of this layer.
    python3 -I -m unittest discover -s tests -p test_desktop.py
"""
import json
import errno
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_security import FxTestCase, ROOT, fx, load_json, write  # noqa: E402

CODE = os.path.join(ROOT, "plugins", "auto-factory", "skills", "code")
FIXTURE = os.path.join(ROOT, "tests", "desktop-fixture")
sys.path.insert(0, CODE)

import desktop as D  # noqa: E402

NODE = shutil.which("node")  # found now: a test runs with the PATH of a made-up machine

CONF = {"productName": "Report tool", "version": "1.4.2", "identifier": "test.factory.report",
        "build": {"devUrl": "http://localhost:1420", "beforeDevCommand": "npm run dev"},
        "app": {"windows": [{"title": "Report tool", "width": 1024, "height": 700}],
                "security": {"csp": "default-src 'self'"}}}

MOCKS = """module.exports = {
  load_settings: { theme: 'light' },
  save_settings: (args, { state }) => { state.saved = args.settings; return null; },
  load_report: async (args, { channel }) => {
    const c = channel(args.onProgress);
    await c.send(50); await c.send(100); await c.end();
    return { rows: 7 };
  },
  fails: () => { throw 'disk full'; },
};
"""


def repo(files):
    d = tempfile.mkdtemp(prefix="fx-desktop-")
    for name, body in files.items():
        write(os.path.join(d, name), body if isinstance(body, str) else json.dumps(body))
    return d


class Ctx:
    def __init__(self, cfg):
        self.cfg = cfg

    def c(self, dotted, default=None):
        v = self.cfg
        for k in dotted.split("."):
            if not isinstance(v, dict) or k not in v:
                return default
            v = v[k]
        return v


def node_modules():
    for d in (os.environ.get("FACTORY_TEST_NODE_PATH"), os.path.expanduser("~/.cache/code-factory/tools/node_modules")):
        if d and os.path.isdir(os.path.join(d, "playwright")):
            return d
    return None


class WhatTheRepositoryIsTest(unittest.TestCase):
    """desktop.py: a Tauri app from its files, and what its configuration says."""

    def tidy(self, d):
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def test_a_tauri_app_is_known_by_its_configuration_or_its_cli(self):
        self.assertEqual(D.app_kind(self.tidy(repo({"src-tauri/tauri.conf.json": CONF}))), "tauri")
        self.assertEqual(D.app_kind(self.tidy(repo({"src-tauri/Tauri.toml": "productName = 'x'\n"}))), "tauri")
        self.assertEqual(D.app_kind(self.tidy(repo({"package.json": {"devDependencies": {"@tauri-apps/cli": "2"}}}))),
                         "tauri")
        # the API alone can be a library's, a web's never has any of it
        self.assertIsNone(D.app_kind(self.tidy(repo({"package.json": {"dependencies": {"@tauri-apps/api": "2"}}}))))
        self.assertIsNone(D.app_kind(self.tidy(repo({"package.json": {"dependencies": {"vite": "6"}}}))))
        self.assertIsNone(D.app_kind(self.tidy(repo({"package.json": "[]"}))))
        self.assertIsNone(D.app_kind(None))

    def test_the_configuration_says_the_name_version_and_window(self):
        d = self.tidy(repo({"src-tauri/tauri.conf.json": CONF}))
        info = D.read_conf(d)
        self.assertEqual((info["name"], info["version"], info["identifier"]), ("Report tool", "1.4.2", "test.factory.report"))
        self.assertEqual(info["window"], [1024, 700])
        self.assertEqual(info["conf"], os.path.join("src-tauri", "tauri.conf.json"))
        self.assertEqual(info["dev_url"], "http://localhost:1420")
        self.assertEqual(info["csp"], "default-src 'self'")

    def test_a_version_read_from_package_json_and_json5_comments(self):
        d = self.tidy(repo({"package.json": {"version": "3.1.0"},
                            "src-tauri/tauri.conf.json5": "{\n  // the app\n  productName: 'x',\n"
                                                          "  \"productName\": \"Shop\", /* name */\n"
                                                          "  \"version\": \"../package.json\",\n"
                                                          "  \"app\": {\"windows\": [{\"label\": \"main\",},],},\n}\n"}))
        info = D.read_conf(d)
        self.assertEqual(info["version"], "3.1.0")
        self.assertEqual(info["window"], list(D.DEFAULT_WINDOW))   # no size given: Tauri's default

    def test_tauri_1_names(self):
        d = self.tidy(repo({"src-tauri/tauri.conf.json": {"package": {"productName": "Old", "version": "0.9.0"},
                                                          "tauri": {"windows": [{"width": 640, "height": 480}],
                                                                    "bundle": {"identifier": "old.app"}}}}))
        info = D.read_conf(d)
        self.assertEqual((info["name"], info["version"], info["identifier"], info["window"]),
                         ("Old", "0.9.0", "old.app", [640, 480]))

    def test_the_check_setup(self):
        d = self.tidy(repo({"src-tauri/tauri.conf.json": CONF, "ui/desktop-mocks.js": MOCKS}))
        setup, notes = D.check_setup({"framework": "tauri", "mocks": "ui/desktop-mocks.js"}, d)
        self.assertEqual(notes, [])
        self.assertEqual(setup["variants"], ["web", "desktop"])
        self.assertEqual(setup["viewports"], [[1024, 700]])
        self.assertEqual(setup["mocks"], os.path.realpath(os.path.join(d, "ui", "desktop-mocks.js")))
        self.assertEqual(setup["app"]["version"], "1.4.2")
        only, _ = D.check_setup({"framework": "tauri", "web": False, "viewports": [[1440, 900], [1024, 700]]}, d)
        self.assertEqual(only["variants"], ["desktop"])
        self.assertEqual(only["viewports"], [[1440, 900], [1024, 700]])

    def test_a_mocks_file_outside_the_repository_or_missing_is_not_read(self):
        d = self.tidy(repo({"src-tauri/tauri.conf.json": CONF}))
        setup, notes = D.check_setup({"framework": "tauri", "mocks": "../../etc/passwd"}, d)
        self.assertIsNone(setup["mocks"])
        self.assertIn("outside the repository", notes[0])
        setup, notes = D.check_setup({"framework": "tauri", "mocks": "ui/later.js"}, d)
        self.assertIsNone(setup["mocks"])
        self.assertIn("does not exist yet", notes[0])
        _, notes = D.check_setup({"framework": "tauri"}, self.tidy(repo({"package.json": {}})))
        self.assertIn("no tauri.conf.json", notes[0])


class FxDesktopTest(unittest.TestCase):
    """fx: the desktop block, a repository that became an app, and how the check is judged."""

    def test_the_desktop_block(self):
        self.assertIsNone(fx.desktop_conf(Ctx({})))
        self.assertIsNone(fx.desktop_conf(Ctx({"desktop": {"app": "later"}})))
        self.assertIsNone(fx.desktop_conf(Ctx({"desktop": {"framework": "electron"}})))
        self.assertTrue(fx.desktop_conf(Ctx({"desktop": {"framework": "tauri"}})))
        self.assertTrue(fx.repo_key("desktop"))
        self.assertTrue(fx.repo_key("desktop.mocks"))

    def test_a_repository_that_became_an_app_stops_and_asks_for_its_set_up(self):
        d = repo({"package.json": {"dependencies": {"vite": "6"}}})
        self.addCleanup(shutil.rmtree, d, True)
        fx.desktop_missing(Ctx({}), d)   # a web: nothing to ask
        write(os.path.join(d, "src-tauri", "tauri.conf.json"), json.dumps(CONF))
        with self.assertRaises(fx.FxError) as e:
            fx.desktop_missing(Ctx({"desktop": {"app": "later"}}), d)
        self.assertEqual(e.exception.extra["next"], "setup-desktop")
        self.assertEqual(e.exception.extra["where"], "desktop")
        fx.desktop_missing(Ctx({"desktop": {"app": "no"}}), d)   # init was told this repository is not the app

    def test_a_reviewed_repository_is_an_app_by_its_own_settings(self):
        ctx = Ctx({"desktop": {"framework": "tauri", "app": "this"}})
        web = fx.ReviewBoxCtx(ctx, {"primary": False, "name": "fe"})
        app = fx.ReviewBoxCtx(ctx, {"primary": False, "name": "app",
                                    "desktop": {"framework": "tauri", "mocks": "m.js"}})
        self.assertIsNone(fx.desktop_conf(web))
        self.assertEqual(fx.desktop_conf(app)["mocks"], "m.js")
        self.assertTrue(fx.desktop_conf(fx.ReviewBoxCtx(ctx, {"primary": True})))
        d = repo({"src-tauri/tauri.conf.json": CONF})
        self.addCleanup(shutil.rmtree, d, True)
        with self.assertRaises(fx.FxError) as e:
            fx.desktop_missing(web, d)
        self.assertEqual(e.exception.extra["where"], "review.repos[].desktop")

    def test_a_command_nothing_answers_fails_the_check(self):
        rep = {"steps": [{"name": "[web] AC1", "ok": True, "variant": "web"},
                         {"name": "[desktop] AC1", "ok": False, "error": "no", "variant": "desktop"}],
               "console": [{"text": "boom", "url": "http://x/", "variant": "desktop"}],
               "requests": [{"url": "http://x/api", "method": "GET", "status": 404, "variant": "web"}],
               "a11y": [], "design": [], "shots": [], "error": None,
               "desktop": {"variants": ["web", "desktop"], "mocks": "ui/desktop-mocks.js", "app": {"name": "Shop"},
                           "viewports": [[1024, 700]], "notes": ["desktop.mocks (ui/x.js) does not exist yet"]},
               "ipc": {"calls": [{"cmd": "plugin:app|version", "answered_by": "built-in"},
                                 {"cmd": "load_cart", "answered_by": "none"}], "missing": ["load_cart"]}}
        out = fx.ui_judge(Ctx({}), rep)
        joined = "\n".join(out["blocking"])
        self.assertIn("UI step failed: [desktop] AC1", joined)
        self.assertIn("[desktop] browser console error", joined)
        self.assertIn("load_cart", joined)
        self.assertIn("ui/desktop-mocks.js", joined)
        self.assertIn("[web] request GET http://x/api -> 404", out["notes"])
        self.assertTrue(any("does not exist yet" in n for n in out["notes"]))
        md = fx.ui_markdown(out)
        self.assertIn("## Desktop app", md)
        self.assertIn("| plugin:app\\|version | built-in | 1 |", md)   # a pipe of a command keeps the table whole

    def test_a_mocks_file_that_does_not_load_fails_the_check(self):
        rep = {"steps": [], "console": [], "requests": [], "a11y": [], "design": [], "error": None,
               "desktop": {"mocks": "ui/m.js"}, "ipc": {"calls": [], "missing": [], "mocks_error": "Unexpected token"}}
        self.assertIn("cannot be loaded", "\n".join(fx.ui_judge(Ctx({}), rep)["blocking"]))

    def test_a_web_report_is_judged_as_before(self):
        rep = {"steps": [{"name": "AC1", "ok": True}], "console": [{"text": "x", "url": "u"}], "requests": [],
               "a11y": [], "design": [], "error": None}
        out = fx.ui_judge(Ctx({}), rep)
        self.assertEqual(out["blocking"], ["browser console error on u: x"])
        self.assertNotIn("## Desktop app", fx.ui_markdown(out))

    def test_the_agents_are_told_how_the_app_is_checked(self):
        line = fx.desktop_line(Ctx({"desktop": {"framework": "tauri", "mocks": "ui/desktop-mocks.js"}}))
        for words in ("t.ipc.handle", "t.desktop", "t.ipc.mocked", "ui/desktop-mocks.js", "as the website", "cargo test",
                      "The real app is not run"):
            self.assertIn(words, line)
        real = fx.desktop_line(Ctx({"desktop": {"framework": "tauri", "web": False, "build": {"windows": "b"}}}))
        self.assertIn("runs as the mocked app (`desktop`) and on the real app (`app`)", real)
        self.assertIn("fx ui --app", real)


class UiRunTest(FxTestCase):
    """fx ui_run: a desktop app's check gets its setup; a repository that became one stops."""
    base_files = {"README.md": "project\n", "src-tauri/tauri.conf.json": json.dumps(CONF),
                  "ui/desktop-mocks.js": MOCKS}

    def run_ui(self):
        captured = {}

        def fake_run(cmd, cwd=None, check=True, capture=True, env=None, timeout=None, input_text=None, group=False):
            captured["env"] = env
            captured["setup"] = load_json(env["FACTORY_UI_DESKTOP"]) if env.get("FACTORY_UI_DESKTOP") else None
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(fx, "playwright_ok", return_value=(True, "ok")), \
                mock.patch.object(fx, "app_start", return_value={"url": "http://127.0.0.1:5100/"}), \
                mock.patch.object(fx, "run", side_effect=fake_run):
            fx.ui_run(self.f.ctx(), self.f.job(), os.path.join(self.tmp, "check.js"), os.path.join(self.tmp, "ui"))
        return captured

    def set_desktop(self, block):
        path = os.path.join(self.f.factory, "factory.json")
        conf = load_json(path)
        if block is None:
            conf.pop("desktop", None)
        else:
            conf["desktop"] = block
        write(path, json.dumps(conf, indent=1))

    def test_the_check_runs_as_the_website_and_the_app(self):
        self.set_desktop({"framework": "tauri", "mocks": "ui/desktop-mocks.js"})
        got = self.run_ui()
        setup = got["setup"]
        self.assertEqual(setup["variants"], ["web", "desktop"])
        self.assertEqual(setup["viewports"], [[1024, 700]])
        self.assertEqual(setup["mocks"], os.path.realpath(os.path.join(self.f.wt, "ui", "desktop-mocks.js")))
        self.assertEqual(setup["app"]["name"], "Report tool")
        self.assertIn("BASE_URL", got["env"])

    def test_a_web_gets_no_desktop_setup(self):
        self.set_desktop({"app": "no"})
        self.assertIsNone(self.run_ui()["setup"])

    def test_a_repository_that_became_an_app_stops(self):
        self.set_desktop(None)
        with self.assertRaises(fx.FxError) as e:
            self.run_ui()
        self.assertEqual(e.exception.extra["next"], "setup-desktop")


class RealAppPartsTest(unittest.TestCase):
    """desktop.py's half of the real app on Windows, on any machine: what it copies, writes and stops."""

    def tidy(self, d):
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def test_a_set_line_keeps_what_cmd_would_read_as_its_own(self):
        self.assertEqual(D.cmd_set("API", "http://127.0.0.1:5100/?a=1&b=2"), 'set "API=http://127.0.0.1:5100/?a=1&b=2"')
        self.assertEqual(D.cmd_set("P", "50%"), 'set "P=50%%"')
        self.assertEqual(D.cmd_set("Q", 'say "hi"'), 'set "Q=say hi"')

    def test_the_app_is_stopped_by_its_own_path_and_data_folder_only(self):
        script = D.stop_script(r"C:\x\runs\ab12\Shop-app.exe", r"C:\x\runs\ab12\webview")
        self.assertIn(r"$exe = 'C:\x\runs\ab12\Shop-app.exe'", script)
        self.assertIn("$_.ExecutablePath -eq $exe", script)
        self.assertIn("$_.CommandLine.Contains($data)", script)
        self.assertNotIn("Name", script)   # never every process of a name
        self.assertIn("O''Brien", D.stop_script(r"C:\O'Brien\a.exe", "d"))
        import base64
        self.assertTrue(base64.b64decode(D.ps_encoded("ü")).decode("utf-16-le").endswith("\nü"))

    def test_the_copy_on_windows_follows_the_clone_and_keeps_what_builds_slowly(self):
        src = self.tidy(repo({"README.md": "x", "src/app.ts": "a", "src-tauri/src/main.rs": "fn main(){}",
                              ".gitignore": "dist/\n.env\n", "dist/old.js": "built", ".env": "API=x"}))
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
        subprocess.run(["git", "add", "README.md", "src", "src-tauri", ".gitignore"], cwd=src, check=True)
        write(os.path.join(src, "src", "new.ts"), "not committed yet")
        files = D.files_to_copy(src, extra=[".env"])
        self.assertIn("src/new.ts", files)          # a builder checks before committing
        self.assertIn(".env", files)                # copy_into_worktree
        self.assertNotIn("dist/old.js", files)      # ignored
        dest = self.tidy(tempfile.mkdtemp())
        write(os.path.join(dest, "node_modules", "x", "index.js"), "kept")
        write(os.path.join(dest, "src-tauri", "target", "debug", "app.exe"), "kept")
        write(os.path.join(dest, "src", "gone.ts"), "removed")
        self.assertEqual(D.sync_tree(src, dest, files), len(files))
        self.assertEqual(D.sync_tree(src, dest, files), 0)   # nothing changed: nothing copied
        self.assertTrue(os.path.exists(os.path.join(dest, "node_modules", "x", "index.js")))
        self.assertTrue(os.path.exists(os.path.join(dest, "src-tauri", "target", "debug", "app.exe")))
        self.assertFalse(os.path.exists(os.path.join(dest, "src", "gone.ts")))

    def test_the_build_is_found_where_tauri_puts_it_or_where_the_project_says(self):
        work = self.tidy(repo({"src-tauri/target/debug/shop.exe": "x", "src-tauri/target/debug/deps/a.exe": "x",
                               "out/custom.exe": "y"}))
        self.assertTrue(D.find_artifact(work, {}).endswith(os.path.join("debug", "shop.exe")))
        self.assertTrue(D.find_artifact(work, {"artifact": {"windows": "out/*.exe"}}).endswith("custom.exe"))
        self.assertIsNone(D.find_artifact(work, {"artifact": {"windows": "nothing/*.exe"}}))

    def test_away_from_wsl_the_real_app_is_not_run(self):
        with mock.patch.object(D, "is_wsl", return_value=False):
            host = D.WinHost()
            self.assertEqual(host.status()[0]["name"], "windows")
            self.assertFalse(host.status()[0]["ok"])


class RealAppInFxTest(unittest.TestCase):
    """fx's half: when the real app runs, what a machine without the tools gets, and how it is judged."""

    def test_a_machine_without_the_tools_gets_a_note_not_a_failure(self):
        class Host:
            wsl = True

            def runner(self):
                return "tunnel", None

            def cargo(self):
                return None

            def msvc(self):
                return "x"
        with mock.patch.object(D, "WinHost", return_value=Host()):
            info = fx.desktop_app_run(Ctx({}), {"worktree": "/nowhere"}, "c.js", "/tmp", {"build": {"windows": "b"}},
                                      {}, "/tmp/r.json")
        self.assertIn("Rust is not installed", info["skipped"])
        rep = fx.ui_judge(Ctx({}), {"steps": [], "console": [], "requests": [], "a11y": [], "design": [], "error": None,
                                    "desktop": {"real": info}, "ipc": {"calls": [], "missing": []}})
        self.assertEqual(rep["blocking"], [])
        self.assertTrue(any("real app was not run" in n for n in rep["notes"]))

    def test_a_build_that_fails_is_the_jobs(self):
        rep = fx.ui_judge(Ctx({}), {"steps": [], "console": [], "requests": [], "a11y": [], "design": [], "error": None,
                                    "desktop": {"real": {"build_error": "the app's build exited with 101"}},
                                    "ipc": {"calls": [], "missing": [], "notes": ["t.ipc.handle('x') is ignored"]}})
        self.assertIn("did not build on Windows", rep["blocking"][0])
        self.assertTrue(any("ignored" in n for n in rep["notes"]))
        self.assertIn("The real app did not build", fx.ui_markdown(rep))

    def test_the_builder_asks_for_the_real_app_on_purpose(self):
        a = fx.build_parser().parse_args(["ui", "T-1", "--app"])
        self.assertTrue(a.app)
        self.assertFalse(fx.build_parser().parse_args(["ui", "T-1"]).app)
        a = fx.build_parser().parse_args(["desktop", "doctor"])
        self.assertEqual(a.action, "doctor")


class ReviewFixesTest(unittest.TestCase):
    """What a review of the desktop part found: each case it named, held."""

    def tidy(self, d):
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def git_repo(self, files):
        d = self.tidy(repo(files))
        for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@e.test", "commit", "-qm", "base"]):
            subprocess.run(["git"] + args, cwd=d, check=True)
        return d

    def test_a_new_file_changed_again_is_built_again(self):
        d = self.git_repo({"README.md": "x"})
        clean = fx.job_commit(d)
        self.assertNotIn("+", clean)
        write(os.path.join(d, "src", "NewView.tsx"), "one")
        first = fx.job_commit(d)
        write(os.path.join(d, "src", "NewView.tsx"), "two")   # same name, other content
        second = fx.job_commit(d)
        self.assertTrue(first.startswith(clean + "+"))
        self.assertNotEqual(first, second)
        os.remove(os.path.join(d, "src", "NewView.tsx"))
        write(os.path.join(d, "README.md"), "changed")
        self.assertNotEqual(fx.job_commit(d), clean)

    def test_a_held_lock_is_busy_any_other_error_is_said_at_once(self):
        path = os.path.join(self.tidy(tempfile.mkdtemp()), "k.lock")
        with D._Lock(path):
            with self.assertRaises(D.DesktopError) as e:
                with D._Lock(path, wait_sec=0):
                    pass
            self.assertIn("is running", str(e.exception))
        with D._Lock(path, wait_sec=0):   # free again
            pass
        with mock.patch.object(D.fcntl, "flock", side_effect=OSError(errno.ENOLCK, "No locks available")):
            with self.assertRaises(D.DesktopError) as e:
                with D._Lock(path):
                    pass
        self.assertIn("cannot lock", str(e.exception))
        self.assertFalse(D.lock_dir().startswith("/mnt/"))   # on WSL's own disk

    @unittest.skipUnless(NODE, "node is not installed")
    def test_every_call_is_seen_though_the_report_keeps_the_first(self):
        # the mocked run already kept its 500 (a front end that polls); the real app's run keeps its own 500 and sees all
        script = ("const D=require(process.argv[1]);const report={ipc:{calls:Array(500).fill({cmd:'status',variant:"
                  "'desktop'}),missing:[],kept:{desktop:500}}};"
                  "const m=D.ipcOfApp(report,()=>null);for(let i=0;i<600;i++)m.log.add({cmd:i<599?'status':'save',args:'{}'});"
                  "const a=D.ipcOfApp(report,()=>null);a.log.add({cmd:'later',args:'{}'});"
                  "process.stdout.write(JSON.stringify({seen:m.calls().length,save:m.calls('save').length,"
                  "kept:report.ipc.calls.length,dropped:report.ipc.calls_dropped,later:a.calls('later').length}))")
        p = subprocess.run([NODE, "-e", script, os.path.join(CODE, "factory-desktop.js")], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {"seen": 600, "save": 1, "kept": 1000, "dropped": 101, "later": 1})

    def test_json5_of_a_large_configuration(self):
        big = "{\n" + "".join("  key%d: 'v%d', // %d\n" % (i, i, i) for i in range(20000)) + "}\n"
        import time
        t0 = time.time()
        self.assertEqual(len(D._json5(big)), 20000)
        self.assertLess(time.time() - t0, 5)

    class Host:
        """Windows as fx's real-app run sees it, without Windows."""
        wsl = True

        def __init__(self, test, report_after):
            self.test, self.report_after, self.builds, self.ended = test, report_after, [], []

        def runner(self):
            return "tunnel", None

        def cargo(self):
            return "cargo.exe"

        def msvc(self):
            return "msvc"

        def node_exe(self):
            return "/mnt/c/node.exe"

        def build(self, conf, root, key, commit, env_vars, log, timeout=0, extra_files=()):
            self.builds.append(commit)
            return "/mnt/c/b/%s.exe" % commit, False

        def new_run(self, exe):
            return {"dir": "/x", "exe": "/mnt/c/r/a.exe", "exe_win": r"C:\r\a.exe", "data_win": r"C:\r\webview"}

        def free_port(self, start=9300):
            return 9301

        def end_run(self, run_):
            self.ended.append(run_)

    def app_run(self, report_after, env=None, run_raises=None):
        tmp = self.tidy(tempfile.mkdtemp())
        app = self.git_repo({"src-tauri/tauri.conf.json": json.dumps(CONF)})
        rp = os.path.join(tmp, "ui-report.json")
        write(rp, json.dumps({"steps": [], "desktop": {}, "ipc": {"calls": [], "missing": []}, "error": None}))
        host = self.Host(self, report_after)

        real_run = fx.run

        def fake_run(cmd, cwd=None, check=True, capture=True, env=None, timeout=None, input_text=None, group=False):
            if cmd[0] != "node":   # git and the rest: as they are
                return real_run(cmd, cwd=cwd, check=check, capture=capture, env=env, timeout=timeout,
                                input_text=input_text, group=group)
            if run_raises:
                raise run_raises
            if report_after is not None:
                write(rp, json.dumps(report_after))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        class C:
            factory = "/x/f"

            def c(self, k, d=None):
                return d
        with mock.patch.object(D, "WinHost", return_value=host), mock.patch.object(fx, "run", side_effect=fake_run), \
                mock.patch.object(fx, "app_db", return_value="db"):
            info = fx.desktop_app_run(C(), {"id": "J-1", "port": 5100, "worktree": app, "repo": "app"}, "c.js", tmp,
                                      {"framework": "tauri", "build": {"windows": "cargo build"}},
                                      env or {"BASE_URL": "http://127.0.0.1:5100"}, rp)
        return info, host

    def test_a_real_run_that_ends_is_fine(self):
        info, host = self.app_run({"desktop": {"real": {"finished": True}}, "error": None})
        self.assertNotIn("error", info)
        self.assertNotIn("skipped", info)
        self.assertEqual(len(host.ended), 1)   # the app is stopped and its run folder removed whatever happened

    def test_a_real_run_stopped_on_the_way_fails(self):
        info, host = self.app_run({"desktop": {"real": {}}, "error": None})   # killed before its end
        self.assertIn("stopped before its end", info["error"])
        info, host = self.app_run(None, run_raises=subprocess.TimeoutExpired("node", 10))
        self.assertIn("did not finish", info["error"])
        self.assertEqual(len(host.ended), 1)
        rep = fx.ui_judge(Ctx({}), {"steps": [], "console": [], "requests": [], "a11y": [], "design": [], "error": None,
                                    "desktop": {"real": info}, "ipc": {}})
        self.assertTrue(any("did not finish" in b for b in rep["blocking"]))

    def test_a_port_that_never_opens_is_the_machines(self):
        info, _ = self.app_run({"desktop": {"real": {"launch_error": "the app did not open WebView2's DevTools port",
                                                     "finished": True}}, "error": None})
        self.assertIn("did not open for the check here", info["skipped"])
        self.assertNotIn("error", info)

    def test_an_app_that_ends_while_starting_is_the_codes(self):
        info, _ = self.app_run({"desktop": {"real": {}}, "error": "[app] Error: the app ended while starting: ..."})
        self.assertNotIn("error", info)   # report.error already fails the check: not said twice

    def test_the_same_commit_for_other_addresses_is_another_build(self):
        _, a = self.app_run({"desktop": {"real": {"finished": True}}}, env={"BASE_URL": "http://127.0.0.1:5100"})
        _, b = self.app_run({"desktop": {"real": {"finished": True}}}, env={"BASE_URL": "http://127.0.0.1:5200"})
        self.assertNotEqual(a.builds[0].split("-")[-1], b.builds[0].split("-")[-1])


class RealAppUiRunTest(UiRunTest):
    """ui_run with desktop.build.windows: the real app runs after the browser, in the gate and in `fx ui --app`."""

    def test_the_real_app_runs_when_asked_and_its_answer_is_in_the_report(self):
        self.set_desktop({"framework": "tauri", "mocks": "ui/desktop-mocks.js", "build": {"windows": "npm run tauri build"}})
        seen = []

        def fake_run(cmd, cwd=None, check=True, capture=True, env=None, timeout=None, input_text=None, group=False):
            write(env["FACTORY_UI_REPORT"], json.dumps({"steps": [], "console": [], "requests": [], "a11y": [],
                                                        "design": [], "error": None, "desktop": {}, "ipc": {}}))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def fake_app(ctx, job, script, out_dir, conf, env, report_path):
            seen.append(conf["build"]["windows"])
            return {"skipped": "Rust is not installed on Windows"}
        for real_app, runs in ((True, 1), (False, 1)):
            with mock.patch.object(fx, "playwright_ok", return_value=(True, "ok")), \
                    mock.patch.object(fx, "app_start", return_value={"url": "http://127.0.0.1:5100/"}), \
                    mock.patch.object(fx, "run", side_effect=fake_run), \
                    mock.patch.object(fx, "desktop_app_run", side_effect=fake_app):
                rep = fx.ui_run(self.f.ctx(), self.f.job(), os.path.join(self.tmp, "check.js"),
                                os.path.join(self.tmp, "ui"), real_app=real_app)
            self.assertEqual(len(seen), runs)
        self.assertEqual(seen, ["npm run tauri build"])
        self.assertFalse(any("real app was not run" in n for n in rep["notes"]))   # the second run did not ask for it
        self.assertNotIn("real", rep["desktop"])


FAKE_APP = r"""// a stand-in for a Tauri app: Edge (a Chromium, like WebView2), opened the way WebView2 would be for it
const { spawn } = require('child_process');
const m = /--remote-debugging-port=(\d+)/.exec(process.env.WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS || '');
const data = process.env.WEBVIEW2_USER_DATA_FOLDER;
if (!m || !data) process.exit(3);
spawn(process.env.FAKE_EDGE, ['--headless=new', '--remote-debugging-port=' + m[1], '--user-data-dir=' + data,
  '--no-first-run', process.env.FAKE_APP_URL], { stdio: 'ignore' });
setInterval(() => {}, 1e6);
"""

APP_CHECK_JS = r"""
const ui = require(process.env.FACTORY_UI);
ui.run(async (t) => {
  await t.step('the real app opened', async () => {
    if (!t.desktop || !t.desktop.real) throw new Error('not the real app');
    await t.page.waitForSelector('#mode:text("web")', { timeout: 15000 });
  });
  await t.page.evaluate(() => localStorage.setItem('kept', 'yes'));
  await t.snap('main', { a11y: false });
  await t.restart();
  await t.step('its data is kept after a restart', async () => {
    const v = await t.page.evaluate(() => localStorage.getItem('kept'));
    if (v !== 'yes') throw new Error('after the restart: ' + v);
  });
});
"""

EDGE = "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"


@unittest.skipUnless(os.environ.get("FACTORY_TEST_WINDOWS") == "1" and D.is_wsl() and os.path.isfile(EDGE) and node_modules(),
                     "opens programs on Windows: set FACTORY_TEST_WINDOWS=1 in WSL with Edge and the factory's Playwright")
class OnWindowsTest(unittest.TestCase):
    """The real app's whole way on this machine, with a stand-in app: the copy and the build on Windows, the build kept
    by commit, the app started with WebView2's variables, the check from the right Node, a restart, the app stopped."""

    def test_a_stand_in_app_end_to_end(self):
        import http.server
        import threading
        host = D.WinHost()
        if host.runner()[1]:
            self.skipTest(host.runner()[1])
        tmp = tempfile.mkdtemp(prefix="fx-desktop-win-")
        self.addCleanup(shutil.rmtree, tmp, True)
        app = os.path.join(tmp, "repo")
        write(os.path.join(app, "index.html"), Path(FIXTURE, "index.html").read_text())
        write(os.path.join(app, "src-tauri", "tauri.conf.json"), json.dumps(CONF))
        write(os.path.join(app, "fakeapp.js"), FAKE_APP)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=app, check=True)
        subprocess.run(["git", "add", "-A"], cwd=app, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.test", "commit", "-qm", "base"], cwd=app, check=True)

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=app, **k)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        key = "test-%s-app" % uuid_hex()
        self.addCleanup(lambda: [shutil.rmtree(os.path.join(host.home(), d, key), True) for d in ("src", "builds")])

        class C:
            factory = "/x/test-%s" % key[5:-4]

            def c(self, k, d=None):
                return {"e2e_timeout_sec": 600}.get(k, d)
        fake = host.winpath(os.path.join(host.home(), "src", key, "fakeapp.js"))
        conf = {"framework": "tauri",
                "build": {"windows": 'mkdir src-tauri\\target\\debug 2>nul & copy /y "%s" src-tauri\\target\\debug\\fake.exe'
                          % host.winpath(host.node_exe())},
                "run_env": {"NODE_OPTIONS": "--require " + fake, "FAKE_EDGE": host.winpath(EDGE),
                            "FAKE_APP_URL": "http://127.0.0.1:%d/index.html" % srv.server_address[1]}}
        out = os.path.join(tmp, "out")
        rp = os.path.join(out, "ui-report.json")
        env = dict(os.environ, NODE_PATH=node_modules(), FACTORY_UI=os.path.join(CODE, "factory-ui.js"),
                   FACTORY_UI_REPORT=rp, FACTORY_UI_SHOTS=os.path.join(out, "shots"))
        script = os.path.join(tmp, "check.js")
        write(script, APP_CHECK_JS)
        job = {"id": "W-1", "port": 5300, "worktree": app, "repo": "app"}
        with mock.patch.object(fx, "app_db", return_value="db"), \
                mock.patch.object(D.WinHost, "cargo", return_value="cargo.exe"):
            first = fx.desktop_app_run(C(), job, script, out, conf, env, rp)
            self.assertFalse(first.get("skipped"), first)
            self.assertFalse(first["cached"])
            os.remove(rp)
            again = fx.desktop_app_run(C(), job, script, out, conf, env, rp)
        self.assertTrue(again["cached"], again)   # the same commit is not built twice
        rep = load_json(rp)
        self.assertIsNone(rep.get("error"), rep.get("error"))
        self.assertEqual([(s["name"], s["ok"]) for s in rep["steps"]],
                         [("[app] the real app opened", True), ("[app] its data is kept after a restart", True)])
        self.assertTrue(os.path.isfile(rep["shots"][0]["file"]))
        self.assertEqual(again["mode"], "straight (mirrored network)" if host.mirrored() else "through node.exe (NAT network)")
        left = host.ps("@(Get-CimInstance Win32_Process | Where-Object { $_.Name -ne 'powershell.exe' -and "
                       "$_.CommandLine -like '*%s\\desktop\\runs*' }).Count" % D.NAME).stdout.strip()
        self.assertEqual(left.splitlines()[-1], "0")   # nothing of the run is left running


def ico_32():
    """A 32x32 icon: Tauri's build embeds one in the .exe."""
    import struct
    w = h = 32
    pixels = struct.pack("<BBBB", 200, 120, 40, 255) * (w * h)
    mask = b"\0" * (4 * h)
    img = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0) + pixels + mask
    return struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(img), 22) + img


@unittest.skipUnless(os.environ.get("FACTORY_TEST_TAURI") == "1" and D.is_wsl() and node_modules(),
                     "builds a real Tauri app on Windows (Rust, MSVC; a few minutes the first time): set FACTORY_TEST_TAURI=1")
class TauriOnWindowsTest(unittest.TestCase):
    """A real Tauri 2 app (tests/desktop-fixture/tauri-app) through fx's real-app run: built on Windows, its WebView2
    reached with the factory's variables, its Rust side answering over http://ipc.localhost, a fresh start each run."""

    def test_a_real_tauri_app(self):
        host = D.WinHost()
        why = host.runner()[1] or (None if host.cargo() else "no Rust on Windows") or \
            (None if host.msvc() else "no MSVC build tools on Windows")
        if why:
            self.skipTest(why)
        tmp = tempfile.mkdtemp(prefix="fx-tauri-")
        self.addCleanup(shutil.rmtree, tmp, True)
        # its copy and Rust's target on Windows weigh about 2 GB: gone after the test
        self.addCleanup(lambda: [shutil.rmtree(os.path.join(host.home(), d, "test-tauri-app"), True)
                                 for d in ("src", "builds")])
        # and what Tauri keeps of the app by its identifier (its own data folder)
        self.addCleanup(lambda: shutil.rmtree(os.path.join(host.to_wsl(host.win_var("LOCALAPPDATA")),
                                                           "test.factory.realtauri"), True))
        app = os.path.join(tmp, "app")
        shutil.copytree(os.path.join(FIXTURE, "tauri-app"), app)
        os.makedirs(os.path.join(app, "src-tauri", "icons"))
        with open(os.path.join(app, "src-tauri", "icons", "icon.ico"), "wb") as f:
            f.write(ico_32())
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=app, check=True)
        subprocess.run(["git", "add", "-A"], cwd=app, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.test", "commit", "-qm", "app"], cwd=app, check=True)

        class C:
            factory = "/x/test-tauri"

            def c(self, k, d=None):
                return {"e2e_timeout_sec": 900}.get(k, d)
        conf = {"framework": "tauri", "build": {"windows": "cd src-tauri && cargo build"}, "build_timeout_sec": 3000}
        out = os.path.join(tmp, "out")
        rp = os.path.join(out, "ui-report.json")
        env = dict(os.environ, NODE_PATH=node_modules(), FACTORY_UI=os.path.join(CODE, "factory-ui.js"),
                   FACTORY_UI_REPORT=rp, FACTORY_UI_SHOTS=os.path.join(out, "shots"))
        job = {"id": "T-1", "port": 5300, "worktree": app, "repo": "app"}
        with mock.patch.object(fx, "app_db", return_value="db"):
            for _ in range(2):   # the second run opens the build of the first, from a fresh start again
                if os.path.exists(rp):
                    os.remove(rp)
                info = fx.desktop_app_run(C(), job, os.path.join(app, "check.js"), out, conf, env, rp)
                self.assertFalse(info.get("skipped") or info.get("build_error"), info)
        self.assertTrue(info["cached"])
        rep = load_json(rp)
        self.assertIsNone(rep.get("error"), rep.get("error"))
        self.assertEqual([s["name"] for s in rep["steps"] if not s["ok"]], [])
        self.assertEqual(len(rep["steps"]), 4)
        self.assertEqual(rep["desktop"]["real"]["origin"], "http://tauri.localhost")
        self.assertEqual(rep["desktop"]["real"]["window"], [900, 650])
        cmds = {c["cmd"] for c in rep["ipc"]["calls"]}
        self.assertTrue({"greet", "fails", "plugin:event|emit"} <= cmds, cmds)
        self.assertEqual(fx.ui_judge(Ctx({}), rep)["blocking"], [])


def uuid_hex():
    import uuid
    return uuid.uuid4().hex[:8]




CHECK_JS = r"""
const ui = require(process.env.FACTORY_UI);
const want = (sel, text) => async () => { await ui_page().waitForSelector(`${sel}:text("${text}")`, { timeout: 5000 }); };
let ui_page;
ui.run(async (t) => {
  ui_page = () => t.page;
  await t.goto(process.env.PAGE);
  await t.step('mode', want('#mode', t.desktop ? 'desktop' : 'web'));
  if (!t.desktop) return;
  await t.step('settings, machine and window', async () => {
    await want('#settings', 'theme light')();
    await want('#os', 'os windows')();
    await want('#ver', 'version 1.4.2')();
    await want('#size', 'size 1024')();
  });
  await t.step('an event from the Rust side', async () => {
    await t.ipc.emit('sync-done', { count: 3 });
    await want('#sync', 'synced 3')();
  });
  await t.step('a command and an event of the page', async () => {
    await t.page.click('#save');
    await want('#sync', 'local saved')();
    const c = t.ipc.calls('save_settings');
    if (c.length !== 1 || c[0].args.settings.theme !== 'dark') throw new Error(JSON.stringify(c));
  });
  await t.step('a link opened outside', async () => {
    await t.page.click('#help');
    await t.page.waitForTimeout(200);
    const c = t.ipc.calls('plugin:opener|open_url');
    if (c.length !== 1 || c[0].args.url !== 'https://example.test/help') throw new Error(JSON.stringify(c));
  });
  await t.page.click('#load');
  await t.step('a channel, in order, then the answer', want('#progress', 'progress 100 rows 7'));
  await t.page.click('#broken');
  await t.step('an Err of a command', want('#err', 'error disk full'));
  t.ipc.handle('load_settings', { theme: 'dark' });
  await t.goto(process.env.PAGE);
  await t.step('the script answers before the mocks file', want('#settings', 'theme dark'));
  await t.snap('main', { a11y: false });
  await t.page.evaluate(() => window.__TAURI_INTERNALS__.invoke('not_mocked').catch(() => {}));
});
"""


@unittest.skipUnless(NODE and node_modules(), "needs Node and Playwright for the factory")
class InTheBrowserTest(unittest.TestCase):
    """factory-ui.js and factory-desktop.js on a made-up Tauri 2 front end, in Chromium."""

    def test_the_front_end_runs_as_the_website_and_as_the_app(self):
        tmp = tempfile.mkdtemp(prefix="fx-desktop-ui-")
        self.addCleanup(shutil.rmtree, tmp, True)
        app = os.path.join(tmp, "repo")
        write(os.path.join(app, "src-tauri", "tauri.conf.json"), json.dumps(CONF))
        write(os.path.join(app, "ui", "desktop-mocks.js"), MOCKS)
        setup, _ = D.check_setup({"framework": "tauri", "mocks": "ui/desktop-mocks.js"}, app)
        out = os.path.join(tmp, "out")
        write(os.path.join(out, "desktop.json"), json.dumps(setup))
        write(os.path.join(tmp, "check.js"), CHECK_JS)
        env = dict(os.environ, NODE_PATH=node_modules(), PAGE=Path(FIXTURE, "index.html").as_uri(),
                   PATH=os.path.dirname(NODE) + os.pathsep + os.environ.get("PATH", ""),
                   FACTORY_UI=os.path.join(CODE, "factory-ui.js"), FACTORY_UI_REPORT=os.path.join(out, "ui-report.json"),
                   FACTORY_UI_SHOTS=os.path.join(out, "shots"), FACTORY_UI_VIEWPORTS="[[1280,800],[390,844]]",
                   FACTORY_UI_DESKTOP=os.path.join(out, "desktop.json"), HOME=os.path.expanduser("~"))
        p = subprocess.run([NODE, os.path.join(tmp, "check.js")], cwd=tmp, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=300)
        rep = load_json(os.path.join(out, "ui-report.json"))
        if rep.get("browser_missing"):
            self.skipTest("Playwright could not launch Chromium: %s" % rep["browser_missing"])
        self.assertIsNone(rep.get("error"), p.stdout)
        failed = [(s["name"], s.get("error")) for s in rep["steps"] if not s["ok"]]
        self.assertEqual(failed, [])
        self.assertEqual([s["name"] for s in rep["steps"]][:2], ["[web] mode", "[desktop] mode"])
        self.assertEqual(rep["console"], [])
        self.assertEqual(rep["ipc"]["missing"], ["not_mocked"])
        by = {(c["cmd"], c["answered_by"]) for c in rep["ipc"]["calls"]}
        for k in (("load_settings", "mocks"), ("load_settings", "script"), ("plugin:app|version", "built-in"),
                  ("plugin:opener|open_url", "built-in"), ("fails", "mocks"), ("not_mocked", "none")):
            self.assertIn(k, by)
        self.assertNotIn("plugin:event|listen", {c["cmd"] for c in rep["ipc"]["calls"]})   # events stay in the page
        self.assertEqual([(s["name"], s["viewport"]) for s in rep["shots"]], [("[desktop] main", "1024x700")])
        self.assertTrue(os.path.isfile(os.path.join(out, "shots", "desktop-main-1024x700.png")))
        judged = fx.ui_judge(Ctx({}), rep)
        self.assertEqual(len(judged["blocking"]), 1, judged["blocking"])
        self.assertIn("not_mocked", judged["blocking"][0])


if __name__ == "__main__":
    unittest.main()
