"""Tests for setting up a factory over a project of one or more repositories, and for its knowledge (standard library
only). Run from the repository root:
    python3 -I -m unittest discover -s tests

fx init writes factory.json with repos[], a checks script and a knowledge folder per repository, and brings an older
factory.json (one project.url) to that form with the same keys folder. Knowledge and logic cite code as repo:path; fx scan,
style-sample and the knowledge refresh work per repository. The per-machine workspace, fx plan and fx feature are gone.
Every origin here is a real bare repository and every clone a real clone.
"""

import json
import os
import stat
import unittest

from test_project_repos import CHECKS, ProjectCase, old_keys_id
from test_security import fx, load_json, sh, slurp, write

LOGIC_INDEX = "# Logic\n\n| Module | File | Title | Issue |\n|---|---|---|---|\n| billing | [billing/invoices.md]" \
              "(billing/invoices.md) | Invoices | - |\n<!-- index:end -->\n\n<!-- log:end -->\n"
LOGIC_MODULE = "# Billing\n\n## 1. What it does\nInvoices.\n\n## 2. Features\n- invoices.md\n"
LOGIC_FEATURE = "# Invoices\n\n## 1. Goal\nBill.\n\n## 2. Rules\n%s\n\nMore.\n\nMore.\n\n## 3. Flow\n```mermaid\nflowchart LR\n  a-->b\n```\n" \
                "\n## 4. Data\nNone.\n\n## 5. Tests\nNone.\n"


class InitCase(ProjectCase):
    """The origins of be and fe, and an empty factory repository that fx init has not seen yet."""

    def setUp(self):
        super().setUp()
        self.factory = os.path.join(self.tmp, "new-factory")
        os.makedirs(self.factory)
        sh(["git", "init", "-q", "-b", "main"], self.factory)

    def cfg(self):
        return load_json(os.path.join(self.factory, "factory.json"))

    def path(self, *parts):
        return os.path.join(self.factory, *parts)

    def init(self, *argv):
        return self.ok("init", *argv)

    def refused(self, *argv):
        before = slurp(self.path("factory.json")) if os.path.exists(self.path("factory.json")) else None
        code, res = self.fx("init", *argv)
        self.assertEqual(code, 3, res)
        after = slurp(self.path("factory.json")) if os.path.exists(self.path("factory.json")) else None
        self.assertEqual(after, before, "a refused init changes nothing")
        return res["error"]


# --------------------------------------------------------------------------
# a new factory, and adding repositories

class NewFactoryTest(InitCase):
    def test_two_repositories_in_the_new_form(self):
        res = self.init("--project-name", "shop", "--repo", "be=" + self.origin["be"], "--repo", "fe=" + self.origin["fe"])
        cfg = self.cfg()
        self.assertEqual(cfg["project"], {"name": "shop"})
        self.assertEqual(cfg["keys_id"], old_keys_id("shop", self.origin["be"]))
        self.assertEqual([(r["name"], r["url"], r["base_branch"]) for r in cfg["repos"]],
                         [("be", self.origin["be"], "main"), ("fe", self.origin["fe"], "main")])
        for key in ("base_branch", "setup", "e2e", "copy_into_worktree", "protected_paths"):
            self.assertNotIn(key, cfg, "a repository's own setting: in its entry")
        self.assertNotIn("scope_writer", cfg["models"])
        for n in ("be", "fe"):
            script = self.path("checks", "%s.sh" % n)
            self.assertIn("FACTORY_TODO", slurp(script))
            self.assertTrue(os.stat(script).st_mode & stat.S_IXUSR)
            self.assertIn("Repository %s" % n, slurp(self.path("knowledge", n, "README.md")))
        self.assertFalse(os.path.exists(self.path("checks.sh")))
        self.assertTrue(os.path.isfile(self.path("knowledge", "README.md")))
        self.assertTrue(os.path.isfile(self.path("logic", "README.md")))
        self.assertEqual([r["name"] for r in res["repos"]], ["be", "fe"])
        self.assertEqual(res["added"], ["be", "fe"])
        self.assertNotIn("project_url", res, "the single keys of before are for a project of one repository")
        ctx = self.ctx()
        self.assertFalse(ctx.old_form)
        self.assertEqual(fx.keys_id(ctx), old_keys_id("shop", self.origin["be"]))
        self.assertEqual(fx.checks_script(ctx, repo="fe"), self.path("checks", "fe.sh"))

    def test_running_again_adds_a_repository_and_keeps_the_rest(self):
        self.init("--project-name", "shop", "--repo", "be=" + self.origin["be"])
        cfg = self.cfg()
        cfg["repos"][0]["setup"] = "make"
        write(self.path("factory.json"), json.dumps(cfg, indent=1))
        write(self.path("checks", "be.sh"), CHECKS)
        res = self.init("--repo", "fe=" + self.origin["fe"], "--repo", "be=" + self.origin["be"])
        self.assertEqual(res["added"], ["fe"])
        cfg = self.cfg()
        self.assertEqual([r["name"] for r in cfg["repos"]], ["be", "fe"])
        self.assertEqual(cfg["repos"][0]["setup"], "make")
        self.assertEqual(cfg["keys_id"], old_keys_id("shop", self.origin["be"]), "written once, never changed")
        self.assertEqual(slurp(self.path("checks", "be.sh")), CHECKS)
        self.assertEqual(self.init()["added"], [])
        self.assertEqual(self.init("--project-name", "Shop 2")["keys_id"], old_keys_id("shop", self.origin["be"]))

    def test_adding_a_repository_raises_ports_step(self):
        self.init("--project-name", "shop", "--repo", "be=" + self.origin["be"])
        self.assertEqual(self.cfg()["ports"]["step"], 10)  # one repository: 1 app + 3 agents + 4 candidates fit in 10
        res = self.init("--repo", "fe=" + self.origin["fe"])
        self.assertEqual(self.cfg()["ports"]["step"], 13)  # 2 apps + 3 agents + 4 candidates of 2 apps
        self.assertTrue(any("ports.step raised from 10 to 13" in n for n in res["notes"]), res["notes"])
        cfg = self.cfg()
        cfg["ports"]["step"] = 40
        write(self.path("factory.json"), json.dumps(cfg, indent=1))
        self.init("--repo", "web=" + self.make_origin("web"))
        self.assertEqual(self.cfg()["ports"]["step"], 40, "a larger step is kept")

    def test_a_local_folder_means_its_remote(self):
        clone = os.path.join(self.tmp, "my-fe")
        sh(["git", "clone", "-q", self.origin["fe"], clone], self.tmp)
        res = self.init("--project-name", "shop", "--repo", "fe=" + clone)
        self.assertEqual(self.cfg()["repos"][0]["url"], self.origin["fe"])
        self.assertTrue([n for n in res["notes"] if "instead of your folder" in n])

    def test_project_is_the_shorthand_for_one_repository(self):
        res = self.init("--project", self.origin["be"])
        self.assertEqual([r["name"] for r in self.cfg()["repos"]], ["be"])
        self.assertEqual(self.cfg()["project"]["name"], "be")
        self.assertEqual((res["project_url"], res["base_branch"]), (self.origin["be"], "main"))
        self.assertEqual(self.init("--project", self.origin["be"])["added"], [], "the same repository again")
        self.assertIn("--repo <name>=<git URL>", self.refused("--project", self.origin["fe"]))

    def test_project_named_after_the_project(self):
        self.init("--project", self.origin["be"], "--project-name", "Shop API")
        cfg = self.cfg()
        self.assertEqual((cfg["project"]["name"], cfg["repos"][0]["name"]), ("Shop API", "shop-api"))

    def test_what_is_refused(self):
        self.assertIn("give the project's repositories", self.refused())
        own = os.path.join(self.tmp, "factory-origin.git")
        sh(["git", "init", "-q", "--bare", own], self.tmp)
        sh(["git", "remote", "add", "origin", own], self.factory)
        self.assertIn("this factory repository itself", self.refused("--repo", "be=" + own))
        self.init("--project-name", "shop", "--repo", "be=" + self.origin["be"])
        for argv, words in ((("--repo", "repos=" + self.origin["fe"]), "cannot be named 'repos'"),
                            (("--repo", "Fe=" + self.origin["fe"]), "not a repository name"),
                            (("--repo", "fe"), "--repo <name>=<git URL"),
                            (("--repo", "fe=" + self.origin["fe"], "--repo", "fe=" + self.origin["fe"]),
                             "two repositories are named fe"),
                            (("--repo", "web=" + self.origin["be"]), "has the URL of the repository be"),
                            (("--repo", "be=" + self.origin["fe"]), "a repository named be already"),
                            (("--repo", "fe=" + own), "this factory repository itself")):
            self.assertIn(words, self.refused(*argv), argv)
        self.assertEqual([r["name"] for r in self.cfg()["repos"]], ["be"])

    def test_an_empty_repository_gets_its_first_commit_when_asked(self):
        empty = os.path.join(self.tmp, "mobile.git")
        sh(["git", "init", "-q", "--bare", empty], self.tmp)
        res = self.init("--project-name", "shop", "--repo", "be=" + self.origin["be"], "--repo", "mobile=" + empty,
                        "--initial-commit")
        rows = {r["name"]: r for r in res["repos"]}
        self.assertIsNone(rows["be"]["initial_commit"], "a repository with commits is left as it is")
        self.assertEqual(rows["mobile"]["initial_commit"]["branch"], "main")
        self.assertTrue(sh(["git", "rev-parse", "refs/heads/main"], empty).strip())


# --------------------------------------------------------------------------
# an older factory: one project.url, checks.sh and a flat knowledge folder

OLD_JOB = {"id": "T-1", "issue_key": "T-1", "summary": "old job", "stage": "build", "round": 0, "round_start": 0,
           "max_rounds": 3, "risk": "low", "surfaces": [], "models": {}, "reviewers": [], "reviews": {}, "flags": [],
           "history": [], "branch": "factory/T-1-old", "base_branch": "main", "base_ref": "origin/main",
           "setup_done": True, "feature": {"id": "PROJ-9", "repo": "be", "shared": True},
           "upstream": ["/somewhere/else/api-notes.md"]}


class ConvertTest(InitCase):
    def setUp(self):
        super().setUp()
        self.old_cfg = {"project": {"name": "Shop", "url": self.origin["be"]}, "base_branch": "develop",
                        "setup": "make", "app": {"up": "serve {port}", "accounts_file": "knowledge/environment.md"},
                        "ui_paths": ["src/**"], "sensitive_paths": ["*auth*"], "max_rounds": 4}
        write(self.path("factory.json"), json.dumps(self.old_cfg, indent=1))
        write(self.path("checks.sh"), CHECKS)
        for name in ("README.md", "overview.md", "architecture.md", "style.md"):
            write(self.path("knowledge", name), "# %s\n" % name)
        write(self.path("knowledge", ".ref"), json.dumps({"commit": "abc"}))
        write(self.path(".gitignore"), "jobs/\n")
        self.commit_factory()
        write(self.path("knowledge", "environment.md"), "# accounts\n")  # not committed yet
        job = dict(OLD_JOB, worktree=self.work("T-1"))
        write(self.path("jobs", "T-1", "job.json"), json.dumps(job, indent=1))
        self.keys_before = self.ok("where")["keys_folder"]

    def test_it_becomes_one_repository_with_the_same_keys_folder(self):
        res = self.init()
        self.assertTrue(res["converted"])
        cfg = self.cfg()
        self.assertEqual(cfg["keys_id"], old_keys_id("Shop", self.origin["be"]))
        self.assertEqual(self.ok("where")["keys_folder"], self.keys_before)
        self.assertEqual(cfg["repos"], [{"name": "shop", "url": self.origin["be"], "base_branch": "develop",
                                         "setup": "make", "app": {"up": "serve {port}",
                                                                  "accounts_file": "knowledge/shop/environment.md"},
                                         "ui_paths": ["src/**"]}])
        self.assertNotIn("url", cfg["project"])
        for key in ("base_branch", "setup", "app", "ui_paths"):
            self.assertNotIn(key, cfg)
        self.assertEqual((cfg["sensitive_paths"], cfg["max_rounds"]), (["*auth*"], 4), "the project's own stay")

    def test_checks_and_the_repository_s_knowledge_move(self):
        res = self.init()
        self.assertFalse(os.path.exists(self.path("checks.sh")))
        self.assertEqual(slurp(self.path("checks", "shop.sh")), CHECKS)
        for name in ("architecture.md", "style.md", ".ref", "environment.md"):
            self.assertTrue(os.path.isfile(self.path("knowledge", "shop", name)), name)
            self.assertFalse(os.path.exists(self.path("knowledge", name)), name)
        for name in ("README.md", "overview.md"):
            self.assertTrue(os.path.isfile(self.path("knowledge", name)), "shared: " + name)
        self.assertNotIn("FACTORY_TODO", slurp(self.path("knowledge", "shop", "README.md")))
        staged = sh(["git", "status", "--porcelain"], self.factory)
        self.assertRegex(staged, r"(?m)^R. checks.sh -> checks/shop.sh$")  # renamed in git (and made executable)
        self.assertIn("R  knowledge/architecture.md -> knowledge/shop/architecture.md", staged)
        self.assertIn("git mv checks.sh checks/shop.sh", res["moved"])
        self.assertIn("moved knowledge/environment.md to knowledge/shop/environment.md", res["moved"])
        ctx = self.ctx()
        self.assertFalse(ctx.old_form)
        self.assertEqual(fx.checks_script(ctx), self.path("checks", "shop.sh"))
        self.assertEqual(fx.knowledge_ref_path(ctx), self.path("knowledge", "shop", ".ref"))

    def test_an_old_job_still_loads(self):
        self.init()
        rows = self.ok("status")["jobs"]
        self.assertEqual([(r["job"], [x["name"] for x in r["repos"]]) for r in rows], [("T-1", ["shop"])])
        self.assertNotIn("feature", rows[0])
        job = self.ok("show", "T-1")
        self.assertEqual(job["repos"]["shop"]["branch"], "factory/T-1-old")
        prompt = self.ok("ctx", "T-1", "--agent", "code")["prompt"]
        self.assertNotIn("api-notes", prompt)
        self.assertNotIn("/somewhere/else", prompt)

    def test_another_repository_in_the_same_run(self):
        res = self.init("--repo", "fe=" + self.origin["fe"])
        self.assertEqual(res["added"], ["fe"])
        self.assertEqual([r["name"] for r in self.cfg()["repos"]], ["shop", "fe"])
        self.assertTrue(os.path.isfile(self.path("checks", "fe.sh")))
        self.assertIn("FACTORY_TODO", slurp(self.path("knowledge", "fe", "README.md")))

    def test_the_repository_keeps_its_name(self):
        self.assertIn("has the URL of the repository shop", self.refused("--repo", "be=" + self.origin["be"]))
        self.assertEqual(self.init("--repo", "shop=" + self.origin["be"])["added"], [])


class ImportTest(InitCase):
    def test_an_old_in_project_factory_gives_a_project_of_one(self):
        src = os.path.join(self.tmp, "old-project")
        write(os.path.join(src, "factory", "config.json"), json.dumps({"setup": "make", "base_branch": "develop",
                                                                       "max_rounds": 5}))
        write(os.path.join(src, "factory", "checks.sh"), "#!/usr/bin/env bash\nmake test\n")
        write(os.path.join(src, "factory", "knowledge", "README.md"), "# Knowledge\n")
        write(os.path.join(src, "factory", "knowledge", "overview.md"), "# Overview\n")
        write(os.path.join(src, "factory", "knowledge", "architecture.md"), "# Architecture\n")
        res = self.init("--project", self.origin["be"], "--import", src)
        self.assertIn("factory.json (from config.json)", res["imported"])
        cfg = self.cfg()
        self.assertEqual([(r["name"], r["base_branch"], r["setup"]) for r in cfg["repos"]], [("be", "develop", "make")])
        self.assertEqual(cfg["max_rounds"], 5)
        self.assertNotIn("base_branch", cfg)
        self.assertEqual(slurp(self.path("checks", "be.sh")), "#!/usr/bin/env bash\nmake test\n")
        self.assertFalse(os.path.exists(self.path("checks.sh")))
        self.assertEqual(slurp(self.path("knowledge", "be", "architecture.md")), "# Architecture\n")
        self.assertEqual(slurp(self.path("knowledge", "overview.md")), "# Overview\n")


# --------------------------------------------------------------------------
# reading each repository: fx scan, style-sample

class ScanTest(ProjectCase):
    def test_scan_and_style_sample_name_the_repository(self):
        code, res = self.fx("scan")
        self.assertEqual(code, 3, res)
        self.assertIn("name one with --repo (be, fe)", res["error"])
        res = self.ok("scan", "--repo", "fe")
        self.assertEqual((res["repo_name"], res["repo"]), ("fe", self.work("_read-fe")))
        res = self.ok("style-sample", "--repo", "fe")
        self.assertEqual(res["repo"], "fe")
        self.assertIn("knowledge/fe/style.md", res["next"])
        self.assertIn("fe:path:lines", res["next"])
        self.assertEqual(self.ok("scan", "--source", self.work("_read-fe"))["repo_name"], None)


class OneRepoScanTest(ProjectCase):
    names = ("be",)

    def test_one_repository_needs_no_name(self):
        res = self.ok("scan")
        self.assertEqual(res["repo_name"], "be")
        self.assertIn("(path:lines)", self.ok("style-sample")["next"])


# --------------------------------------------------------------------------
# knowledge/ and logic/: shared files, knowledge/<repo>/, citations repo:path

class KnowledgeCase(ProjectCase):
    def setUp(self):
        super().setUp()
        self.push("be", {"app/m%d.py" % i: "x = %d\n" % i for i in range(5)})
        k = os.path.join(self.factory, "knowledge")
        write(os.path.join(k, "README.md"), "# Knowledge\n")
        write(os.path.join(k, "overview.md"), "# Overview\n\nThe API is `be:app/m1.py:1`, the screens `fe:README.md`.\n")
        write(os.path.join(k, "be", "README.md"), "# be\n")
        write(os.path.join(k, "be", "architecture.md"), "# be\n\n- `be:app/` the code\n")
        write(os.path.join(k, "fe", "README.md"), "# fe\n")

    def push(self, name, files, msg="more"):
        clone = os.path.join(self.tmp, "push-%s" % name)
        if not os.path.isdir(clone):
            sh(["git", "clone", "-q", self.origin[name], clone], self.tmp)
        sh(["git", "pull", "-q", "origin", "main"], clone)
        for rel, text in files.items():
            write(os.path.join(clone, rel), text)
        sh(["git", "add", "-A"], clone)
        sh(["git", "commit", "-q", "-m", msg], clone)
        sh(["git", "push", "-q", "origin", "HEAD:main"], clone)
        return sh(["git", "rev-parse", "HEAD"], clone).strip()

    def check(self):
        return self.ok("knowledge", "check")


class KnowledgeCheckTest(KnowledgeCase):
    def test_repo_paths_pass(self):
        res = self.check()
        self.assertTrue(res["clean"], res)
        self.assertEqual(res["citations_checked"], 3)
        self.assertEqual(res["repos"]["be"]["missing_modules"], [])

    def test_a_plain_path_is_wrong_with_several_repositories(self):
        write(os.path.join(self.factory, "knowledge", "fe", "ui.md"), "# UI\n\nSee `app/m1.py` and `be:nope.py`.\n"
                                                                         "Lines `be:app/m1.py:1-9`.\n")
        res = self.check()
        self.assertFalse(res["clean"])
        problems = "\n".join(res["problems"])
        self.assertIn("fe/ui.md:3: `app/m1.py`: write it as <repo>:app/m1.py", problems)
        self.assertIn("found in be", problems)
        self.assertIn("fe/ui.md:3: `be:nope.py` does not exist in the code", problems)
        self.assertIn("fe/ui.md:4: `be:app/m1.py:1-9` is outside the file", problems)

    def test_an_unknown_repository(self):
        write(os.path.join(self.factory, "knowledge", "domain.md"), "# Domain\n\n`web:src/a.ts`\n")
        self.assertIn("web is not one of the repositories (be, fe)", "\n".join(self.check()["problems"]))

    def test_each_repository_has_its_own_module_map(self):
        write(os.path.join(self.factory, "knowledge", "be", "architecture.md"), "# be\n\nNothing yet.\n")
        res = self.check()
        self.assertEqual(res["missing_modules"], ["be:app"])
        self.assertEqual(res["repos"]["be"]["missing_modules"], ["app"])
        self.assertEqual(res["repos"]["fe"]["missing_modules"], [])
        # the shared files are no module map
        write(os.path.join(self.factory, "knowledge", "architecture.md"), "# all\n\n- `be:app/`\n")
        self.assertEqual(self.check()["missing_modules"], ["be:app"])

    def test_a_repository_without_its_folder(self):
        import shutil
        shutil.rmtree(os.path.join(self.factory, "knowledge", "fe"))
        self.assertIn("fe/: missing", "\n".join(self.check()["problems"]))

    def test_the_logic_cites_repo_paths_too(self):
        l = os.path.join(self.factory, "logic")
        write(os.path.join(l, "README.md"), LOGIC_INDEX)
        write(os.path.join(l, "billing", "README.md"), LOGIC_MODULE)
        write(os.path.join(l, "billing", "invoices.md"), LOGIC_FEATURE % "- `be:app/m2.py:1` bills.")
        res = self.ok("logic", "check")
        self.assertTrue(res["clean"], res)
        write(os.path.join(l, "billing", "invoices.md"), LOGIC_FEATURE % "- `app/m2.py:1` bills.")
        res = self.ok("logic", "check")
        self.assertIn("write it as <repo>:app/m2.py", "\n".join(res["problems"]))

    def test_the_prompts_name_every_repository_s_code(self):
        review = self.ok("knowledge", "review")["prompt"]
        self.assertIn("-code-be", review)
        self.assertIn("-code-fe", review)
        self.assertIn("one folder per repository", review)
        self.assertIn("Main modules of the code of be: app (5 files)", review)
        self.assertIn("<repo>:<path>", review)
        logic = self.ok("logic", "ctx", "--module", "billing")["prompt"]
        self.assertIn("one folder per repository", logic)
        self.assertIn("a plain path fails the check", logic)


class OneRepoKnowledgeTest(ProjectCase):
    names = ("be",)

    def test_plain_paths_stay_fine(self):
        write(os.path.join(self.factory, "knowledge", "be", "architecture.md"), "# be\n\n`README.md:1` and "
                                                                                "`be:README.md`\n")
        res = self.ok("knowledge", "check")
        self.assertTrue(res["clean"], res)
        self.assertEqual(res["citations_checked"], 2)
        self.assertNotIn("repos", res)


class KnowledgeRefreshTest(KnowledgeCase):
    def test_each_repository_has_its_own_ref(self):
        res = self.ok("knowledge", "mark")
        self.assertEqual(sorted(res["repos"]), ["be", "fe"])
        for n in ("be", "fe"):
            head = sh(["git", "rev-parse", "main"], self.origin[n]).strip()
            self.assertEqual(load_json(os.path.join(self.factory, "knowledge", n, ".ref"))["commit"], head)
        self.assertFalse(os.path.exists(os.path.join(self.factory, "knowledge", ".ref")))
        self.assertFalse(self.ok("knowledge", "stale")["stale"])
        new = self.push("fe", {"package.json": '{"name": "web"}\n'})
        st = self.ok("knowledge", "stale")
        self.assertEqual(st["stale_repos"], ["fe"])
        self.assertEqual(sorted(st["repos"]["fe"]["files"]), ["fe/stack.md", "security.md"])
        self.assertFalse(self.ok("knowledge", "stale", "--repo", "be")["stale"])
        rows = {c["check"]: c for c in self.ok_doctor()}
        self.assertIn("stale", rows["knowledge in step with the base branch (fe) (advice)"]["detail"])
        self.assertIn("describes", rows["knowledge in step with the base branch (be) (advice)"]["detail"])
        code, res = self.fx("knowledge", "ctx")
        self.assertEqual(code, 3, res)
        res = self.ok("knowledge", "ctx", "--repo", "fe")
        stage = res["stage"]
        self.assertEqual(res["repo"], "fe")
        self.assertIn("this repository's own files in %s" % os.path.join(stage, "fe"), res["prompt"])
        write(os.path.join(stage, "fe", "stack.md"), "# fe stack\n\nA web app (`fe:package.json`).\n")
        write(os.path.join(stage, "stack.md"), "# misplaced\n")
        write(os.path.join(stage, "changes.md"), "# changes\n")
        self.assertTrue(self.ok("knowledge", "check", "--stage", stage)["clean"])
        code, res = self.fx("knowledge", "apply", "--stage", stage, "--files", "stack.md")
        self.assertEqual(code, 1, res)
        self.assertIn("stage it as fe/stack.md", res["error"])
        before_be = slurp(os.path.join(self.factory, "knowledge", "be", ".ref"))
        res = self.ok("knowledge", "apply", "--stage", stage, "--files", "fe/stack.md")
        self.assertEqual(res["applied"], ["fe/stack.md"])
        self.assertIn("A web app", slurp(os.path.join(self.factory, "knowledge", "fe", "stack.md")))
        self.assertEqual(load_json(os.path.join(self.factory, "knowledge", "fe", ".ref"))["commit"], new)
        self.assertEqual(slurp(os.path.join(self.factory, "knowledge", "be", ".ref")), before_be)
        self.assertFalse(self.ok("knowledge", "stale")["stale"])

    def test_apply_and_skip_delete_only_a_snapshot_of_the_work_folder(self):
        self.ok("knowledge", "mark")
        self.push("fe", {"package.json": '{"name": "web"}\n'})
        res = self.ok("knowledge", "ctx", "--repo", "fe")
        stage = res["stage"]
        meta_path = os.path.join(stage, "meta.json")
        meta = load_json(meta_path)
        self.assertEqual(os.path.basename(meta["snapshot"]), "_knowledge-fe")
        precious = os.path.join(self.tmp, "precious")
        write(os.path.join(precious, "keep.txt"), "mine\n")
        for bad in (precious, os.path.join(os.path.dirname(meta["snapshot"]), "_knowledge-fe", "..", "fe-elsewhere"),
                    os.path.join(self.tmp, "_knowledge-fe")):
            os.makedirs(bad, exist_ok=True)
            write(meta_path, json.dumps(dict(meta, snapshot=bad)))
            for action in (("apply", "--stage", stage, "--files", "fe/stack.md"), ("skip", "--stage", stage)):
                write(os.path.join(stage, "fe", "stack.md"), "# fe stack\n")
                code, out = self.fx("knowledge", *action)
                self.assertEqual(code, 3, (bad, action, out))
                self.assertIn("not a knowledge snapshot", out["error"])
                self.assertTrue(os.path.isdir(bad), bad)
        self.assertEqual(slurp(os.path.join(precious, "keep.txt")), "mine\n")
        write(meta_path, json.dumps(meta))
        self.ok("knowledge", "skip", "--stage", stage)
        self.assertFalse(os.path.exists(meta["snapshot"]))

    def ok_doctor(self):
        code, res = self.fx("doctor")
        return res["checks"]


# --------------------------------------------------------------------------
# what is gone

class RemovedTest(ProjectCase):
    def test_removed_commands_answer_as_unknown_ones(self):
        for argv, words in ((("feature", "ctx", "PROJ-1"), "one job now covers every repository"),
                            (("feature", "start", "PROJ-1"), "fx jira children <KEY> lists them"),
                            (("plan",), "intake picks the ones an issue needs"),
                            (("workspace", "list"), "fx init --repo <name>=<git URL>"),
                            (("--repo", self.factory, "status"), "run it from there")):
            code, res = self.fx(*argv)
            self.assertEqual(code, 3, (argv, res))
            self.assertIn(words, res["error"], argv)
            self.assertTrue(res["unknown_command"])
        self.assertIn("children", fx.JIRA_READS, "fx jira children stays, for a parent issue")

    def test_the_scope_writer_is_gone(self):
        plugin = os.path.dirname(os.path.dirname(fx.SKILL_DIR))
        self.assertFalse(os.path.exists(os.path.join(plugin, "agents", "factory-scope-writer.md")))
        self.assertTrue(os.path.exists(os.path.join(plugin, "agents", "factory-fit-checker.md")))
        source = slurp(fx.SELF)
        for gone in ("scope-writer", "scope_writer", "workspace.json", "feature_", "api-notes"):
            self.assertNotIn(gone, source)

    def test_no_api_notes_in_a_pull_request_and_no_repository_before_a_comment(self):
        ctx = self.ctx()
        jd = os.path.join(self.factory, "jobs", "T-9")
        write(os.path.join(jd, "spec.md"), "# Spec\n\n## Goal\nReset.\n")
        write(os.path.join(jd, "api-notes.md"), "POST /api/reset\n")
        job = {"id": "T-9", "issue_key": "T-9", "summary": "s", "base_branch": "main", "round": 0, "round_start": 0,
               "max_rounds": 3, "models": {}, "jira_base": "https://example.atlassian.net",
               "feature": {"id": "PROJ-9", "repo": "be", "shared": True}}
        self.assertNotIn("API notes", fx.pr_body(ctx, job))
        ctx.cfg["jira"] = {"mode": "mcp"}
        fx.jira_comment(ctx, job, "## Summary\n\nDone.")
        self.assertEqual(job["jira_todo"][-1]["markdown"], "## Summary\n\nDone.")


if __name__ == "__main__":
    unittest.main()
