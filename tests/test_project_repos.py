"""Tests for one factory over a project of several repositories (standard library only). Run from the repository root:
    python3 -I -m unittest discover -s tests

factory.json lists the repositories (repos[]); an old one, with project.url, reads as one. A job keeps each repository's
branch, clone and pull request under repos{}. Every origin here is a real bare repository and every clone a real clone.
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_security import STUB, SECRET_ENV, fx, load_json, sh, slurp, write

KEY = "PROJ-7"
CHECKS = "#!/usr/bin/env bash\nexit 0\n"
GITIGNORE = "jobs/\nbaseline.json\nlocal/\n.claude/settings.local.json\n"


def old_keys_id(name, url):
    """The keys folder's name as fx always computed it, before keys_id."""
    import re
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "project"
    return "%s-%s" % (safe, hashlib.sha256(url.encode()).hexdigest()[:8])


class ProjectCase(unittest.TestCase):
    """A project of two repositories, be and fe, each with main and develop, and one factory repository for both."""
    names = ("be", "fe")
    extra = {}       # per repository: more settings for its entry
    top = {}         # more settings at the top of factory.json

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="fx-proj-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cwd = os.getcwd()
        self.addCleanup(os.chdir, self.cwd)
        gitcfg = os.path.join(self.tmp, "gitconfig")
        write(gitcfg, "[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for tool in ("gitleaks", "semgrep", "osv-scanner", "docker"):
            path = os.path.join(self.bin, tool)
            write(path, STUB % {"python": sys.executable, "secret_names": sorted(SECRET_ENV)})
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        for real in ("git", "bash", "tar"):
            os.symlink(shutil.which(real), os.path.join(self.bin, real))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("JIRA_", "GH_", "GITHUB_", "GITLAB_"))}
        env.update(GIT_CONFIG_GLOBAL=gitcfg, GIT_CONFIG_NOSYSTEM="1", FACTORY_STATE_DIR=os.path.join(self.tmp, "state"),
                   FACTORY_TOOLS_DIR=os.path.join(self.tmp, "tools"),
                   FACTORY_WORKSPACE=os.path.join(self.tmp, "machine", "workspace.json"),
                   PATH=self.bin + os.pathsep + "/usr/bin:/bin", STUB_LOG=os.path.join(self.tmp, "stub.log"), HOME=self.tmp)
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.origin = {n: self.make_origin(n) for n in self.names}
        self.factory = os.path.join(self.tmp, "shop-factory")
        os.makedirs(self.factory)
        sh(["git", "init", "-q", "-b", "main"], self.factory)
        self.write_config(self.config())
        for n in self.names:
            write(os.path.join(self.factory, "checks", "%s.sh" % n), CHECKS)
        write(os.path.join(self.factory, ".gitignore"), GITIGNORE)
        self.commit_factory()
        self.issue = os.path.join(self.tmp, "issue.md")
        write(self.issue, "# %s: Reset password\n\nBoth sides change.\n" % KEY)

    def make_origin(self, name):
        seed = os.path.join(self.tmp, "seed-" + name)
        os.makedirs(seed)
        sh(["git", "init", "-q", "-b", "main"], seed)
        write(os.path.join(seed, "README.md"), "%s\n" % name)
        sh(["git", "add", "-A"], seed)
        sh(["git", "commit", "-q", "-m", "base"], seed)
        sh(["git", "checkout", "-q", "-b", "develop"], seed)
        write(os.path.join(seed, "only-on-develop.txt"), "develop\n")
        sh(["git", "add", "-A"], seed)
        sh(["git", "commit", "-q", "-m", "develop"], seed)
        sh(["git", "checkout", "-q", "main"], seed)
        origin = os.path.join(self.tmp, name + ".git")
        sh(["git", "clone", "-q", "--bare", seed, origin], self.tmp)
        return origin

    def config(self):
        repos = []
        for n in self.names:
            entry = {"name": n, "url": self.origin[n], "base_branch": "main"}
            entry.update(self.extra.get(n, {}))
            repos.append(entry)
        cfg = {"project": {"name": "shop"}, "repos": repos, "max_rounds": 3, "ports": {"base": 5100, "step": 10},
               "learning": {"ask_after": "never"}, "pr": {"create": False}}
        cfg.update(self.top)
        return cfg

    def write_config(self, cfg):
        write(os.path.join(self.factory, "factory.json"), json.dumps(cfg, indent=1))

    def commit_factory(self):
        sh(["git", "add", "-A"], self.factory)
        sh(["git", "commit", "-q", "-m", "factory"], self.factory)

    def fx(self, *argv):
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(sys, "stdin", io.StringIO("")):
            try:
                os.chdir(self.factory)  # fx works for the factory repository it runs in
                fx.main(list(argv))
            except SystemExit as e:
                code = e.code
        os.chdir(self.cwd)
        return code, json.loads(buf.getvalue())

    def ok(self, *argv):
        code, res = self.fx(*argv)
        self.assertEqual(code, 0, res)
        return res

    def ctx(self):
        os.chdir(self.factory)
        try:
            return fx.Ctx()
        finally:
            os.chdir(self.cwd)

    def raw(self, job=KEY):
        return load_json(os.path.join(self.factory, "jobs", job, "job.json"))

    def work(self, *parts):
        return os.path.join(self.tmp, "shop-factory-work", *parts)

    def new(self, *argv):
        return self.ok("new", "--local", "Reset password", "--issue-file", self.issue, *argv)

    def intake(self, job, text):
        write(os.path.join(self.factory, "jobs", job, "intake.md"), text)
        return self.fx("result", job, "intake")

    def commit_in(self, wt, name="x.txt", text="x\n", msg="work"):
        write(os.path.join(wt, name), text)
        sh(["git", "add", "-A"], wt)
        sh(["git", "commit", "-q", "-m", msg], wt)
        return sh(["git", "rev-parse", "HEAD"], wt).strip()


# --------------------------------------------------------------------------
# factory.json: the new form, the old one, the name rule, settings lookup

class ConfigTest(unittest.TestCase):
    def test_an_old_factory_json_is_one_repository(self):
        repos, problems = fx.config_repos({"project": {"url": "git@x:a/shop-api.git", "name": "Shop API"},
                                           "base_branch": "develop", "setup": "make"})
        self.assertEqual(problems, [])
        self.assertEqual(repos, [{"name": "shop-api", "url": "git@x:a/shop-api.git", "base_branch": "develop"}])

    def test_an_old_name_that_breaks_the_rule_becomes_main(self):
        for name in ("", "123", "Repos", "!!!", None):
            self.assertEqual(fx.repo_slug(name), "main", name)
        self.assertEqual(fx.repo_slug("Đơn hàng"), "don-hang")
        self.assertEqual(fx.repo_slug("2fa Service"), "fa-service")
        self.assertEqual(len(fx.repo_slug("a" * 60)), 31)

    def test_no_url_no_repository(self):
        self.assertEqual(fx.config_repos({"project": {"url": "", "name": "x"}}), ([], []))
        self.assertEqual(fx.config_repos({}), ([], []))

    def test_the_name_rule(self):
        for bad in ("repos", "Be", "1be", "-be", "b_e", "a" * 32, "", None, "be/x", ".."):
            self.assertTrue(fx.repo_name_problem(bad), bad)
        for good in ("be", "web-2", "a", "a" * 31, "c1", "web-c", "web-cx1", "web-1"):
            self.assertIsNone(fx.repo_name_problem(good), good)

    def test_names_a_candidate_or_a_package_clone_could_have(self):
        # candidate k of web is web-c<k>, package wp1 of web is web--wp1: a repository by that name would share a folder
        for bad in ("web-c1", "web-c12", "a--b", "web--wp1"):
            why = fx.repo_name_problem(bad)
            self.assertTrue(why, bad)
            self.assertIn("candidate", why)
        self.assertIn("web-c1", " | ".join(fx.config_repos({"repos": [{"name": "web", "url": "a"},
                                                                       {"name": "web-c1", "url": "b"}]})[1]))
        self.assertEqual(fx.repo_slug("Shop C2"), "main")

    def test_what_is_wrong_with_the_list(self):
        def problems(repos):
            return " | ".join(fx.config_repos({"repos": repos})[1])
        self.assertIn("'repos'", problems([{"name": "repos", "url": "a"}]))
        self.assertIn("two repositories are named be", problems([{"name": "be", "url": "a"}, {"name": "be", "url": "b"}]))
        self.assertIn("the url of another", problems([{"name": "be", "url": "git@github.com:t/a.git"},
                                                      {"name": "fe", "url": "https://github.com/t/a"}]))
        self.assertIn("has no url", problems([{"name": "be"}]))
        self.assertIn("not one of the repositories", problems([{"name": "be", "url": "a", "app": {"needs": ["db"]}}]))
        cyc = problems([{"name": "be", "url": "a", "app": {"needs": ["fe"]}},
                        {"name": "fe", "url": "b", "app": {"needs": ["be"]}}])
        self.assertIn("circle", cyc)
        self.assertEqual(problems([{"name": "be", "url": "a"}, {"name": "fe", "url": "b", "app": {"needs": ["be"]}}]), "")


class LookupTest(ProjectCase):
    top = {"setup": "make all", "protected_paths": ["tests/**"], "base_branch": "release", "app": {"health": "/up"}}
    extra = {"fe": {"setup": "npm ci", "base_branch": "develop", "app": {"up": "serve {port}", "needs": ["be"]}}}

    def test_the_repository_first_then_the_project_then_the_default(self):
        ctx = self.ctx()
        self.assertEqual(ctx.repo_names(), ["be", "fe"])
        self.assertEqual(ctx.rc("fe", "setup"), "npm ci")
        self.assertEqual(ctx.rc("be", "setup"), "make all")
        self.assertEqual(ctx.rc("be", "protected_paths"), ["tests/**"])
        self.assertEqual(ctx.rc("fe", "app.health"), "/up")  # the project's, not in fe's app block
        self.assertEqual(ctx.rc("fe", "app.up"), "serve {port}")
        self.assertIsNone(ctx.rc("be", "app.up"))
        self.assertEqual(ctx.rc("be", "e2e", "none"), "none")

    def test_keys_only_a_repository_has(self):
        ctx = self.ctx()
        self.assertEqual(ctx.rc("be", "base_branch", "main"), "main")
        self.assertEqual(ctx.rc("fe", "base_branch", "main"), "develop")
        self.assertEqual(ctx.rc("be", "app.needs", []), [])  # never the top level's
        self.assertEqual(ctx.rc("fe", "app.needs"), ["be"])
        self.assertEqual(ctx.url("fe"), self.origin["fe"])

    def test_an_unknown_repository(self):
        with self.assertRaises(fx.FxError) as e:
            self.ctx().repo_entry("web")
        self.assertEqual(e.exception.code, 3)
        self.assertIn("be, fe", str(e.exception))


class OneRepoNewFormTest(ProjectCase):
    names = ("be",)
    extra = {"be": {"setup": "make", "protected_paths": ["db/**"]}}

    def test_a_project_setting_reads_the_only_repository(self):
        ctx = self.ctx()
        self.assertEqual(ctx.c("setup"), "make")
        self.assertEqual(ctx.c("protected_paths"), ["db/**"])
        self.assertEqual(ctx.c("base_branch"), "main")
        self.assertEqual(ctx.c("project.url"), self.origin["be"])
        self.assertEqual(ctx.c("max_rounds"), 3)

    def test_the_checks_script(self):
        ctx = self.ctx()
        self.assertFalse(ctx.old_form)
        self.assertEqual(fx.checks_script(ctx, repo="be"), os.path.join(self.factory, "checks", "be.sh"))


# --------------------------------------------------------------------------
# the keys folder never moves

class KeysFolderTest(ProjectCase):
    def test_an_old_factory_keeps_its_folder(self):
        cfg = {"project": {"name": "shop", "url": self.origin["be"]}, "base_branch": "main"}
        self.write_config(cfg)
        before = fx.baselines_key_dir(self.ctx())
        self.assertEqual(os.path.basename(before), old_keys_id("shop", self.origin["be"]))
        # converted by hand to the new form, without keys_id: still the same folder
        self.write_config({"project": {"name": "shop"}, "repos": [{"name": "be", "url": self.origin["be"]},
                                                                  {"name": "fe", "url": self.origin["fe"]}]})
        self.assertEqual(fx.baselines_key_dir(self.ctx()), before)
        # keys_id written: that is the folder, whatever else changes
        self.write_config({"project": {"name": "renamed"}, "keys_id": os.path.basename(before),
                           "repos": [{"name": "web", "url": self.origin["fe"]}]})
        self.assertEqual(fx.baselines_key_dir(self.ctx()), before)

    def test_a_new_factory(self):
        self.assertEqual(os.path.basename(fx.baselines_key_dir(self.ctx())), old_keys_id("shop", self.origin["be"]))
        self.assertEqual(fx.keys_id_for("shop", self.origin["be"]), old_keys_id("shop", self.origin["be"]))

    def test_keys_id_cannot_leave_the_keys_root(self):
        cfg = self.config()
        cfg["keys_id"] = "../../etc"
        self.write_config(cfg)
        d = fx.baselines_key_dir(self.ctx())
        self.assertEqual(os.path.dirname(d), fx.keys_root())

    def test_where_names_it(self):
        res = self.ok("where")
        self.assertEqual(res["keys_folder"], fx.baselines_key_dir(self.ctx()))
        self.assertEqual([r["name"] for r in res["repos"]], ["be", "fe"])
        self.assertEqual(res["repos"][1]["url"], self.origin["fe"])
        self.assertNotIn("project_url", res)


# --------------------------------------------------------------------------
# checks scripts: one per repository, and the gate's settings cover them all

class ChecksScriptsTest(ProjectCase):
    def test_paths(self):
        ctx = self.ctx()
        self.assertEqual(fx.checks_script(ctx, repo="fe"), os.path.join(self.factory, "checks", "fe.sh"))
        paths = fx.factory_settings_paths(ctx)
        self.assertIn("checks", paths, "the whole folder: the scripts and every helper they source")
        self.assertIn("checks.sh", paths)

    def test_a_helper_a_script_sources_is_watched(self):
        write(os.path.join(self.factory, "checks", "_lib.sh"), "check() { exit 0; }\n")
        self.assertIn("not committed: checks/_lib.sh", fx.factory_dirty(self.ctx()))
        self.commit_factory()
        self.assertEqual(fx.factory_dirty(self.ctx()), "")
        start = sh(["git", "rev-parse", "HEAD"], self.factory).strip()
        write(os.path.join(self.factory, "checks", "_lib.sh"), "check() { exit 0; }  # changed\n")
        self.assertIn("differs from what is committed: checks/_lib.sh", fx.factory_dirty(self.ctx()))
        self.commit_factory()
        self.assertIn("checks/_lib.sh", fx.factory_changed_since(self.ctx(), start))
        write(os.path.join(self.factory, "checks", "lib", "deep.sh"), "x=1\n")
        self.assertIn("not committed: checks/lib/deep.sh", fx.factory_dirty(self.ctx()))

    def test_an_old_factory_keeps_checks_sh(self):
        self.write_config({"project": {"name": "shop", "url": self.origin["be"]}})
        ctx = self.ctx()
        self.assertTrue(ctx.old_form)
        self.assertEqual(fx.checks_script(ctx), os.path.join(self.factory, "checks.sh"))
        self.assertIn("checks.sh", fx.factory_settings_paths(ctx))

    def test_a_changed_script_of_any_repository_stops_the_gate(self):
        self.assertEqual(fx.factory_dirty(self.ctx()), "")
        write(os.path.join(self.factory, "checks", "fe.sh"), "exit 0 # changed\n")
        self.assertIn("checks/fe.sh", fx.factory_dirty(self.ctx()))
        self.assertIn("checks/fe.sh", fx.factory_settings_refusal(self.ctx()))

    def test_a_new_script_not_committed(self):
        cfg = self.config()
        cfg["repos"].append({"name": "app", "url": self.make_origin("app")})
        self.write_config(cfg)
        self.commit_factory()
        write(os.path.join(self.factory, "checks", "app.sh"), CHECKS)
        self.assertIn("not committed: checks/app.sh", fx.factory_dirty(self.ctx()))

    def test_carriage_returns_in_a_script_are_a_change(self):
        write(os.path.join(self.factory, "checks", "be.sh"), CHECKS.replace("\n", "\r\n"))
        self.assertIn("checks/be.sh", fx.factory_dirty(self.ctx()))

    def test_a_commit_changing_one_is_seen(self):
        start = sh(["git", "rev-parse", "HEAD"], self.factory).strip()
        write(os.path.join(self.factory, "checks", "be.sh"), "exit 1\n")
        self.commit_factory()
        self.assertIn("checks/be.sh", fx.factory_changed_since(self.ctx(), start))


# --------------------------------------------------------------------------
# job.json: repos{} and the old flat jobs

class OldJobTest(ProjectCase):
    names = ("be",)

    def setUp(self):
        super().setUp()
        self.write_config({"project": {"name": "Shop", "url": self.origin["be"]}, "base_branch": "main"})
        self.wt = self.work("T-1")
        write(os.path.join(self.factory, "jobs", "T-1", "job.json"), json.dumps({
            "id": "T-1", "issue_key": "T-1", "stage": "handed-off", "round": 1, "round_start": 0, "max_rounds": 3,
            "branch": "factory/T-1-x", "worktree": self.wt, "base_branch": "main", "base_ref": "origin/main",
            "pr_url": "https://example.test/pr/1", "pushed_head": "abc", "setup_done": True, "slot": 1, "port": 5110,
            "candidates": [], "history": []}))

    def test_it_reads_as_its_one_repository(self):
        job = self.ctx().load_exact("T-1")
        self.assertEqual(job["repo_order"], ["shop"])
        e = job["repos"]["shop"]
        self.assertEqual((e["role"], e["branch"], e["worktree"], e["pr_url"]), ("change", "factory/T-1-x", self.wt,
                                                                                "https://example.test/pr/1"))
        self.assertEqual(job["worktree"], self.wt, "shown at the top too, for code not per repository yet")

    def test_saved_in_the_new_form(self):
        ctx = self.ctx()
        job = ctx.load_exact("T-1")
        ctx.save(job, "touched")
        raw = self.raw("T-1")
        for k in fx.JOB_REPO_FIELDS:
            self.assertNotIn(k, raw)
        self.assertEqual(raw["repos"]["shop"]["pushed_head"], "abc")
        self.assertEqual(raw["repo_order"], ["shop"])

    def test_what_code_changes_at_the_top_goes_into_its_repository(self):
        ctx = self.ctx()
        job = ctx.load_exact("T-1")
        job["pushed_head"] = "def"
        job["target_branch"] = "develop"
        job.pop("pr_url")
        job["candidates"].append({"k": 1})
        ctx.save(job)
        e = self.raw("T-1")["repos"]["shop"]
        self.assertEqual((e["pushed_head"], e["target_branch"], e["candidates"]), ("def", "develop", [{"k": 1}]))
        self.assertNotIn("pr_url", e)
        self.assertEqual(job["pushed_head"], "def", "still shown after the save")

    def test_what_code_changes_in_the_repository_stays(self):
        ctx = self.ctx()
        job = ctx.load_exact("T-1")
        job["repos"]["shop"]["setup_done"] = False
        job["repos"]["shop"]["checks_passed_at"] = "now"
        ctx.save(job)
        e = self.raw("T-1")["repos"]["shop"]
        self.assertFalse(e["setup_done"])
        self.assertEqual(e["checks_passed_at"], "now")
        self.assertEqual(job["checks_passed_at"], "now")

    def test_a_field_written_at_the_top_by_hand_counts_for_the_first_repository(self):
        raw = self.raw("T-1")
        ctx = self.ctx()
        job = ctx.load_exact("T-1")
        ctx.save(job)
        raw = dict(self.raw("T-1"), pr_hint="https://example.test/compare")
        write(os.path.join(self.factory, "jobs", "T-1", "job.json"), json.dumps(raw))
        self.assertEqual(self.ctx().load_exact("T-1")["repos"]["shop"]["pr_hint"], "https://example.test/compare")

    def test_status_and_show(self):
        res = self.ok("status")
        self.assertEqual(res["jobs"][0]["repos"], [{"name": "shop", "role": "change", "branch": "factory/T-1-x",
                                                     "base": "main", "target": None, "pr": "https://example.test/pr/1"}])
        shown = self.ok("show", "T-1")
        self.assertEqual(shown["pr_url"], "https://example.test/pr/1")
        self.assertIn("shop", shown["repos"])

    def test_the_board_reads_it(self):
        data = fx.board_data([self.factory])
        v = data["done"][0]
        self.assertEqual((v["branch"], v["pr"]), ("factory/T-1-x", "https://example.test/pr/1"))
        self.assertEqual(v["repos"][0]["name"], "shop")


# --------------------------------------------------------------------------
# fx new

class OneRepoNewTest(ProjectCase):
    names = ("be",)

    def test_cloned_at_once_no_extra_step(self):
        res = self.new()
        self.assertEqual(res["next"], "intake")
        wt = self.work("LOCAL-1", "be")
        self.assertEqual(res["worktree"], wt)
        self.assertTrue(res["branch"].startswith("factory/LOCAL-1-reset-password"))
        self.assertEqual(sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], wt).strip(), res["branch"])
        raw = self.raw("LOCAL-1")
        self.assertEqual(raw["repo_order"], ["be"])
        self.assertEqual(raw["repos"]["be"]["role"], "change")
        self.assertNotIn("worktree", raw)
        # intake without a REPOS line goes straight to the spec
        code, res = self.intake("LOCAL-1", "INTAKE: ACCEPT\nRISK: low\nSURFACES: api\n")
        self.assertEqual((code, res["next"]), (0, "spec"), res)

    def test_a_plain_base(self):
        res = self.new("--base", "develop")
        self.assertTrue(os.path.exists(os.path.join(res["worktree"], "only-on-develop.txt")))
        self.assertEqual(self.raw("LOCAL-1")["repos"]["be"]["base_ref"], "origin/develop")

    def test_a_base_that_is_not_there(self):
        code, res = self.fx("new", "--local", "x", "--issue-file", self.issue, "--base", "nope")
        self.assertEqual(code, 3, res)
        self.assertFalse(os.path.exists(os.path.join(self.factory, "jobs", "LOCAL-1")))
        self.assertFalse(os.path.exists(self.work("LOCAL-1")))

    def test_multi_repo_is_no_verdict_any_more(self):
        self.new()
        code, res = self.intake("LOCAL-1", "INTAKE: MULTI-REPO\n\n## Reason\nx\n")
        self.assertEqual(code, 6, res)
        self.assertEqual(res["retry_agent"], "intake")


class TwoReposNewTest(ProjectCase):
    def test_repos_and_bases_given(self):
        res = self.new("--repos", "fe,be", "--base", "be=develop", "--base", "fe=main")
        self.assertEqual([r["name"] for r in res["repos"]], ["be", "fe"])
        raw = self.raw("LOCAL-1")
        self.assertEqual(raw["repo_order"], ["be", "fe"])
        be, fe = raw["repos"]["be"], raw["repos"]["fe"]
        self.assertEqual(be["branch"], fe["branch"])
        self.assertEqual((be["base_branch"], fe["base_branch"]), ("develop", "main"))
        self.assertTrue(os.path.exists(os.path.join(self.work("LOCAL-1", "be"), "only-on-develop.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.work("LOCAL-1", "fe"), "only-on-develop.txt")))
        for wt in (be["worktree"], fe["worktree"]):
            self.assertEqual(sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], wt).strip(), be["branch"])
        self.assertEqual(res["worktree"], be["worktree"])

    def test_ports_are_a_block_per_job(self):
        a = self.new("--repos", "be,fe")
        write(self.issue, "# PROJ-8: other\n")
        b = self.ok("new", "--local", "Other", "--issue-file", self.issue, "--repos", "be,fe")
        self.assertEqual([r["port"] for r in a["repos"]], [5110, 5111])
        self.assertEqual([r["port"] for r in b["repos"]], [5120, 5121])
        ctx = self.ctx()
        job = ctx.load_exact(b["job"])
        self.assertEqual(fx.job_port(ctx, job, "fe"), 5100 + 10 * job["slot"] + 1)

    def test_an_unknown_repository_or_base(self):
        code, res = self.fx("new", "--local", "x", "--issue-file", self.issue, "--repos", "be,web")
        self.assertEqual(code, 3, res)
        self.assertIn("web", res["error"])
        code, res = self.fx("new", "--local", "x", "--issue-file", self.issue, "--repos", "be", "--base", "fe=main")
        self.assertEqual(code, 3, res)
        self.assertFalse(os.path.exists(os.path.join(self.factory, "jobs", "LOCAL-1")))

    def test_a_broken_list_is_refused(self):
        cfg = self.config()
        cfg["repos"].append({"name": "repos", "url": "x"})
        self.write_config(cfg)
        code, res = self.fx("new", "--local", "x", "--issue-file", self.issue)
        self.assertEqual(code, 3, res)
        self.assertIn("'repos'", res["error"])


class DeferredClonesTest(ProjectCase):
    """Several repositories and no --repos: intake names them, the user picks each base, fx repos clones them."""

    def setUp(self):
        super().setUp()
        self.res = self.new()
        self.job = self.res["job"]

    def test_nothing_is_cloned_before_intake(self):
        self.assertEqual(self.res["next"], "intake")
        self.assertTrue(self.res["repos_after_intake"])
        self.assertIsNone(self.res["worktree"])
        self.assertFalse(os.path.exists(self.work(self.job)))
        prompt = self.ok("ctx", self.job, "--agent", "intake")["prompt"]
        self.assertIn("REPOS:", prompt)
        self.assertIn("be (%s, base main)" % self.origin["be"], prompt)
        code, res = self.fx("ctx", self.job, "--agent", "spec")
        self.assertEqual(code, 3, res)

    def test_the_whole_way_to_the_spec(self):
        code, res = self.intake(self.job, "INTAKE: ACCEPT\nRISK: high\nSURFACES: api, ui\nREPOS: fe, be\n")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "branches")
        self.assertEqual(res["repos"], [{"name": "be", "proposed_base": "main"}, {"name": "fe", "proposed_base": "main"}])
        self.assertEqual(self.raw(self.job)["stage"], "spec")
        br = self.ok("branches", "--job", self.job)
        self.assertEqual(sorted(br["repos"]), ["be", "fe"])
        self.assertIn("develop", br["repos"]["fe"])
        self.assertNotIn("branches", br)
        code, res = self.fx("ctx", self.job, "--agent", "spec")
        self.assertEqual(code, 3, "the spec waits for the clones")
        res = self.ok("repos", self.job, "--base", "be=main", "--base", "fe=develop")
        self.assertEqual(res["next"], "spec")
        self.assertEqual(res["cloned"], ["be", "fe"])
        raw = self.raw(self.job)
        self.assertNotIn("repos_pending", raw)
        self.assertEqual(raw["repos"]["fe"]["base_branch"], "develop")
        self.assertTrue(os.path.exists(os.path.join(self.work(self.job, "fe"), "only-on-develop.txt")))
        self.assertEqual(raw["repos"]["be"]["branch"], raw["repos"]["fe"]["branch"])
        self.assertIn("- be: change; worktree %s;" % self.work(self.job, "be"),
                      self.ok("ctx", self.job, "--agent", "spec")["prompt"])

    def test_one_repository_named(self):
        self.intake(self.job, "INTAKE: ACCEPT\nREPOS: fe\n")
        res = self.ok("repos", self.job, "--base", "fe=main")
        self.assertEqual(res["cloned"], ["fe"])
        self.assertEqual(list(self.raw(self.job)["repos"]), ["fe"])
        self.assertFalse(os.path.exists(self.work(self.job, "be")))

    def test_branches_before_intake_offers_every_repository(self):
        self.assertEqual(sorted(self.ok("branches", "--job", self.job)["repos"]), ["be", "fe"])

    def test_intake_must_name_them(self):
        code, res = self.intake(self.job, "INTAKE: ACCEPT\nRISK: low\n")
        self.assertEqual(code, 6, res)
        self.assertEqual(res["retry_agent"], "intake")
        code, res = self.intake(self.job, "INTAKE: ACCEPT\nREPOS: be, web\n")
        self.assertEqual(code, 6, res)
        self.assertIn("web", res["error"])
        self.assertEqual(self.raw(self.job)["stage"], "intake")

    def test_questions_first(self):
        write(os.path.join(self.factory, "jobs", self.job, "questions.md"), "## Q1: which?\n")
        code, res = self.intake(self.job, "INTAKE: NEEDS-INFO\n")
        self.assertEqual((code, res["next"]), (0, "ask"), res)

    def test_repos_before_intake(self):
        code, res = self.fx("repos", self.job, "--base", "be=main")
        self.assertEqual(code, 0, res)  # a repository named outright is cloned
        self.assertEqual(res["cloned"], ["be"])
        code, res = self.fx("repos", self.job)
        self.assertEqual(code, 3, res)

    def test_a_base_that_is_not_there(self):
        self.intake(self.job, "INTAKE: ACCEPT\nREPOS: be\n")
        code, res = self.fx("repos", self.job, "--base", "be=nope")
        self.assertEqual(code, 3, res)
        self.assertFalse(os.path.exists(self.work(self.job, "be")))
        self.assertEqual(self.raw(self.job)["repos_pending"], ["be"])

    def test_intake_may_name_one_more_than_fx_new_cloned(self):
        write(self.issue, "# PROJ-8: other\n")
        job = self.ok("new", "--local", "Other", "--issue-file", self.issue, "--repos", "be")["job"]
        code, res = self.intake(job, "INTAKE: ACCEPT\nREPOS: be, fe\n")
        self.assertEqual(res["next"], "branches")
        self.assertEqual([r["name"] for r in res["repos"]], ["fe"])
        self.assertEqual(self.ok("repos", job, "--base", "fe=main")["cloned"], ["fe"])


class RunOnlyTest(ProjectCase):
    """fe's app needs be's: a job that changes only fe runs be from its base, never on a branch."""
    extra = {"fe": {"app": {"up": "true", "needs": ["be"]}}, "be": {"base_branch": "develop", "app": {"up": "true"}}}

    def test_a_needed_repository_is_cloned_at_its_base(self):
        res = self.new("--repos", "fe")
        raw = self.raw(res["job"])
        self.assertEqual(raw["repo_order"], ["be", "fe"])
        be = raw["repos"]["be"]
        self.assertEqual(be["role"], "run")
        self.assertNotIn("branch", be)
        self.assertEqual(be["base_branch"], "develop")
        self.assertEqual(sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], be["worktree"]).strip(), "HEAD")  # detached
        self.assertTrue(os.path.exists(os.path.join(be["worktree"], "only-on-develop.txt")))
        self.assertEqual(res["worktree"], raw["repos"]["fe"]["worktree"])
        self.assertEqual(fx.job_primary(raw), "fe")

    def test_a_job_that_changes_both_has_no_run_repository(self):
        raw = self.raw(self.new("--repos", "be,fe")["job"])
        self.assertEqual({n: e["role"] for n, e in raw["repos"].items()}, {"be": "change", "fe": "change"})

    def test_cleanup_takes_it_too(self):
        res = self.new("--repos", "fe")
        self.ok("stage", res["job"], "rejected", "--force")
        self.assertEqual(self.ok("cleanup", res["job"])["clone"], "removed")
        self.assertFalse(os.path.exists(self.work(res["job"])))


# --------------------------------------------------------------------------
# setup, cleanup, rework, candidates, split

class PerRepoStepsTest(ProjectCase):
    extra = {"be": {"setup": "echo be > setup-ran.txt"}, "fe": {"setup": "echo fe > setup-ran.txt"}}

    def setUp(self):
        super().setUp()
        self.res = self.new("--repos", "be,fe")
        self.job = self.res["job"]
        self.wt = {n: self.work(self.job, n) for n in ("be", "fe")}
        sh(["git", "add", "-A"], self.factory)

    def test_setup_runs_in_each_clone(self):
        res = self.ok("setup", self.job)
        self.assertEqual(sorted(res["logs"]), ["be", "fe"])
        for n in ("be", "fe"):
            self.assertEqual(slurp(os.path.join(self.wt[n], "setup-ran.txt")).strip(), n)
            self.assertTrue(self.raw(self.job)["repos"][n]["setup_done"])
        self.assertEqual(self.ok("setup", self.job)["skipped"], "already done")
        os.remove(os.path.join(self.wt["fe"], "setup-ran.txt"))
        self.ok("setup", self.job, "--repo", "fe", "--force")
        self.assertTrue(os.path.exists(os.path.join(self.wt["fe"], "setup-ran.txt")))

    def test_cleanup_keeps_everything_when_one_has_unpushed_work(self):
        self.ok("stage", self.job, "rejected", "--force")
        self.commit_in(self.wt["fe"])
        code, res = self.fx("cleanup", self.job)
        self.assertEqual(code, 3, res)
        self.assertIn("fe:", res["error"])
        self.assertTrue(os.path.isdir(self.wt["be"]))
        res = self.ok("cleanup", self.job, "--force")
        self.assertEqual(res["clone"], "removed")
        self.assertFalse(os.path.exists(self.work(self.job)))
        raw = self.raw(self.job)
        self.assertTrue(all(e.get("clone_removed_at") for e in raw["repos"].values()))

    def test_rework_brings_every_clone_back(self):
        heads = {}
        for n in ("be", "fe"):
            heads[n] = self.commit_in(self.wt[n], msg="%s work" % n)
            branch = self.raw(self.job)["repos"][n]["branch"]
            sh(["git", "push", "-q", "origin", "HEAD:refs/heads/%s" % branch], self.wt[n])
        ctx = self.ctx()
        job = ctx.load_exact(self.job)
        for n in ("be", "fe"):
            job["repos"][n].update(pushed_head=heads[n], setup_done=True, pr_url="https://example.test/%s/1" % n)
        job["stage"] = "handed-off"
        ctx.save(job)
        # a human adds a commit to fe's pull request branch
        other = os.path.join(self.tmp, "human-fe")
        sh(["git", "clone", "-q", "-b", job["repos"]["fe"]["branch"], self.origin["fe"], other], self.tmp)
        human = self.commit_in(other, "human.txt", "fix\n", "human fix")
        sh(["git", "push", "-q", "origin", "HEAD"], other)
        self.ok("cleanup", self.job)
        res = self.ok("rework", self.job, "please also do X")
        self.assertTrue(res["setup_needed"])
        raw = self.raw(self.job)
        for n in ("be", "fe"):
            self.assertTrue(os.path.isdir(os.path.join(self.wt[n], ".git")))
            self.assertFalse(raw["repos"][n]["setup_done"])
            self.assertNotIn("clone_removed_at", raw["repos"][n])
        self.assertEqual(sh(["git", "rev-parse", "HEAD"], self.wt["fe"]).strip(), human)
        rework = slurp(res["rework_file"])
        self.assertIn("https://example.test/be/1", rework)
        self.assertIn("https://example.test/fe/1", rework)
        self.assertEqual(raw["repos"]["fe"]["learn"]["edits_seen"], human)

    def test_rework_of_a_branch_never_pushed_starts_from_the_base(self):
        self.ok("stage", self.job, "handed-off", "--force")
        shutil.rmtree(self.wt["fe"])
        res = self.ok("rework", self.job, "go on")
        self.assertTrue(os.path.isdir(os.path.join(self.wt["fe"], ".git")))
        self.assertTrue(any("fe" in n for n in res["notes"]), res["notes"])

    def test_candidates_and_pick_over_both_repositories(self):
        ctx = self.ctx()
        job = ctx.load_exact(self.job)
        job["best_of"] = 2
        job["stage"] = "build"
        ctx.save(job)
        res = self.ok("candidates", self.job)
        self.assertEqual(res["candidates"], [1, 2])
        raw = self.raw(self.job)
        for n in ("be", "fe"):
            cands = raw["repos"][n]["candidates"]
            self.assertEqual([c["worktree"] for c in cands], [self.wt[n] + "-c1", self.wt[n] + "-c2"])
            for c in cands:
                self.assertTrue(c["setup"])
                self.assertEqual(slurp(os.path.join(c["worktree"], "setup-ran.txt")).strip(), n)
        win = {n: self.commit_in(self.wt[n] + "-c2", "c2.txt", "c2 %s\n" % n) for n in ("be", "fe")}
        write(os.path.join(self.factory, "jobs", self.job, "build-c2.md"), "STATUS: READY\n")
        prep = self.ok("judge-prep", self.job)
        self.assertEqual(prep["passing"], [2])
        md = slurp(prep["report"])
        self.assertIn("be:", md)
        self.assertIn(self.wt["fe"] + "-c2", md)
        res = self.ok("pick", self.job, "--k", "2")
        self.assertEqual(res["picked"], 2)
        for n in ("be", "fe"):
            self.assertEqual(sh(["git", "rev-parse", "HEAD"], self.wt[n]).strip(), win[n])
            self.assertFalse(os.path.exists(self.wt[n] + "-c1"))
            self.assertEqual(self.raw(self.job)["repos"][n]["candidates"], [])

    def test_split_packages_belong_to_one_repository(self):
        ctx = self.ctx()
        job = ctx.load_exact(self.job)
        job["stage"] = "build"
        ctx.save(job)
        plan = {"packages": [{"id": "wp0", "title": "api", "repo": "be", "owns": ["api/**"]},
                             {"id": "wp1", "title": "form", "repo": "fe", "owns": ["api/**"]}]}
        write(os.path.join(self.factory, "jobs", self.job, "plan.md"),
              "PLAN: SPLIT\n\n```json\n%s\n```\n" % json.dumps(plan))
        res = self.ok("split", self.job)  # the same glob in two repositories is no conflict
        self.assertEqual([p["repo"] for p in res["packages"]], ["be", "fe"])
        pk = self.raw(self.job)["split"]["packages"]
        self.assertEqual(pk["wp1"]["clone"], self.wt["fe"] + "--wp1")
        self.assertTrue(pk["wp1"]["branch"].endswith("--wp1"))
        res = self.ok("wp", "next", self.job)
        self.assertEqual([s["id"] for s in res["start"]], ["wp0", "wp1"])
        self.assertTrue(os.path.isdir(os.path.join(self.wt["fe"] + "--wp1", ".git")))
        # a package's work merges into its own repository's branch
        self.commit_in(self.wt["fe"] + "--wp1", "api/form.js", "form\n")
        write(os.path.join(self.factory, "jobs", self.job, "build-wp1.md"), "STATUS: READY\n")
        res = self.ok("wp", "done", self.job, "wp1")
        self.assertTrue(res.get("merged"), res)
        self.assertTrue(os.path.exists(os.path.join(self.wt["fe"], "api", "form.js")))
        self.assertFalse(os.path.exists(os.path.join(self.wt["be"], "api", "form.js")))

    def test_a_package_without_its_repository(self):
        problems = fx.validate_packages([{"id": "wp0", "owns": ["a/**"]}, {"id": "wp1", "repo": "web", "owns": ["b"]}],
                                        {"be": [], "fe": []}, ["be", "fe"])
        self.assertTrue(any("wp0: name its repository" in p for p in problems), problems)
        self.assertTrue(any("wp1: repo must be one of" in p for p in problems), problems)
        one = fx.validate_packages([{"id": "wp0", "owns": ["a/**"]}, {"id": "wp1", "owns": ["a/**"]}], ["a/x"])
        self.assertTrue(any("owned by both" in p for p in one), one)

    def test_board_and_status_show_each_repository(self):
        rows = self.ok("status")["jobs"][0]["repos"]
        self.assertEqual([r["name"] for r in rows], ["be", "fe"])
        v = fx.board_data([self.factory])["active"][0]
        self.assertEqual([r["name"] for r in v["repos"]], ["be", "fe"])
        self.assertEqual(v["repos"][1]["branch"], self.raw(self.job)["repos"]["fe"]["branch"])


# --------------------------------------------------------------------------
# read-only clones and branches per repository

class ReadOnlyCloneTest(ProjectCase):
    def test_open_and_close_per_repository(self):
        code, res = self.fx("code", "open")
        self.assertEqual(code, 3, "with two repositories, one must be named")
        res = self.ok("code", "open", "--repo", "fe")
        self.assertEqual(res["path"], self.work("_read-fe"))
        self.assertTrue(os.path.isdir(os.path.join(res["path"], ".git")))
        self.assertEqual(self.ok("where")["repos"][1]["code"], self.work("_read-fe"))
        self.assertEqual(self.ok("code", "close", "--repo", "fe")["removed"], self.work("_read-fe"))
        self.assertFalse(os.path.exists(self.work("_read-fe")))

    def test_branches_of_each_repository(self):
        res = self.ok("branches")
        self.assertEqual(sorted(res["repos"]), ["be", "fe"])
        res = self.ok("branches", "--repo", "fe")
        self.assertEqual(res["branches"][0], "main")
        self.assertEqual(list(res["repos"]), ["fe"])


class OneRepoReadOnlyTest(ProjectCase):
    names = ("be",)

    def test_the_repository_may_be_left_out(self):
        res = self.ok("code", "open")
        self.assertEqual(res["path"], self.work("_read-be"))
        where = self.ok("where")
        self.assertEqual((where["project_url"], where["code"]), (self.origin["be"], self.work("_read-be")))
        self.ok("code", "close")
        self.assertFalse(os.path.exists(self.work("_read-be")))
        res = self.ok("branches")
        self.assertEqual(res["branches"][0], "main")
        self.assertEqual(res["configured_base"], "main")


# --------------------------------------------------------------------------
# fx doctor: every repository

class DoctorTest(ProjectCase):
    def rows(self):
        code, res = self.fx("doctor")
        return {c["check"]: c for c in res["checks"]}

    def test_each_repository_is_checked(self):
        os.remove(os.path.join(self.factory, "checks", "fe.sh"))
        rows = self.rows()
        self.assertTrue(rows["repository be reachable"]["ok"])
        self.assertTrue(rows["repository fe base branch main"]["ok"])
        self.assertTrue(rows["checks/be.sh filled in"]["ok"])
        self.assertFalse(rows["checks/fe.sh filled in"]["ok"])
        self.assertIn("missing", rows["checks/fe.sh filled in"]["detail"])
        self.assertIn("pull requests (fe)", rows)  # pr.create is false here: one row per repository

    def test_a_base_branch_that_is_not_there(self):
        cfg = self.config()
        cfg["repos"][1]["base_branch"] = "nope"
        self.write_config(cfg)
        self.assertFalse(self.rows()["repository fe base branch nope"]["ok"])

    def test_a_wrong_list_fails(self):
        for repos, words in (
                ([{"name": "repos", "url": self.origin["be"]}], "'repos'"),
                ([{"name": "be", "url": self.origin["be"]}, {"name": "be", "url": self.origin["fe"]}], "two repositories"),
                ([{"name": "be", "url": self.origin["be"]}, {"name": "fe", "url": self.origin["be"]}], "url of another"),
                ([{"name": "be", "url": self.origin["be"], "app": {"needs": ["fe"]}},
                  {"name": "fe", "url": self.origin["fe"], "app": {"needs": ["be"]}}], "circle"),
                ([{"name": "be", "url": self.origin["be"], "app": {"needs": ["db"]}}], "not one of the repositories")):
            cfg = self.config()
            cfg["repos"] = repos
            self.write_config(cfg)
            code, res = self.fx("doctor")
            bad = [c for c in res["checks"] if c["check"] == "repositories listed right in factory.json"]
            self.assertTrue(bad and not bad[0]["ok"], res["checks"][:6])
            self.assertTrue(any(words in c["detail"] for c in bad), (words, bad))
            self.assertFalse(res["ok"])

    def test_two_repositories_copying_one_file_are_named(self):
        cfg = self.config()
        for r in cfg["repos"]:
            r["copy_into_worktree"] = [".env"]
        self.write_config(cfg)
        write(os.path.join(self.factory, "local", ".env"), "A=1\n")
        row = self.rows()["each repository's own local files (advice)"]
        self.assertIn("copied into the clones of be, fe", row["detail"])
        write(os.path.join(self.factory, "local", "be", ".env"), "A=2\n")
        write(os.path.join(self.factory, "local", "fe", ".env"), "A=3\n")
        rows = self.rows()
        self.assertNotIn("each repository's own local files (advice)", rows)
        self.assertTrue(rows["files every clone of fe needs (copy_into_worktree)"]["ok"])

    def test_a_ports_step_without_room_for_the_apps_and_the_agents_fails(self):
        cfg = self.config()
        cfg["ports"] = {"base": 5100, "step": 4}  # 2 apps + the agents' 3 need 5
        self.write_config(cfg)
        row = self.rows()["ports.step holds every app and the agents"]
        self.assertFalse(row["ok"])
        self.assertIn("at least 5", row["detail"])
        cfg["ports"]["step"] = 5
        self.write_config(cfg)
        self.assertTrue(self.rows()["ports.step holds every app and the agents"]["ok"])

    def test_each_repository_s_own_app_e2e_and_accepted_rules(self):
        cfg = self.config()
        cfg["repos"][0].update(app={"up": "serve-be {port}"}, security={"accepted_rules": [{"rule": "x", "reason": "y"}]})
        cfg["repos"][1].update(e2e="npm run e2e", security={"accepted_rules": "nope"})
        self.write_config(cfg)
        rows = self.rows()
        self.assertEqual(rows["app control (be) (optional)"]["detail"], "serve-be {port}")
        self.assertIn("not set", rows["app control (fe) (optional)"]["detail"])
        self.assertEqual(rows["e2e in the gate (fe) (optional)"]["detail"], "npm run e2e")
        self.assertIn("not set", rows["e2e in the gate (be) (optional)"]["detail"])
        self.assertFalse(rows["security.accepted_rules (fe)"]["ok"])
        self.assertIn("x covered in every file", rows["accepted rules name their files (be) (advice)"]["detail"])
        gaps = rows["what the security gate leaves out (advice)"]["detail"]
        self.assertIn("fe: no app.up", gaps)
        self.assertNotIn("be: no app.up", gaps)
        self.assertIn("be: 1 rule(s) accepted", gaps)

    def test_each_repository_s_header_baseline(self):
        cfg = self.config()
        for r in cfg["repos"]:
            r["app"] = {"up": "serve {port}"}
        cfg["security"] = {"scanners": {"zap": False}}
        self.write_config(cfg)
        ctx = self.ctx()
        sig = fx.scan_signature(fx.repo_ctx(ctx, "be"), "headers")
        write(os.path.join(self.factory, "baseline.json"), json.dumps(
            {"repos": {"be": {"security": {"headers": []}, "security_scan": {"headers": sig}},
                       "fe": {"security": {"headers": []}}}}))
        rows = self.rows()
        self.assertNotIn("headers baseline usable (be)", rows)
        self.assertFalse(rows["headers baseline usable (fe)"]["ok"])

    def test_every_repository_s_host_for_gh_and_glab(self):
        cfg = self.config()
        cfg["repos"][0]["url"] = "git@github.com:acme/shop-api.git"
        cfg["repos"][1]["url"] = "https://gitlab.example.com/web/shop.git"
        self.write_config(cfg)
        self.assertEqual(fx.project_host(self.ctx()), [("github", "github.com"), ("gitlab", "gitlab.example.com")])
        with mock.patch.object(fx, "tool_version", return_value="1.0"), \
                mock.patch.object(fx, "login_status", side_effect=lambda n, h=None: {"ok": True, "command": h}):
            os.chdir(self.factory)
            try:
                st = fx.machine_status(fx.Ctx())
            finally:
                os.chdir(self.cwd)
        tools = {t["name"]: t for t in st["tools"]}
        self.assertEqual((tools["gh"]["need"], tools["glab"]["need"]), ("recommended", "recommended"))
        self.assertEqual(tools["glab"]["logins"], {"gitlab.example.com": {"ok": True, "command": "gitlab.example.com"}})
        self.assertEqual(st["project_hosts"], [{"provider": "github", "host": "github.com"},
                                               {"provider": "gitlab", "host": "gitlab.example.com"}])

    def test_an_old_factory_keeps_its_rows(self):
        self.write_config({"project": {"name": "shop", "url": self.origin["be"]}, "base_branch": "main"})
        write(os.path.join(self.factory, "checks.sh"), CHECKS)
        rows = self.rows()
        self.assertTrue(rows["project reachable"]["ok"])
        self.assertTrue(rows["project base branch main"]["ok"])
        self.assertTrue(rows["checks.sh filled in"]["ok"])


# --------------------------------------------------------------------------
# a job on S3: every repository comes back

class RestoreTest(ProjectCase):
    extra = {"fe": {"app": {"up": "true", "needs": ["be"]}}, "be": {"app": {"up": "true"}}}

    def test_a_two_repository_job_gets_every_clone_back(self):
        res = self.new("--repos", "fe")
        job_id = res["job"]
        raw = self.raw(job_id)
        fe_wt = raw["repos"]["fe"]["worktree"]
        head = self.commit_in(fe_wt)
        sh(["git", "push", "-q", "origin", "HEAD:refs/heads/%s" % raw["repos"]["fe"]["branch"]], fe_wt)
        snap = {"factory_root": "/old/shop-factory", "work_root": "/old/shop-factory-work", "saved_at": "x"}
        moved = json.loads(json.dumps(raw).replace(self.factory, "/old/shop-factory")
                           .replace(self.work(), "/old/shop-factory-work"))
        shutil.rmtree(self.work(job_id))
        ctx = self.ctx()
        job = fx.jobs_new_home(ctx, moved, snap, job_id)
        self.assertEqual(job["repos"]["fe"]["worktree"], self.work(job_id, "fe"))
        self.assertEqual(job["repos"]["be"]["worktree"], self.work(job_id, "be"))
        self.assertFalse(job["repos"]["fe"]["setup_done"])
        said = fx.jobs_after_restore(ctx, job)
        self.assertIn("Cloned its branch", said)
        self.assertIn("at its base", said)
        self.assertEqual(sh(["git", "rev-parse", "HEAD"], self.work(job_id, "fe")).strip(), head)
        self.assertEqual(sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], self.work(job_id, "be")).strip(), "HEAD")

    def test_an_old_flat_job_keeps_its_one_clone_folder(self):
        self.write_config({"project": {"name": "shop", "url": self.origin["be"]}, "base_branch": "main"})
        old = {"id": "T-1", "issue_key": "T-1", "stage": "handed-off", "branch": "factory/T-1-x",
               "worktree": "/old/shop-factory-work/T-1", "base_branch": "main", "base_ref": "origin/main",
               "candidates": [{"k": 1, "worktree": "/old/shop-factory-work/T-1-c1"}], "setup_done": True}
        job = fx.jobs_new_home(self.ctx(), old, {"work_root": "/old/shop-factory-work"}, "T-1")
        self.assertEqual(job["repos"]["shop"]["worktree"], self.work("T-1"))
        self.assertEqual(job["repos"]["shop"]["candidates"][0]["worktree"], self.work("T-1") + "-c1")
        self.assertNotIn("worktree", job)

    def test_a_repository_name_fx_could_not_have_written(self):
        bad = {"id": "T-1", "stage": "build", "repos": {"../x": {"role": "change", "worktree": "/a"}}}
        with self.assertRaises(fx.StoreError):
            fx.jobs_new_home(self.ctx(), bad, {}, "T-1")

    def test_logic_base_of_each_repository_is_not_taken_for_a_key(self):
        data = json.dumps({"id": "T-1", "repos": {"be": {"logic_base": {"auth/api-key-rotation.md": "a" * 40}}}})
        self.assertNotIn("logic_base", fx.jobs_scan_copy(data.encode()).decode())


# --------------------------------------------------------------------------
# a repository that turns out to need nothing leaves the job; work done in a run-only clone is never lost

class DropRepoTest(ProjectCase):
    def test_a_repository_with_no_work_leaves_the_job(self):
        job = self.new("--repos", "be,fe")["job"]
        ctx = self.ctx()
        j = ctx.load_exact(job)
        j.update(stage="build", risk="high")
        fx.route(ctx, j)
        ctx.save(j)
        self.assertIn("fit", self.raw(job)["reviewers"])
        res = self.ok("repos", job, "--drop", "fe")
        self.assertEqual((res["dropped"], res["became"]), ("fe", "removed"))
        raw = self.raw(job)
        self.assertEqual(list(raw["repos"]), ["be"])
        self.assertEqual(raw["repo_order"], ["be"])
        self.assertFalse(os.path.exists(self.work(job, "fe")))
        self.assertNotIn("fit", raw["reviewers"], "one changed repository left: no fit checker")
        code, res = self.fx("repos", job, "--drop", "be")
        self.assertEqual(code, 3, res)
        self.assertIn("only repository", res["error"])

    def test_refused_while_its_clone_has_work(self):
        job = self.new("--repos", "be,fe")["job"]
        self.commit_in(self.work(job, "fe"))
        code, res = self.fx("repos", job, "--drop", "fe")
        self.assertEqual(code, 3, res)
        self.assertIn("1 commit(s)", res["error"])
        self.assertTrue(os.path.isdir(self.work(job, "fe")))
        self.assertEqual(self.raw(job)["repos"]["fe"]["role"], "change")
        sh(["git", "reset", "-q", "--hard", "HEAD~1"], self.work(job, "fe"))
        write(os.path.join(self.work(job, "fe"), "README.md"), "edited\n")
        code, res = self.fx("repos", job, "--drop", "fe")
        self.assertEqual(code, 3, res)
        self.assertIn("README.md", res["error"])


class DropNeededRepoTest(ProjectCase):
    extra = {"be": {"app": {"up": "true"}}, "fe": {"app": {"up": "true", "needs": ["be"]}}}

    def test_a_repository_another_app_needs_becomes_run_only(self):
        job = self.new("--repos", "be,fe")["job"]
        res = self.ok("repos", job, "--drop", "be")
        self.assertEqual(res["became"], "run")
        be = self.raw(job)["repos"]["be"]
        self.assertEqual(be["role"], "run")
        self.assertNotIn("branch", be)
        wt = self.work(job, "be")
        self.assertEqual(sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], wt).strip(), "HEAD")  # detached at its base
        self.assertNotIn("factory/", sh(["git", "branch"], wt))
        self.assertEqual(fx.job_names(self.raw(job), "change"), ["fe"])


class RunCloneWorkTest(ProjectCase):
    """fe's app needs be's, so a job that changes fe has a run-only checkout of be. Work done there by mistake is kept."""
    extra = {"be": {"app": {"up": "true"}}, "fe": {"app": {"up": "true", "needs": ["be"]}}}

    def setUp(self):
        super().setUp()
        self.job = self.new("--repos", "fe")["job"]
        self.be = self.work(self.job, "be")
        self.assertEqual(self.raw(self.job)["repos"]["be"]["role"], "run")

    def test_adding_it_keeps_its_commits_and_its_changes(self):
        made = self.commit_in(self.be, "fix.py", "fixed\n", "a fix in the run clone")
        write(os.path.join(self.be, "README.md"), "edited too\n")
        res = self.ok("repos", self.job, "--add", "be")
        self.assertEqual(res["cloned"], [])
        be = self.raw(self.job)["repos"]["be"]
        self.assertEqual(be["role"], "change")
        self.assertEqual(be["worktree"], self.be)
        self.assertEqual(sh(["git", "symbolic-ref", "--short", "HEAD"], self.be).strip(), be["branch"])
        self.assertEqual(sh(["git", "rev-parse", "HEAD"], self.be).strip(), made)
        self.assertEqual(slurp(os.path.join(self.be, "README.md")), "edited too\n")

    def test_adding_one_without_work_clones_it_again(self):
        res = self.ok("repos", self.job, "--add", "be")
        self.assertEqual(res["cloned"], [])
        be = self.raw(self.job)["repos"]["be"]
        self.assertEqual(be["role"], "change")
        self.assertEqual(sh(["git", "symbolic-ref", "--short", "HEAD"], self.be).strip(), be["branch"])

    def test_cleanup_keeps_a_run_clone_with_work(self):
        self.commit_in(self.be, "fix.py", "fixed\n")
        self.ok("stage", self.job, "rejected", "--force")
        code, res = self.fx("cleanup", self.job)
        self.assertEqual(code, 3, res)
        self.assertIn("be:", res["error"])
        self.assertTrue(os.path.isdir(self.be))


# --------------------------------------------------------------------------
# each repository's own local files

class LocalPerRepoTest(ProjectCase):
    extra = {"be": {"copy_into_worktree": [".env"]}, "fe": {"copy_into_worktree": [".env"]}}

    def test_local_repo_first_then_the_shared_place(self):
        write(os.path.join(self.factory, "local", "be", ".env"), "WHO=be\n")
        write(os.path.join(self.factory, "local", ".env"), "WHO=shared\n")
        job = self.new("--repos", "be,fe")["job"]
        self.assertEqual(slurp(os.path.join(self.work(job, "be"), ".env")), "WHO=be\n")
        self.assertEqual(slurp(os.path.join(self.work(job, "fe"), ".env")), "WHO=shared\n")


if __name__ == "__main__":
    unittest.main()
