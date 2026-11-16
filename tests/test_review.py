"""Tests for /auto-factory:review, a colleague's merge request against its Jira ticket (standard library only). Run from the
repository root:
    python3 -I -m unittest discover -s tests

The project and its merge request are real: a bare origin holding refs/merge-requests/7/head, so conflicts come from a
real merge. Only glab and the scanners are stubs: the glab stub answers from a table and records every call, so "GitLab
is only read" is checked on what would have reached it.
"""

import json
import os
import re
import shutil
import stat
import sys
from unittest import mock

from test_security import FxTestCase, ROOT, fx, load_json, sh, slurp, write

URL = "https://gitlab.example.com/acme/app/-/merge_requests/7"
GLAB_STUB = r'''#!%(python)s -I
import json, os, sys
args = sys.argv[1:]
with open(os.environ["GLAB_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
with open(os.environ["GLAB_ROUTES"]) as f:
    routes = json.load(f)
for pattern in sorted(routes, key=len, reverse=True):
    if pattern == "^" + args[-1] or (not pattern.startswith("^") and pattern in args[-1]):  # ^ is the whole path
        print(json.dumps(routes[pattern]))
        sys.exit(0)
print("404 not found", file=sys.stderr)
sys.exit(1)
'''
ISSUE = "# PROJ-9: Export invoices\n\n- Link: https://example.atlassian.net/browse/PROJ-9\n\n## Description\nCSV export.\n"
AGENTS = os.path.join(ROOT, "plugins", "auto-factory", "agents")
TRACE = ("\n## Logic traced\n### R1: CSV export\n- State: implemented\n- Path: app/new.py:1 calls app/old.py:2\n"
         "- Rules: one condition\n- Cases: normal: 3 rows; empty: header only\n")


TABLES = {"ui": "\n## Components\n| Element | Spec | Code | Props | Overrides | Result |\n|---|---|---|---|---|---|\n"
                "| Button | C2PButton | src/page.vue:1 C2PButton | size | none | ok |\n",
          "ux": "\n## Text\n| String | Source | Shown | Match |\n|---|---|---|---|\n| Save | ticket | src/page.vue:1 | yes |\n"}


def traced(reviewer, text):
    """What a reviewer's file must hold besides its own findings, so that each test can say only what it is about."""
    if reviewer != "security":
        text = re.sub(r"(?m)^(\s*\d+\.\s*\[blocking\].*)$", r"\1 Repro: call 1. Accept: it works.%s" % (
            " Type: code." if reviewer in TABLES else ""), text)
    if reviewer == "requirement" and "## Logic traced" not in text:
        text += TRACE
    if reviewer in TABLES and TABLES[reviewer].split("\n")[1] not in text:
        text += TABLES[reviewer]
    return text


class ReviewCase(FxTestCase):
    cfg = {"pr": {"provider": "gitlab"}, "app": {}, "setup": "",
           "jira": {"base_url": "https://example.atlassian.net",
                    "transitions": {"review_changes": "Doing", "conflict": "Fix again"}}}
    base_files = {"README.md": "project\n", "app/old.py": "".join("line %d\n" % i for i in range(1, 41))}

    def setUp(self):
        super().setUp()
        glab = os.path.join(self.f.bin, "glab")
        write(glab, GLAB_STUB % {"python": sys.executable})
        os.chmod(glab, os.stat(glab).st_mode | stat.S_IEXEC)
        self.glab_log = os.path.join(self.tmp, "glab.log")
        self.routes_file = os.path.join(self.tmp, "routes.json")
        p = mock.patch.dict(os.environ, {"GLAB_LOG": self.glab_log, "GLAB_ROUTES": self.routes_file})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(fx, "gitlab_project", return_value=("gitlab.example.com", "acme/app"))
        p.start()
        self.addCleanup(p.stop)
        self.head = self.push_mr({"app/new.py": "".join("new %d\n" % i for i in range(1, 11))})
        self.routes = {}
        self.set_mr()

    def push_mr(self, files, msg="work"):
        self.f.commit(files, msg)
        sh(["git", "push", "-q", "-f", "origin", "HEAD:refs/merge-requests/7/head"], self.f.wt)
        return sh(["git", "rev-parse", "HEAD"], self.f.wt).strip()

    def mr(self, **changes):
        d = {"iid": 7, "web_url": URL, "title": "PROJ-9: Export invoices", "source_branch": "feature/PROJ-9",
             "target_branch": "main", "sha": self.head, "state": "opened", "detailed_merge_status": "mergeable",
             "has_conflicts": False, "author": {"username": "dev"}, "description": "Adds the export.", "draft": False,
             "head_pipeline": {"id": 55, "status": "success", "web_url": "https://ci/55"}}
        d.update(changes)
        return d

    def set_mr(self, **changes):
        self.routes["merge_requests/7"] = self.mr(**changes)
        self.routes["merge_requests/7/notes"] = [
            {"author": {"username": "lead"}, "body": "Mind the header row.", "system": False, "resolved": True,
             "position": {"new_path": "app/new.py", "new_line": 2}}]
        self.save_routes()

    def save_routes(self):
        write(self.routes_file, json.dumps(self.routes))

    def glab_calls(self):
        if not os.path.exists(self.glab_log):
            return []
        with open(self.glab_log) as f:
            return [json.loads(line) for line in f if line.strip()]

    def review(self, action, *args):
        return self.fx("review", action, *args)

    @property
    def jd(self):
        return os.path.join(self.f.factory, "jobs", "_reviews", "PROJ-9-mr7")

    def job(self):
        return load_json(os.path.join(self.jd, "job.json"))

    def rfile(self, name, n=1):
        return os.path.join(self.jd, "round-%d" % n, name)

    def started(self):
        issue = os.path.join(self.tmp, "issue.md")
        write(issue, ISSUE)
        code, res = self.review("start", URL, "--ticket", "PROJ-9", "--issue-file", issue)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "checklist")
        return res["review"]

    def scanned(self, n=1):
        rid = self.started()
        write(self.rfile("checklist.md", n), "CHECKLIST: READY\n\n## Requirements\n1. CSV export\n")
        code, res = self.review("scan", rid)
        self.assertEqual(code, 0, res)
        self.no_sandbox(rid)
        return rid, res

    def no_sandbox(self, rid):
        """These fixtures have no app.up: fx stops, and the person reviewing goes on. SandboxTest runs a real one."""
        code, res = self.review("sandbox", rid)
        self.assertEqual(code, 3, res)
        self.assertEqual(res["next"], "ask-no-sandbox")
        code, res = self.review("sandbox", rid, "--skip", "--reason", "no app here", "--confirm", res["waive"])
        self.assertEqual(code, 0, res)

    def reviews(self, n=1, **texts):
        for r in self.job()["reviewers"]:
            write(self.rfile("review-%s.md" % r, n), traced(r, texts.get(r, "VERDICT: PASS\n\n## Findings\n")))

    def decided(self, n=1, **texts):
        """Reach the point of choosing. Blocking findings all stand: the verifier confirms each one."""
        rid, _ = self.scanned(n)
        self.reviews(n, **texts)
        code, res = self.review("result", rid)
        self.assertEqual(code, 0, res)
        if res["next"] == "verify":
            write(self.rfile("verify.md", n), "VERDICT: CHANGES\n\n## Findings\n" + "".join(
                "%d. confirmed: it stands\n" % i for i in range(1, res["blocking"] + 1)))
            code, res = self.review("result", rid)
            self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "decide")
        return rid, res

    def clones(self):
        work = os.path.join(self.tmp, "proj-factory-work")
        return [d for d in os.listdir(work) if d.startswith("_review")]


class StartTest(ReviewCase):
    def test_the_ticket_comes_from_the_title_or_branch(self):
        code, res = self.review("start", URL)
        self.assertEqual(code, 0, res)
        self.assertEqual((res["review"], res["next"]), ("_reviews/PROJ-9-mr7", "fetch"))
        self.assertEqual(res["issue_key"], "PROJ-9")
        self.assertIn("--issue-file", res["then"])

    def test_no_ticket_named_is_asked(self):
        self.set_mr(title="Export", source_branch="export", description="")
        code, res = self.review("start", URL)
        self.assertEqual((code, res["next"], res["candidates"]), (0, "ask-ticket", []))
        self.assertFalse(os.path.exists(self.jd))

    def test_two_tickets_named_are_asked(self):
        self.set_mr(title="PROJ-9 and PROJ-10")
        code, res = self.review("start", URL)
        self.assertEqual((res["next"], res["candidates"]), ("ask-ticket", ["PROJ-9", "PROJ-10"]))

    def test_a_key_finds_its_open_merge_request(self):
        self.routes["merge_requests?state=opened"] = [self.mr(), self.mr(iid=8, title="PROJ-1000 other", source_branch="o")]
        self.save_routes()
        code, res = self.review("start", "PROJ-9")
        self.assertEqual((code, res["review"]), (0, "_reviews/PROJ-9-mr7"))

    def test_a_key_with_two_merge_requests_is_asked(self):
        self.routes["merge_requests?state=opened"] = [self.mr(), self.mr(iid=8)]
        self.save_routes()
        code, res = self.review("start", "PROJ-9")
        self.assertEqual((res["next"], len(res["candidates"])), ("ask-mrs", 2))

    def test_a_key_without_a_merge_request_stops(self):
        self.routes["merge_requests?state=opened"] = [self.mr(title="PROJ-1", source_branch="x", description="")]
        self.save_routes()
        code, res = self.review("start", "PROJ-9")
        self.assertEqual(code, 3)
        self.assertIn("no open or merged merge request", res["error"])

    def test_another_projects_merge_request_is_refused(self):
        code, res = self.review("start", "https://gitlab.example.com/other/app/-/merge_requests/7")
        self.assertEqual(code, 3)
        self.assertIn("factory repository", res["error"])

    def test_a_closed_merge_request_is_refused(self):
        self.set_mr(state="closed")
        code, res = self.review("start", URL)
        self.assertEqual(code, 3)
        self.assertIn("closed", res["error"])

    def test_github_is_refused(self):
        self.f.ctx()
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["pr"] = {"provider": "github"}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        code, res = self.review("start", URL)
        self.assertEqual(code, 3)
        self.assertIn("github", res["error"])

    def test_a_running_review_says_where_it_is(self):
        rid = self.started()
        code, res = self.review("start", URL, "--ticket", "PROJ-9")
        self.assertEqual((code, res["review"], res["stage"], res["next"]), (0, rid, "checklist", "checklist"))

    def test_list_asks_gitlab_for_what_waits_for_me(self):
        self.routes["user"] = {"username": "me"}
        self.routes["reviewer_username=me"] = [self.mr()]
        self.save_routes()
        code, res = self.review("list")
        self.assertEqual((code, res["reviewer"]), (0, "me"))
        self.assertEqual(res["merge_requests"][0]["tickets"], ["PROJ-9"])

    def test_without_glab_it_says_how_to_get_it(self):
        os.remove(os.path.join(self.f.bin, "glab"))
        code, res = self.review("start", URL)
        self.assertEqual(code, 3)
        self.assertIn("/auto-factory:code install", res["error"])


class MergeConflictTest(ReviewCase):
    def conflict_on_main(self):
        """A change on the target branch that really conflicts with the merge request."""
        other = os.path.join(self.tmp, "other")
        sh(["git", "clone", "-q", self.f.origin, other], self.tmp)
        write(os.path.join(other, "app", "new.py"), "something else\n")
        sh(["git", "add", "-A"], other)
        sh(["git", "commit", "-q", "-m", "on main"], other)
        sh(["git", "push", "-q", "origin", "HEAD:main"], other)

    def test_a_conflict_stops_before_anything_runs(self):
        self.conflict_on_main()
        self.set_mr(detailed_merge_status="conflict", has_conflicts=True)
        code, res = self.review("start", URL)
        self.assertEqual((code, res["stage"], res["next"]), (0, "conflict", "ask-conflict"))
        self.assertEqual(res["conflict_files"], ["app/new.py"])
        self.assertEqual(res["transition_to"], "Fix again")
        self.assertEqual(self.clones(), [])
        self.assertFalse(self.job().get("jira_todo"))
        self.assertFalse(os.path.exists(self.rfile("checklist.md")))

    def test_no_review_can_follow_a_conflict(self):
        self.set_mr(detailed_merge_status="conflict", has_conflicts=True)
        code, res = self.review("start", URL)
        rid = res["review"]
        code, res = self.review("ctx", rid, "--agent", "checklist")
        self.assertEqual(code, 1)
        code, res = self.review("scan", rid)
        self.assertEqual(code, 1)

    def test_while_gitlab_is_working_a_real_merge_decides(self):
        self.conflict_on_main()
        self.set_mr(detailed_merge_status="checking", has_conflicts=False)
        code, res = self.review("start", URL)
        self.assertEqual((res["stage"], res["conflict_files"]), ("conflict", ["app/new.py"]))

    def test_an_old_answer_from_gitlab_does_not_stop_a_clean_merge(self):
        self.set_mr(detailed_merge_status="checking", has_conflicts=True)
        code, res = self.review("start", URL)
        self.assertEqual((res["merge_state"], res["next"]), ("clean", "fetch"))

    def test_when_nobody_can_tell_it_goes_on_and_says_so(self):
        self.set_mr(iid=8, web_url=URL, detailed_merge_status="unchecked")
        code, res = self.review("start", URL)
        self.assertEqual((res["merge_state"], res["next"]), ("unknown", "fetch"))
        self.assertIn("conflict", res["note"])

    def test_the_ticket_moves_only_after_the_reviewer_says_yes(self):
        self.conflict_on_main()
        self.set_mr(detailed_merge_status="conflict", has_conflicts=True)
        _, res = self.review("start", URL)
        rid = res["review"]
        code, res = self.review("draft", rid, "--transition", "--assignee", "acc-1")
        self.assertEqual((code, res["transition_to"], res["next"]), (0, "Fix again", "confirm"))
        self.assertIn("app/new.py", res["comment"])
        self.assertFalse(self.job().get("jira_todo"))
        code, res = self.review("post", rid, "--confirm", res["confirm"])
        self.assertEqual(code, 0, res)
        got = {i["action"]: i for i in res["jira_todo"]}
        self.assertEqual(got["transition"]["to"], "Fix again")
        self.assertEqual(got["assign"]["account_id"], "acc-1")
        self.assertIn("merge conflict", got["comment"]["markdown"])

    def test_a_comment_without_moving_the_ticket(self):
        self.set_mr(detailed_merge_status="conflict", has_conflicts=True)
        _, res = self.review("start", URL)
        code, res2 = self.review("draft", res["review"])
        self.assertIsNone(res2["transition_to"])
        _, res3 = self.review("post", res["review"], "--confirm", res2["confirm"])
        self.assertEqual([i["action"] for i in res3["jira_todo"]], ["comment"])

    def test_closing_writes_nothing(self):
        self.set_mr(detailed_merge_status="conflict", has_conflicts=True)
        _, res = self.review("start", URL)
        code, res = self.review("close", res["review"])
        self.assertEqual((code, res["posted"]), (0, False))
        self.assertFalse(self.job().get("jira_todo"))

    def test_a_later_round_of_a_fixed_conflict_is_a_normal_review(self):
        self.set_mr(detailed_merge_status="conflict", has_conflicts=True)
        _, res = self.review("start", URL)
        self.review("close", res["review"])
        self.set_mr()
        issue = os.path.join(self.tmp, "issue.md")
        write(issue, ISSUE)
        code, res = self.review("start", URL, "--issue-file", issue)
        self.assertEqual((code, res["round"], res["next"]), (0, 2, "checklist"))


class ChecklistFirstTest(ReviewCase):
    def test_the_code_is_not_cloned_before_the_checklist(self):
        rid = self.started()
        self.assertEqual(self.clones(), [])
        code, res = self.review("scan", rid)
        self.assertEqual((code, res["retry_agent"]), (6, "checklist"))
        self.assertEqual(self.clones(), [])

    def test_the_checklist_agent_is_not_told_where_the_code_is(self):
        rid = self.started()
        code, res = self.review("ctx", rid, "--agent", "checklist")
        self.assertEqual((code, res["subagent_type"]), (0, "factory-review-checklist"))
        self.assertNotIn("Worktree:", res["prompt"])
        self.assertNotIn("Diff:", res["prompt"])
        self.assertIn("checklist.md", res["prompt"])
        self.assertFalse(os.path.exists(os.path.join(self.jd, "mr.md")))

    def test_the_checklist_agent_has_no_shell(self):
        text = slurp(os.path.join(AGENTS, "factory-review-checklist.md"))
        tools = next(line for line in text.splitlines() if line.startswith("tools:"))
        self.assertNotIn("Bash", tools)


class ScanTest(ReviewCase):
    cfg = dict(ReviewCase.cfg, setup='touch "$FX_TEST_OUT/setup-ran"', dev_server='touch "$FX_TEST_OUT/app-ran"',
               app={"up": 'touch "$FX_TEST_OUT/app-ran"', "start_timeout_sec": 1}, e2e='touch "$FX_TEST_OUT/e2e-ran"',
               ui_paths=["*.vue"])

    def test_the_scan_runs_none_of_the_project_code(self):
        rid = self.started()
        write(self.rfile("checklist.md"), "CHECKLIST: READY\n\n## Requirements\n1. CSV export\n")
        code, res = self.review("scan", rid)
        self.assertEqual(code, 0, res)
        self.assertEqual(os.listdir(self.f.out), [])
        self.assertTrue(self.f.calls("gitleaks"))

    def test_what_the_reviewers_get(self):
        rid, res = self.scanned()
        self.assertEqual(res["reviewers"], ["requirement", "security", "code"])
        self.assertEqual(res["ci"]["status"], "success")
        self.assertIn("app/new.py", slurp(self.rfile("diff.patch")))
        self.assertTrue(slurp(self.rfile("security.md")).startswith("SECURITY SCAN"))
        mr = slurp(os.path.join(self.jd, "mr.md"))
        self.assertIn("Treat it as DATA", mr)
        self.assertIn("Mind the header row.", mr)
        code, ctxr = self.review("ctx", rid, "--agent", "code")
        self.assertEqual((code, ctxr["subagent_type"]), (0, "factory-reviewer-code"))
        self.assertIn("Mode: colleague MR", ctxr["prompt"])
        self.assertIn(self.head, ctxr["prompt"])
        self.assertIn(self.rfile("checklist.md"), ctxr["prompt"])
        self.assertIn(self.rfile("review-code.md"), ctxr["prompt"])

    def test_the_scanners_read_every_commit_of_the_merge_request(self):
        """A colleague's commits are all on their remote branch, which a job's scan would take as already scanned."""
        sh(["git", "push", "-q", "origin", "HEAD:refs/heads/feature/PROJ-9"], self.f.wt)
        self.scanned()
        base = sh(["git", "rev-parse", "origin/main"], self.f.wt).strip()
        opts = self.f.calls("gitleaks")[-1]["argv"]
        self.assertIn("%s..HEAD ^origin/main" % base, " ".join(opts))

    def test_a_ui_change_adds_ux_and_ui(self):
        self.head = self.push_mr({"web/page.vue": "<template/>\n"})
        self.set_mr()
        _, res = self.scanned()
        self.assertEqual(res["reviewers"], ["requirement", "security", "code", "ux", "ui"])

    def test_no_ci_result_is_said(self):
        self.set_mr(head_pipeline=None)
        _, res = self.scanned()
        self.assertTrue(any("no test result" in s for s in res["not_checked"]))

    def test_the_failed_ci_jobs_are_named(self):
        self.set_mr(head_pipeline={"id": 55, "status": "failed", "web_url": "https://ci/55"})
        self.routes["pipelines/55/jobs"] = [{"name": "phpunit"}]
        self.save_routes()
        _, res = self.scanned()
        self.assertEqual(res["ci"]["failed_jobs"], ["phpunit"])

    def test_a_scanner_that_did_not_run_is_said(self):
        with mock.patch.dict(os.environ, {"STUB_SEMGREP_OUT": "not json"}):
            _, res = self.scanned()
        self.assertTrue(any("semgrep" in s for s in res["not_checked"]))

    def test_a_merge_request_that_changes_nothing_stops(self):
        rid = self.started()
        write(self.rfile("checklist.md"), "CHECKLIST: READY\n")
        sh(["git", "push", "-q", "-f", "origin", "origin/main:refs/merge-requests/7/head"], self.f.wt)
        code, res = self.review("scan", rid)
        self.assertEqual(code, 3)
        self.assertEqual(self.clones(), [])


class ResultTest(ReviewCase):
    def test_it_waits_for_reviewers(self):
        rid, _ = self.scanned()
        code, res = self.review("result", rid)
        self.assertEqual((res["next"], res["missing"]), ("wait", ["requirement", "security", "code"]))

    def test_a_malformed_review_goes_back(self):
        rid, _ = self.scanned()
        self.reviews(code="Looks fine to me\n")
        code, res = self.review("result", rid)
        self.assertEqual((code, res["retry_agent"]), (6, "code"))

    def test_changes_without_a_numbered_finding_goes_back(self):
        rid, _ = self.scanned()
        self.reviews(code="VERDICT: CHANGES\n\nThis is bad.\n")
        code, res = self.review("result", rid)
        self.assertEqual((code, res["retry_agent"]), (6, "code"))

    def test_pass_with_a_blocking_finding_goes_back(self):
        rid, _ = self.scanned()
        self.reviews(code="VERDICT: PASS\n\n## Findings\n1. [blocking] app/new.py:2: bad\n")
        code, res = self.review("result", rid)
        self.assertEqual((code, res["retry_agent"]), (6, "code"))

    def test_the_verifier_sees_every_reviewers_blocking_findings(self):
        rid, _ = self.scanned()
        self.reviews(code="VERDICT: CHANGES\n\n## Findings\n1. [blocking] app/new.py:2: slow\n2. [note] app/new.py:3: nit\n",
                     requirement="VERDICT: CHANGES\n\n## Findings\n1. [blocking] no month filter\n")
        code, res = self.review("result", rid)
        self.assertEqual((res["next"], res["blocking"]), ("verify", 2))
        text = slurp(self.rfile("blocking.md"))
        self.assertIn("[requirement] no month filter", text)
        self.assertIn("[code] app/new.py:2: slow", text)
        self.assertNotIn("nit", text)
        code, ctxr = self.review("ctx", rid, "--agent", "verifier")
        self.assertEqual((code, ctxr["subagent_type"]), (0, "factory-security-verifier"))
        self.assertIn(self.rfile("verify.md"), ctxr["prompt"])

    def verified(self, answers):
        rid, _ = self.scanned()
        self.reviews(code="VERDICT: CHANGES\n\n## Findings\n1. [blocking] app/new.py:2: slow\n2. [note] app/new.py:3: nit\n",
                     requirement="VERDICT: CHANGES\n\n## Findings\n1. [blocking] no month filter\n")
        self.review("result", rid)
        write(self.rfile("verify.md"), answers)
        return rid

    def test_what_the_verifier_rejects_is_dropped(self):
        rid = self.verified("VERDICT: CHANGES\n\n1. rejected: the route checks it\n2. unsure: needs the schema\n")
        code, res = self.review("result", rid)
        self.assertEqual((code, res["next"], res["rejected_by_verifier"]), (0, "decide", 1))
        texts = {f["text"].split(" Repro:")[0]: f for f in res["findings"]}
        self.assertNotIn("no month filter", texts)
        self.assertEqual(texts["app/new.py:2: slow"]["verified"], "unsure")
        self.assertIn("Rejected by the verifier", slurp(res["report"]))

    def test_the_verifier_must_answer_every_finding(self):
        rid = self.verified("VERDICT: PASS\n\n1. rejected: fine\n")
        code, res = self.review("result", rid)
        self.assertEqual((code, res["retry_agent"]), (6, "verifier"))

    def test_a_bare_pass_cannot_clear_findings(self):
        rid = self.verified("VERDICT: PASS\n\n1. confirmed: real\n2. rejected: fine\n")
        code, res = self.review("result", rid)
        self.assertEqual((code, res["retry_agent"]), (6, "verifier"))

    def test_the_clone_goes_once_the_agents_are_done(self):
        self.decided()
        self.assertEqual(self.clones(), [])
        self.assertIsNone(self.job()["worktree"])


class GroupTest(ReviewCase):
    def test_each_finding_goes_by_where_it_points(self):
        _, res = self.decided(code="VERDICT: PASS\n\n## Findings\n"
                                   "1. [note] app/new.py:2: in the new file\n"
                                   "2. [note] app/old.py:30: in a file this does not touch\n"
                                   "3. [note] about the change as a whole\n"
                                   "4. [question] does month mean calendar month?\n")
        got = {f["text"].split(":")[-1].strip(): f["group"] for f in res["findings"]}
        self.assertEqual(got["in the new file"], "diff")
        self.assertEqual(got["in a file this does not touch"], "pre-existing")
        self.assertEqual(got["about the change as a whole"], "diff")
        self.assertEqual(got["does month mean calendar month?"], "question")

    def test_a_line_of_a_changed_file_outside_its_hunks_is_old(self):
        self.head = self.push_mr({"app/old.py": "changed\n" + "".join("line %d\n" % i for i in range(2, 41))})
        self.set_mr()
        _, res = self.decided(code="VERDICT: PASS\n\n## Findings\n"
                                   "1. [note] app/old.py:1: changed here\n2. [note] app/old.py:35: far away\n")
        got = [f["group"] for f in res["findings"]]
        self.assertEqual(got[:2], ["diff", "pre-existing"])

    def test_lesson_ids_stay_out_of_the_text(self):
        _, res = self.decided(code="VERDICT: CHANGES\n\n## Findings\n1. [blocking] [L12] app/new.py:2: breaks a lesson\n")
        self.assertTrue(res["findings"][0]["text"].startswith("app/new.py:2: breaks a lesson"))

    def test_diff_hunks_is_not_fooled_by_the_content(self):
        diff = ("diff --git a/a.sql b/a.sql\n--- a/a.sql\n+++ b/a.sql\n@@ -1,2 +1,3 @@\n"
                "--- a comment that went\n+++ looks like a header\n ctx\n"
                "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n"
                "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n")
        got = fx.diff_hunks(diff)
        self.assertEqual(got["a.sql"], [(1, 3)])
        self.assertEqual(got["new.py"], [(1, 1)])
        self.assertTrue(got["gone.py"][0][0] <= 500 <= got["gone.py"][0][1])
        self.assertEqual(sorted(got), ["a.sql", "gone.py", "new.py"])

    def test_review_location(self):
        self.assertEqual(fx.review_location("`app/x.php:12`: bad"), ("app/x.php", 12))
        self.assertEqual(fx.review_location("./app/x.py:3-9 bad"), ("app/x.py", 3))
        self.assertEqual(fx.review_location("A01:2025 in app/y.py:4"), ("app/y.py", 4))
        self.assertEqual(fx.review_location("see https://example.com:8080/a"), (None, None))
        self.assertEqual(fx.review_location("no place"), (None, None))


class DraftAndPostTest(ReviewCase):
    FINDINGS = ("VERDICT: CHANGES\n\n## Findings\n1. [blocking] app/new.py:2: slow\n2. [note] app/new.py:3: nit\n"
                "3. [note] app/old.py:30: old problem\n4. [question] which month?\n")

    def test_nothing_goes_out_without_a_choice(self):
        rid, _ = self.decided(code=self.FINDINGS)
        code, res = self.review("draft", rid)
        self.assertEqual(code, 1)
        self.assertIn("--select", res["error"])
        self.assertFalse(self.job().get("jira_todo"))

    def test_only_findings_in_the_diff_can_go_out(self):
        rid, res = self.decided(code=self.FINDINGS)
        ids = {f["group"]: f["id"] for f in res["findings"]}
        for group in ("pre-existing", "question"):
            code, res2 = self.review("draft", rid, "--select", str(ids[group]))
            self.assertEqual(code, 1)
            self.assertIn("does not go to Jira", res2["error"])
        code, res2 = self.review("draft", rid, "--select", "99")
        self.assertEqual(code, 1)

    def test_drafting_writes_nothing_to_jira(self):
        rid, _ = self.decided(code=self.FINDINGS)
        code, res = self.review("draft", rid, "--select", "1", "--transition", "--assignee", "acc-1")
        self.assertEqual((code, res["transition_to"], res["assignee"]), (0, "Doing", "acc-1"))
        self.assertFalse(self.job().get("jira_todo"))

    def test_the_comment_points_at_the_reviewed_commit(self):
        rid, _ = self.decided(code=self.FINDINGS)
        _, res = self.review("draft", rid, "--select", "1")
        self.assertIn(self.head[:10], res["comment"])
        self.assertIn("slow", res["comment"])
        self.assertNotIn("nit", res["comment"])
        self.assertIn("https://gitlab.example.com/acme/app/-/blob/%s/app/new.py#L2" % self.head, res["comment"])

    def test_a_security_finding_goes_out_in_full(self):
        text = "app/new.py:2: SQL injection through the month parameter: send 1 OR 1=1"
        rid, _ = self.decided(security="VERDICT: CHANGES\n\n## Findings\n1. [blocking] %s\n" % text)
        _, res = self.review("draft", rid, "--select", "1")
        self.assertIn(text, res["comment"])

    def test_post_needs_the_code_of_the_draft(self):
        rid, _ = self.decided(code=self.FINDINGS)
        code, res = self.review("post", rid, "--confirm", "abc")
        self.assertEqual(code, 1)
        self.review("draft", rid, "--select", "1")
        for argv in (("post", rid), ("post", rid, "--confirm", "abc")):
            code, res = self.review(*argv)
            self.assertEqual(code, 3)
        self.assertFalse(self.job().get("jira_todo"))

    def test_post_refuses_a_draft_changed_after_the_reviewer_saw_it(self):
        rid, _ = self.decided(code=self.FINDINGS)
        _, res = self.review("draft", rid, "--select", "1")
        write(self.rfile("comment.md"), "something the reviewer never saw\n")
        code, res2 = self.review("post", rid, "--confirm", res["confirm"])
        self.assertEqual(code, 3)
        self.assertFalse(self.job().get("jira_todo"))

    def test_a_new_draft_replaces_the_old_code(self):
        rid, _ = self.decided(code=self.FINDINGS)
        _, first = self.review("draft", rid, "--select", "1")
        _, second = self.review("draft", rid, "--select", "1", "--assignee", "acc-1")
        code, _ = self.review("post", rid, "--confirm", first["confirm"])
        self.assertEqual(code, 3)
        code, res = self.review("post", rid, "--confirm", second["confirm"])
        self.assertEqual(code, 0)

    def test_post_queues_what_the_reviewer_agreed_to(self):
        rid, _ = self.decided(code=self.FINDINGS)
        _, d = self.review("draft", rid, "--select", "1", "--transition", "--assignee", "acc-1")
        code, res = self.review("post", rid, "--confirm", d["confirm"])
        self.assertEqual((code, res["next"]), (0, "done"))
        got = {i["action"]: i for i in res["jira_todo"]}
        self.assertEqual(sorted(got), ["assign", "comment", "transition"])
        self.assertEqual(got["transition"]["to"], "Doing")
        self.assertEqual(got["comment"]["issue"], "PROJ-9")
        self.assertEqual(self.job()["stage"], "done")

    def test_without_being_asked_to_it_does_not_move_the_ticket(self):
        rid, _ = self.decided(code=self.FINDINGS)
        _, d = self.review("draft", rid, "--select", "1")
        _, res = self.review("post", rid, "--confirm", d["confirm"])
        self.assertEqual([i["action"] for i in res["jira_todo"]], ["comment"])

    def test_a_status_not_set_yet_keeps_the_ticket_where_it_is(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["jira"]["transitions"]["review_changes"] = ""
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        rid, _ = self.decided(code=self.FINDINGS)
        _, d = self.review("draft", rid, "--select", "1", "--transition")
        self.assertIsNone(d["transition_to"])
        self.assertTrue(any("review_changes is empty" in n for n in d["notes"]))
        _, res = self.review("post", rid, "--confirm", d["confirm"])
        self.assertEqual([i["action"] for i in res["jira_todo"]], ["comment"])

    def test_a_clean_review_never_moves_the_ticket(self):
        rid, _ = self.decided()
        code, d = self.review("draft", rid, "--clean", "--transition")
        self.assertEqual((code, d["transition_to"]), (0, None))
        self.assertIn("No findings", d["comment"])
        _, res = self.review("post", rid, "--confirm", d["confirm"])
        self.assertEqual([i["action"] for i in res["jira_todo"]], ["comment"])

    def test_a_merge_request_that_moved_is_said(self):
        rid, _ = self.decided(code=self.FINDINGS)
        self.set_mr(sha="f" * 40)
        _, d = self.review("draft", rid, "--select", "1")
        self.assertTrue(any("not reviewed" in n for n in d["notes"]))

    def test_a_secret_in_a_finding_never_reaches_jira(self):
        rid, _ = self.decided(code="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: key AKIAIOSFODNN7EXAMPLE here\n")
        _, d = self.review("draft", rid, "--select", "1")
        _, res = self.review("post", rid, "--confirm", d["confirm"])
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", res["jira_todo"][0]["markdown"])

    def test_jira_done_works_on_a_review(self):
        rid, _ = self.decided(code=self.FINDINGS)
        _, d = self.review("draft", rid, "--select", "1")
        _, res = self.review("post", rid, "--confirm", d["confirm"])
        code, done = self.fx("jira-done", rid, res["jira_todo"][0]["id"])
        self.assertEqual((code, done["jira_todo"]), (0, []))

    def test_with_a_token_fx_does_it_itself(self):
        rid, _ = self.decided(code=self.FINDINGS)
        _, d = self.review("draft", rid, "--select", "1", "--transition", "--assignee", "acc-1")
        fake = mock.MagicMock()
        fake.return_value.issue.return_value = {"fields": {"status": {"name": "To review"}}}
        fake.return_value.transitions.return_value = [{"id": "21", "name": "In Progress", "to": {"name": "Doing"}}]
        with mock.patch.dict(os.environ, {"JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "t"}), \
                mock.patch.object(fx, "Jira", fake):
            code, res = self.review("post", rid, "--confirm", d["confirm"])
        self.assertEqual(code, 0, res)
        self.assertNotIn("jira_todo", res)
        fake.return_value.comment.assert_called_once()
        fake.return_value.transition.assert_called_once_with("PROJ-9", "21")
        fake.return_value.assign.assert_called_once_with("PROJ-9", "acc-1")

    def test_gitlab_is_only_read(self):
        rid, _ = self.decided(code=self.FINDINGS)
        _, d = self.review("draft", rid, "--select", "1", "--transition")
        self.review("post", rid, "--confirm", d["confirm"])
        calls = self.glab_calls()
        self.assertTrue(calls)
        for argv in calls:
            self.assertEqual(argv[0], "api", argv)
            self.assertFalse({"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"} & set(argv), argv)


class MustFixOrLaterTest(ReviewCase):
    """Each finding is serious (fix before merging) or can wait for a ticket of its own. The reviewers lean one way, the
    person decides, and the comment says whether the merge request can be merged."""
    FINDINGS = ("VERDICT: CHANGES\n\n## Findings\n1. [blocking] app/new.py:2: SQL injection through month\n"
                "2. [note] app/new.py:3: no rate limit on the export\n")

    def decided_security(self):
        return self.decided(security=self.FINDINGS)

    def test_the_reviewers_lean_is_shown_before_the_person_decides(self):
        _, res = self.decided_security()
        self.assertEqual([f["tier"] for f in res["findings"]], ["must-fix", "later"])
        report = slurp(res["report"])
        self.assertIn("must be fixed before merging", report)
        self.assertIn("can wait for a follow-up ticket", report)

    def test_a_serious_one_left_stops_the_merge_and_the_rest_wait_in_their_own_ticket(self):
        rid, _ = self.decided_security()
        code, d = self.review("draft", rid, "--select", "1,2", "--later", "2", "--later-ticket", "[BE] Security later")
        self.assertEqual(code, 0, d)
        self.assertEqual((d["merge"], d["must_fix"], d["later"]), (False, [1], [2]))
        self.assertIn("Verdict: not ready to merge", d["comment"])
        self.assertIn("[must fix before merging] app/new.py:2: SQL injection", d["comment"])
        self.assertIn("[can wait: follow-up ticket] app/new.py:3: no rate limit", d["comment"])
        (later,) = d["tickets"]
        self.assertEqual(later["summary"], "[BE] Security later")
        self.assertIn("no rate limit", later["markdown"])
        self.assertNotIn("SQL injection", later["markdown"])
        self.assertFalse([n for n in d["notes"] if "the person decided" in n])
        code, res = self.review("post", rid, "--confirm", d["confirm"])
        self.assertEqual(code, 0, res)
        self.assertEqual({i["action"]: i for i in res["jira_todo"]}["create-subtask"]["summary"], "[BE] Security later")

    def test_the_fixes_ticket_holds_only_what_must_be_fixed(self):
        rid, _ = self.decided_security()
        code, d = self.review("draft", rid, "--select", "1,2", "--later", "2", "--later-ticket", "later",
                              "--fix-ticket", "fixes")
        self.assertEqual(code, 0, d)
        fix, later = d["tickets"]
        self.assertIn("SQL injection", fix["markdown"])
        self.assertNotIn("rate limit", fix["markdown"])
        self.assertIn("RV-02", later["markdown"])

    def test_with_nothing_serious_left_it_can_be_merged(self):
        rid, _ = self.decided_security()
        code, d = self.review("draft", rid, "--select", "1,2", "--later", "1,2", "--later-ticket", "later")
        self.assertEqual((code, d["merge"]), (0, True), d)
        self.assertIn("Verdict: can be merged", d["comment"])
        said = [n for n in d["notes"] if "the person decided" in n]
        self.assertEqual(len(said), 1, d["notes"])
        self.assertIn("finding 1", said[0])

    def test_what_is_not_left_for_later_must_be_fixed(self):
        rid, _ = self.decided_security()
        code, d = self.review("draft", rid, "--select", "1,2")
        self.assertEqual((code, d["merge"], d["must_fix"]), (0, False, [1, 2]), d)
        self.assertTrue([n for n in d["notes"] if "finding 2" in n and "the person decided" in n], d["notes"])

    def test_later_needs_its_ticket_and_findings_that_were_chosen(self):
        rid, _ = self.decided_security()
        for argv, word in ((("--select", "1,2", "--later", "2"), "--later-ticket"),
                           (("--select", "1", "--later", "2", "--later-ticket", "x"), "not in --select"),
                           (("--select", "1,2", "--later-ticket", "x"), "--later"),
                           (("--select", "1", "--later-summary-file", "x.md"), "--later-ticket")):
            code, res = self.review("draft", rid, *argv)
            self.assertEqual(code, 3, (argv, res))
            self.assertIn(word, res["error"])
        self.assertFalse(self.job().get("jira_todo"))

    def test_a_later_ticket_alone_still_lets_the_ticket_go_back_to_a_dev(self):
        rid, _ = self.decided_security()
        code, d = self.review("draft", rid, "--select", "1,2", "--later", "2", "--later-ticket", "later", "--transition")
        self.assertEqual((code, d["transition_to"]), (0, "Doing"), d)

    def test_a_clean_review_says_no_verdict(self):
        rid, _ = self.decided_security()
        code, d = self.review("draft", rid, "--clean")
        self.assertEqual((code, d["merge"]), (0, None), d)
        self.assertNotIn("Verdict", d["comment"])


class LaterRoundTest(ReviewCase):
    def first_round(self, requirement=None):
        rid, _ = self.decided(code="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: slow\n2. [note] app/new.py:3: nit\n")
        _, d = self.review("draft", rid, "--select", "1")
        self.review("post", rid, "--confirm", d["confirm"])
        return rid

    def test_the_next_round_looks_at_what_changed(self):
        rid = self.first_round()
        first = self.head
        self.head = self.push_mr({"app/more.py": "more\n"})
        self.set_mr()
        _, res = self.scanned(2)
        self.assertEqual(self.job()["round"], 2)
        delta = slurp(self.rfile("delta.patch", 2))
        self.assertIn("app/more.py", delta)
        self.assertNotIn("app/new.py", delta)
        _, ctxr = self.review("ctx", rid, "--agent", "requirement")
        self.assertIn("delta.patch", ctxr["prompt"])
        self.assertIn(os.path.join(self.jd, "previous.md"), ctxr["prompt"])
        previous = slurp(os.path.join(self.jd, "previous.md"))
        self.assertIn(first, previous)
        self.assertIn("slow", previous)
        self.assertNotIn("nit", previous)

    def test_a_rebase_means_the_whole_diff_again(self):
        self.first_round()
        sh(["git", "commit", "-q", "--amend", "-m", "rewritten"], self.f.wt)
        sh(["git", "push", "-q", "-f", "origin", "HEAD:refs/merge-requests/7/head"], self.f.wt)
        self.head = sh(["git", "rev-parse", "HEAD"], self.f.wt).strip()
        self.set_mr()
        self.scanned(2)
        self.assertFalse(os.path.exists(self.rfile("delta.patch", 2)))
        self.assertFalse(self.job()["delta"])

    def test_the_comment_says_what_was_fixed(self):
        self.first_round()
        self.head = self.push_mr({"app/more.py": "more\n"})
        self.set_mr()
        rid, _ = self.decided(2, requirement="VERDICT: PASS\n\n## Findings\n\n## Previous findings\n- fixed: slow\n")
        _, d = self.review("draft", rid, "--clean")
        self.assertIn("Since the last review", d["comment"])
        self.assertIn("- fixed: slow", d["comment"])
        self.assertIn(self.head[:10], d["comment"])


class LearningTest(ReviewCase):
    def rejected(self):
        rid, _ = self.decided(code="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: kept\n"
                                   "2. [note] app/new.py:3: not wanted\n3. [note] app/old.py:30: old\n")
        _, d = self.review("draft", rid, "--select", "1")
        self.review("post", rid, "--confirm", d["confirm"])
        return rid

    def test_a_finding_not_chosen_is_a_false_positive(self):
        rid = self.rejected()
        got = self.job()["false_positives"]
        self.assertEqual([f["text"] for f in got], ["app/new.py:3: not wanted"])

    def test_closing_rejects_what_was_in_the_diff(self):
        rid, _ = self.decided(code="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: not wanted\n")
        self.review("close", rid)
        self.assertEqual(len(self.job()["false_positives"]), 1)

    def test_lessons_read_them_once(self):
        rid = self.rejected()
        with mock.patch.object(fx, "learning_stats", return_value={
                "repeat_offenders": [], "maybe_obsolete": [], "graduate": [], "over_limit": False}):
            code, res = self.fx("lessons", "collect")
            self.assertEqual(code, 0, res)
            item = next(i for i in res["items"] if i["job"] == rid)
            self.assertEqual(item["false_positives"][0]["text"], "app/new.py:3: not wanted")
            with mock.patch.object(fx.sys, "stdin", mock.MagicMock(read=lambda: "{}")):
                code, res = self.fx("lessons", "update", "--jobs", rid)
            self.assertEqual(code, 0, res)
            code, res = self.fx("lessons", "collect")
            self.assertFalse([i for i in res["items"] if i["job"] == rid])


class KeptApartTest(ReviewCase):
    def test_a_review_is_not_a_job(self):
        self.decided()
        ctx = self.f.ctx()
        self.assertEqual(ctx.list_ids(), ["T-1"])
        code, res = self.fx("status")
        self.assertEqual(code, 0)
        self.assertNotIn("_reviews", json.dumps(res["jobs"]))

    def test_a_review_goes_to_s3_only_when_asked_for(self):
        self.decided()
        ctx = self.f.ctx()
        names = fx.jobs_names_here(ctx, {"include_audits": True, "include_reviews": False})
        self.assertFalse([n for n in names if "_reviews" in n])
        names = fx.jobs_names_here(ctx, {"include_audits": False, "include_reviews": True})
        self.assertIn("_reviews/PROJ-9-mr7", names)

    def test_status_lists_the_reviews(self):
        rid = self.started()
        code, res = self.review("status")
        self.assertEqual((code, res["reviews"][0]["review"]), (0, rid))


class AgentsTest(ReviewCase):
    def test_every_review_agent_exists(self):
        for name in fx.REVIEW_AGENTS.values():
            self.assertTrue(os.path.exists(os.path.join(AGENTS, name + ".md")), name)

    def test_the_reused_agents_know_this_mode(self):
        for role in ("code", "security", "ux", "ui", "verifier"):
            text = slurp(os.path.join(AGENTS, fx.REVIEW_AGENTS[role] + ".md"))
            self.assertIn("`Mode: colleague MR`", text, role)
            self.assertIn("Never run the code", text, role)

    def test_a_reviewer_not_of_this_review_gets_no_prompt(self):
        rid, _ = self.scanned()
        code, res = self.review("ctx", rid, "--agent", "ux")
        self.assertEqual(code, 1)
        code, res = self.review("ctx", rid, "--agent", "verifier")
        self.assertEqual(code, 1)

    def test_models_never_go_below_the_floor(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["models"] = {"review": {"code": "haiku"}}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        rid, _ = self.scanned()
        code, res = self.review("ctx", rid, "--agent", "code")
        self.assertEqual(res["model"], "sonnet")
        code, res = self.review("ctx", rid, "--agent", "security")
        self.assertEqual(res["model"], "opus")


URL_FE = "https://gitlab.example.com/acme/fe/-/merge_requests/3"
URL_8 = "https://gitlab.example.com/acme/app/-/merge_requests/8"
APP, FE = "_reviews/PROJ-9-mr7", "_reviews/PROJ-9-fe-mr3"
PASS = "VERDICT: PASS\n\n## Findings\n"


class SeveralReposCase(ReviewCase):
    """A second repository with a real bare origin too, so a clone or a merge of the wrong one shows."""

    def setUp(self):
        super().setUp()
        self.fe_origin = os.path.join(self.tmp, "fe.git")
        self.fe_wt = os.path.join(self.tmp, "fe-work")
        sh(["git", "init", "-q", "--bare", self.fe_origin], self.tmp)
        sh(["git", "clone", "-q", self.fe_origin, self.fe_wt], self.tmp)
        write(os.path.join(self.fe_wt, "src", "page.vue"), "".join("row %d\n" % i for i in range(1, 11)))
        self.fe_commit("first", "HEAD:main")
        write(os.path.join(self.fe_wt, "src", "page.vue"), "changed\n" + "".join("row %d\n" % i for i in range(2, 11)))
        self.fe_head = self.fe_commit("the button", "HEAD:refs/merge-requests/3/head")
        p = mock.patch.object(fx, "gitlab_project", side_effect=lambda url: (
            "gitlab.example.com", "acme/fe" if "fe.git" in (url or "") else "acme/app"))
        p.start()
        self.addCleanup(p.stop)
        self.set_repos({"name": "fe", "url": self.fe_origin})
        self.routes["^user"] = {"username": "me"}
        self.routes["acme%2Ffe/merge_requests/3/notes"] = []
        self.routes["acme%2Fapp/merge_requests?state=opened"] = [self.mr()]
        self.set_fe()

    def fe_commit(self, msg, ref):
        sh(["git", "add", "-A"], self.fe_wt)
        sh(["git", "commit", "-q", "-m", msg], self.fe_wt)
        sh(["git", "push", "-q", "-f", "origin", ref], self.fe_wt)
        return sh(["git", "rev-parse", "HEAD"], self.fe_wt).strip()

    def set_repos(self, *repos):
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["review"] = {"repos": list(repos)}
        write(path, json.dumps(cfg))

    def fe_mr(self, **changes):
        d = self.mr(iid=3, web_url=URL_FE, title="PROJ-9: Export button", source_branch="feature/PROJ-9-ui",
                    sha=self.fe_head)
        d.update(changes)
        return d

    def set_fe(self, **changes):
        self.routes["acme%2Ffe/merge_requests/3"] = self.fe_mr(**changes)
        self.routes["acme%2Ffe/merge_requests?state=opened"] = [self.fe_mr(**changes)]
        self.save_routes()

    def job_of(self, rid):
        return load_json(os.path.join(self.f.factory, "jobs", rid, "job.json"))

    def rfile_of(self, rid, name, n=1):
        return os.path.join(self.f.factory, "jobs", rid, "round-%d" % n, name)

    def issue_file(self):
        issue = os.path.join(self.tmp, "issue.md")
        write(issue, ISSUE)
        return issue

    def together(self, *links):
        code, res = self.review("start", "PROJ-9", "--mrs", ",".join(links or (URL, URL_FE)), "--issue-file", self.issue_file())
        self.assertEqual(code, 0, res)
        return res

    def scanned_all(self, n=1):
        res = self.together()
        write(self.rfile_of(res["lead"], "checklist.md", n), "CHECKLIST: READY\n\n## Requirements\n1. CSV export\n")
        for rid in res["reviews"]:
            code, last = self.review("scan", rid)
            self.assertEqual(code, 0, last)
        for rid in res["reviews"]:
            self.no_sandbox(rid)
        return last

    def reviewed(self, rid, n=1, **texts):
        for r in self.job_of(rid)["reviewers"]:
            write(self.rfile_of(rid, "review-%s.md" % r, n), traced(r, texts.get(r, PASS)))
        code, res = self.review("result", rid)
        self.assertEqual(code, 0, res)
        if res["next"] == "verify":
            write(self.rfile_of(rid, "verify.md", n), "VERDICT: CHANGES\n\n## Findings\n" + "".join(
                "%d. confirmed: it stands\n" % i for i in range(1, res["blocking"] + 1)))
            code, res = self.review("result", rid)
            self.assertEqual(code, 0, res)
        return res

    def fit(self, text="FIT: PASS\n\n## Checked\nThe export call.\n"):
        code, res = self.review("fit", "PROJ-9")
        self.assertEqual(code, 0, res)
        write(res["fit_file"], text)
        return self.review("fit-result", "PROJ-9")

    def decided_all(self, fit="FIT: PASS\n\n## Checked\nThe export call.\n", app=PASS, fe=PASS, n=1):
        self.scanned_all(n)
        self.reviewed(APP, n, code=app)
        self.reviewed(FE, n, code=fe)
        code, res = self.fit(fit)
        self.assertEqual(code, 0, res)
        return res


class SeveralReposTest(SeveralReposCase):
    def test_a_merge_request_of_another_repository_of_the_factory_is_reviewed_from_that_repository(self):
        self.routes["acme%2Fapp/merge_requests?state=opened"] = []
        self.save_routes()
        code, res = self.review("start", URL_FE, "--issue-file", self.issue_file())
        self.assertEqual((code, res["review"]), (0, FE), res)
        write(self.rfile_of(FE, "checklist.md"), "CHECKLIST: READY\n\n## Requirements\n1. a button\n")
        code, res = self.review("scan", FE)
        self.assertEqual(code, 0, res)
        self.assertIn("src/page.vue", slurp(self.rfile_of(FE, "diff.patch")))
        self.assertEqual(self.job_of(FE)["head"], self.fe_head)

    def test_the_same_number_in_two_repositories_is_two_reviews(self):
        self.routes["acme%2Fapp/merge_requests?state=opened"] = []
        self.routes["acme%2Ffe/merge_requests/7"] = self.fe_mr(iid=7, web_url=URL_FE.replace("/3", "/7"))
        self.routes["acme%2Ffe/merge_requests?state=opened"] = []
        self.save_routes()
        _, one = self.review("start", URL, "--issue-file", self.issue_file())
        _, two = self.review("start", URL_FE.replace("/3", "/7"), "--issue-file", self.issue_file())
        self.assertEqual((one["review"], two["review"]), (APP, "_reviews/PROJ-9-fe-mr7"))

    def test_a_repository_not_listed_is_refused_and_says_where_to_list_it(self):
        code, res = self.review("start", "https://gitlab.example.com/other/app/-/merge_requests/7")
        self.assertEqual(code, 3)
        self.assertIn("review.repos", res["error"])

    def test_a_bad_list_of_repositories_is_refused(self):
        for bad, word in (({"name": "x", "url": "git@github.com:a/b.git"}, "GitLab"),
                          ({"name": "x y", "url": self.fe_origin}, "short name"), ({"url": self.fe_origin}, "short name"),
                          ({"name": "fit", "url": self.fe_origin}, "short name"), ("fe", "short name"),
                          ({"name": "app2", "url": self.f.origin}, "twice")):
            self.set_repos(bad)
            code, res = self.review("repos")
            self.assertEqual(code, 3, bad)
            self.assertIn(word, res["error"])

    def test_the_repositories_are_listed_with_their_knowledge(self):
        code, res = self.review("repos")
        self.assertEqual(code, 0, res)
        self.assertEqual([(r["name"], r["primary"]) for r in res["repos"]], [("app", True), ("fe", False)])
        self.assertEqual(res["repos"][1]["knowledge_files"], 0)
        self.assertTrue(res["repos"][1]["knowledge"].endswith(os.path.join("knowledge", "repos", "fe")))

    def test_each_repository_has_its_own_ui_paths(self):
        self.set_repos({"name": "fe", "url": self.fe_origin, "ui_paths": ["docs/*"]})
        last = self.scanned_all()
        self.assertEqual(last["reviewers_of"][FE], ["requirement", "security", "code"])

    def test_each_reviewer_reads_only_its_own_repositorys_knowledge(self):
        write(os.path.join(self.f.factory, "knowledge", "overview.md"), "the project\n")
        write(os.path.join(self.f.factory, "knowledge", "repos", "fe", "overview.md"), "the web app\n")
        self.scanned_all()
        _, fe = self.review("ctx", FE, "--agent", "code")
        _, app = self.review("ctx", APP, "--agent", "code")
        ours = os.path.join("knowledge", "overview.md")
        theirs = os.path.join("knowledge", "repos", "fe", "overview.md")
        self.assertIn(theirs, fe["prompt"])
        self.assertNotIn(ours, fe["prompt"])
        self.assertIn(ours, app["prompt"])
        self.assertNotIn(theirs, app["prompt"])
        self.assertIn("Repository: fe (acme/fe)", fe["prompt"])

    def test_a_repository_with_no_knowledge_gets_none_of_the_projects(self):
        write(os.path.join(self.f.factory, "knowledge", "overview.md"), "the project\n")
        self.scanned_all()
        _, fe = self.review("ctx", FE, "--agent", "code")
        self.assertNotIn(os.path.join("knowledge", "overview.md"), fe["prompt"])

    def test_the_projects_schema_is_not_given_for_another_repository(self):
        self.scanned_all()
        with mock.patch.object(fx, "schema_path", return_value=self.f.origin):
            _, fe = self.review("ctx", FE, "--agent", "code")
            _, app = self.review("ctx", APP, "--agent", "code")
        self.assertNotIn("Database schema", fe["prompt"])
        self.assertIn("Database schema", app["prompt"])

    def test_merge_requests_waiting_are_listed_from_every_repository(self):
        code, res = self.review("list")
        self.assertEqual(code, 0, res)
        self.assertEqual([(m["repo"], m["iid"]) for m in res["merge_requests"]], [("app", 7), ("fe", 3)])
        self.assertEqual(res["reviewer"], "me")

    def test_a_repository_gitlab_does_not_answer_for_is_named_in_the_list(self):
        del self.routes["acme%2Ffe/merge_requests?state=opened"]
        self.save_routes()
        code, res = self.review("list")
        self.assertEqual((code, [m["repo"] for m in res["merge_requests"]]), (0, ["app"]))
        self.assertIn("fe", res["not_searched"][0])

    def test_learning_a_repository_checks_its_knowledge_against_its_own_code(self):
        code, res = self.review("learn", "fe")
        self.assertEqual(code, 0, res)
        self.assertTrue(os.path.isfile(os.path.join(res["path"], "src", "page.vue")))
        self.assertFalse(os.path.exists(os.path.join(res["path"], "app", "old.py")))
        kdir = res["knowledge"]
        write(os.path.join(kdir, "architecture.md"), "`src/` holds the pages. See `src/gone.vue:1-3`.\n")
        code, res = self.review("learn", "fe", "--check")
        self.assertEqual((code, res["clean"]), (0, False), res)
        write(os.path.join(kdir, "architecture.md"), "`src/` holds the pages. See `src/page.vue:1-3`.\n")
        code, res = self.review("learn", "fe", "--check")
        self.assertEqual((code, res["clean"]), (0, True), res)
        code, res = self.review("learn", "fe", "--close")
        self.assertEqual(code, 0, res)
        self.assertFalse(os.path.exists(res["removed"]))
        self.assertEqual(self.clones(), [])

    def test_the_project_is_not_learned_this_way(self):
        code, res = self.review("learn", "app")
        self.assertEqual(code, 3)
        self.assertIn("/auto-factory:code init", res["error"])
        code, res = self.review("learn", "nope")
        self.assertEqual(code, 3)

    def test_the_knowledge_of_other_repositories_stays_out_of_the_projects_own_check(self):
        write(os.path.join(self.f.factory, "knowledge", "repos", "fe", "overview.md"), "the web app\n")
        names = fx.md_files(os.path.join(self.f.factory, "knowledge"))
        self.assertFalse([n for n in names if "repos" in n])


class AskWhichTest(SeveralReposCase):
    def test_a_ticket_is_searched_in_every_repository(self):
        code, res = self.review("start", "PROJ-9")
        self.assertEqual((code, res["next"]), (0, "ask-mrs"), res)
        self.assertEqual([(c["repo"], c["iid"]) for c in res["candidates"]], [("app", 7), ("fe", 3)])
        self.assertIn("--mrs", res["then"])

    def test_a_link_with_a_sibling_in_another_repository_asks_too(self):
        code, res = self.review("start", URL)
        self.assertEqual((code, res["next"]), (0, "ask-mrs"), res)
        self.assertEqual({c["iid"] for c in res["candidates"]}, {7, 3})

    def test_a_repository_gitlab_does_not_answer_for_is_named(self):
        del self.routes["acme%2Ffe/merge_requests?state=opened"]
        self.save_routes()
        code, res = self.review("start", URL, "--issue-file", self.issue_file())
        self.assertEqual((code, res["review"]), (0, APP), res)
        self.assertIn("fe", res["not_searched"][0])

    def test_no_answer_from_any_repository_is_not_taken_for_no_merge_request(self):
        for k in ("acme%2Ffe/merge_requests?state=opened", "acme%2Fapp/merge_requests?state=opened"):
            del self.routes[k]
        self.save_routes()
        code, res = self.review("start", "PROJ-9")
        self.assertEqual(code, 3)
        self.assertIn("could not search", res["error"])

    def test_choosing_one_tells_the_reviewers_about_the_other(self):
        code, res = self.review("start", "PROJ-9", "--mrs", URL, "--issue-file", self.issue_file())
        self.assertEqual((code, res["review"], res["left_out"]), (0, APP, [URL_FE]), res)
        write(self.rfile_of(APP, "checklist.md"), "CHECKLIST: READY\n\n## Requirements\n1. a\n")
        self.review("scan", APP)
        self.no_sandbox(APP)
        _, ctx = self.review("ctx", APP, "--agent", "requirement")
        self.assertIn("left out of this review", ctx["prompt"])
        self.assertIn(URL_FE, ctx["prompt"])

    def test_the_same_link_twice_is_one_merge_request(self):
        code, res = self.review("start", "PROJ-9", "--mrs", "%s,%s" % (URL, URL), "--issue-file", self.issue_file())
        self.assertEqual((code, res["review"]), (0, APP), res)

    def test_a_list_holding_something_else_is_refused(self):
        code, res = self.review("start", "PROJ-9", "--mrs", "%s,nonsense" % URL)
        self.assertEqual(code, 1)
        self.assertIn("not a merge request link", res["error"])

    def test_without_a_token_the_ticket_is_fetched_once_for_all(self):
        code, res = self.review("start", "PROJ-9", "--mrs", "%s,%s" % (URL, URL_FE))
        self.assertEqual((code, res["next"], res["issue_key"]), (0, "fetch", "PROJ-9"), res)
        self.assertIn("--mrs", res["then"])
        self.assertIn(URL_FE, res["then"])
        write(res["write_to"], ISSUE)
        code, res = self.review("start", "PROJ-9", "--mrs", "%s,%s" % (URL, URL_FE), "--issue-file", res["write_to"])
        self.assertEqual((code, res["next"]), (0, "checklist"), res)
        self.assertIn("Export invoices", slurp(os.path.join(self.f.factory, "jobs", FE, "issue.md")))


class TogetherTest(SeveralReposCase):
    def test_one_ticket_has_one_checklist(self):
        res = self.together()
        self.assertEqual((res["next"], res["lead"], res["reviews"]), ("checklist", APP, [APP, FE]))
        code, ctx = self.review("ctx", FE, "--agent", "checklist")
        self.assertEqual(code, 1)
        self.assertIn("one checklist", ctx["error"])
        code, ctx = self.review("ctx", APP, "--agent", "checklist")
        self.assertEqual(code, 0, ctx)
        self.assertIn(self.rfile_of(APP, "checklist.md"), ctx["prompt"])

    def test_no_code_is_cloned_before_the_checklist(self):
        self.together()
        code, res = self.review("scan", FE)
        self.assertEqual(code, 6)
        self.assertEqual(res["retry_agent"], "checklist")
        self.assertEqual(self.clones(), [])

    def test_every_merge_request_gets_the_checklist_and_its_own_repositorys_code(self):
        last = self.scanned_all()
        self.assertEqual((last["next"], sorted(last["reviewers_of"])), ("sandbox", [FE, APP]))
        self.assertIn("CSV export", slurp(self.rfile_of(FE, "checklist.md")))
        self.assertIn("src/page.vue", slurp(self.rfile_of(FE, "diff.patch")))
        self.assertNotIn("src/page.vue", slurp(self.rfile_of(APP, "diff.patch")))

    def test_the_scan_says_which_comes_next(self):
        res = self.together()
        write(self.rfile_of(res["lead"], "checklist.md"), "CHECKLIST: READY\n\n## Requirements\n1. a\n")
        _, one = self.review("scan", APP)
        self.assertEqual((one["next"], one["scan"]), ("scan", FE))

    def test_no_reviewer_starts_before_all_are_scanned(self):
        res = self.together()
        write(self.rfile_of(res["lead"], "checklist.md"), "CHECKLIST: READY\n\n## Requirements\n1. a\n")
        self.review("scan", APP)
        code, ctx = self.review("ctx", APP, "--agent", "requirement")
        self.assertEqual(code, 1)
        self.assertIn("stage run", ctx["error"])

    def test_each_reviewer_can_read_the_other_merge_request(self):
        self.scanned_all()
        _, ctx = self.review("ctx", APP, "--agent", "requirement")
        self.assertIn("Other merge requests of this ticket", ctx["prompt"])
        self.assertIn(self.rfile_of(FE, "diff.patch"), ctx["prompt"])
        self.assertIn(self.job_of(FE)["worktree"], ctx["prompt"])
        self.assertNotIn(self.rfile_of(APP, "diff.patch") + ", worktree", ctx["prompt"])

    def test_the_clones_stay_until_they_are_checked_together(self):
        self.scanned_all()
        first = self.reviewed(APP)
        self.assertEqual((first["next"], first["waiting"]), ("next-review", [FE]))
        self.assertTrue(os.path.isdir(self.job_of(APP)["worktree"]))
        last = self.reviewed(FE)
        self.assertEqual(last["next"], "fit")
        self.assertEqual(len(self.clones()), 2)
        code, res = self.fit()
        self.assertEqual((code, res["fit"]["result"]), (0, "PASS"))
        self.assertEqual(self.clones(), [])

    def test_the_check_waits_for_every_review(self):
        self.scanned_all()
        self.reviewed(APP)
        code, res = self.review("fit", "PROJ-9")
        self.assertEqual(code, 3)
        self.assertIn(FE, res["error"])

    def test_the_fit_checker_gets_each_copy_and_what_changed(self):
        self.scanned_all()
        self.reviewed(APP)
        self.reviewed(FE)
        code, res = self.review("fit", "PROJ-9")
        self.assertEqual((code, res["subagent_type"]), (0, "factory-fit-checker"), res)
        self.assertEqual(res["model"], "opus")
        for rid in (APP, FE):
            j = self.job_of(rid)
            self.assertIn("`git -C %s diff %s HEAD`" % (j["worktree"], j["merge_base"]), res["prompt"])
        self.assertIn("Mode: colleagues' merge requests", res["prompt"])
        self.assertIn("- fe: copy", res["prompt"])

    def test_a_bad_fit_file_is_asked_again(self):
        self.scanned_all()
        self.reviewed(APP)
        self.reviewed(FE)
        self.review("fit", "PROJ-9")
        code, res = self.review("fit-result", "PROJ-9")
        self.assertEqual((code, res["retry_agent"]), (6, "fit-checker"))
        for bad in ("nonsense\n", "FIT: CHANGES\n\n## Checked\nx\n", "FIT: CHANGES\n\n## Repo: be\n1. x\n",
                    "FIT: CHANGES\n\n## Repo: fe\n"):
            _, ask = self.review("fit", "PROJ-9")
            write(ask["fit_file"], bad)
            code, res = self.review("fit-result", "PROJ-9")
            self.assertEqual((code, res["retry_agent"]), (6, "fit-checker"), bad)
        self.assertEqual(len(self.clones()), 2)

    def test_findings_are_named_by_repository_and_the_check_adds_its_own(self):
        res = self.decided_all(fit="FIT: CHANGES\n\n## Checked\nx\n\n## Repo: fe\n1. src/page.vue:1 calls /export, "
                                   "app/new.py:1 serves /exports\n",
                               app="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: app note\n",
                               fe="VERDICT: PASS\n\n## Findings\n1. [note] src/page.vue:1: fe note\n")
        self.assertEqual(res["next"], "decide")
        self.assertEqual(sorted(f["id"] for f in res["findings"]), ["app:1", "fe:1", "fit:1"])
        fit = next(f for f in res["findings"] if f["id"] == "fit:1")
        self.assertEqual((fit["severity"], fit["group"], fit["repo"]), ("blocking", "diff", "fe"))

    def test_the_whole_body_counts_when_the_checker_did_not_number_it(self):
        res = self.decided_all(fit="FIT: CHANGES\n\n## Repo: fe\nThe call does not match.\n")
        self.assertEqual([f["text"] for f in res["findings"]], ["The call does not match."])

    def test_two_merge_requests_of_one_repository_are_told_apart(self):
        self.f.commit({"app/other.py": "other\n"}, "other")
        sh(["git", "push", "-q", "-f", "origin", "HEAD:refs/merge-requests/8/head"], self.f.wt)
        head8 = sh(["git", "rev-parse", "HEAD"], self.f.wt).strip()
        self.routes["merge_requests/8"] = self.mr(iid=8, web_url=URL_8, sha=head8)
        self.routes["merge_requests/8/notes"] = []
        self.save_routes()
        res = self.together(URL, URL_8)
        write(self.rfile_of(res["lead"], "checklist.md"), "CHECKLIST: READY\n\n## Requirements\n1. a\n")
        two = "_reviews/PROJ-9-mr8"
        self.review("scan", APP)
        self.review("scan", two)
        self.no_sandbox(APP)
        self.no_sandbox(two)
        self.reviewed(APP, code="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: seven\n")
        self.reviewed(two, code="VERDICT: PASS\n\n## Findings\n1. [note] app/other.py:1: eight\n")
        code, ask = self.review("fit", "PROJ-9")
        self.assertIn("- app (mr8): copy", ask["prompt"])
        write(ask["fit_file"], "FIT: CHANGES\n\n## Repo: app (mr8)\n1. no\n")
        code, res = self.review("fit-result", "PROJ-9")
        self.assertEqual(code, 0, res)
        self.assertEqual(sorted(f["id"] for f in res["findings"]), ["app-mr7:1", "app-mr8:1", "fit:1"])

    def test_a_start_while_one_is_under_way_changes_nothing(self):
        self.scanned_all()
        code, res = self.review("start", "PROJ-9", "--mrs", "%s,%s" % (URL, URL_FE))
        self.assertEqual((code, res["next"], res["stage"]), (0, "status", "review"))
        self.assertIn("--again", res["note"])
        self.assertEqual(self.job_of(FE)["stage"], "review")

    def test_again_starts_the_round_over_and_drops_the_clones(self):
        self.scanned_all()
        res = self.review("start", "PROJ-9", "--mrs", "%s,%s" % (URL, URL_FE), "--again", "--issue-file", self.issue_file())[1]
        self.assertEqual((res["next"], res["round"]), ("checklist", 1))
        self.assertEqual(self.clones(), [])

    def test_the_status_shows_the_ticket(self):
        self.scanned_all()
        code, res = self.review("status")
        self.assertEqual((code, res["tickets"][0]["ticket"], res["tickets"][0]["stage"]), (0, "PROJ-9", "review"))
        self.assertEqual({r["repo"] for r in res["reviews"]}, {"app", "fe"})
        self.assertTrue(all(r["together"] for r in res["reviews"]))

    def test_the_ticket_folder_is_not_taken_for_a_review(self):
        self.scanned_all()
        _, res = self.review("status")
        self.assertEqual(sorted(r["review"] for r in res["reviews"]), sorted([APP, FE]))


class ConflictTogetherTest(SeveralReposCase):
    def conflict_in_fe(self):
        sh(["git", "checkout", "-q", "-b", "other", "HEAD~1"], self.fe_wt)
        write(os.path.join(self.fe_wt, "src", "page.vue"), "theirs\n" + "".join("row %d\n" % i for i in range(2, 11)))
        self.fe_commit("another change", "HEAD:main")
        self.set_fe(detailed_merge_status="conflict", has_conflicts=True)

    def test_a_conflict_in_one_stops_all_before_any_agent(self):
        self.conflict_in_fe()
        res = self.together()
        self.assertEqual((res["stage"], res["next"]), ("conflict", "ask-conflict"))
        self.assertEqual(res["conflicts"][0]["repo"], "fe")
        self.assertEqual(res["conflicts"][0]["files"], ["src/page.vue"])  # from a real merge in that repository
        self.assertEqual({self.job_of(APP)["stage"], self.job_of(FE)["stage"]}, {"conflict"})
        code, ctx = self.review("ctx", APP, "--agent", "checklist")
        self.assertEqual(code, 1)
        self.assertEqual(self.clones(), [])

    def test_the_comment_needs_a_yes_and_goes_out_once(self):
        self.conflict_in_fe()
        self.together()
        code, d = self.review("draft", "PROJ-9", "--transition", "--assignee", "acc-1")
        self.assertEqual((code, d["transition_to"], d["assignee"]), (0, "Fix again", "acc-1"), d)
        self.assertIn("src/page.vue", d["comment"])
        self.assertIn("none of this ticket's merge requests was reviewed", d["comment"])
        self.assertFalse(self.job_of(APP).get("jira_todo"))
        code, res = self.review("post", "PROJ-9", "--confirm", d["confirm"])
        self.assertEqual(code, 0, res)
        todo = self.job_of(APP)["jira_todo"]
        self.assertEqual(sorted(t["action"] for t in todo), ["assign", "comment", "transition"])
        self.assertFalse(self.job_of(FE).get("jira_todo"))
        self.assertEqual({self.job_of(APP)["stage"], self.job_of(FE)["stage"]}, {"done"})

    def test_closing_writes_nothing(self):
        self.conflict_in_fe()
        self.together()
        code, res = self.review("close", "PROJ-9")
        self.assertEqual((code, res["posted"]), (0, False))
        self.assertFalse(self.job_of(APP).get("jira_todo"))
        self.assertEqual(self.job_of(FE)["stage"], "done")


class OneCommentTest(SeveralReposCase):
    APP_FINDINGS = "VERDICT: CHANGES\n\n## Findings\n1. [blocking] app/new.py:2: app slow\n2. [note] app/old.py:30: app old\n"
    FE_FINDINGS = "VERDICT: PASS\n\n## Findings\n1. [note] src/page.vue:1: fe nit\n2. [question] which month?\n"
    FIT = "FIT: CHANGES\n\n## Repo: fe\n1. src/page.vue:1 calls /export, app/new.py:1 serves /exports\n"

    def decided(self):
        return self.decided_all(fit=self.FIT, app=self.APP_FINDINGS, fe=self.FE_FINDINGS)

    def test_one_merge_request_cannot_go_out_alone(self):
        self.decided()
        for action in ("draft", "post", "close"):
            code, res = self.review(action, FE)
            self.assertEqual(code, 3, action)
            self.assertIn("rest of ticket PROJ-9", res["error"])

    def test_nothing_goes_out_without_a_choice(self):
        self.decided()
        code, res = self.review("draft", "PROJ-9")
        self.assertEqual(code, 1)
        self.assertIn("--select", res["error"])

    def test_only_findings_in_the_diff_can_go_out(self):
        res = self.decided()
        for f in res["findings"]:
            if f["group"] != "diff":
                code, out = self.review("draft", "PROJ-9", "--select", f["id"])
                self.assertEqual(code, 1, f["id"])
                self.assertIn("does not go to Jira", out["error"])
        code, out = self.review("draft", "PROJ-9", "--select", "fe:99")
        self.assertEqual(code, 1)

    def test_the_comment_lists_every_merge_request_and_the_chosen_findings(self):
        self.decided()
        code, d = self.review("draft", "PROJ-9", "--select", "app:1,fit:1")
        self.assertEqual(code, 0, d)
        self.assertIn(URL, d["comment"])
        self.assertIn(URL_FE, d["comment"])
        self.assertIn(self.head[:10], d["comment"])
        self.assertIn(self.fe_head[:10], d["comment"])
        self.assertIn("app slow", d["comment"])
        self.assertIn("https://gitlab.example.com/acme/app/-/blob/%s/app/new.py#L2" % self.head, d["comment"])
        self.assertIn("### Between the merge requests (1)", d["comment"])
        self.assertIn("serves /exports", d["comment"])
        self.assertNotIn("fe nit", d["comment"])

    def test_drafting_writes_nothing_to_jira(self):
        self.decided()
        self.review("draft", "PROJ-9", "--select", "app:1", "--transition")
        self.assertFalse(self.job_of(APP).get("jira_todo"))

    def test_posting_needs_the_code_of_the_draft(self):
        self.decided()
        _, d = self.review("draft", "PROJ-9", "--select", "app:1")
        code, res = self.review("post", "PROJ-9", "--confirm", "wrong")
        self.assertEqual(code, 3)
        path = os.path.join(self.f.factory, "jobs", "_reviews", "_ticket-PROJ-9", "round-1", "comment.md")
        write(path, slurp(path) + "and a line nobody saw\n")
        code, res = self.review("post", "PROJ-9", "--confirm", d["confirm"])
        self.assertEqual(code, 3)
        self.assertFalse(self.job_of(APP).get("jira_todo"))

    def test_the_ticket_gets_one_comment_one_move_and_one_assignee(self):
        self.decided()
        _, d = self.review("draft", "PROJ-9", "--select", "app:1,fit:1", "--transition", "--assignee", "acc-1")
        self.assertEqual(d["transition_to"], "Doing")
        code, res = self.review("post", "PROJ-9", "--confirm", d["confirm"])
        self.assertEqual((code, res["review"]), (0, APP), res)
        self.assertEqual(sorted(t["action"] for t in self.job_of(APP)["jira_todo"]), ["assign", "comment", "transition"])
        self.assertFalse(self.job_of(FE).get("jira_todo"))
        self.assertEqual({self.job_of(APP)["stage"], self.job_of(FE)["stage"]}, {"done"})

    def test_a_clean_review_never_moves_the_ticket(self):
        self.decided_all()
        code, d = self.review("draft", "PROJ-9", "--clean", "--transition")
        self.assertEqual((code, d["transition_to"]), (0, None), d)
        self.assertIn("No findings to raise at these commits", d["comment"])

    def test_a_merge_request_that_moved_is_said(self):
        self.decided()
        self.set_fe(sha="f" * 40)
        _, d = self.review("draft", "PROJ-9", "--select", "app:1")
        self.assertTrue([n for n in d["notes"] if n.startswith("fe moved to")], d["notes"])
        self.assertFalse([n for n in d["notes"] if n.startswith("app moved")], d["notes"])

    def test_gitlab_is_only_read(self):
        self.decided()
        _, d = self.review("draft", "PROJ-9", "--select", "app:1")
        self.review("post", "PROJ-9", "--confirm", d["confirm"])
        calls = self.glab_calls()
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call[0], "api", call)
            self.assertFalse({"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"} & set(call), call)

    def test_what_was_not_chosen_counts_as_a_false_positive(self):
        self.decided()
        _, d = self.review("draft", "PROJ-9", "--select", "app:1")
        self.review("post", "PROJ-9", "--confirm", d["confirm"])
        said = [f["text"] for f in self.job_of(FE)["false_positives"]]
        self.assertEqual(said, ["src/page.vue:1: fe nit"])
        fits = [f for f in self.job_of(APP)["false_positives"] if f["reviewer"] == "fit"]
        self.assertEqual(len(fits), 1)

    def test_closing_ends_every_review_and_writes_nothing(self):
        self.decided()
        code, res = self.review("close", "PROJ-9")
        self.assertEqual((code, res["posted"]), (0, False))
        self.assertEqual({self.job_of(APP)["stage"], self.job_of(FE)["stage"]}, {"done"})
        self.assertFalse(self.job_of(APP).get("jira_todo"))
        self.assertEqual(self.clones(), [])

    def test_the_next_round_knows_what_was_posted(self):
        self.decided()
        _, d = self.review("draft", "PROJ-9", "--select", "app:1,fe:1,fit:1")
        self.review("post", "PROJ-9", "--confirm", d["confirm"])
        write(os.path.join(self.fe_wt, "src", "page.vue"), "fixed\n" + "".join("row %d\n" % i for i in range(2, 11)))
        self.fe_head = self.fe_commit("the fix", "HEAD:refs/merge-requests/3/head")
        self.set_fe()
        res = self.together()
        self.assertEqual((res["round"], res["next"]), (2, "checklist"))
        write(self.rfile_of(APP, "checklist.md", 2), "CHECKLIST: READY\n\n## Requirements\n1. a\n")
        self.review("scan", APP)
        self.review("scan", FE)
        self.no_sandbox(APP)
        self.no_sandbox(FE)
        _, ctx = self.review("ctx", FE, "--agent", "code")
        self.assertIn("previous.md", ctx["prompt"])
        self.assertIn("delta.patch", ctx["prompt"])
        self.reviewed(APP, 2)
        self.reviewed(FE, 2)
        _, ask = self.review("fit", "PROJ-9")
        self.assertIn("previous-fit.md", ask["prompt"])

    def test_a_merge_request_reviewed_alone_after_a_ticket_goes_out_by_itself(self):
        self.decided()
        self.review("close", "PROJ-9")
        code, res = self.review("start", "PROJ-9", "--mrs", URL, "--issue-file", self.issue_file())
        self.assertEqual(code, 0, res)
        self.assertIsNone(self.job_of(APP)["ticket"])
        self.assertEqual(self.job_of(APP)["round"], 2)


class SeveralReposAgentsTest(ReviewCase):
    def test_the_agents_know_about_the_other_merge_requests(self):
        for role in ("code", "security", "ux", "ui", "verifier", "requirement"):
            text = slurp(os.path.join(AGENTS, fx.REVIEW_AGENTS[role] + ".md"))
            self.assertIn("merge request", text, role)
            self.assertTrue("other merge requests of this ticket" in text or "another merge request of this ticket" in text,
                            role)

    def test_the_fit_checker_knows_this_mode(self):
        text = slurp(os.path.join(AGENTS, "factory-fit-checker.md"))
        self.assertIn("`Mode: colleagues' merge requests`", text)
        self.assertIn("never run it", text)


class BoardTest(SeveralReposCase):
    def board(self):
        return fx.board_data([self.f.factory])

    def card(self):
        data = self.board()
        cards = [v for v in data["active"] + data["done"] if v.get("kind") == "review"]
        self.assertEqual(len(cards), 1, cards)
        return cards[0]

    def alone(self):
        self.routes["acme%2Ffe/merge_requests?state=opened"] = []
        self.save_routes()
        return self.scanned()[0]

    def test_a_review_shows_on_the_board_from_its_start(self):
        self.routes["acme%2Ffe/merge_requests?state=opened"] = []
        self.save_routes()
        self.started()
        v = self.card()
        self.assertEqual((v["key"], v["phase"], v["active"], v["waiting"]), ("PROJ-9", "checklist", True, None))
        self.assertEqual(v["mrs"][0]["url"], URL)
        self.assertNotIn("fit", v["phases"])

    def test_the_agent_at_work_shows_and_ends_when_its_file_is_written(self):
        self.routes["acme%2Ffe/merge_requests?state=opened"] = []
        self.save_routes()
        rid = self.started()
        self.review("ctx", rid, "--agent", "checklist")
        running = self.card()["running"]
        self.assertEqual([(r["name"], r["model"]) for r in running], [("checklist", "sonnet")])
        write(self.rfile("checklist.md"), "CHECKLIST: READY\n\n## Requirements\n1. a\n")
        self.assertEqual(self.card()["running"], [])

    def test_the_scan_shows_as_a_step_that_ends(self):
        rid = self.alone()
        steps = self.job()["steps"]
        self.assertEqual([s["step"] for s in steps], ["scan", "sandbox"])
        self.assertTrue(all(s["ended"] for s in steps))

    def test_a_scan_that_fails_does_not_stay_running(self):
        self.routes["acme%2Ffe/merge_requests?state=opened"] = []
        self.save_routes()
        rid = self.started()
        code, _ = self.review("scan", rid)  # no checklist yet
        self.assertEqual(code, 6)
        self.assertEqual(self.card()["running"], [])

    def test_a_ticket_is_one_card_with_each_merge_request(self):
        self.scanned_all()
        v = self.card()
        self.assertEqual(v["id"], "_ticket-PROJ-9")
        self.assertEqual([(m["name"], m["iid"]) for m in v["mrs"]], [("app", 7), ("fe", 3)])
        self.assertIn("fit", v["phases"])
        self.assertEqual(v["phase"], "review")

    def test_each_reviewer_says_how_far_it_is_and_whose_it_is(self):
        self.scanned_all()
        self.review("ctx", FE, "--agent", "code")
        v = self.card()
        fe = next(m for m in v["mrs"] if m["name"] == "fe")
        self.assertEqual({r["name"]: r["state"] for r in fe["reviewers"]}["code"], "running")
        self.assertEqual({r["name"]: r["state"] for r in fe["reviewers"]}["security"], "todo")
        self.assertEqual([(r["name"], r["wp"]) for r in v["running"]], [("code", "fe")])
        write(self.rfile_of(FE, "review-code.md"), PASS)
        fe = next(m for m in self.card()["mrs"] if m["name"] == "fe")
        self.assertEqual({r["name"]: r["state"] for r in fe["reviewers"]}["code"], "done")

    def test_the_fit_check_shows_and_ends(self):
        self.scanned_all()
        self.reviewed(APP)
        self.reviewed(FE)
        self.assertEqual(self.card()["phase"], "fit")
        code, ask = self.review("fit", "PROJ-9")
        self.assertEqual(code, 0, ask)
        v = self.card()
        self.assertEqual([(r["name"], r["model"], r["wp"]) for r in v["running"]], [("fit-checker", "opus", None)])
        self.assertEqual(ask["review"], APP)
        write(ask["fit_file"], "FIT: PASS\n\n## Checked\nx\n")
        self.review("fit-result", "PROJ-9")
        v = self.card()
        self.assertEqual((v["running"], v["phase"], v["waiting"]), ([], "decide", "rchoose"))

    def test_what_waits_for_the_person_reviewing_is_said(self):
        self.decided_all(app="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: a\n")
        self.assertEqual(self.card()["waiting"], "rchoose")
        self.assertIsNone(self.card()["eta"])
        _, d = self.review("draft", "PROJ-9", "--select", "app:1")
        self.assertEqual((self.card()["waiting"], self.card()["phase"]), ("rconfirm", "post"))
        self.review("post", "PROJ-9", "--confirm", d["confirm"])
        v = self.card()
        self.assertEqual((v["active"], v["waiting"], v["decision"]), (False, None, "posted"))

    def test_a_merge_conflict_waits_for_the_person_reviewing(self):
        sh(["git", "checkout", "-q", "-b", "other", "HEAD~1"], self.fe_wt)
        write(os.path.join(self.fe_wt, "src", "page.vue"), "theirs\n")
        self.fe_commit("another change", "HEAD:main")
        self.set_fe(detailed_merge_status="conflict", has_conflicts=True)
        self.together()
        v = self.card()
        self.assertEqual((v["waiting"], v["phase"], v["running"]), ("rconflict", "decide", []))

    def test_a_review_closed_without_posting_says_so(self):
        self.decided_all()
        self.review("close", "PROJ-9")
        v = self.card()
        self.assertEqual((v["active"], v["decision"]), (False, "closed"))

    def test_one_merge_request_alone_is_a_card_of_its_own(self):
        self.routes["acme%2Ffe/merge_requests?state=opened"] = []
        self.save_routes()
        rid, _ = self.decided(code="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: a\n")
        v = self.card()
        self.assertEqual((v["id"], v["waiting"]), (rid, "rchoose"))
        _, d = self.review("draft", rid, "--select", "1")
        self.review("post", rid, "--confirm", d["confirm"])
        self.assertEqual(self.card()["decision"], "posted")

    def test_tokens_add_up_across_the_merge_requests(self):
        self.scanned_all()
        self.review("ctx", APP, "--agent", "code")
        self.review("ctx", FE, "--agent", "code")
        code, res = self.fx("usage", APP, "--agent", "code", "--tokens", "100", "--ms", "1000")
        self.assertEqual(code, 0, res)
        self.fx("usage", FE, "--agent", "code", "--tokens", "50", "--ms", "1000")
        v = self.card()
        self.assertEqual(v["tokens"], 150)
        self.assertEqual(v["running"], [])

    def test_the_files_of_the_review_are_linked(self):
        res = self.decided_all()
        labels = [f["label"] for f in self.card()["files"]]
        for want in ("issue", "checklist", "fit", "app: report", "fe: report", "fe: review code"):
            self.assertIn(want, labels)

    def test_starting_over_stops_waiting_for_the_agents_of_the_old_round(self):
        self.scanned_all()
        self.review("ctx", FE, "--agent", "code")
        self.review("start", "PROJ-9", "--mrs", "%s,%s" % (URL, URL_FE), "--again", "--issue-file", self.issue_file())
        self.assertEqual(self.card()["running"], [])

    def test_two_merge_requests_of_one_repository_are_told_apart(self):
        self.f.commit({"app/other.py": "other\n"}, "other")
        sh(["git", "push", "-q", "-f", "origin", "HEAD:refs/merge-requests/8/head"], self.f.wt)
        self.routes["merge_requests/8"] = self.mr(iid=8, web_url=URL_8)
        self.routes["merge_requests/8/notes"] = []
        self.save_routes()
        self.together(URL, URL_8)
        self.assertEqual([m["name"] for m in self.card()["mrs"]], ["app !7", "app !8"])

    def test_a_review_is_not_counted_as_a_job(self):
        self.scanned_all()
        data = self.board()
        before = [v["id"] for v in data["active"] + data["done"] if v.get("kind") != "review"]
        self.assertFalse([i for i in before if "_reviews" in i or "_ticket" in i])
        self.assertEqual(data["factories"][0]["jobs"], len(before))
        code, res = self.fx("status")
        self.assertFalse([j for j in res["jobs"] if "_reviews" in str(j.get("id") or j.get("job"))])

    def test_a_broken_file_does_not_take_the_board_down(self):
        self.scanned_all()
        write(os.path.join(self.f.factory, "jobs", "_reviews", "PROJ-1-mr1", "job.json"), "not json")
        write(os.path.join(self.f.factory, "jobs", "_reviews", "_ticket-PROJ-1", "ticket.json"), "[]")
        self.assertEqual(self.card()["id"], "_ticket-PROJ-9")

    def test_a_hostile_title_cannot_get_out_of_the_data_on_the_page(self):
        bad = "</script><script>alert(1)</script>"
        self.set_fe(title="PROJ-9: " + bad)
        self.together()
        self.fx("board")
        page = slurp(fx.board_file())
        self.assertNotIn(bad, page)

    def test_the_page_knows_the_words_of_a_review(self):
        for word in ("rchoose:", "rconfirm:", "rconflict:", "'fit-checker':", "requirement:", "etaReview:"):
            self.assertEqual(fx.BOARD_HTML.count(word), 2, word)

    def test_the_page_opens_on_the_flow_map_and_keeps_the_office(self):
        self.assertIn("let S = {layout: 'flow',", fx.BOARD_HTML)
        for word in ("flowTitle:", "lFlow:", "lOffice:", "flowLegend:", "flowJobs:", "flowMrs:", "colCount:", "pkgWait:", "mayStop:"):
            self.assertEqual(fx.BOARD_HTML.count(word), 2, word)  # once in English, once in Vietnamese
        for part in ('data-layout="', "function flowMap(", "function rooms(", "function jobRow("):
            self.assertIn(part, fx.BOARD_HTML)
        self.assertEqual(fx.BOARD_HTML.count("__DATA__"), 1)


class LogicTracedTest(ReviewCase):
    CHECKLIST = "CHECKLIST: READY\n\n## Requirements\n1. CSV export\n2. Only paid invoices\n\n## Open questions\n1. which month?\n"

    def ready(self):
        rid, _ = self.scanned()
        write(self.rfile("checklist.md"), self.CHECKLIST)
        for r in self.job()["reviewers"]:
            write(self.rfile("review-%s.md" % r), "VERDICT: PASS\n\n## Findings\n")
        return rid

    def result_with(self, rid, text):
        write(self.rfile("review-requirement.md"), text)
        return self.review("result", rid)

    @staticmethod
    def block(n, state, path="app/new.py:1", cases="normal: 3 rows"):
        rows = ["### R%d: something" % n, "- State: %s" % state]
        if path:
            rows.append("- Path: %s" % path)
        if cases:
            rows.append("- Cases: %s" % cases)
        return "\n".join(rows) + "\n"

    def traced_as(self, *blocks, verdict="PASS", findings=""):
        return "VERDICT: %s\n\n## Findings\n%s\n## Logic traced\n%s" % (verdict, findings, "".join(blocks))

    def test_a_verdict_without_the_logic_followed_is_sent_back(self):
        rid = self.ready()
        code, res = self.result_with(rid, "VERDICT: PASS\n\n## Findings\n")
        self.assertEqual((code, res["retry_agent"]), (6, "requirement"))
        self.assertIn("R1", res["error"])
        self.assertIn("R2", res["error"])

    def test_every_requirement_needs_its_own_trace(self):
        rid = self.ready()
        code, res = self.result_with(rid, self.traced_as(self.block(1, "implemented")))
        self.assertEqual(code, 6)
        self.assertIn("R2", res["error"])
        self.assertNotIn("R1", res["error"])

    def test_implemented_needs_a_path_in_the_code_and_cases(self):
        rid = self.ready()
        for block, word in ((self.block(1, "implemented", path=None), "cites no path:line"),
                            (self.block(1, "implemented", path="the export service"), "cites no path:line"),
                            (self.block(1, "implemented", cases=None), "Cases"),
                            (self.block(1, "different", path=None, cases=None), "cites no path:line")):
            code, res = self.result_with(rid, self.traced_as(block, self.block(2, "implemented")))
            self.assertEqual(code, 6, block)
            self.assertIn(word, res["error"])

    def test_the_state_must_be_one_of_the_known_ones(self):
        rid = self.ready()
        code, res = self.result_with(rid, self.traced_as(self.block(1, "looks fine"), self.block(2, "implemented")))
        self.assertEqual(code, 6)
        self.assertIn("State", res["error"])

    def test_missing_or_different_cannot_pass(self):
        rid = self.ready()
        code, res = self.result_with(rid, self.traced_as(self.block(1, "implemented"), self.block(2, "missing", None, None)))
        self.assertEqual(code, 6)
        self.assertIn("R2", res["error"])
        self.assertIn("[blocking]", res["error"])

    def test_a_requirement_elsewhere_or_unclear_needs_no_path(self):
        rid = self.ready()
        code, res = self.result_with(rid, self.traced_as(self.block(1, "elsewhere", None, None),
                                                          self.block(2, "cannot tell", None, None)))
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "decide")

    def test_a_full_trace_goes_through_and_reaches_the_report(self):
        rid = self.ready()
        code, res = self.result_with(rid, self.traced_as(
            self.block(1, "implemented", path="routes.py:3 → app/new.py:1"), self.block(2, "**implemented**")))
        self.assertEqual(code, 0, res)
        report = slurp(res["report"])
        self.assertIn("## Logic traced", report)
        self.assertIn("routes.py:3", report)

    def test_the_trace_stays_out_of_the_jira_comment(self):
        rid = self.ready()
        write(self.rfile("review-code.md"), "VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: nit\n")
        code, res = self.result_with(rid, self.traced_as(self.block(1, "implemented"), self.block(2, "implemented")))
        self.assertEqual(code, 0, res)
        _, d = self.review("draft", rid, "--select", "1")
        self.assertNotIn("Logic traced", d["comment"])
        self.assertNotIn("normal: 3 rows", d["comment"])

    def test_a_finding_after_the_trace_heading_is_not_lost(self):
        rid = self.ready()
        code, res = self.result_with(rid, self.traced_as(
            self.block(1, "implemented"), self.block(2, "different"), verdict="CHANGES",
            findings="1. [blocking] app/new.py:2: counts unpaid invoices too Repro: a. Accept: b.\n"))
        self.assertEqual((code, res["next"]), (0, "verify"), res)
        self.assertIn("counts unpaid invoices too", slurp(self.rfile("blocking.md")))

    def test_no_requirement_needs_no_trace(self):
        rid = self.ready()
        write(self.rfile("checklist.md"), "CHECKLIST: READY\n\n## Requirements\n\n## Open questions\n1. all of it\n")
        code, res = self.result_with(rid, "VERDICT: PASS\n\n## Findings\n")
        self.assertEqual(code, 0, res)

    def test_the_reviewer_of_logic_uses_the_stronger_model(self):
        rid, _ = self.scanned()
        _, res = self.review("ctx", rid, "--agent", "requirement")
        self.assertEqual(res["model"], "opus")

    def test_the_agents_are_told_to_follow_the_logic(self):
        req = slurp(os.path.join(AGENTS, "factory-review-requirement.md"))
        for word in ("## Logic traced", "concrete values", "git show <base>:<path>", "Around it"):
            self.assertIn(word, req)
        code = slurp(os.path.join(AGENTS, "factory-reviewer-code.md"))
        self.assertIn("Logic is the heart of this mode", code)
        self.assertIn("The check of callers", code)


class ReviewLearningTest(ReviewCase):
    NO_STATS = {"repeat_offenders": [], "maybe_obsolete": [], "graduate": [], "over_limit": False}
    FINDINGS = "VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: kept\n2. [note] app/new.py:3: not wanted\n"

    def setUp(self):
        super().setUp()
        self.routes["acme%2Fapp/merge_requests?state=opened"] = [self.mr()]
        self.routes["merge_requests/7/commits"] = [{"id": self.head, "title": "work", "created_at": "2020-01-01T00:00:00Z"}]
        self.save_routes()

    def collect(self):
        with mock.patch.object(fx, "learning_stats", return_value=self.NO_STATS):
            code, res = self.fx("lessons", "collect")
        self.assertEqual(code, 0, res)
        return res

    def item(self, res=None):
        res = res or self.collect()
        return next((i for i in res["items"] if i["job"] == APP), None)

    def mark_read(self):
        with mock.patch.object(fx.sys, "stdin", mock.MagicMock(read=lambda: "{}")):
            code, res = self.fx("lessons", "update", "--jobs", APP)
        self.assertEqual(code, 0, res)

    def posted(self):
        rid, _ = self.decided(code=self.FINDINGS)
        _, d = self.review("draft", rid, "--select", "1")
        self.review("post", rid, "--confirm", d["confirm"])
        return rid

    def add_lessons(self, *rules):
        with mock.patch.object(fx.sys, "stdin", mock.MagicMock(read=lambda: json.dumps({"add": list(rules)}))):
            code, res = self.fx("lessons", "update")
        self.assertEqual(code, 0, res)

    def lessons_of(self, prompt):
        for line in prompt.splitlines():
            if line.startswith("Lessons from earlier jobs"):
                return slurp(line.split(": ", 1)[1])
        return ""

    def test_the_person_can_say_what_it_missed(self):
        rid = self.posted()
        code, res = self.review("missed", rid, "--text", "the total ignores refunds")
        self.assertEqual((code, res["review"]), (0, rid), res)
        got = self.item()["missed"]
        self.assertEqual((got[0]["text"], got[0]["head"]), ("the total ignores refunds", self.head))

    def test_saying_nothing_is_refused(self):
        rid = self.posted()
        code, res = self.review("missed", rid, "--text", "  ")
        self.assertEqual(code, 1)
        self.assertIn("--text", res["error"])

    def test_each_thing_is_read_once(self):
        rid = self.posted()
        self.review("missed", rid, "--text", "refunds")
        self.assertTrue(self.item())
        self.mark_read()
        self.assertIsNone(self.item())
        self.review("missed", rid, "--text", "rounding too")
        self.assertEqual([m["text"] for m in self.item()["missed"]], ["rounding too"])

    def test_what_was_posted_alone_is_nothing_to_draft_from(self):
        rid, _ = self.decided(code="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: kept\n")
        _, d = self.review("draft", rid, "--select", "1")
        self.review("post", rid, "--confirm", d["confirm"])
        self.assertIsNone(self.item())
        self.review("missed", rid, "--text", "refunds")
        self.assertEqual(self.item()["posted_findings"][0]["text"], "app/new.py:2: kept")

    def test_a_comment_after_the_review_is_read_and_one_before_is_not(self):
        rid = self.posted()
        self.routes["merge_requests/7/notes"] = [
            {"id": 1, "author": {"username": "lead"}, "body": "before", "system": False, "created_at": "2020-01-01T00:00:00Z"},
            {"id": 2, "author": {"username": "lead"}, "body": "the total ignores refunds", "system": False,
             "created_at": "2099-01-01T00:00:00Z", "position": {"new_path": "app/new.py", "new_line": 4}},
            {"id": 3, "author": {"username": "dev"}, "body": "pushed 1 commit", "system": True, "created_at": "2099-01-01T00:00:00Z"}]
        self.save_routes()
        fu = self.item()["followup"]
        self.assertEqual([(c["who"], c["where"], c["text"]) for c in fu["comments"]],
                         [("lead", "app/new.py:4", "the total ignores refunds")])
        self.assertEqual(fu["posted_findings"], ["app/new.py:2: kept"])
        self.mark_read()
        self.assertIsNone(self.item())

    def test_a_commit_after_the_review_is_read_with_its_change(self):
        rid = self.posted()
        self.routes["merge_requests/7/commits"] = [
            {"id": "newer", "title": "fix the total", "author_name": "dev", "created_at": "2099-01-01T00:00:00Z"},
            {"id": "merged", "title": "Merge branch main", "created_at": "2099-01-01T00:00:00Z"},
            {"id": self.head, "title": "work", "created_at": "2020-01-01T00:00:00Z"}]
        self.routes["repository/compare"] = {"diffs": [{"new_path": "app/new.py", "diff": "+fixed\n-broken\n"}]}
        self.save_routes()
        fu = self.item()["followup"]
        self.assertEqual([c["title"] for c in fu["commits"]], ["fix the total"])
        self.assertEqual((fu["files"], fu["lines_changed"]), (["app/new.py"], 2))
        self.assertIn("+fixed", slurp(fu["patch_file"]))

    def test_a_history_rewritten_after_the_review_goes_by_date(self):
        rid = self.posted()
        self.routes["merge_requests/7/commits"] = [
            {"id": "rewritten", "title": "all of it again", "created_at": "2099-01-01T00:00:00Z"},
            {"id": "old", "title": "from before", "created_at": "2020-01-01T00:00:00Z"}]
        self.save_routes()
        self.assertEqual([c["title"] for c in self.item()["followup"]["commits"]], ["all of it again"])

    def test_a_merged_merge_request_is_read_a_last_time(self):
        rid = self.posted()
        self.set_mr(state="merged")
        self.collect()
        self.mark_read()
        calls = len(self.glab_calls())
        self.assertTrue(self.job()["followup"]["final"])
        self.collect()
        self.assertEqual(len(self.glab_calls()), calls)

    def test_a_merge_request_gitlab_does_not_answer_for_is_said(self):
        rid = self.posted()
        del self.routes["merge_requests/7"]
        self.save_routes()
        res = self.collect()
        self.assertIn(rid, res["notes"][0])
        self.assertEqual(self.job().get("followup"), None)

    def test_only_reading_goes_to_gitlab(self):
        self.posted()
        self.collect()
        for call in self.glab_calls():
            self.assertEqual(call[0], "api", call)
            self.assertFalse({"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"} & set(call), call)

    def test_a_conflict_has_no_review_to_follow_up_on(self):
        self.set_mr(detailed_merge_status="conflict", has_conflicts=True)
        code, res = self.review("start", URL, "--ticket", "PROJ-9")
        self.review("close", res["review"])
        self.assertIsNone(self.job().get("reviewed_at"))
        self.collect()
        self.assertIsNone(self.job().get("followup"))

    def test_a_source_can_be_turned_off(self):
        rid = self.posted()
        self.review("missed", rid, "--text", "refunds")
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg.setdefault("learning", {})["sources"] = {"review_missed": False, "review_rejections": False,
                                                     "review_outcomes": False, "review_followup": False}
        write(path, json.dumps(cfg))
        self.assertIsNone(self.item())

    def test_what_the_verifier_disproved_is_kept(self):
        rid, _ = self.scanned()
        self.reviews(code="VERDICT: CHANGES\n\n## Findings\n1. [blocking] app/new.py:2: not a bug\n")
        self.review("result", rid)
        write(self.rfile("verify.md"), "VERDICT: PASS\n\n## Findings\n1. rejected: it is fine\n")
        code, res = self.review("result", rid)
        self.assertEqual(code, 0, res)
        self.assertTrue(self.job()["verifier_rejected"][0]["text"].startswith("app/new.py:2: not a bug"))
        self.assertEqual(self.item()["verifier_rejected"][0]["reviewer"], "code")

    def test_a_posted_finding_fixed_later_is_kept(self):
        rid = self.posted()
        self.head = self.push_mr({"app/more.py": "more\n"})
        self.set_mr()
        rid, _ = self.decided(2, requirement="VERDICT: PASS\n\n## Findings\n\n## Previous findings\n- fixed: kept\n- still open: other\n")
        self.assertEqual(self.job()["fixed_later"][0]["text"], "kept")
        self.assertEqual(self.job()["still_open"][0]["text"], "other")

    def test_the_count_per_reviewer_is_given(self):
        self.posted()
        stats = self.collect()["review_stats"]
        self.assertEqual(stats["by_reviewer"]["code"], {"posted": 1, "not_chosen": 1, "rejected_by_verifier": 0})

    def test_a_review_lesson_reaches_the_reviewers_and_no_builder(self):
        self.add_lessons("[review] do not raise X", "[code] use paginate", "[ui] use the grid")
        rid, _ = self.scanned()
        _, code = self.review("ctx", rid, "--agent", "code")
        self.assertIn("do not raise X", self.lessons_of(code["prompt"]))
        self.assertNotIn("use the grid", self.lessons_of(code["prompt"]))
        builder = fx.lessons_for(self.f.ctx(), "builder", self.job())
        self.assertNotIn("do not raise X", slurp(builder))

    def test_the_reviewer_of_requirements_reads_the_lessons_on_code_too(self):
        self.add_lessons("[review] do not raise X", "[code] use paginate", "[ui] use the grid")
        rid, _ = self.scanned()
        _, req = self.review("ctx", rid, "--agent", "requirement")
        text = self.lessons_of(req["prompt"])
        for rule in ("do not raise X", "use paginate", "use the grid"):
            self.assertIn(rule, text)

    def test_the_checklist_is_not_told_how_to_review(self):
        self.add_lessons("[review] do not raise X", "[spec] say which month")
        self.started()
        _, ck = self.review("ctx", "_reviews/PROJ-9-mr7", "--agent", "checklist")
        text = self.lessons_of(ck["prompt"])
        self.assertNotIn("do not raise X", text)
        self.assertIn("say which month", text)

    def test_a_review_lesson_does_not_move_into_the_knowledge(self):
        self.add_lessons("[review] do not raise X", "[code] use paginate")
        write(os.path.join(self.f.factory, "knowledge", "style.md"), "style\n")
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg.setdefault("learning", {})["graduate_after_jobs"] = 1
        write(path, json.dumps(cfg))
        finished = os.path.join(self.f.factory, "jobs", "T-1", "job.json")
        job = load_json(finished)
        job.update(stage="handed-off", created_at="2099-01-01T00:00:00")  # one job since the lessons were added
        write(finished, json.dumps(job))
        code, res = self.fx("lessons", "stats")
        self.assertEqual(code, 0, res)
        texts = {l["id"]: l["text"] for l in res["lessons"]}
        self.assertFalse([g for g in res["graduate"] if "do not raise" in texts[g["id"]]])
        self.assertTrue([g for g in res["graduate"] if "paginate" in texts[g["id"]]])


class TicketLearningTest(SeveralReposCase):
    def test_what_it_missed_on_a_ticket_goes_on_one_review(self):
        self.decided_all()
        self.review("close", "PROJ-9")
        code, res = self.review("missed", "PROJ-9", "--text", "the two sides round differently")
        self.assertEqual((code, res["review"]), (0, APP), res)
        self.assertEqual(self.job_of(APP)["missed"][0]["text"], "the two sides round differently")

    def test_each_merge_request_of_a_ticket_is_followed_up(self):
        self.decided_all(app="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: a\n")
        _, d = self.review("draft", "PROJ-9", "--select", "app:1")
        self.review("post", "PROJ-9", "--confirm", d["confirm"])
        self.routes["acme%2Ffe/merge_requests/3/notes"] = [
            {"id": 9, "author": {"username": "lead"}, "body": "the button is wrong", "system": False,
             "created_at": "2099-01-01T00:00:00Z"}]
        self.routes["acme%2Ffe/merge_requests/3/commits"] = []
        self.routes["acme%2Fapp/merge_requests/7/commits"] = []
        self.save_routes()
        with mock.patch.object(fx, "learning_stats", return_value=ReviewLearningTest.NO_STATS):
            code, res = self.fx("lessons", "collect")
        self.assertEqual(code, 0, res)
        item = next(i for i in res["items"] if i["job"] == FE)
        self.assertEqual(item["followup"]["comments"][0]["text"], "the button is wrong")
        self.assertEqual(item["repo"], "fe")

    def test_a_fit_finding_counts_too(self):
        self.decided_all(fit="FIT: CHANGES\n\n## Repo: fe\n1. kept\n2. not wanted\n")
        _, d = self.review("draft", "PROJ-9", "--select", "fit:1")
        self.review("post", "PROJ-9", "--confirm", d["confirm"])
        self.assertEqual(self.job_of(APP)["posted_findings"][0]["reviewer"], "fit")
        self.assertEqual([f["text"] for f in self.job_of(APP)["false_positives"]], ["not wanted"])


class ReviewInStatusTest(SeveralReposCase):
    def test_an_open_review_is_in_the_status_and_a_closed_one_is_not(self):
        self.scanned_all()
        code, res = self.fx("status")
        self.assertEqual(code, 0, res)
        self.assertEqual({(r["review"], r["repo"], r["stage"], r["together"]) for r in res["reviews"]},
                         {(APP, "app", "review", True), (FE, "fe", "review", True)})
        self.assertFalse([j for j in res["jobs"] if "_reviews" in j["job"]])
        for rid in (APP, FE):
            self.reviewed(rid)
        self.fit()
        self.review("close", "PROJ-9")
        self.assertNotIn("reviews", self.fx("status")[1])

    def test_jira_work_left_keeps_it_in_the_status(self):
        self.decided_all()
        _, d = self.review("draft", "PROJ-9", "--clean")
        self.review("post", "PROJ-9", "--confirm", d["confirm"])
        rows = self.fx("status")[1]["reviews"]
        self.assertEqual([(r["review"], r["stage"], r["jira_pending"]) for r in rows], [(APP, "done", 1)])


class ResumeReviewTest(SeveralReposCase):
    def alone(self):
        self.routes["acme%2Ffe/merge_requests?state=opened"] = []
        self.save_routes()

    def resume(self, ref="PROJ-9"):
        code, res = self.review("resume", ref)
        self.assertEqual(code, 0, res)
        return res

    def test_nothing_open_is_said(self):
        code, res = self.review("resume", "PROJ-9")
        self.assertEqual(code, 3)
        self.assertIn("no unfinished review", res["error"])

    def test_one_merge_request_step_by_step(self):
        self.alone()
        rid = self.started()
        res = self.resume()
        self.assertEqual((res["review"], res["next"]), (rid, "checklist"))
        write(self.rfile("checklist.md"), "CHECKLIST: READY\n\n## Requirements\n1. CSV export\n")
        self.assertEqual(self.resume()["next"], "scan")
        self.review("scan", rid)
        self.assertEqual(self.resume()["next"], "sandbox")
        self.no_sandbox(rid)
        res = self.resume()
        self.assertEqual((res["next"], res["missing"]), ("review", ["requirement", "security", "code"]))
        write(self.rfile("review-code.md"), PASS)
        self.assertEqual(self.resume()["missing"], ["requirement", "security"])
        write(self.rfile("review-security.md"), PASS)
        write(self.rfile("review-requirement.md"), traced("requirement", PASS))
        self.assertEqual(self.resume()["next"], "result")

    def test_the_id_works_as_well_as_the_key(self):
        self.alone()
        rid = self.started()
        self.assertEqual(self.resume(rid)["review"], rid)

    def test_a_clone_that_is_gone_starts_the_round_over(self):
        self.alone()
        rid, _ = self.scanned()
        shutil.rmtree(self.job()["worktree"])
        res = self.resume()
        self.assertEqual(res["next"], "again")
        self.assertIn("--again", res["then"])
        self.assertIn(URL, res["then"])

    def test_deciding_gets_the_findings_back(self):
        self.alone()
        rid, _ = self.decided(code="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: kept\n")
        res = self.resume()
        self.assertEqual((res["next"], res["findings"][0]["text"]), ("decide", "app/new.py:2: kept"))
        self.assertTrue(os.path.isfile(res["report"]))

    def test_a_draft_is_shown_again_and_writes_nothing(self):
        self.alone()
        rid, _ = self.decided(code="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: kept\n")
        _, d = self.review("draft", rid, "--select", "1")
        res = self.resume()
        self.assertEqual((res["next"], res["comment"], res["confirm"]), ("confirm", d["comment"], d["confirm"]))
        self.assertFalse(self.job().get("jira_todo"))

    def test_jira_work_left_after_the_end(self):
        self.alone()
        rid, _ = self.decided()
        _, d = self.review("draft", rid, "--clean")
        self.review("post", rid, "--confirm", d["confirm"])
        res = self.resume()
        self.assertEqual((res["next"], len(res["jira_todo"])), ("jira", 1))

    def test_a_ticket_step_by_step(self):
        res = self.together()
        got = self.resume()
        self.assertEqual((got["ticket"], got["next"], got["at"]), ("PROJ-9", "checklist", APP))
        write(self.rfile_of(res["lead"], "checklist.md"), "CHECKLIST: READY\n\n## Requirements\n1. a\n")
        self.assertEqual(self.resume()["next"], "scan")
        self.review("scan", APP)
        got = self.resume()
        self.assertEqual((got["next"], got["at"]), ("sandbox", APP))
        self.review("scan", FE)
        self.no_sandbox(APP)
        self.no_sandbox(FE)
        self.reviewed(APP)
        got = self.resume()
        self.assertEqual((got["next"], got["at"]), ("review", FE))
        self.reviewed(FE)
        self.assertEqual(self.resume()["next"], "fit")
        _, ask = self.review("fit", "PROJ-9")
        self.assertEqual(self.resume()["next"], "fit")
        write(ask["fit_file"], "FIT: PASS\n\n## Checked\nx\n")
        self.assertEqual(self.resume()["next"], "fit-result")
        self.review("fit-result", "PROJ-9")
        got = self.resume()
        self.assertEqual((got["next"], got["fit"]["result"]), ("decide", "PASS"))

    def test_a_ticket_whose_clones_are_gone_starts_over_with_every_link(self):
        self.scanned_all()
        self.reviewed(APP)
        self.reviewed(FE)
        shutil.rmtree(self.job_of(FE)["worktree"])
        got = self.resume()
        self.assertEqual(got["next"], "again")
        self.assertIn(URL_FE, got["then"])
        self.assertIn(URL, got["then"])

    def test_a_ticket_draft_and_conflict(self):
        self.decided_all(app="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: a\n")
        _, d = self.review("draft", "PROJ-9", "--select", "app:1")
        got = self.resume()
        self.assertEqual((got["next"], got["confirm"], got["comment"]), ("confirm", d["confirm"], d["comment"]))
        self.review("close", "PROJ-9")
        self.set_fe(detailed_merge_status="conflict", has_conflicts=True)
        self.together()
        got = self.resume()
        self.assertEqual((got["next"], got["conflicts"][0]["repo"]), ("ask-conflict", "fe"))

    def test_a_ticket_draft_says_whether_it_can_be_merged(self):
        self.decided_all(app="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: a\n")
        code, d = self.review("draft", "PROJ-9", "--select", "app:1", "--later", "app:1", "--later-ticket", "later")
        self.assertEqual((code, d["merge"], d["later"]), (0, True, ["app:1"]), d)
        self.assertIn("Verdict: can be merged", d["comment"])
        self.assertIn("[can wait: follow-up ticket]", d["comment"])
        self.assertIn("app/new.py:2: a", d["tickets"][0]["markdown"])
        code, d = self.review("draft", "PROJ-9", "--select", "app:1")
        self.assertEqual((code, d["merge"]), (0, False), d)
        self.assertIn("Verdict: not ready to merge", d["comment"])

    def test_a_ticket_draft_carries_its_summary_through_a_resume(self):
        self.decided_all(app="VERDICT: PASS\n\n## Findings\n1. [note] app/new.py:2: a\n")
        path = os.path.join(self.f.factory, "summary.md")
        write(path, "People read this first.")
        code, d = self.review("draft", "PROJ-9", "--select", "app:1", "--summary-file", path)
        self.assertEqual(code, 0, d)
        self.assertTrue(d["comment"].startswith("## Summary\n\nPeople read this first."), d["comment"])
        self.assertFalse([n for n in d["notes"] if "no summary" in n])
        self.assertEqual(self.resume()["comment"], d["comment"])
        _, d = self.review("draft", "PROJ-9", "--select", "app:1")
        self.assertTrue([n for n in d["notes"] if "no summary" in n])

    def test_several_open_reviews_of_one_key_are_asked(self):
        self.routes["acme%2Fapp/merge_requests?state=opened"] = []
        self.routes["acme%2Ffe/merge_requests?state=opened"] = []
        self.save_routes()
        self.review("start", URL, "--ticket", "PROJ-9", "--issue-file", self.issue_file())
        self.review("start", URL_FE, "--ticket", "PROJ-9", "--issue-file", self.issue_file())
        got = self.resume()
        self.assertEqual((got["next"], {c["review"] for c in got["candidates"]}), ("ask-review", {APP, FE}))


class ReviewPushedAtTheEndTest(ReviewCase):
    def test_nothing_goes_unless_asked_for(self):
        self.routes["acme%2Fapp/merge_requests?state=opened"] = [self.mr()]
        self.save_routes()
        rid, _ = self.decided()
        with mock.patch.object(fx, "jobs_after_step") as pushed:
            self.review("close", rid)
        pushed.assert_not_called()

    def test_asked_for_it_goes_when_it_ends_and_again_after_the_jira_work(self):
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["storage"] = {"type": "s3", "bucket": "b", "region": "r", "endpoint": "http://127.0.0.1:1", "prefix": "p"}
        cfg["jobs_store"] = {"include_reviews": True}
        write(path, json.dumps(cfg))
        rid, _ = self.decided()
        _, d = self.review("draft", rid, "--clean")
        with mock.patch.object(fx, "jobs_after_step") as pushed:
            code, res = self.review("post", rid, "--confirm", d["confirm"])
            self.assertEqual(code, 0, res)
            self.assertEqual(pushed.call_args[0][2], [rid])
            ids = [t["id"] for t in res["jira_todo"]]
            pushed.reset_mock()
            code, res = self.fx("jira-done", rid, *ids)
            self.assertEqual(code, 0, res)
            self.assertEqual(pushed.call_args[0][2], [rid])
