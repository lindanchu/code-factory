"""Tests for the sandbox step of /auto-factory:review: the code runs before anyone judges it, and what ran is checked against
what fx itself logged. The app is real, a small HTTP server started through app.up, so each request really happens. Run
from the repository root:
    python3 -I -m unittest discover -s tests
"""

import concurrent.futures
import json
import os
import re
import socket
import sys
import zipfile
from unittest import mock

from test_review import ISSUE, ReviewCase, TRACE
from test_security import load_json, sh, slurp, write

SERVER = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        got = self.rfile.read(n).decode()
        code = 200 if self.path != "/bad" else 409
        if self.path == "/moved":
            self.send_response(302); self.send_header("Location", "http://example.com/"); self.end_headers(); return
        data = json.dumps({"path": self.path, "body": got, "auth": self.headers.get("Authorization")}).encode()
        self.send_response(code); self.send_header("Set-Cookie", "s=secret"); self.end_headers(); self.wfile.write(data)
    do_GET = do_POST = _go
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''
RUN_OK = ("RUN: DONE\n\n## Results\n### R1: CSV export\n- Result: pass\n- Calls: 1, 2\n- Saw: both fine\n\n"
          "## Simulated\nA clock aged in the database\n\n## Not run\nNothing\n")


def is_listening(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


class SandboxCase(ReviewCase):
    def setUp(self):
        super().setUp()
        self.server = os.path.join(self.tmp, "server.py")
        write(self.server, SERVER)
        self.set_app(up="%s %s {port}" % (sys.executable, self.server), health="/health")

    def set_app(self, **app):
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["app"] = dict(app, start_timeout_sec=15)
        cfg["ports"] = {"base": 25100, "step": 10}
        write(path, json.dumps(cfg))

    def no_sandbox(self, rid):  # these run the real one
        pass

    def up(self):
        rid, _ = self.scanned()
        code, res = self.review("sandbox", rid)
        self.assertEqual(code, 0, res)
        self.addCleanup(self.stop, rid)
        return rid, res

    def stop(self, rid):
        job = self.job()
        if job.get("sandbox"):
            ctx = self.f.ctx()
            self.fx_ctx_stop(ctx, job)

    def fx_ctx_stop(self, ctx, job):
        from test_security import fx
        fx.review_sandbox_stop(ctx, job)

    def call(self, rid, case, req="R1", path="/items", *more):
        return self.review("call", rid, "--req", req, "--case", case, "--path", path, *more)

    def two_calls(self, rid, req="R1"):
        for case in ("normal", "empty"):
            code, res = self.call(rid, case, req)
            self.assertEqual(code, 0, res)

    def rows(self):
        with open(self.rfile(os.path.join("run", "calls.jsonl"))) as f:
            return [json.loads(x) for x in f]


class SemgrepScannedNothingTest(SandboxCase):
    def test_an_empty_scan_of_no_file_is_said(self):
        from test_security import fx
        real = fx.run_security

        def fake(ctx, job, rdir, *a, **k):
            r = real(ctx, job, rdir, *a, **k)
            write(os.path.join(rdir, "semgrep.json"), json.dumps({"results": [], "paths": {"scanned": []}}))
            return r
        with mock.patch.object(fx, "run_security", fake):
            _, res = self.scanned()
        self.assertTrue(any("scanned no file" in x for x in res["not_checked"]), res["not_checked"])


class NothingBeforeTheRunTest(SandboxCase):
    def test_no_reviewer_gets_a_prompt_before_the_run(self):
        rid, res = self.scanned()
        self.assertEqual(res["next"], "sandbox")
        code, res = self.review("ctx", rid, "--agent", "requirement")
        self.assertEqual(code, 1)
        self.assertIn("stage run", res["error"])

    def test_result_does_not_move_on_without_the_sandbox(self):
        rid, _ = self.scanned()
        code, res = self.review("result", rid)
        self.assertEqual(code, 3)
        self.assertEqual(res["next"], "sandbox")

    def test_the_sandbox_cannot_be_skipped_before_it_failed(self):
        rid, _ = self.scanned()
        code, res = self.review("sandbox", rid, "--skip", "--reason", "quicker", "--confirm", "whatever")
        self.assertEqual(code, 3)
        self.assertIn("nothing stopped the sandbox", res["error"])
        self.assertEqual(self.job()["stage"], "run")

    def test_a_runner_needs_a_running_sandbox(self):
        rid, _ = self.scanned()
        code, res = self.review("ctx", rid, "--agent", "runner")
        self.assertEqual(code, 3)
        self.assertEqual(res["next"], "sandbox")


class NoSandboxTest(SandboxCase):
    def failed(self):
        rid, _ = self.scanned()
        code, res = self.review("sandbox", rid)
        self.assertEqual(code, 3)
        self.assertEqual(res["next"], "ask-no-sandbox")
        return rid, res

    def test_no_app_up_stops_the_review_and_asks(self):
        self.set_app()
        rid, res = self.failed()
        self.assertIn("app.up", res["error"])
        self.assertEqual(self.job()["stage"], "run")

    def test_an_app_that_does_not_start_stops_the_review_and_leaves_nothing(self):
        self.set_app(up="exit 1", health="/health")
        rid, res = self.failed()
        self.assertIsNone(self.job().get("sandbox"))
        self.assertFalse([n for n in os.listdir(self.f.ctx().work) if n.startswith("_sandbox-")])

    def test_going_on_takes_the_code_and_a_reason(self):
        self.set_app()
        rid, res = self.failed()
        code, r = self.review("sandbox", rid, "--skip", "--reason", "x", "--confirm", "wrong")
        self.assertEqual(code, 3)
        code, r = self.review("sandbox", rid, "--skip", "--confirm", res["waive"])
        self.assertEqual(code, 3)
        code, r = self.review("sandbox", rid, "--skip", "--reason", "front end only", "--confirm", res["waive"])
        self.assertEqual(code, 0, r)
        self.assertEqual(r["next"], "review")
        self.assertIn("front end only", " ".join(r["not_checked"]))

    def test_a_skipped_run_is_said_on_the_ticket(self):
        self.set_app()
        rid, res = self.failed()
        self.review("sandbox", rid, "--skip", "--reason", "front end only", "--confirm", res["waive"])
        self.reviews()
        code, r = self.review("result", rid)
        self.assertEqual(code, 0, r)
        self.assertIn("not run", slurp(r["report"]))
        code, d = self.review("draft", rid, "--clean")
        self.assertEqual(code, 0, d)
        self.assertIn("The code was not run for this review", d["comment"])
        self.assertIn("front end only", d["comment"])
        self.assertTrue(any("not run" in n for n in d["notes"]))


class CallTest(SandboxCase):
    def test_a_call_really_happens_and_is_logged(self):
        rid, _ = self.up()
        code, res = self.call(rid, "wrong one", "R1", "/bad", "--method", "POST", "--data", "a=1")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["status"], 409)
        self.assertEqual(json.loads(res["body"])["body"], "a=1")
        row = self.rows()[0]
        self.assertEqual((row["n"], row["req"], row["case"], row["status"], row["kind"]), (1, "R1", "wrong one", 409, "http"))
        self.assertEqual(row["commit"], self.head)

    def test_calls_side_by_side_keep_the_log_whole(self):
        from test_security import fx
        rid, _ = self.up()
        job = self.job()
        ctx = self.f.ctx()
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda i: fx.review_log_row(ctx, dict(job), {"kind": "http", "req": "R1", "case": str(i), "status": 200}), range(24)))
        self.assertEqual([r["n"] for r in fx.review_read_calls(ctx, job)], list(range(1, 25)))

    def test_only_a_path_of_the_sandbox_is_taken(self):
        rid, _ = self.up()
        for bad in ("http://example.com/", "//example.com/", "items", "/a\r\nX: y"):
            code, res = self.call(rid, "x", "R1", bad)
            self.assertEqual(code, 3, bad)
        self.assertFalse(os.path.exists(self.rfile(os.path.join("run", "calls.jsonl"))))

    def test_a_redirect_is_not_followed(self):
        rid, _ = self.up()
        code, res = self.call(rid, "x", "R1", "/moved")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["status"], 302)

    def test_secrets_stay_out_of_the_log(self):
        rid, _ = self.up()
        code, res = self.call(rid, "x", "R1", "/items", "--header", "Authorization: Bearer abc")
        self.assertEqual(json.loads(res["body"])["auth"], "Bearer abc")
        row = self.rows()[0]
        self.assertEqual(row["headers"]["Authorization"], "(hidden)")
        self.assertEqual(row["response_headers"]["Set-Cookie"], "(hidden)")

    def test_a_call_names_its_requirement_and_case(self):
        rid, _ = self.up()
        code, res = self.review("call", rid, "--req", "first", "--case", "x", "--path", "/")
        self.assertEqual(code, 3)
        code, res = self.review("call", rid, "--req", "R1", "--path", "/")
        self.assertEqual(code, 3)

    def test_exec_is_logged_and_has_no_credentials_of_fx(self):
        rid, _ = self.up()
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN": "s3cret"}):
            code, res = self.review("exec", rid, "--req", "R1", "--note", "look", "--command", "echo [$GITLAB_TOKEN] $PWD")
        self.assertEqual(code, 0, res)
        self.assertIn("[]", res["output"])
        self.assertIn("_sandbox-", res["output"])
        self.assertEqual(self.rows()[0]["kind"], "exec")

    def test_a_call_needs_the_run_stage(self):
        rid, _ = self.up()
        self.two_calls(rid)
        write(self.rfile("run.md"), RUN_OK)
        self.assertEqual(self.review("result", rid)[0], 0)
        code, res = self.call(rid, "late")
        self.assertEqual(code, 3)


class RunCheckTest(SandboxCase):
    def refused(self, rid, text, *words):
        write(self.rfile("run.md"), text)
        code, res = self.review("result", rid)
        self.assertEqual(code, 6, res)
        self.assertEqual(res["retry_agent"], "runner")
        for w in words:
            self.assertIn(w, res["error"])
        self.assertEqual(self.job()["stage"], "run")

    def test_a_run_with_calls_passes_and_stops_the_sandbox(self):
        rid, up = self.up()
        port = self.job()["sandbox"]["port"]
        box = self.job()["sandbox"]["dir"]
        self.two_calls(rid)
        write(self.rfile("run.md"), RUN_OK)
        code, res = self.review("result", rid)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "review")
        self.assertEqual(res["ran"]["calls"], 2)
        self.assertFalse(is_listening(port))
        self.assertFalse(os.path.exists(box))
        self.assertTrue(os.path.exists(self.rfile(os.path.join("run", "calls.jsonl"))))

    def test_it_waits_for_the_runner(self):
        rid, _ = self.up()
        code, res = self.review("result", rid)
        self.assertEqual((code, res["next"], res["missing"]), (0, "wait", ["runner"]))

    def test_a_call_that_never_happened_is_refused(self):
        rid, _ = self.up()
        self.two_calls(rid)
        self.refused(rid, RUN_OK.replace("Calls: 1, 2", "Calls: 1, 2, 9"), "call 9")

    def test_no_calls_at_all_is_refused(self):
        rid, _ = self.up()
        self.refused(rid, RUN_OK, "call 1")

    def test_a_call_for_another_requirement_is_refused(self):
        rid, _ = self.up()
        self.two_calls(rid, "R2")
        self.refused(rid, RUN_OK, "R1 cites")

    def test_one_case_is_not_enough(self):
        rid, _ = self.up()
        self.call(rid, "normal")
        self.call(rid, "normal")
        self.refused(rid, RUN_OK, "at least 2")

    def test_a_requirement_left_out_is_refused(self):
        rid, _ = self.up()
        self.refused(rid, "RUN: DONE\n\n## Results\n", "R1 has no")

    def test_not_runnable_needs_a_reason(self):
        rid, _ = self.up()
        self.refused(rid, "RUN: DONE\n\n## Results\n### R1: x\n- Result: not runnable\n", "Why not")

    def test_everything_not_runnable_is_a_review_of_the_code_alone(self):
        rid, _ = self.up()
        self.refused(rid, "RUN: DONE\n\n## Results\n### R1: x\n- Result: not runnable\n- Why not: hard\n", "code alone")

    def test_a_log_changed_by_hand_is_refused(self):
        rid, _ = self.up()
        self.two_calls(rid)
        path = self.rfile(os.path.join("run", "calls.jsonl"))
        text = slurp(path)
        write(path, text.replace('"status": 200', '"status": 201', 1))
        write(self.rfile("run.md"), RUN_OK)
        code, res = self.review("result", rid)
        self.assertEqual(code, 3)
        self.assertIn("changed after fx wrote it", res["error"])

    def test_a_row_written_by_hand_is_refused(self):
        rid, _ = self.up()
        self.two_calls(rid)
        path = self.rfile(os.path.join("run", "calls.jsonl"))
        rows = self.rows()
        rows[1]["case"] = "another"
        write(path, "".join(json.dumps(r) + "\n" for r in rows))
        write(self.rfile("run.md"), RUN_OK)
        self.assertEqual(self.review("result", rid)[0], 3)


class AfterTheRunTest(SandboxCase):
    def ran(self, text=RUN_OK, calls=True):
        rid, _ = self.up()
        if calls:
            self.two_calls(rid)
        write(self.rfile("run.md"), text)
        code, res = self.review("result", rid)
        self.assertEqual(code, 0, res)
        return rid

    def test_the_reviewers_are_told_about_the_run(self):
        rid = self.ran()
        code, res = self.review("ctx", rid, "--agent", "code")
        self.assertEqual(code, 0, res)
        self.assertIn("Real run in a throw-away sandbox", res["prompt"])
        self.assertIn("calls.jsonl", res["prompt"])

    def test_report_and_comment_say_what_ran(self):
        rid = self.ran()
        self.reviews()
        code, res = self.review("result", rid)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["ran"]["calls"], 2)
        report = slurp(res["report"])
        self.assertIn("## Ran in the sandbox", report)
        self.assertIn("2 real API calls", report)
        self.assertIn("A clock aged", report)
        self.assertIn("R1: pass (calls 1, 2)", report)
        code, d = self.review("draft", rid, "--clean")
        self.assertIn("2 real API calls", d["comment"])
        self.assertNotIn("R1: pass", d["comment"])  # the ticket gets what failed; every call stays in the report

    def test_what_failed_is_on_the_ticket(self):
        rid = self.ran(RUN_OK.replace("Result: pass", "Result: fail"))
        self.reviews(requirement="VERDICT: PASS\n\n## Findings\n" + TRACE + "- Disputed: the call was wrong\n")
        self.review("result", rid)
        code, d = self.review("draft", rid, "--clean")
        self.assertIn("R1: fail", d["comment"])

    def test_implemented_needs_more_than_reading_when_it_was_not_run(self):
        rid, _ = self.up()
        write(self.rfile("checklist.md"), "CHECKLIST: READY\n\n## Requirements\n1. CSV export\n2. other\n")
        self.two_calls(rid, "R2")
        write(self.rfile("run.md"), "RUN: DONE\n\n## Results\n### R1: x\n- Result: not runnable\n- Why not: a screen\n\n"
                                    "### R2: other\n- Result: pass\n- Calls: 1, 2\n")
        code, res = self.review("result", rid)
        self.assertEqual(code, 0, res)
        self.assertIn("a screen", " ".join(res["not_checked"]))
        trace = TRACE + "### R2: other\n- State: implemented\n- Path: app/new.py:1\n- Cases: normal: fine\n"
        self.reviews(requirement="VERDICT: PASS\n\n## Findings\n" + trace)
        code, res = self.review("result", rid)
        self.assertEqual(code, 6, res)
        self.assertIn("never run", res["error"])
        self.assertEqual(res["retry_agent"], "requirement")

    def failed_run(self):
        return self.ran(RUN_OK.replace("Result: pass", "Result: fail"))

    def test_a_failed_run_cannot_be_called_implemented(self):
        rid = self.failed_run()
        self.reviews()
        code, res = self.review("result", rid)
        self.assertEqual(code, 6, res)
        self.assertIn("failed in the sandbox", res["error"])

    def test_a_failed_run_can_be_disputed_with_a_reason(self):
        rid = self.failed_run()
        self.reviews(requirement="VERDICT: PASS\n\n## Findings\n" + TRACE + "- Disputed: call 2 sent the wrong token\n")
        code, res = self.review("result", rid)
        self.assertEqual(code, 0, res)

    def test_a_failed_run_called_different_needs_a_blocking_finding(self):
        rid = self.failed_run()
        different = TRACE.replace("State: implemented", "State: different")
        self.reviews(requirement="VERDICT: PASS\n\n## Findings\n" + different)
        code, res = self.review("result", rid)
        self.assertEqual(code, 6, res)

    def test_resume_knows_each_step(self):
        rid, _ = self.scanned()
        self.assertEqual(self.review("resume", rid)[1]["next"], "sandbox")
        self.review("sandbox", rid)
        self.addCleanup(self.stop, rid)
        self.assertEqual(self.review("resume", rid)[1]["next"], "runner")
        self.two_calls(rid)
        write(self.rfile("run.md"), RUN_OK)
        self.assertEqual(self.review("resume", rid)[1]["next"], "result")
        self.review("result", rid)
        self.assertEqual(self.review("resume", rid)[1]["next"], "review")

    def test_starting_over_stops_the_sandbox(self):
        rid, _ = self.up()
        port = self.job()["sandbox"]["port"]
        issue = os.path.join(self.tmp, "issue.md")
        code, res = self.review("start", "https://gitlab.example.com/acme/app/-/merge_requests/7", "--ticket", "PROJ-9",
                                "--issue-file", issue, "--again")
        self.assertEqual(code, 0, res)
        self.assertFalse(is_listening(port))
        self.assertIsNone(self.job().get("sandbox"))

    def test_closing_at_the_run_stops_the_sandbox(self):
        rid, _ = self.up()
        port = self.job()["sandbox"]["port"]
        code, res = self.review("close", rid)
        self.assertEqual(code, 0, res)
        self.assertFalse(is_listening(port))

    def test_a_round_from_before_the_sandbox_starts_over(self):
        rid, _ = self.scanned()
        job = self.job()
        job["stage"] = "review"
        write(os.path.join(self.jd, "job.json"), json.dumps(job))
        self.assertEqual(self.review("ctx", rid, "--agent", "code")[0], 3)
        self.assertEqual(self.review("result", rid)[0], 3)
        self.assertEqual(self.review("resume", rid)[1]["next"], "again")


class MergedTest(SandboxCase):
    def merge(self):
        self.base = sh(["git", "rev-parse", "HEAD~1"], self.f.wt).strip()
        sh(["git", "push", "-q", "origin", "HEAD:main"], self.f.wt)
        self.set_mr(state="merged", diff_refs={"base_sha": self.base, "head_sha": self.head})

    def test_a_ticket_with_only_merged_ones_is_reviewed_through_them(self):
        self.merge()
        self.routes["merge_requests?state=opened"] = []
        self.routes["merge_requests?state=merged"] = [self.mr(state="merged")]
        self.save_routes()
        issue = os.path.join(self.tmp, "issue.md")
        write(issue, ISSUE)
        code, res = self.review("start", "PROJ-9", "--issue-file", issue)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "checklist")
        self.assertEqual(res["merge_state"], "merged")

    def test_several_merged_ones_are_asked_about(self):
        self.merge()
        self.routes["merge_requests?state=opened"] = []
        self.routes["merge_requests?state=merged"] = [self.mr(state="merged"), self.mr(state="merged", iid=8)]
        self.save_routes()
        code, res = self.review("start", "PROJ-9")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "ask-mrs")
        self.assertEqual({c["state"] for c in res["candidates"]}, {"merged"})

    def test_a_ticket_with_nothing_open_or_merged_stops(self):
        self.routes["merge_requests?state=opened"] = []
        self.routes["merge_requests?state=merged"] = []
        self.save_routes()
        code, res = self.review("start", "PROJ-9")
        self.assertEqual(code, 3)
        self.assertIn("no open or merged", res["error"])

    def test_a_closed_one_is_refused(self):
        self.set_mr(state="closed")
        code, res = self.review("start", "https://gitlab.example.com/acme/app/-/merge_requests/7", "--ticket", "PROJ-9")
        self.assertEqual(code, 3)
        self.assertIn("closed", res["error"])

    def test_its_own_diff_is_read_and_the_target_runs(self):
        self.merge()
        self.f.commit({"app/later.py": "later\n"}, "later work")
        sh(["git", "push", "-q", "origin", "HEAD:main"], self.f.wt)
        tip = sh(["git", "rev-parse", "HEAD"], self.f.wt).strip()
        rid, _ = self.up()
        diff = slurp(self.rfile("diff.patch"))
        self.assertIn("app/new.py", diff)
        self.assertNotIn("app/later.py", diff)
        job = self.job()
        self.assertEqual(job["head"], self.head)
        self.assertEqual(job["sandbox"]["commit"], tip)

    def test_a_line_from_the_tip_still_counts_as_this_merge_requests_file(self):
        from test_security import fx
        hunks = {"a.py": [(1, 10)]}
        f = {"severity": "blocking", "path": "a.py", "line": 500}
        self.assertEqual(fx.review_group(f, hunks), "pre-existing")
        self.assertEqual(fx.review_group(f, hunks, merged=True), "diff")
        self.assertEqual(fx.review_group(dict(f, path="b.py"), hunks, merged=True), "pre-existing")

    def test_the_draft_says_it_is_merged(self):
        self.merge()
        rid, _ = self.up()
        self.two_calls(rid)
        write(self.rfile("run.md"), RUN_OK)
        self.review("result", rid)
        self.reviews()
        self.review("result", rid)
        code, d = self.review("draft", rid, "--clean")
        self.assertTrue(any("already merged" in n for n in d["notes"]))
        self.assertIn("target branch", d["comment"])


class RunnerAgentTest(SandboxCase):
    def test_the_rules_are_in_the_agent(self):
        from test_review import AGENTS
        text = slurp(os.path.join(AGENTS, "factory-review-runner.md"))
        for rule in ("fx review call", "fx review exec", "RUN: DONE", "Only the sandbox"):
            self.assertIn(rule, text)

    def test_the_orchestrator_may_not_review_by_itself(self):
        from test_review import ROOT
        text = " ".join(slurp(os.path.join(ROOT, "plugins", "auto-factory", "skills", "code", "SKILL.md")).split())
        self.assertIn('never offer a "direct review"', text)
        self.assertNotIn("code never runs here", text)


UI_CFG = dict(ReviewCase.cfg, ui_paths=["app/*"], reviewers={"always": ["code"], "ui": ["ux", "ui"]})
UI_RUN = RUN_OK


class UiCase(SandboxCase):
    cfg = UI_CFG

    def waived(self):
        self.set_app()
        rid, _ = self.scanned()
        code, res = self.review("sandbox", rid)
        self.assertEqual(res["next"], "ask-no-sandbox", res)
        self.review("sandbox", rid, "--skip", "--reason", "no app", "--confirm", res["waive"])
        return rid

    def write_all(self, **texts):
        from test_review import traced
        for r in self.job()["reviewers"]:
            write(self.rfile("review-%s.md" % r), texts[r] if r in texts else traced(r, "VERDICT: PASS\n\n## Findings\n"))


class TablesTest(UiCase):
    def refused(self, who, *words, **texts):
        rid = self.waived()
        self.write_all(**texts)
        code, res = self.review("result", rid)
        self.assertEqual(code, 6, res)
        self.assertEqual(res["retry_agent"], who)
        for w in words:
            self.assertIn(w, res["error"])

    def test_a_ui_review_without_the_table_is_refused(self):
        self.refused("ui", "## Components", ui="VERDICT: PASS\n\n## Findings\n")

    def test_a_row_must_cite_the_code(self):
        table = "\n## Components\n| E | S | C | Result |\n|---|---|---|---|\n| Button | x | a button | ok |\n"
        self.refused("ui", "path:line", ui="VERDICT: PASS\n\n## Findings\n" + table)

    def test_a_wrong_row_needs_a_finding(self):
        table = "\n## Components\n| E | S | C | Result |\n|---|---|---|---|\n| Button | x | src/a.vue:1 | wrong |\n"
        self.refused("ui", "no finding", ui="VERDICT: PASS\n\n## Findings\n" + table)

    def test_a_result_may_say_why_but_not_something_else(self):
        table = "\n## Components\n| E | S | C | Result |\n|---|---|---|---|\n| Button | x | src/a.vue:1 | wrong (see finding 1) |\n"
        self.refused("ui", "no finding", ui="VERDICT: PASS\n\n## Findings\n" + table)

    def test_a_result_that_is_none_of_them_is_refused(self):
        table = "\n## Components\n| E | S | C | Result |\n|---|---|---|---|\n| Button | x | src/a.vue:1 | looks fine |\n"
        self.refused("ui", "one of ok", ui="VERDICT: PASS\n\n## Findings\n" + table)

    def test_a_ux_review_without_the_text_table_is_refused(self):
        self.refused("ux", "## Text", ux="VERDICT: PASS\n\n## Findings\n")

    def test_a_text_that_differs_needs_a_finding(self):
        table = "\n## Text\n| S | Src | Shown | Match |\n|---|---|---|---|\n| Save | ticket | Sav | no |\n"
        self.refused("ux", "no finding", ux="VERDICT: PASS\n\n## Findings\n" + table)

    def test_a_blocking_ui_finding_says_whose_mistake_it_is(self):
        from test_review import TABLES
        self.refused("ui", "Type:", ui="VERDICT: CHANGES\n\n## Findings\n1. [blocking] app/new.py:1: x Repro: a. Accept: b.\n" + TABLES["ui"])

    def test_the_tables_reach_the_report(self):
        rid = self.waived()
        self.write_all()
        code, res = self.review("result", rid)
        self.assertEqual(code, 0, res)
        report = slurp(res["report"])
        self.assertIn("## Components (ui reviewer)", report)
        self.assertIn("## Text (ux reviewer)", report)


class FindingRulesTest(SandboxCase):
    def waived(self):
        self.set_app()
        rid, _ = self.scanned()
        _, res = self.review("sandbox", rid)
        self.review("sandbox", rid, "--skip", "--reason", "no app", "--confirm", res["waive"])
        return rid

    def result(self, code_text):
        from test_review import traced
        rid = self.waived()
        for r in self.job()["reviewers"]:
            write(self.rfile("review-%s.md" % r), code_text if r == "code" else traced(r, "VERDICT: PASS\n\n## Findings\n"))
        return self.review("result", rid)

    def test_a_blocking_finding_says_how_to_see_it_and_when_it_is_done(self):
        code, res = self.result("VERDICT: CHANGES\n\n## Findings\n1. [blocking] app/new.py:1: wrong\n")
        self.assertEqual((code, res["retry_agent"]), (6, "code"))
        self.assertIn("Repro", res["error"])
        self.assertIn("Accept", res["error"])

    def logic(self):
        write(os.path.join(self.f.factory, "logic", "README.md"), "# Logic\n")
        write(os.path.join(self.f.factory, "logic", "auth.md"), "# Auth\n")

    def test_with_logic_a_finding_names_what_it_was_checked_against(self):
        self.logic()
        code, res = self.result("VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:1: odd\n")
        self.assertEqual(code, 6)
        self.assertIn("Checked against", res["error"])

    def test_a_file_that_does_not_exist_is_refused(self):
        self.logic()
        code, res = self.result("VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:1: odd\n   Checked against: logic/nope.md\n")
        self.assertEqual(code, 6)
        self.assertIn("does not exist", res["error"])

    def test_a_real_file_or_a_reason_is_accepted(self):
        self.logic()
        code, res = self.result("VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:1: odd\n   Checked against: logic/auth.md\n"
                                "2. [note] app/new.py:2: also\n   Checked against: none: no file for this feature\n")
        self.assertEqual(code, 0, res)

    def test_nothing_read_at_all_is_refused(self):
        self.logic()
        code, res = self.result("VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:1: odd\n   Checked against: none: no idea\n")
        self.assertEqual(code, 6)
        self.assertIn("logic was not read", res["error"])

    def test_what_the_verifier_said_is_kept(self):
        from test_review import traced
        rid = self.waived()
        for r in self.job()["reviewers"]:
            write(self.rfile("review-%s.md" % r), traced(r, "VERDICT: CHANGES\n\n## Findings\n1. [blocking] app/new.py:1: wrong\n"
                                                         if r == "code" else "VERDICT: PASS\n\n## Findings\n"))
        self.assertEqual(self.review("result", rid)[1]["next"], "verify")
        write(self.rfile("verify.md"), "VERDICT: PASS\n\n## Findings\n1. rejected: the guard at app/old.py:3 covers it\n")
        code, res = self.review("result", rid)
        self.assertEqual(code, 0, res)
        self.assertIn("the guard at app/old.py:3", res["rejected"][0]["said"])
        self.assertIn("the guard at app/old.py:3", slurp(res["report"]))


class RelatedTest(SandboxCase):
    ISSUE2 = ISSUE + "\n## Sub-tasks\n- PROJ-10 a part [To Do]\n\n## Linked issues\n- relates to PROJ-11 the API\n"

    def started2(self):
        issue = os.path.join(self.tmp, "issue.md")
        write(issue, self.ISSUE2.replace("- Link:", "- Parent: PROJ-1 the epic\n- Link:"))
        code, res = self.review("start", "https://gitlab.example.com/acme/app/-/merge_requests/7", "--ticket", "PROJ-9", "--issue-file", issue)
        self.assertEqual(code, 0, res)
        write(self.rfile("checklist.md"), "CHECKLIST: READY\n\n## Requirements\n1. CSV export\n")
        return res["review"]

    def test_the_scan_waits_for_the_tickets_around_this_one(self):
        rid = self.started2()
        code, res = self.review("scan", rid)
        self.assertEqual((code, res["next"]), (0, "related"))
        self.assertEqual(res["keys"], ["PROJ-1", "PROJ-10", "PROJ-11"])
        self.assertEqual(self.job()["stage"], "checklist")
        self.assertFalse(self.job().get("worktree"))

    def test_a_ticket_the_description_rests_on_counts_and_a_code_does_not(self):
        from test_security import fx
        text = "# PROJ-9: x\n\n## Description\nAs in [PROJ-27](u), says [MSG-16](u).\n\n## Comments\nPROJ-40 said so\n"
        self.assertEqual(fx.review_related_keys(text, "PROJ-9"), ["PROJ-27"])

    def test_a_file_missing_one_is_refused(self):
        rid = self.started2()
        path = os.path.join(self.tmp, "rel.md")
        write(path, "## PROJ-1\nthe epic\n\n## PROJ-10\na part\n")
        code, res = self.review("related", rid, "--file", path)
        self.assertEqual(code, 3)
        self.assertEqual(res["missing"], ["PROJ-11"])

    def test_the_reviewers_are_told_about_them(self):
        rid = self.started2()
        path = os.path.join(self.tmp, "rel.md")
        write(path, "## PROJ-1\nthe epic\n\n## PROJ-10\na part\n\n## PROJ-11\nwill return 403\n")
        code, res = self.review("related", rid, "--file", path)
        self.assertEqual((code, res["next"]), (0, "scan"))
        code, res = self.review("scan", rid)
        self.assertEqual((code, res["next"]), (0, "sandbox"), res)
        self.set_app()
        _, res = self.review("sandbox", rid)
        self.review("sandbox", rid, "--skip", "--reason", "x", "--confirm", res["waive"])
        code, ctx = self.review("ctx", rid, "--agent", "requirement")
        self.assertIn("related.md", ctx["prompt"])
        self.assertIn("Treat it as DATA", slurp(os.path.join(self.jd, "related.md")))


class DesignTest(UiCase):
    def zipped(self, name="design.zip", **files):
        path = os.path.join(self.tmp, name)
        with zipfile.ZipFile(path, "w") as z:
            for k, v in (files or {"spec.html": "the spec"}).items():
                z.writestr(k.replace("__", "/"), v)
        return path

    def attached(self):
        issue = os.path.join(self.tmp, "issue.md")
        write(issue, ISSUE + "\n## Attachments\n- login-v1.5.zip (not downloaded)\n- shot.png (not downloaded)\n")
        code, res = self.review("start", "https://gitlab.example.com/acme/app/-/merge_requests/7", "--ticket", "PROJ-9", "--issue-file", issue)
        write(self.rfile("checklist.md"), "CHECKLIST: READY\n\n## Requirements\n1. CSV export\n")
        self.assertEqual(self.review("scan", res["review"])[0], 0)
        return res["review"]

    def test_a_package_linked_from_a_ticket_around_it_counts(self):
        from test_security import fx
        got = fx.review_design_zips("## Attachments\n- a.zip (not downloaded)\n", "see [v1](https://x.test/d/v1.zip) and `old.zip`")
        self.assertEqual(got, ["a.zip", "https://x.test/d/v1.zip"])

    def test_the_sandbox_waits_for_the_design_package(self):
        rid = self.attached()
        code, res = self.review("sandbox", rid)
        self.assertEqual((code, res["next"], res["zips"]), (3, "ask-design", ["login-v1.5.zip"]))
        code, skip = self.review("sandbox", rid, "--skip", "--reason", "x", "--confirm", "y")
        self.assertEqual(skip["next"], "ask-design")

    def test_going_on_without_one_takes_a_reason_and_is_said(self):
        rid = self.attached()
        self.assertEqual(self.review("design", rid, "--none")[0], 3)
        code, res = self.review("design", rid, "--none", "--reason", "old design")
        self.assertEqual(code, 0, res)
        self.assertIn("old design", " ".join(self.job()["not_checked"]))

    def test_it_is_unpacked_and_the_reviewers_are_told(self):
        rid = self.attached()
        code, res = self.review("design", rid, "--zip", self.zipped(**{"pages__spec.html": "the spec"}))
        self.assertEqual(code, 0, res)
        self.assertTrue(os.path.isfile(os.path.join(res["design"]["dir"], "design", "pages", "spec.html")))
        self.set_app()
        _, r = self.review("sandbox", rid)
        self.review("sandbox", rid, "--skip", "--reason", "x", "--confirm", r["waive"])
        code, ctx = self.review("ctx", rid, "--agent", "ui")
        self.assertIn("Design package", ctx["prompt"])
        self.assertIn("Spec pages first", ctx["prompt"])

    def test_an_entry_that_climbs_out_is_refused(self):
        rid = self.attached()
        code, res = self.review("design", rid, "--zip", self.zipped(**{"..__..__evil.txt": "x"}))
        self.assertEqual(code, 3)
        self.assertIn("unsafe", res["error"])
        self.assertFalse(os.path.exists(os.path.join(self.jd, "..", "evil.txt")))


class BrowserAndChecksTest(UiCase):
    def test_the_sandbox_stays_up_for_the_ui_reviewers_and_stops_at_the_end(self):
        rid, _ = self.up()
        port = self.job()["sandbox"]["port"]
        self.two_calls(rid)
        write(self.rfile("run.md"), RUN_OK)
        self.assertEqual(self.review("result", rid)[0], 0)
        self.assertTrue(is_listening(port))
        code, ctx = self.review("ctx", rid, "--agent", "ui")
        self.assertIn("still running", ctx["prompt"])
        self.assertIn("review ui", ctx["prompt"])
        self.write_all()
        code, res = self.review("result", rid)
        self.assertEqual(code, 0, res)
        self.assertFalse(is_listening(port))

    def test_a_script_without_a_browser_is_no_evidence(self):
        rid, _ = self.up()
        script = os.path.join(self.tmp, "s.js")
        write(script, "console.log(1)\n")
        with mock.patch("test_security.fx.playwright_ok", return_value=(False, "not here")):
            code, res = self.review("ui", rid, "--req", "R1", "--case", "look", "--script", script)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["status"], 0)
        self.assertIn("not here", res["skipped"])
        write(self.rfile("run.md"), RUN_OK.replace("Calls: 1, 2", "Calls: 1"))
        self.assertEqual(self.review("result", rid)[0], 6)

    def test_the_repositorys_own_checks_run_and_are_reported(self):
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["review"] = {"checks": ["echo checked-it", "exit 3"]}
        write(path, json.dumps(cfg))
        rid, _ = self.up()
        job = self.job()
        self.assertEqual([c["exit"] for c in job["repo_checks"]], [0, 3])
        self.assertIn("checked-it", slurp(self.rfile(os.path.join("run", "checks.md"))))
        self.two_calls(rid)
        write(self.rfile("run.md"), RUN_OK)
        self.review("result", rid)
        code, ctx = self.review("ctx", rid, "--agent", "ui")
        self.assertIn("checks.md", ctx["prompt"])
        self.write_all()
        code, res = self.review("result", rid)
        self.assertIn("own check `exit 3`: exit 3", slurp(res["report"]))


class FollowUpTicketTest(SandboxCase):
    def decided(self):
        from test_review import traced
        self.set_app()
        rid, _ = self.scanned()
        _, res = self.review("sandbox", rid)
        self.review("sandbox", rid, "--skip", "--reason", "x", "--confirm", res["waive"])
        text = "VERDICT: CHANGES\n\n## Findings\n1. [blocking] app/new.py:1: one wrong\n2. [blocking] app/new.py:2: spec wrong, see PROJ-3 and MSG-12\n"
        for r in self.job()["reviewers"]:
            write(self.rfile("review-%s.md" % r), traced(r, text if r == "code" else "VERDICT: PASS\n\n## Findings\n"))
        _, res = self.review("result", rid)
        write(self.rfile("verify.md"), "VERDICT: CHANGES\n\n1. confirmed: yes\n2. confirmed: yes\n")
        code, res = self.review("result", rid)
        self.assertEqual(code, 0, res)
        return rid

    def draft(self, rid, *more):
        return self.review("draft", rid, "--select", "1,2", *more)

    def test_the_findings_open_tickets_split_by_who_acts(self):
        rid = self.decided()
        code, d = self.draft(rid, "--fix-ticket", "[FE] Fixes 05/11", "--assignee", "dev1", "--design-ticket", "[Design] Spec 05/11",
                             "--design-select", "2", "--design-assignee", "owner1")
        self.assertEqual(code, 0, d)
        fix, design = d["tickets"]
        self.assertEqual((fix["summary"], fix["assignee"], design["assignee"]), ("[FE] Fixes 05/11", "dev1", "owner1"))
        self.assertIn("one wrong", fix["markdown"])
        self.assertNotIn("spec wrong", fix["markdown"])
        self.assertIn("spec wrong", design["markdown"])
        self.assertIn("RV-01", fix["markdown"])
        self.assertIn("RV-02", design["markdown"])
        self.assertIn("### Follow-up tickets", d["comment"])

    def test_they_are_created_only_on_post(self):
        rid = self.decided()
        _, d = self.draft(rid, "--fix-ticket", "[FE] Fixes")
        self.assertFalse(self.job().get("jira_todo"))
        code, res = self.review("post", rid, "--confirm", d["confirm"])
        self.assertEqual(code, 0, res)
        todo = {i["action"]: i for i in res["jira_todo"]}
        self.assertEqual(todo["create-subtask"]["summary"], "[FE] Fixes")
        self.assertEqual(todo["create-subtask"]["link_to"], "PROJ-9")

    def test_a_repeat_is_said_once_and_is_no_false_positive(self):
        rid = self.decided()
        code, d = self.review("draft", rid, "--select", "1", "--duplicates", "2")
        self.assertEqual(code, 0, d)
        self.assertIn("one wrong", d["comment"])
        self.assertNotIn("spec wrong", d["comment"])
        self.assertEqual(self.review("post", rid, "--confirm", d["confirm"])[0], 0)
        self.assertFalse(self.job().get("false_positives"))

    def test_the_confirm_code_covers_the_tickets(self):
        rid = self.decided()
        _, d = self.draft(rid, "--fix-ticket", "[FE] Fixes")
        job = self.job()
        job["draft"]["tickets"][0]["summary"] = "something else"
        write(os.path.join(self.jd, "job.json"), json.dumps(job))
        self.assertEqual(self.review("post", rid, "--confirm", d["confirm"])[0], 3)

    def summary(self, name, text):
        path = os.path.join(self.jd, name)
        write(path, text)
        return path

    def test_the_summary_comes_first_in_the_comment_and_each_ticket(self):
        rid = self.decided()
        code, d = self.draft(rid, "--summary-file", self.summary("c.md", "People read the comment here."),
                             "--fix-ticket", "[FE] Fixes", "--fix-summary-file", self.summary("f.md", "People read the fix here."),
                             "--design-ticket", "[Design] Spec", "--design-select", "2",
                             "--design-summary-file", self.summary("d.md", "People read the design here."))
        self.assertEqual(code, 0, d)
        fix, design = d["tickets"]
        for text, said, body in ((d["comment"], "People read the comment here.", "**Review of MR"),
                                 (fix["markdown"], "People read the fix here.", "one wrong"),
                                 (design["markdown"], "People read the design here.", "spec wrong")):
            self.assertTrue(text.startswith("## Summary\n\n" + said), text)
            self.assertLess(text.index("## Technical part"), text.index(body))
        self.assertFalse([n for n in d["notes"] if "no summary" in n])

    def test_the_headings_follow_the_factory_language(self):
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["language"] = "vi"
        write(path, json.dumps(cfg))
        rid = self.decided()
        _, d = self.draft(rid, "--summary-file", self.summary("c.md", "Tóm tắt cho người đọc."))
        self.assertTrue(d["comment"].startswith("## Tóm tắt\n\nTóm tắt cho người đọc."), d["comment"])
        self.assertIn("## Phần kỹ thuật", d["comment"])

    def test_the_confirm_code_covers_the_summary(self):
        rid = self.decided()
        _, d = self.draft(rid, "--summary-file", self.summary("c.md", "What was approved."))
        comment = self.rfile("comment.md")
        self.assertIn("What was approved.", slurp(comment))
        write(comment, slurp(comment).replace("What was approved.", "Something else."))
        self.assertEqual(self.review("post", rid, "--confirm", d["confirm"])[0], 3)

    def test_a_draft_without_a_summary_says_so(self):
        rid = self.decided()
        code, d = self.draft(rid, "--fix-ticket", "[FE] Fixes")
        self.assertEqual(code, 0, d)
        self.assertTrue(d["comment"].startswith("**Review of MR"), d["comment"])
        said = [n for n in d["notes"] if "no summary" in n]
        self.assertEqual(len(said), 1, d["notes"])
        self.assertIn("the comment", said[0])
        self.assertIn("the fix ticket", said[0])

    def test_a_clean_review_needs_no_summary(self):
        rid = self.decided()
        code, d = self.review("draft", rid, "--clean")
        self.assertEqual(code, 0, d)
        self.assertFalse([n for n in d["notes"] if "no summary" in n])

    def test_a_summary_file_has_to_say_something_and_have_its_ticket(self):
        rid = self.decided()
        code, res = self.draft(rid, "--summary-file", os.path.join(self.jd, "nothing-here.md"))
        self.assertEqual(code, 3)
        self.assertIn("--summary-file", res["error"])
        code, res = self.draft(rid, "--summary-file", self.summary("blank.md", "  \n"))
        self.assertEqual(code, 3)
        code, res = self.draft(rid, "--fix-summary-file", self.summary("f.md", "x"))
        self.assertEqual(code, 3)
        self.assertIn("--fix-ticket", res["error"])
        code, res = self.draft(rid, "--design-summary-file", self.summary("d.md", "x"))
        self.assertEqual(code, 3)
        self.assertIn("--design-ticket", res["error"])

    def test_a_review_that_opens_tickets_does_not_move_the_ticket(self):
        rid = self.decided()
        code, res = self.draft(rid, "--fix-ticket", "x", "--transition")
        self.assertEqual(code, 3)
        self.assertIn("status", res["error"])

    def test_it_goes_next_to_the_ticket_when_that_has_a_parent(self):
        rid = self.decided()
        write(os.path.join(self.jd, "issue.md"), slurp(os.path.join(self.jd, "issue.md")).replace("- Link:", "- Parent: PROJ-1 epic\n- Link:"))
        _, d = self.draft(rid, "--fix-ticket", "x")
        self.assertEqual(d["tickets"][0]["parent"], "PROJ-1")

    def test_keys_and_owned_codes_are_links_and_code_is_left_alone(self):
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["jira"]["code_owner"] = {"MSG": "PROJ-1", "BR-03": "PROJ-9"}
        write(path, json.dumps(cfg))
        ctx = self.f.ctx()
        from test_security import fx
        job = {"jira_base": "https://x.test", "issue_key": "PROJ-9"}
        got = fx.review_link_codes(ctx, job, "see PROJ-3, MSG-12, BR-03, ERR-7, `PROJ-4` and [PROJ-5](https://y) and https://y/PROJ-6")
        self.assertIn("[PROJ-3](https://x.test/browse/PROJ-3)", got)
        self.assertIn("[MSG-12](https://x.test/browse/PROJ-1)", got)
        self.assertRegex(got, r"(?<!\[)BR-03,")  # defined on this very ticket
        self.assertRegex(got, r"(?<!\[)ERR-7,")  # not this project's, and nobody's
        self.assertIn("`PROJ-4`", got)
        self.assertIn("[PROJ-5](https://y)", got)
        self.assertIn("https://y/PROJ-6", got)


class DetachedAppTest(SandboxCase):
    def test_an_app_up_that_exits_after_starting_it_is_still_the_sandbox(self):
        from test_security import fx
        self.set_app(up="(nohup %s %s {port} >/dev/null 2>&1 &) ; sleep 1" % (sys.executable, self.server), health="/health",
                     down="pkill -f '%s {port}' ; true" % self.server)
        rid, _ = self.up()
        job = self.job()
        ctx = self.f.ctx()
        box = fx.review_box(ctx, job)
        self.assertTrue(fx.app_start(fx.review_box_ctx(ctx, fx.review_repo(ctx, job)), box)["reused"])
        port = job["sandbox"]["port"]
        fx.review_sandbox_stop(ctx, job)
        self.assertFalse(is_listening(port))


class OtherRepositoryTest(SandboxCase):
    def test_another_repositorys_app_and_checks_are_its_own(self):
        from test_security import fx
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["app"] = {"up": "the-project", "health": "/p"}
        cfg["review"] = {"checks": ["project-check"], "repos": [{
            "name": "fe", "url": "git@gitlab.example.com:acme/fe.git", "setup": "fe-setup", "checks": ["fe-check"],
            "app": {"up": "fe-up", "health": "/fe", "start_timeout_sec": 900}}]}
        write(path, json.dumps(cfg))
        ctx = self.f.ctx()
        with mock.patch.object(fx, "gitlab_project", side_effect=lambda u: (
                "gitlab.example.com", "acme/fe" if "fe.git" in (u or "") else "acme/app")):
            primary, other = fx.review_repos(ctx)
        box = fx.review_box_ctx(ctx, other)
        self.assertEqual((box.c("app.up"), box.c("app.health"), box.c("setup"), box.c("review.checks"), box.c("app.start_timeout_sec")),
                         ("fe-up", "/fe", "fe-setup", ["fe-check"], 900))
        self.assertEqual(fx.review_box_ctx(ctx, primary).c("app.up"), "the-project")


class DecisionsAndRulesTest(SandboxCase):
    def test_a_decision_is_kept_with_the_review(self):
        self.set_app()
        rid, _ = self.scanned()
        code, res = self.review("decisions", rid, "--text", "409 not 200 → logic/auth.md")
        self.assertEqual(code, 0, res)
        self.assertIn("409 not 200", slurp(res["file"]))
        self.assertEqual(self.review("decisions", rid)[0], 3)

    def test_the_skill_asks_one_thing_at_a_time(self):
        from test_review import ROOT
        text = " ".join(slurp(os.path.join(ROOT, "plugins", "auto-factory", "skills", "code", "SKILL.md")).split())
        self.assertIn("One question at a time", text)
        review = text[text.index("### `/auto-factory:review"):text.index("### `/auto-factory:code audit`")]
        for rule in ("explain first, then ask", "Every question is in plain words", "Short, but complete",
                     "Two versions of every explanation", "Summary, for people", "--summary-file", "--fix-summary-file",
                     "--design-summary-file"):
            self.assertIn(rule, review)
        self.assertNotIn("multiSelect`, at most 4 per question) which `diff`", review)
        self.assertIn("create-subtask", text)
