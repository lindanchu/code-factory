"""Screens measured against the design package in code and review (/auto-factory:code, /auto-factory:review).

The auto test's comparer runs as it is, from a throw-away folder laid out like a test repository, with the thresholds,
the screen size and the profile (how each screen's data is read) of the project's auto test repository. A heavy
difference does not stop the gate: the UI reviewer, on Opus, decides each one in a `## Design` table.

Standard library only. The browser test needs Node and the factory's Playwright (or FACTORY_TEST_NODE_PATH), and is
skipped without them.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile

from test_security import FxTestCase, ROOT, fx, load_json, sh, slurp, write

FIXTURE = os.path.join(ROOT, "tests", "autotest-fixture")
PACKAGE = os.path.join(FIXTURE, "design-src", "v0.1")


def make_test_repo(root, conventions=True, profile=True, viewport=None, current="v0.1"):
    """An auto test repository like the fixture's, as a git repository: the factory reads it from a clone."""
    repo = os.path.join(root, "autotest-proj")
    os.makedirs(os.path.join(repo, "web", "lib"))
    cfg = load_json(os.path.join(FIXTURE, "autotest.json"))
    if not conventions:
        cfg.pop("conventions", None)
    if viewport:
        cfg.setdefault("run", {})["viewport"] = viewport
    cfg["design"]["current"] = current
    write(os.path.join(repo, "autotest.json"), json.dumps(cfg, indent=1, ensure_ascii=False))
    shutil.copytree(os.path.join(FIXTURE, "design-src"), os.path.join(repo, "design-src"))
    if profile:
        for name in ("ui-profile.js", "ui-profile.map.json"):
            shutil.copyfile(os.path.join(FIXTURE, "web", "lib", name), os.path.join(repo, "web", "lib", name))
    # what else a test repository keeps in lib/ is not the comparer's and never leaves it
    write(os.path.join(repo, "web", "lib", "accounts.js"), "module.exports = { password: 'not for the factory' };\n")
    sh(["git", "init", "-q", "-b", "main"], repo)
    sh(["git", "add", "-A"], repo)
    sh(["git", "commit", "-q", "-m", "tests"], repo)
    return repo


class DesignCase(FxTestCase):
    def use_repo(self, **kw):
        self.repo = make_test_repo(self.tmp, **kw)
        self.set_cfg(ui_checks={"design": {"test_repo": self.repo}})

    def set_cfg(self, **kw):
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg.update(kw)
        write(path, json.dumps(cfg, indent=1))

    def job_package(self, with_index=False, name="v0.2"):
        """The ticket's design package, unpacked in the job folder as fx does."""
        dest = os.path.join(self.f.jd, "design", name)
        shutil.copytree(PACKAGE, dest)
        if not with_index:
            os.remove(os.path.join(dest, "autotest-states.json"))
        self.f.save_job(design={"dir": os.path.join(self.f.jd, "design"), "files": [name + ".zip"]})
        return dest

    def info(self):
        return fx.design_info(self.f.ctx(), self.f.job())


class WhatIsMeasuredWithTest(DesignCase):
    def test_the_tickets_package_gets_its_state_table_and_the_repositorys_rules(self):
        self.use_repo()
        pkg = self.job_package()
        info = self.info()
        self.assertIsNone(info["why"])
        self.assertEqual([p["dir"] for p in info["packages"]], [pkg])
        self.assertTrue(os.path.isfile(os.path.join(pkg, "autotest-states.json")))  # made as the auto test makes it
        self.assertIn("BK-1", info["packages"][0]["codes"])
        self.assertEqual(info["viewport"], [1440, 900])  # the auto test's own screen size
        self.assertEqual(info["thresholds"], {"ui_threshold": "medium", "ui_percent_from": 4})
        self.assertEqual(sorted(os.path.basename(p) for p in info["profile"]), ["ui-profile.js", "ui-profile.map.json"])

    def test_without_a_package_of_the_ticket_the_repositorys_current_one(self):
        self.use_repo()
        info = self.info()
        self.assertIsNone(info["why"])
        self.assertEqual(os.path.basename(info["packages"][0]["dir"]), "v0.1")

    def test_the_screen_size_of_the_auto_test(self):
        self.use_repo(viewport="1280x720")
        self.assertEqual(self.info()["viewport"], [1280, 720])

    def test_without_an_auto_test_repository_nothing_is_measured_and_it_says_why(self):
        self.job_package()
        info = self.info()
        self.assertIn("ui_checks.design.test_repo", info["why"])
        self.assertIn("BK-1", info["packages"][0]["codes"])  # the reviewer still knows the states

    def test_without_any_package(self):
        self.use_repo(current="")
        self.assertIn("design package", self.info()["why"])

    def test_the_folder_the_comparer_runs_in(self):
        self.use_repo()
        self.job_package()
        env, tmp, info = fx.design_setup(self.f.ctx(), self.f.job())
        self.addCleanup(shutil.rmtree, tmp, True)
        setup = load_json(env["FACTORY_UI_DESIGN"])
        self.assertEqual(setup["viewport"], [1440, 900])
        self.assertIsNone(setup["why"])
        (pk,) = setup["packages"]
        self.assertIn("BK-1", pk["codes"])
        root = pk["root"]
        conf = load_json(os.path.join(root, "autotest.json"))
        # the comparer judges; whether a heavy difference blocks is the reviewer's to say, never a failed step
        self.assertEqual(conf["conventions"]["ui_bad"], {"value": "column"})
        self.assertEqual(conf["conventions"]["ui_threshold"], {"value": "medium"})
        self.assertEqual(os.path.join(conf["design"]["dir"], conf["design"]["current"]), info["packages"][0]["dir"])
        self.assertTrue(os.path.isfile(os.path.join(root, "web", "autotest", "uicheck", "index.js")))
        self.assertEqual(sorted(os.listdir(os.path.join(root, "web", "lib"))), ["ui-profile.js", "ui-profile.map.json"])
        self.assertFalse(tmp.startswith(self.f.jd))  # nothing of the test repository lands in the job folder

    def test_a_repository_that_cannot_be_read(self):
        self.set_cfg(ui_checks={"design": {"test_repo": os.path.join(self.tmp, "no-such-repo")}})
        self.job_package()
        self.assertIn("could not", self.info()["why"])


class JudgedTest(DesignCase):
    REPORT = {"steps": [{"name": "AC1", "ok": True}], "shots": [], "console": [], "requests": [], "a11y": [],
              "design": [
                  {"code": "BK-1", "name": "BK-1 Danh sách", "level": "OK", "diffs": [], "web": "/s/1-web.png",
                   "design": "/s/1-design.png"},
                  {"code": "BK-2", "name": "BK-2 Thêm sách", "level": "BAD", "web": "/s/2-web.png",
                   "design": "/s/2-design.png", "marked": "/s/2-marked.png",
                   "diffs": [{"element": "Nút \"Lưu\"", "type": "BACKGROUND_COLOR", "level": "BAD",
                              "design": "màu nền: #1F6FEB", "actual": "#DC2626"}]},
                  {"code": "BK-3", "name": "BK-3", "level": "MINOR", "diffs": [
                      {"element": "Chữ \"Mã\"", "type": "FONT", "level": "MINOR", "design": "cỡ chữ: 14px", "actual": "15px"}]},
                  {"code": "RP-1", "name": "RP-1", "level": None, "notComparable": "trang design chưa nhận dữ liệu"}]}

    def test_a_heavy_difference_is_for_the_reviewer_not_a_failed_gate(self):
        rep_ = fx.ui_judge(self.f.ctx(), json.loads(json.dumps(self.REPORT)))
        self.assertEqual(rep_["blocking"], [])
        self.assertEqual(rep_["design_bad"], ["BK-2"])
        self.assertTrue(any("RP-1" in n and "not compared" in n for n in rep_["notes"]))
        md = fx.ui_markdown(rep_)
        self.assertTrue(md.startswith("UI CHECK: PASS"))
        design = md[md.index("## Design"):]
        self.assertIn("BK-2 Thêm sách: BAD", design)
        self.assertIn("| Nút \"Lưu\" | BACKGROUND_COLOR | màu nền: #1F6FEB | #DC2626 |", design)
        self.assertIn("/s/2-marked.png", design)
        self.assertIn("RP-1: not compared (trang design chưa nhận dữ liệu)", design)
        self.assertIn("the UI reviewer decides", design)


class LookModelTest(DesignCase):
    def test_the_ui_reviewer_looks_with_opus(self):
        self.set_cfg(models={"high": {"ui": "sonnet", "ux": "sonnet"}, "low": {"ui": "sonnet"}})
        job = self.f.job()
        models, _ = fx.route(self.f.ctx(), job)
        self.assertEqual(models["ui"], "opus")
        self.assertEqual(models["ux"], "sonnet")
        self.assertEqual(fx.review_model(self.f.ctx(), "ui"), "opus")
        self.assertEqual(fx.review_model(self.f.ctx(), "code"), "sonnet")

    def test_a_project_may_choose_otherwise(self):
        self.set_cfg(ui_checks={"look_model": "sonnet"}, models={"high": {"ui": "sonnet"}})
        models, _ = fx.route(self.f.ctx(), self.f.job())
        self.assertEqual(models["ui"], "sonnet")


class DesignTableTest(DesignCase):
    """A job's UI reviewer answers every measured screen in a `## Design` table."""

    def setUp(self):
        super().setUp()
        self.rdir = os.path.join(self.f.jd, "round-1")
        write(os.path.join(self.rdir, "ui", "ui-report.json"), json.dumps(JudgedTest.REPORT))
        self.f.save_job(stage="review", round=1, surfaces=["ui"], reviewers=["ui"], reviews={},
                        models={"ui": "opus"})

    def review(self, verdict, rows, findings=""):
        table = "" if rows is None else "## Design\n| Screen | Same data | Measured | Decision |\n|---|---|---|---|\n" + \
            "".join("| %s | yes | %s | %s |\n" % r for r in rows)
        write(os.path.join(self.rdir, "review-ui.md"), "VERDICT: %s\n\n## Findings\n%s\n%s" % (verdict, findings, table))
        return self.fx("verdicts", "T-1")

    def test_no_table(self):
        code, res = self.review("PASS", None)
        self.assertEqual(code, 6)
        self.assertEqual(res["retry_agent"], "ui")
        self.assertIn("## Design", res["error"])

    def test_every_measured_screen_is_answered(self):
        code, res = self.review("PASS", [("BK-2 Thêm sách", "BAD", "design: the package draws it red whatever the data"),
                                         ("BK-3", "MINOR", "ok")])
        self.assertEqual(code, 6)
        self.assertIn("BK-1", res["error"])

    def test_a_heavy_difference_is_never_ok(self):
        code, res = self.review("PASS", [("BK-1", "OK", "ok"), ("BK-2", "BAD", "ok"), ("BK-3", "MINOR", "ok")])
        self.assertEqual(code, 6)
        self.assertIn("BK-2", res["error"])

    def test_the_code_is_wrong_means_changes(self):
        code, res = self.review("PASS", [("BK-1", "OK", "ok"), ("BK-2", "BAD", "code (finding 1)"), ("BK-3", "MINOR", "ok")])
        self.assertEqual(code, 6)
        self.assertIn("CHANGES", res["error"])
        code, res = self.review("CHANGES", [("BK-1", "OK", "ok"), ("BK-2", "BAD", "code (finding 1)"), ("BK-3", "MINOR", "ok")],
                                "1. [blocking] app/books.css:4: the Save button is red, the design blue\n")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "build")

    def test_decided_the_package_draws_it_so(self):
        code, res = self.review("PASS", [("BK-1", "OK", "ok"), ("BK-2", "BAD", "design (note 2)"),
                                         ("BK-3", "MINOR", "different data")])
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "approve")

    def test_nothing_measured_needs_no_table(self):
        write(os.path.join(self.rdir, "ui", "ui-report.json"), json.dumps({"steps": [], "design": [
            {"code": "RP-1", "level": None, "notComparable": "x"}]}))
        code, res = self.review("PASS", None)
        self.assertEqual(code, 0, res)


class TicketPackageTest(DesignCase):
    """A job's ticket attaches its design package: it is unpacked in the job folder and gets its state table."""

    def zip_of(self, name, entries):
        path = os.path.join(self.f.jd, "attachments", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(path, "w") as z:
            for rel, data in entries.items():
                z.writestr(rel, data)
        return path

    def package_files(self, top=""):
        out = {}
        for n in os.listdir(PACKAGE):
            if n != "autotest-states.json":
                with open(os.path.join(PACKAGE, n), "rb") as f:
                    out[top + n] = f.read()
        return out

    def test_unpacked_with_its_state_table(self):
        self.zip_of("Kho sach v0.2.zip", self.package_files("v0.2/"))
        job = fx.job_design_unpack(self.f.ctx(), self.f.job())
        self.assertEqual(job["design"]["files"], ["Kho sach v0.2.zip"])
        pkgs = fx.design_packages(job["design"]["dir"])
        self.assertEqual(len(pkgs), 1)
        self.assertTrue(os.path.isfile(os.path.join(pkgs[0], "autotest-states.json")))
        self.assertEqual(load_json(self.f.jd + "/job.json")["design"], job["design"])
        # once is enough: the same archive is not unpacked again
        stamp = os.path.getmtime(os.path.join(pkgs[0], "autotest-states.json"))
        fx.job_design_unpack(self.f.ctx(), self.f.job())
        self.assertEqual(os.path.getmtime(os.path.join(pkgs[0], "autotest-states.json")), stamp)

    def test_an_archive_that_reaches_outside_is_not_unpacked(self):
        self.zip_of("evil.zip", {"../../escape.txt": "x"})
        job = fx.job_design_unpack(self.f.ctx(), self.f.job())
        self.assertFalse(os.path.exists(os.path.join(self.f.jd, "..", "escape.txt")))
        self.assertTrue(any("evil.zip" in n for n in job.get("design", {}).get("problems", [])))

    def test_no_archive_no_design(self):
        job = fx.job_design_unpack(self.f.ctx(), self.f.job())
        self.assertNotIn("design", job)


def zip_without_utf8_flag(path, entries):
    """A zip as the macOS archiver writes it: names in UTF-8 without the flag that says so (bit 11)."""
    with zipfile.ZipFile(path, "w") as z:
        for rel, data in entries.items():
            z.writestr(rel, data)
    raw = bytearray(open(path, "rb").read())
    for sig, flags_at in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = raw.find(sig)
        while i != -1:
            raw[i + flags_at] &= ~0x08 & 0xFF  # bit 11 is bit 3 of the second byte of the flags
            raw[i + flags_at + 1] &= ~0x08 & 0xFF
            i = raw.find(sig, i + 4)
    open(path, "wb").write(bytes(raw))
    with zipfile.ZipFile(path) as z:
        assert not any(i.flag_bits & 0x800 for i in z.infolist())  # the test's own premise


class VietnameseNamesTest(unittest.TestCase):
    """The design package of 14/11 ("Autotest Hub - export v1.3.zip") gave "Quß║ún trß╗ï.html" for "Quản trị.html"."""

    NAME = "v1.3/Autotest Hub v5 · Quản trị.html"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vn-zip-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.zip = os.path.join(self.tmp, "pkg.zip")
        zip_without_utf8_flag(self.zip, {self.NAME: "<html></html>", "v1.3/Spec.md": "# Spec\n"})

    def test_fx_reads_the_names_as_written(self):
        dest = os.path.join(self.tmp, "fx")
        fx.safe_unzip(self.zip, dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, self.NAME)), os.listdir(os.path.join(dest, "v1.3")))

    def test_tx_reads_the_names_as_written(self):
        design = fx.tx_design()
        dest = os.path.join(self.tmp, "tx")
        design.unzip(self.zip, dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, "Autotest Hub v5 · Quản trị.html")), os.listdir(dest))


def node_modules():
    for d in (os.environ.get("FACTORY_TEST_NODE_PATH"), os.path.expanduser("~/.cache/auto-code-factory/tools/node_modules")):
        if d and os.path.isdir(os.path.join(d, "playwright")):
            return d
    return None


NODE = shutil.which("node")  # found now: a test runs with the PATH of a made-up machine


CHECK_JS = r"""
const ui = require(process.env.FACTORY_UI);
const path = require('path');
const { pathToFileURL } = require('url');
const product = (q) => pathToFileURL(path.join(process.env.FIXTURE, 'product', 'index.html')).href + (q ? '?' + q : '');
ui.run(async (t) => {
  await t.page.goto(product());
  await t.design('BK-1');
  await t.page.goto(product('seed=bad'));
  await t.design('BK-1');
  await t.design('RP-1');
  await t.design('XX-9');
});
"""


@unittest.skipUnless(NODE and node_modules(), "needs Node and Playwright for the factory")
class InTheBrowserTest(DesignCase):
    """The factory's harness and the auto test's comparer together, on the fixture's made-up product."""

    def test_measured_like_the_auto_test(self):
        self.use_repo()
        env_d, tmp, _ = fx.design_setup(self.f.ctx(), self.f.job())
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(self.tmp, "ui-out")
        script = os.path.join(self.tmp, "check.js")
        write(script, CHECK_JS)
        env = dict(os.environ, NODE_PATH=node_modules(), FIXTURE=FIXTURE,
                   PATH=os.path.dirname(NODE) + os.pathsep + os.environ.get("PATH", ""),
                   FACTORY_UI=os.path.join(ROOT, "plugins", "auto-factory", "skills", "code", "factory-ui.js"),
                   FACTORY_UI_REPORT=os.path.join(out, "ui-report.json"), FACTORY_UI_SHOTS=os.path.join(out, "shots"),
                   FACTORY_UI_VIEWPORTS="[[1280,800]]", HOME=os.path.expanduser("~"), **env_d)
        p = subprocess.run([NODE, script], cwd=self.tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=300)
        rep_ = load_json(os.path.join(out, "ui-report.json"))
        if rep_.get("browser_missing"):
            self.skipTest("Playwright could not launch Chromium: %s" % rep_["browser_missing"])
        self.assertIsNone(rep_.get("error"), p.stdout)
        ok, bad, rp1, xx9 = rep_["design"]
        self.assertIn(ok["level"], ("OK", "MINOR"), ok)
        self.assertEqual(ok["viewport"], {"width": 1440, "height": 900})
        self.assertEqual(bad["level"], "BAD", bad)
        self.assertTrue(any(d["level"] == "BAD" for d in bad["diffs"]))
        for k in ("web", "design", "marked"):
            with open(bad[k], "rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n", k)
        self.assertIsNone(rp1["level"])
        self.assertTrue(rp1["notComparable"])
        self.assertIsNone(xx9["level"])
        self.assertIn("XX-9", xx9["notComparable"])


if __name__ == "__main__":
    unittest.main()
