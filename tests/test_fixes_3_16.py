"""Tests for the fixes of 3.16 (standard library only). Run from the repository root:
    python3 -I -m unittest discover -s tests

A job of one repository in a factory of several reads that repository's settings; what a human allows a job reads each
repository's protected_paths, and never a suppression; folders among the local files; a merge request's address and
not a CLI's release notice; a security audit's findings as its skill writes them; one stage of the checks script with
the test runner's arguments; the builder told which existing files the gate protects.
"""

import json
import os
import sys
import types
import unittest
from unittest import mock

from test_project_gate import AppsCase, GateCase
from test_project_repos import ProjectCase
from test_security import fx, prompt_text, sh, slurp, write


# --------------------------------------------------------------------------
# a job of one repository in a factory of several reads that repository's settings

class OneRepoJobSettingsTest(AppsCase):
    def test_the_gate_starts_the_app_set_in_the_repository_s_entry(self):
        job = self.job_for("be")
        self.assertEqual(list(self.raw(job)["repos"]), ["be"])
        self.commit_in(self.work(job, "be"), "api.txt", "api\n")
        self.addCleanup(self.fx, "app", "down", job)
        code, res = self.gate(job)
        self.assertEqual(code, 0, res)
        self.assertIn("be", slurp(self.order).split(), "app.up of be's entry ran")
        self.assertNotIn("no running app", self.checks_md(job))


class OneRepoProtectedTest(GateCase):
    extra = {"be": {"protected_paths": ["tests/**"]}}

    def setUp(self):
        super().setUp()
        self.push_base("be", {"tests/a.txt": "old\n"})
        self.job = self.job_for("be")
        self.commit_in(self.work(self.job, "be"), "tests/a.txt", "changed\n")

    def test_the_gate_reads_the_repository_s_protected_paths(self):
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", res)
        self.assertIn("Protected file modified: tests/a.txt", self.checks_md())

    def allow(self, *args):
        code, res = self.fx("override", self.job, *args)
        self.assertEqual(code, 0, res)
        code, res = self.fx("override", self.job, *args, "--approved", "the lead said yes", "--confirm", res["confirm"])
        self.assertEqual(code, 0, res)
        return res

    def test_override_reads_the_repository_s_protected_paths(self):
        code, res = self.fx("override", self.job, "--unprotect", "tests/a.txt")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["would_allow"]["unprotect"], ["tests/a.txt"])
        code, res = self.fx("override", self.job, "--unprotect", "src/a.txt")
        self.assertEqual(code, 3, res)
        self.assertIn("tests/**", res["error"])

    def test_a_repository_prefix_and_the_gate_then_passes(self):
        self.allow("--unprotect", "be:tests/a.txt")
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertIn("Existing test changed with that permission: tests/a.txt", self.checks_md())

    def test_a_prefix_of_a_repository_the_job_does_not_change(self):
        code, res = self.fx("override", self.job, "--unprotect", "fe:tests/a.txt")
        self.assertEqual(code, 3, res)
        self.assertIn("does not change repository fe", res["error"])

    def test_the_builder_is_told_the_protected_paths(self):
        text = prompt_text(self.ok("ctx", self.job, "--agent", "builder"))
        self.assertIn("Protected paths (an existing file there may not be modified", text)
        self.assertIn("tests/**", text)


class UnprotectGlobsTest(ProjectCase):
    def test_per_repository_and_plain(self):
        ctx = self.ctx()
        allowed = {"unprotect": ["be:tests/a.txt", "fe:spec/*", "shared/x.txt", "nope:y.txt"]}
        self.assertEqual(fx.unprotect_globs(ctx, allowed, "be"), ["tests/a.txt", "shared/x.txt", "nope:y.txt"])
        self.assertEqual(fx.unprotect_globs(ctx, allowed, "fe"), ["spec/*", "shared/x.txt", "nope:y.txt"])


# --------------------------------------------------------------------------
# a suppression is never accepted for one job

class SuppressionOverrideTest(GateCase):
    def test_refused_at_once(self):
        job = self.job_for("be")
        for rule in ("nosemgrep", ".gitleaksignore", "gitleaks:allow", "osv-scanner.toml"):
            code, res = self.fx("override", job, "--accept", rule, "--path", "**", "--reason", "a human looked")
            self.assertEqual(code, 3, (rule, res))
            self.assertIn("security.allow_suppressions", res["error"])
        self.assertFalse(os.path.exists(os.path.join(self.factory, "overrides", "%s.json" % job)))
        code, res = self.fx("override", job, "--accept", "python.lang.security.x", "--path", "**", "--reason", "looked")
        self.assertEqual(code, 0, res)


# --------------------------------------------------------------------------
# folders among the local files (copy_into_worktree)

class LocalFolderTest(GateCase):
    extra = {"be": {"copy_into_worktree": [".env", "assets/web"]}}

    def setUp(self):
        super().setUp()
        local = os.path.join(self.factory, "local", "be")
        write(os.path.join(local, ".env"), "APP_KEY=base64:Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5\n")
        write(os.path.join(local, "assets", "web", "index.html"), "<html></html>\n")
        write(os.path.join(local, "assets", "web", "config.env"), "SENTRY_DSN=https://abc123def456@sentry.example/1\n")
        with open(os.path.join(local, "assets", "web", "logo.png"), "wb") as f:
            f.write(b"\x89PNG\0\0binary")

    def test_secret_values_read_text_files_in_a_folder(self):
        vals = fx.local_secret_values(self.ctx())
        self.assertIn("base64:Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5", vals)
        self.assertIn("https://abc123def456@sentry.example/1", vals)

    def test_a_clone_gets_the_folder_and_it_is_never_work(self):
        job = self.job_for("be")
        wt = self.work(job, "be")
        self.assertEqual(slurp(os.path.join(wt, "assets", "web", "index.html")), "<html></html>\n")
        ctx = self.ctx()
        self.assertTrue(fx.is_local_file(ctx, "assets/web/index.html", "be"))
        self.assertEqual(fx.worktree_dirt(ctx, wt, "be")[1], [])

    def test_a_local_file_added_later_reaches_the_clone(self):
        job = self.job_for("be")
        wt = self.work(job, "be")
        write(os.path.join(self.factory, "local", "be", "assets", "web", "late.js"), "x\n")
        os.remove(os.path.join(wt, ".env"))
        got = fx.local_files_fill(self.ctx(), self.raw(job))
        self.assertEqual(got, ["be: .env"])
        self.assertTrue(os.path.isfile(os.path.join(wt, ".env")))
        self.assertFalse(os.path.exists(os.path.join(wt, "assets", "web", "late.js")), "what is there stays as it is")

    def test_logic_check_with_nothing_staged(self):
        job = self.job_for("be")
        res = self.ok("logic", "check", "--job", job)
        self.assertTrue(res["clean"], res)
        self.assertIn("nothing to check", res["next"])


# --------------------------------------------------------------------------
# fx pr keeps a merge request's address, not the CLI's release notice

class MergeRequestUrlTest(ProjectCase):
    def test_glab_s_release_notice_is_not_the_merge_request(self):
        ctx = self.ctx()
        job = {"id": "PROJ-7", "repos": {"be": {"worktree": self.tmp, "branch": "f/x", "target_branch": "main"}}}
        mr = "https://git.example.com/team/app/-/merge_requests/12"
        said = [types.SimpleNamespace(returncode=0, stdout="Creating merge request\n%s\n\nA new version of glab is "
                                      "available: https://gitlab.com/gitlab-org/cli/-/releases/v9.9.9\n" % mr)]

        def fake_run(cmd, **kw):
            return said.pop(0) if said else types.SimpleNamespace(returncode=1, stdout="")
        with mock.patch.object(fx, "remote_provider", return_value=("gitlab", "https://git.example.com/team/app.git")), \
                mock.patch.object(fx.shutil, "which", return_value="/usr/bin/glab"), \
                mock.patch.object(fx, "run", side_effect=fake_run), \
                mock.patch.object(fx.Ctx, "c", lambda self, k, d=None: True if k == "pr.create" else d):
            url, _ = fx.pr_open(ctx, job, "be", "t", "b", os.path.join(self.tmp, "b.md"), [], False, False, [], "")
        self.assertEqual(url, mr)

    def test_a_stored_release_url_is_found_again(self):
        ctx = self.ctx()
        job = {"id": "PROJ-7", "repos": {"be": {"worktree": self.tmp, "branch": "f/x", "target_branch": "main",
                                                "pr_url": "https://gitlab.com/gitlab-org/cli/-/releases/v9.9.9"}}}
        mr = "https://git.example.com/team/app/-/merge_requests/3"
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd[:3])
            if cmd[:3] == ["glab", "mr", "create"]:
                return types.SimpleNamespace(returncode=1, stdout="a merge request for this branch already exists")
            return types.SimpleNamespace(returncode=0, stdout="title\n%s\n" % mr)
        with mock.patch.object(fx, "remote_provider", return_value=("gitlab", "https://git.example.com/team/app.git")), \
                mock.patch.object(fx.shutil, "which", return_value="/usr/bin/glab"), \
                mock.patch.object(fx, "run", side_effect=fake_run), \
                mock.patch.object(fx.Ctx, "c", lambda self, k, d=None: True if k == "pr.create" else d):
            url, _ = fx.pr_open(ctx, job, "be", "t", "b", os.path.join(self.tmp, "b.md"), [], False, False, [], "")
        self.assertEqual(url, mr)
        self.assertIn(["glab", "mr", "view"], calls)


# --------------------------------------------------------------------------
# a security audit's findings.json as the skill writes it

FINDINGS = [
    {"verdict": "confirmed", "fingerprint": "src/orders.py:load:idor", "title": "Order read across accounts",
     "description": "x", "root_cause": "x", "intended_behavior": "x",
     "trace": [{"kind": "entrypoint", "file": "src/routes.py", "line": 10, "scope": "r", "description": "d"},
               {"kind": "sink", "file": "src/orders.py", "line": 42, "scope": "s", "description": "d"}],
     "evidence": [{"file": "src/orders.py", "line": 42, "description": "d"}],
     "severity": {"likelihood": {"score": "medium", "reason": "r"}, "impact": {"score": "high", "reason": "r"},
                  "overall_severity": "high"},
     "confidence": {"score": "high", "reason": "r"}},
    {"verdict": "confirmed", "fingerprint": "src/a.py:f:x", "title": "Low one", "description": "x",
     "trace": [{"kind": "sink", "file": "src/a.py", "line": 7, "scope": "s", "description": "d"}],
     "severity": {"likelihood": {"score": "low", "reason": "r"}, "impact": {"score": "low", "reason": "r"},
                  "overall_severity": "low"}},
    {"verdict": "needs_validation", "fingerprint": "src/b.py:g:y", "title": "Maybe", "description": "x",
     "evidence": [{"file": "src/b.py", "line": 3, "description": "d"}]},
]


class AuditImportTest(ProjectCase):
    def test_severity_and_location_of_the_skill_s_records(self):
        path = os.path.join(self.tmp, "findings.json")
        write(path, json.dumps(FINDINGS))
        res = self.ok("audit-import", path)
        self.assertEqual([(r["severity"], r["location"]) for r in res["confirmed"]],
                         [("high", "src/orders.py:42 (from src/routes.py:10)"), ("low", "src/a.py:7")])
        summary = slurp(res["summary"])
        self.assertIn("| needs_validation | medium | Maybe | src/b.py:3 |", summary)
        self.assertTrue(slurp(res["confirmed"][0]["task_file"]).startswith("Security audit finding F1 (high)"))

    def test_older_flat_records(self):
        self.assertEqual(fx.audit_severity({"severity": "critical"}), "critical")
        self.assertEqual(fx.audit_location({"location": {"file": "a.py", "start_line": 3, "end_line": 5}}), "a.py:3-5")
        self.assertEqual(fx.audit_location({"file": "a.py"}), "a.py")


# --------------------------------------------------------------------------
# one stage of the checks script, with the test runner's arguments

ONLY_SCRIPT = r'''#!/usr/bin/env bash
set -euo pipefail
only() { [ -z "${FACTORY_ONLY_STAGE:-}" ] || [[ ",$FACTORY_ONLY_STAGE," == *",$1,"* ]]; }
TEST_ARGS=(); [ -n "${FACTORY_TEST_ARGS:-}" ] && eval "TEST_ARGS=($FACTORY_TEST_ARGS)"
if only unit; then echo "::fx-stage unit $(date +%s)"; echo "unit ran"; fi
if only feature; then
  echo "::fx-stage feature $(date +%s)"
  printf 'arg:%s\n' "${TEST_ARGS[@]}"
  [ "${TEST_ARGS[0]:-}" != "fail" ]
fi
'''


class ChecksOnlyTest(GateCase):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.factory, "checks", "be.sh"), ONLY_SCRIPT)
        self.commit_factory()
        self.job = self.job_for("be")
        self.commit_in(self.work(self.job, "be"), "a.txt", "a\n")

    def test_one_stage_with_arguments(self):
        code, res = self.fx("checks", self.job, "--only", "feature", "--", "tests/Feature/Cart Test.php", "--filter=x")
        self.assertEqual(code, 0, res)
        self.assertEqual((res["checks"], res["stages_ran"], res["kept_for_the_gate"]), ("PASS", ["feature"], False))
        log = slurp(res["log"])
        self.assertIn("arg:tests/Feature/Cart Test.php\narg:--filter=x", log)
        self.assertNotIn("unit ran", log)
        self.assertFalse(self.raw(self.job).get("checks_runs"), "a part of the checks is never kept")

    def test_a_failure_and_an_unknown_stage(self):
        code, res = self.fx("checks", self.job, "--only", "feature", "--", "fail")
        self.assertEqual((code, res["checks"]), (1, "FAIL"), res)
        code, res = self.fx("checks", self.job, "--only", "browser")
        self.assertEqual((code, res["checks"]), (1, "FAIL"), res)
        self.assertIn("no stage named browser", res["problem"])

    def test_a_script_that_does_not_run_single_stages(self):
        write(os.path.join(self.factory, "checks", "be.sh"), "#!/usr/bin/env bash\nexit 0\n")
        self.commit_factory()
        code, res = self.fx("checks", self.job, "--only", "feature")
        self.assertEqual(code, 3, res)
        self.assertIn("FACTORY_ONLY_STAGE", res["error"])

    def test_arguments_need_only(self):
        code, res = self.fx("checks", self.job, "--", "tests/x")
        self.assertEqual(code, 3, res)


if __name__ == "__main__":
    unittest.main()
