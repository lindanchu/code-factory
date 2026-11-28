"""Tests for the fixes of 3.17 (standard library only). Run from the repository root:
    python3 -I -m unittest discover -s tests

A dry run of the gate after the build stage; rounds a person gives back; the job's own permission file read or named;
the permission table's verdict on routes before a cell is called; a job started from another branch than the
repository's base branch.
"""

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from test_permissions import ROUTES, ROWS
from test_project_gate import GateCase
from test_security import fx, prompt_text, sh, write


TECH = {"routes": "true", "snapshot": "true", "restore": "true", "fingerprint": "true",
        "who": {"guest": {"login": None, "deny": {"status": [401]}},
                "viewer": {"login": {"command": "echo 'Bearer v'", "check": "/me"}, "deny": {"status": [403]}}}}


def failed_rounds(case, job, n):
    """n failed rounds on the job's commit, as the gate keeps them."""
    ctx = case.ctx()
    j = ctx.load_exact(job)
    for k in range(1, n + 1):
        j["round"] += 1
        j.setdefault("gate_rounds", []).append({"round": j["round"], "commit": "c", "gate": "FAIL", "failed_at": "other",
                                                "at": fx.now()})
    ctx.save(j)


# --------------------------------------------------------------------------
# a dry run of the gate after the build stage

class DryRunLaterTest(GateCase):
    def setUp(self):
        super().setUp()
        self.job = self.job_for("be")
        self.commit_in(self.work(self.job, "be"), "be.txt", "work\n")

    def move(self, stage, **fields):
        ctx = self.ctx()
        j = ctx.load_exact(self.job)
        j.update(stage=stage, **fields)
        ctx.save(j)

    def test_in_approve_and_with_no_round_left(self):
        self.move("approve")
        failed_rounds(self, self.job, 3)
        code, res = self.fx("gate", self.job, "--dry-run")
        self.assertEqual(code, 3, res)
        self.assertIn("a dry run of the gate", res["error"])
        self.assertEqual(self.raw(self.job)["stage"], "approve")
        code, res = self.fx("gate", self.job)
        self.assertNotEqual(code, 0)
        self.assertIn("--dry-run", res["error"])

    def test_not_once_pushed(self):
        self.move("pr", pushed_head="abc")
        code, res = self.fx("gate", self.job, "--dry-run")
        self.assertNotEqual(code, 0)
        self.assertIn("not pushed yet", res["error"])


# --------------------------------------------------------------------------
# rounds a person gives back

class GiveBackTest(GateCase):
    def setUp(self):
        super().setUp()
        self.job = self.job_for("be")
        failed_rounds(self, self.job, 3)

    def give(self, *args):
        code, res = self.fx("rounds", self.job, "--give-back", "2,3", "--reason", "the seed had no row", *args)
        return code, res

    def test_two_steps_and_back_to_build(self):
        ctx = self.ctx()
        j = ctx.load_exact(self.job)
        j["stage"] = "pr"
        ctx.save(j)
        self.assertEqual(fx.rounds_left(self.raw(self.job)), 0)
        code, res = self.give()
        self.assertEqual(code, 0, res)
        self.assertEqual((res["written"], res["would_give_back"], res["rounds_left_after"]), (False, [2, 3], 2))
        self.assertEqual(fx.rounds_left(self.raw(self.job)), 0, "nothing written before the person's yes")
        code, bad = self.give("--approved", "yes", "--confirm", "000000000000")
        self.assertEqual(code, 3, bad)
        code, done = self.give("--approved", "the lead said yes today", "--confirm", res["confirm"])
        self.assertEqual(code, 0, done)
        raw = self.raw(self.job)
        self.assertEqual((raw["stage"], fx.rounds_left(raw)), ("build", 2))
        self.assertEqual(raw["rounds_human"][0]["rounds"], [2, 3])
        text = prompt_text(self.ok("ctx", self.job, "--agent", "approver"))
        self.assertIn("Gate rounds a human gave back", text)
        self.assertIn("the seed had no row", text)

    def test_only_failed_rounds_of_this_attempt(self):
        code, res = self.fx("rounds", self.job, "--give-back", "4", "--reason", "x")
        self.assertEqual(code, 3, res)
        code, res = self.fx("rounds", self.job, "--give-back", "2")
        self.assertEqual(code, 3, res)
        self.assertIn("--reason", res["error"])

    def test_the_rounds_alone(self):
        res = self.ok("rounds", self.job)
        self.assertEqual(sorted(res["failed_rounds"]), ["1", "2", "3"])


# --------------------------------------------------------------------------
# the job's own permission file, and the verdict on routes before a cell is called

class PermFilesTest(GateCase):
    extra = {"be": {"app": {"up": "true"}}, "fe": {"app": {"up": "true"}}}

    def setUp(self):
        super().setUp()
        write(os.path.join(self.factory, "stack", "permissions-be.json"), json.dumps(TECH))
        self.commit_factory()
        self.job = self.job_for("be")

    def test_a_job_file_of_another_name_is_named(self):
        write(os.path.join(self.factory, "jobs", self.job, "permissions.json"), "{}")
        tech, problems = fx.perm_tech(fx.repo_ctx(self.ctx(), "be"), self.raw(self.job))
        self.assertIsNone(tech)
        self.assertIn("permissions-be.json", problems[0])
        self.assertIn("does not read", problems[0])

    def test_out_of_scope_from_the_job_file(self):
        write(os.path.join(self.factory, "jobs", self.job, "permissions-be.json"),
              json.dumps({"out_of_scope": {"* /reports/*": "reports"}}))
        tech, problems = fx.perm_tech(fx.repo_ctx(self.ctx(), "be"), self.raw(self.job))
        self.assertEqual(problems, [])
        out_scope, unrowed, stale = fx.perm_route_judge(tech, {}, {"GET /reports/x": "", "GET /me": ""})
        self.assertEqual((out_scope, unrowed, stale), ({"GET /reports/x": "reports"}, ["GET /me"], []))

    def test_permissions_check_of_a_job_calls_no_cell(self):
        rows = {k: v for k, v in ROWS.items() if k != "GET /me"}
        tables = {"rows": rows, "problems": [], "whos": set()}
        buf = io.StringIO()
        with mock.patch.object(fx, "perm_tables", return_value=tables), \
                mock.patch.object(fx, "perm_cmd", return_value=ROUTES), \
                mock.patch.object(fx, "baseline_of", return_value={}), \
                mock.patch.object(fx, "perm_matrix", side_effect=AssertionError("no cell is called")), \
                contextlib.redirect_stdout(buf):
            fx.perm_check_job(self.ctx(), self.ctx().load(self.job))
        res = json.loads(buf.getvalue())
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["repos"]["be"]["unrowed"], ["GET /me"])
        self.assertIn("GET /me has no row", res["repos"]["be"]["gate_would_void"][0])

    def test_the_gate_sets_the_round_aside_before_the_table(self):
        seen = []

        def matrix(rctx, job, url, tech, rows, scope=None, early=None):
            early({"GET /me": ""}, ["GET /me"], [])
            seen.append("cells called")
        with mock.patch.object(fx, "perm_matrix", side_effect=matrix), \
                mock.patch.object(fx, "perm_state", return_value={"confirmed": True}), \
                mock.patch.object(fx, "perm_needed", return_value=True), \
                mock.patch.object(fx, "perm_tables", return_value={"rows": {}, "problems": [], "whos": set()}), \
                mock.patch.object(fx, "baseline_of", return_value={"permissions": {"routes": {}}}):
            ctx = self.ctx()
            job = ctx.load(self.job)
            rctx, view = fx.one_app(ctx, job, "be")
            res = fx.permissions_gate(ctx, job, view, rctx, "http://127.0.0.1:1", self.tmp)
        self.assertEqual(seen, [])
        self.assertIn("GET /me has no row", res["void"][0])


# --------------------------------------------------------------------------
# a job started from another branch than the repository's base branch

class OtherBaseTest(GateCase):
    extra = {"be": {"protected_paths": ["tests/*"]}}

    def setUp(self):
        super().setUp()
        self.push_base("be", {"tests/helper.txt": "old\n"})
        # an earlier attempt's branch: it changed a protected test, and main moves on after it
        tmp = os.path.join(self.tmp, "seed-attempt")
        sh(["git", "clone", "-q", self.origin["be"], tmp], self.tmp)
        sh(["git", "checkout", "-q", "-b", "attempt-1"], tmp)
        write(os.path.join(tmp, "tests", "helper.txt"), "changed by the earlier attempt\n")
        write(os.path.join(tmp, "feature.txt"), "earlier work\n")
        sh(["git", "add", "-A"], tmp)
        sh(["git", "commit", "-q", "-m", "attempt"], tmp)
        sh(["git", "push", "-q", "origin", "attempt-1"], tmp)
        write(os.path.join(tmp, "x"), "")  # main moves on: another team's work
        sh(["git", "checkout", "-q", "main"], tmp)
        os.remove(os.path.join(tmp, "x"))
        write(os.path.join(tmp, "auth", "other-team.txt"), "theirs\n")
        sh(["git", "add", "-A"], tmp)
        sh(["git", "commit", "-q", "-m", "other team"], tmp)
        sh(["git", "push", "-q", "origin", "main"], tmp)
        res = self.new("--repos", "be", "--base", "be=attempt-1")
        self.job = res["job"]
        self.assertTrue(any("not its base branch main" in n for n in res.get("notes") or []), res)
        self.to_build(self.job)
        wt = self.work(self.job, "be")
        sh(["git", "fetch", "-q", "origin", "main"], wt)
        sh(["git", "merge", "-q", "--no-edit", "origin/main"], wt)
        self.commit_in(wt, "mine.txt", "mine\n")

    def test_only_its_own_work_counts_and_the_protected_change_is_seen(self):
        ctx = self.ctx()
        view = ctx.load(self.job)
        fx.job_base_from_remote(ctx, view)
        mine = sorted(p for _, ps in fx.changed_files(view) for p in ps)
        self.assertEqual(mine, ["mine.txt"])
        either = sorted(p for _, ps in fx.changed_files(view, guard=True) for p in ps)
        self.assertIn("tests/helper.txt", either)
        self.assertNotIn("auth/other-team.txt", either)
        code, res = self.gate()
        self.assertEqual(res.get("gate"), "FAIL", res)
        self.assertIn("Protected file modified: tests/helper.txt", self.checks_md())


if __name__ == "__main__":
    unittest.main()
