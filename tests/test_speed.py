"""A job that takes less time without leaving out a step, and without taking more of the machine.

Standard library only. Run from the repository root:
    python3 -I -m unittest discover -s tests

What is tested here: the checks script runs once on a commit (fx checks, and the gate, fx pr and the candidates' check
find that pass); a round that failed at the security scan has its ZAP reports judged again once a human accepted
findings; a round that failed for something other than the code does not count (rounds given back, a run stopped from
outside); a package's heavy command takes its own place on the machine; how long a step waited for room is kept; and
a job that does not hold a lane reads the lane's folders as committed, from its own copy.

No Docker anywhere: the machine is the JSON file the other tests use (FACTORY_FAKE_MACHINE), ZAP is the stub docker of
test_security, and what Docker would say about a container stopped from outside is a file the test writes.
"""

import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import unittest
from unittest import mock

from test_baselines import A, B, C, CHECKS as BASELINE_CHECKS, DIR, MANIFEST, BaselinesCase, ProjectRepoCase, entries
from test_parallel import FOUR, PLAN, BoxCase, WORKER, load
from test_scan_coverage import zap_report
from test_security import FX_PATH, FxTestCase, fx, load_json, sh, slurp, write, prompt_text

COUNTING = '''echo ran >> "$FX_TEST_OUT/checks.ran"
env > "$FX_TEST_OUT/checks.env"
if [ -e "$FX_TEST_OUT/fail" ]; then echo "a test failed"; exit 1; fi
echo "every test passed"
exit 0
'''


def ago(hours):
    return fx.iso_of(time.time() - hours * 3600)


class ChecksCase(FxTestCase):
    """One job (T-1) with a commit on its branch, and a checks script that says how often it ran."""
    script = COUNTING

    def setUp(self):
        super().setUp()
        write(os.path.join(self.f.factory, "checks.sh"), self.script)
        self.f.commit({"app.py": "x = 1\n"})

    def times_run(self):
        path = os.path.join(self.f.out, "checks.ran")
        return len(slurp(path).split()) if os.path.exists(path) else 0

    def fail_next(self, yes=True):
        path = os.path.join(self.f.out, "fail")
        if yes:
            write(path, "")
        elif os.path.exists(path):
            os.remove(path)

    def checks(self, *argv):
        return self.fx("checks", "T-1", *argv)

    def configure(self, **changes):
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg.update(changes)
        write(path, json.dumps(cfg, indent=1))

    def runs(self):
        return self.f.job().get("checks_runs") or []

    def head(self):
        return sh(["git", "rev-parse", "HEAD"], self.f.wt).strip()


# --------------------------------------------------------------------------
# 1. the checks script runs once on a commit

class OneChecksRunTest(ChecksCase):
    def test_the_gate_does_not_run_the_script_again_on_the_commit_the_builder_checked(self):
        code, res = self.checks()
        self.assertEqual((code, res["checks"], res["kept_for_the_gate"], res["reused"]), (0, "PASS", True, False), res)
        self.assertEqual(self.times_run(), 1)
        self.assertIn("every test passed", slurp(res["log"]))  # the log is kept in the job folder
        self.assertTrue(res["log"].startswith(os.path.join(self.f.jd, "checks") + os.sep), res["log"])
        row = self.runs()[-1]
        self.assertEqual((row["commit"], row["result"], row["by"], row["reusable"]), (self.head(), "PASS", "builder", True), row)
        self.assertEqual(len(row["fingerprint"]), 64)
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(self.times_run(), 1)  # the same script on the same bytes was not run a second time
        said = res["checks"]
        self.assertEqual((said["reused"], said["run"], said["commit"], said["by"]), (True, row["id"], self.head(), "builder"), said)
        self.assertEqual(said["at"], row["at"])
        for text in (slurp(os.path.join(self.f.jd, "round-1", "checks.log")), self.checks_md(), " ".join(res["notes"])):
            self.assertIn("was not run again", text)
            self.assertIn(row["id"], text)
            self.assertIn(self.head()[:12], text)
        self.assertEqual(self.f.job()["checks_passed_at"], self.head())

    def test_the_environment_the_script_runs_in_counts(self):
        """A builder's run with the slow suite left out is no run of the gate's checks."""
        with mock.patch.dict(os.environ, {"SKIP_SLOW": "1"}):
            code, res = self.checks()
        self.assertEqual((code, res["kept_for_the_gate"]), (0, True), res)
        code, res = self.gate()
        self.assertEqual((res["gate"], res["checks"]["reused"], self.times_run()), ("PASS", False, 2), res)
        self.assertIn("the environment the script runs in (not set now: SKIP_SLOW)", res["checks"]["not_reused_because"])

    def test_a_changed_value_is_named_and_a_secret_never(self):
        with mock.patch.dict(os.environ, {"APP_ENV": "staging", "DB_PASSWORD": "s3cret-1"}):
            self.checks()
        with mock.patch.dict(os.environ, {"APP_ENV": "testing", "DB_PASSWORD": "s3cret-2"}):
            code, res = self.gate()
        self.assertEqual((res["gate"], res["checks"]["reused"], self.times_run()), ("PASS", False, 2), res)
        why = res["checks"]["not_reused_because"]
        self.assertIn("changed: APP_ENV", why)
        self.assertIn("a variable whose name looks secret", why)  # named never, its value even less
        self.assertNotIn("s3cret", json.dumps(self.f.job()))

    def test_what_only_says_which_terminal_ran_it_does_not(self):
        with mock.patch.dict(os.environ, {"TERM_SESSION_ID": "w0t0p0:1", "CLAUDE_CODE_ENTRYPOINT": "sdk-ts",
                                          "VSCODE_PID": "4242", "FACTORY_MAX_WAIT": "0"}):
            self.checks()
        code, res = self.gate()
        self.assertEqual((res["gate"], res["checks"]["reused"], self.times_run()), ("PASS", True, 1), res)

    def test_every_other_step_of_the_gate_still_runs(self):
        self.checks()
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        tools = {c["tool"] for c in self.f.calls()}
        self.assertTrue({"gitleaks", "semgrep", "osv-scanner"} <= tools, tools)  # the scanners ran in the round itself

    def test_another_commit_is_checked_again(self):
        self.checks()
        self.f.commit({"app.py": "x = 2\n"})
        code, res = self.gate()
        self.assertEqual((res["gate"], res["checks"]["reused"], self.times_run()), ("PASS", False, 2), res)
        self.assertEqual([r["by"] for r in self.runs()], ["builder", "gate"])
        self.assertEqual(self.runs()[-1]["round"], 1)

    def test_a_failure_is_never_used_again(self):
        self.fail_next()
        code, res = self.checks()
        self.assertEqual((code, res["ok"], res["checks"], res["exit"]), (1, False, "FAIL", 1), res)
        self.assertIn("a test failed", "\n".join(res["tail"]))
        self.assertFalse(self.runs()[-1]["reusable"])
        code, res = self.gate()
        self.assertEqual((res["gate"], res["checks"]["reused"], self.times_run()), ("FAIL", False, 2), res)
        self.fail_next(False)
        code, res = self.gate()  # and neither is the gate's own failure
        self.assertEqual((res["gate"], res["checks"]["reused"], self.times_run()), ("PASS", False, 3), res)

    def test_a_builder_runs_the_whole_script_once_and_what_failed_alone(self):
        """A builder ran the whole suite twice (about 40 minutes) to learn whether one test it fixed passed."""
        code, res = self.fx("ctx", "T-1", "--agent", "builder")
        text = prompt_text(res)
        self.assertIn("Commit first, then run it once, at the end", text)
        self.assertIn("never the whole script to see one fix", text)
        self.fail_next()
        code, res = self.checks()
        self.assertEqual((code, res["checks"]), (1, "FAIL"), res)
        self.assertIn("read every failure in the log first", res["next"])
        self.assertIn("run the failed tests alone (`%s run --job T-1 -- " % fx.FX_CMD, res["next"])
        builder = slurp(os.path.join(os.path.dirname(os.path.dirname(FX_PATH)), "..", "agents", "factory-builder.md"))
        self.assertIn("never run it to learn whether one\n   fix worked", builder)
        self.assertIn("run that test (or its file) alone", builder)

    def test_a_later_failure_on_the_same_commit_outvotes_the_pass(self):
        """The same commit, the same inputs, and the newest run failed (a test that fails now and then): the gate finds
        out for itself."""
        self.checks()
        self.fail_next()
        code, res = self.checks("--again")
        self.assertEqual((code, res["checks"]), (1, "FAIL"), res)
        self.fail_next(False)
        code, res = self.gate()
        self.assertEqual((res["gate"], res["checks"]["reused"], self.times_run()), ("PASS", False, 3), res)
        self.assertIn("did not pass on this same commit", res["checks"]["not_reused_because"])

    def test_a_failure_on_uncommitted_changes_says_nothing_about_the_commit(self):
        self.checks()
        write(os.path.join(self.f.wt, "app.py"), "x = 'an experiment'\n")
        self.fail_next()
        self.assertEqual(self.checks()[1]["checks"], "FAIL")
        self.fail_next(False)
        sh(["git", "checkout", "-q", "--", "app.py"], self.f.wt)  # the experiment is thrown away
        code, res = self.gate()
        self.assertEqual((res["gate"], res["checks"]["reused"], self.times_run()), ("PASS", True, 2), res)

    def test_a_run_on_uncommitted_changes_is_not_kept_for_the_gate(self):
        write(os.path.join(self.f.wt, "app.py"), "x = 'not committed yet'\n")
        code, res = self.checks()
        self.assertEqual((code, res["checks"], res["kept_for_the_gate"]), (0, "PASS", False), res)
        self.assertIn("uncommitted", res["note"])
        self.assertEqual(res["next"], "commit, then run this again")
        sh(["git", "commit", "-q", "-am", "now it is"], self.f.wt)
        code, res = self.gate()
        self.assertEqual((res["gate"], res["checks"]["reused"], self.times_run()), ("PASS", False, 2), res)

    def test_asked_again_on_a_commit_that_passed_it_answers_at_once(self):
        first = self.checks()[1]
        code, res = self.checks()
        self.assertEqual((code, res["checks"], res["reused"], res["run"], self.times_run()), (0, "PASS", True, first["run"], 1), res)
        code, res = self.checks("--again")
        self.assertEqual((code, res["reused"], self.times_run()), (0, False, 2), res)

    def test_the_checks_script_itself_counts(self):
        self.checks()
        write(os.path.join(self.f.factory, "checks.sh"), COUNTING + "# one more check\n")
        code, res = self.gate()
        self.assertEqual((res["checks"]["reused"], self.times_run()), (False, 2), res)
        self.assertIn("the checks script", res["checks"]["not_reused_because"])

    def test_so_does_every_file_beside_it_that_the_checks_could_read(self):
        """The folders that do the checking are in the factory repository and are no gate setting: a test suite, the
        scripts of a stack. Committed or not, a change there is a change to what the checks do."""
        write(os.path.join(self.f.factory, "stack", "lib.sh"), "run_tests() { true; }\n")
        self.checks()
        write(os.path.join(self.f.factory, "stack", "lib.sh"), "run_tests() { false; }\n")
        code, res = self.gate()
        self.assertEqual((res["checks"]["reused"], self.times_run()), (False, 2), res)
        self.assertIn("files of the factory repository", res["checks"]["not_reused_because"])

    def test_what_fx_itself_owns_there_does_not(self):
        for rel in ("knowledge/overview.md", "logic/billing/rules.md", "lessons.md", "jobs/OTHER-1/build.md",
                    "overrides/OTHER-1.json", ".claude/settings.local.json"):
            write(os.path.join(self.f.factory, rel), "{}\n")
        self.checks()
        for rel in ("knowledge/overview.md", "logic/billing/rules.md", "lessons.md", "jobs/OTHER-1/build.md",
                    "overrides/OTHER-1.json", ".claude/settings.local.json"):
            write(os.path.join(self.f.factory, rel), '{"changed": true}\n')
        code, res = self.gate()
        self.assertEqual((res["gate"], res["checks"]["reused"], self.times_run()), ("PASS", True, 1), res)

    def test_a_setting_counts_unless_it_only_judges_findings(self):
        self.configure(security={"required": [], "block_severity": "medium", "accepted_rules": []})
        self.checks()
        self.configure(security={"required": [], "block_severity": "high",
                                 "accepted_rules": [{"rule": "zap-40018", "path": "/q", "reason": "a human looked"}]})
        self.assertEqual(self.checks()[1]["reused"], True)  # what a human accepts changes no test
        self.configure(gate_timeout_sec=900)
        code, res = self.gate()
        self.assertEqual((res["checks"]["reused"], self.times_run()), (False, 2), res)
        self.assertIn("factory.json", res["checks"]["not_reused_because"])

    def test_the_clones_local_files_count(self):
        """copy_into_worktree files (.env) are no part of any commit, and the checks read them."""
        self.configure(copy_into_worktree=[".env"])
        write(os.path.join(self.f.wt, ".env"), "DB=one\n")
        self.checks()
        write(os.path.join(self.f.wt, ".env"), "DB=two\n")
        code, res = self.gate()
        self.assertEqual((res["checks"]["reused"], self.times_run()), (False, 2), res)
        self.assertIn("local files", res["checks"]["not_reused_because"])

    def test_a_pass_is_used_only_for_so_long(self):
        self.checks()
        job = self.f.job()
        job["checks_runs"][-1]["at"] = ago(25)
        self.f.save_job(checks_runs=job["checks_runs"])
        code, res = self.checks()
        self.assertEqual((res["reused"], self.times_run()), (False, 2), res)
        job = self.f.job()
        for row in job["checks_runs"]:
            row["at"] = ago(25)
        self.f.save_job(checks_runs=job["checks_runs"])
        code, res = self.fx("reuse", "--max-age-hours", "48")
        self.assertEqual((code, res["reuse"]), (0, {"enabled": True, "max_age_hours": 48.0}), res)
        self.assertIn("commit factory.json", res["next"])
        self.assertEqual(load_json(os.path.join(self.f.factory, "factory.json"))["reuse"], {"enabled": True, "max_age_hours": 48.0})
        code, res = self.gate()  # the limit is no part of what the checks read: changing it costs no run
        self.assertEqual((res["gate"], res["checks"]["reused"], self.times_run()), ("PASS", True, 2), res)

    def test_it_can_be_turned_off_and_fx_writes_the_setting(self):
        self.checks()
        code, res = self.fx("reuse", "--off")
        self.assertEqual((code, res["reuse"]["enabled"]), (0, False), res)
        code, res = self.gate()
        self.assertEqual((res["gate"], res["checks"]["reused"], self.times_run()), ("PASS", False, 2), res)
        self.assertIn("turned off", res["checks"]["not_reused_because"])
        code, res = self.fx("reuse", "--on", "--job", "T-1")
        self.assertEqual((res["reuse"]["enabled"], [r["by"] for r in res["runs"]]), (True, ["builder", "gate"]), res)
        for bad in ({"enabled": "false"}, {"max_age_hours": 0}, "yes"):
            self.configure(reuse=bad)
            self.assertEqual(self.fx("reuse")[0], 3)

    def test_the_script_through_fx_run_is_told_to_use_fx_checks(self):
        code, res = self.fx("run", "--job", "T-1", "--", "bash", os.path.join(self.f.factory, "checks.sh"))
        self.assertEqual((code, res["next"], self.times_run()), (1, "checks", 0), res)
        self.assertIn("checks T-1", res["then"])
        os.chdir(self.f.wt)  # however the path is written, and inside a line for a shell too
        rel = os.path.relpath(os.path.join(self.f.factory, "checks.sh"), self.f.wt)
        for cmd in (["bash", rel], ["bash", "-c", "cd . && bash %s" % rel], ["sh", "-c", "exec %s" % rel]):
            code, res = self.fx("run", "--job", "T-1", "--", *cmd)
            self.assertEqual((code, self.times_run()), (1, 0), (cmd, res))

    def test_a_pass_is_kept_though_the_round_it_was_run_for_did_not_count(self):
        """The checks ran for half an hour and passed; then something outside the job made the round void. The gate is
        run again and finds that pass: the half hour is not spent twice."""
        self.configure(app={"up": "sleep 30", "start_timeout_sec": 2})
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen(1)
            self.f.save_job(port=other.getsockname()[1])
            code, res = self.gate()
        self.assertEqual((code, res.get("void"), self.times_run()), (3, True, 1), res)
        self.assertEqual((self.f.job()["round"], [r["by"] for r in self.runs()]), (0, ["gate"]))
        code, res = self.gate()
        self.assertEqual((res["checks"]["reused"], self.times_run()), (True, 1), res)

    def test_fx_pr_checks_what_it_pushes_and_finds_a_pass_of_that_very_commit(self):
        self.checks()
        self.f.save_job(stage="pr", gate="PASS")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual((code, self.times_run()), (0, 1), res)
        self.assertTrue(any("was not run again" in n for n in res["notes"]), res["notes"])
        self.assertIn("was not run again", slurp(os.path.join(self.f.jd, "final-checks.log")))

    def test_fx_pr_runs_them_on_a_commit_nothing_passed_on(self):
        other = os.path.join(self.tmp, "teammate")
        sh(["git", "clone", "-q", self.f.origin, other], self.tmp)
        write(os.path.join(other, "theirs.txt"), "x\n")
        sh(["git", "add", "-A"], other)
        sh(["git", "commit", "-q", "-m", "theirs"], other)
        sh(["git", "push", "-q", "origin", "main"], other)
        self.assertEqual(self.gate()[1]["gate"], "PASS")
        self.f.save_job(stage="pr")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")  # merging main made another commit than the gate passed on
        self.assertEqual((code, self.times_run()), (0, 2), res)
        self.assertEqual([r["by"] for r in self.runs()], ["gate", "pr"])
        self.assertTrue(any("checks still pass" in n for n in res["notes"]), res["notes"])

    def test_a_job_made_by_1_8_runs_as_before(self):
        """Its job.json has none of what 1.9 keeps: nothing to find, so every step runs, and what it had is still read."""
        job = self.f.save_job(round=1, round_start=0, gate="FAIL", checks_passed_at=None,
                              steps=[{"step": "gate", "round": 0, "started": ago(2), "ended": ago(1.5), "result": "FAIL"}])
        self.assertFalse({"checks_runs", "checks_seq", "gate_rounds", "waits", "rounds_returned", "outside_stops"} & set(job))
        self.assertEqual((fx.rounds_used(job), fx.rounds_left(job), fx.waits_of(job)), (1, 2, None))
        code, res = self.gate()
        self.assertEqual((res["gate"], res["round"], res["checks"]["reused"], self.times_run(), res["rounds_left"]),
                         ("PASS", 2, False, 1, 1), res)
        self.assertNotIn("rounds_given_back", res)  # nobody knows what its first round ran on
        self.assertEqual(self.fx("status")[1]["jobs"][0]["round"], "2/3")
        view = fx.job_view(self.f.factory, "proj", self.f.job(), {}, {})
        self.assertEqual((view["round"], [s["kind"] for s in view["segments"]]), (2, ["step", "step"]))


# --------------------------------------------------------------------------
# 2. a human accepted a finding: the reports are judged again, the app is not scanned again

class ZapJudgedAgainTest(ChecksCase):
    cfg = {"app": {"up": "true"}, "security": {"required": [], "scanners": {"headers": False}, "zap_confirm": False}}
    SELFTEST = "sha256:selftest"
    SQLI = zap_report(("40018", "3", "SQL Injection", ["http://127.0.0.1:5100/q"]))
    BOTH = zap_report(("40018", "3", "SQL Injection", ["http://127.0.0.1:5100/q"]),
                      ("40012", "3", "Cross Site Scripting (Reflected)", ["http://127.0.0.1:5100/x"]))
    ACCEPT = {"rule": "zap-40018", "path": "/q", "reason": "the test input comes back, no SQL is run: a human looked"}

    def setUp(self):
        super().setUp()
        with mock.patch.dict(os.environ, STUB_ZAP_IMAGE_ID=self.SELFTEST):
            write(os.path.join(self.f.factory, "baseline.json"), json.dumps({
                "security": {"zap": []}, "security_scan": {"zap": fx.scan_signature(self.f.ctx(), "zap")}}))

    def gate_with_app(self, report=None):
        env = {"STUB_ZAP_REPORT": report or self.SQLI, "STUB_ZAP_IMAGE_ID": self.SELFTEST}
        with mock.patch.dict(os.environ, env), mock.patch.object(fx, "app_start", return_value={"url": "http://127.0.0.1:5100/"}), \
                mock.patch.object(fx, "app_stop"), mock.patch.object(fx, "docker_running", return_value=True):
            return self.gate()[1]

    def scans(self):
        return len([c for c in self.f.calls("docker") if c["argv"][0] == "run"])

    def accept(self, *rules):
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["security"]["accepted_rules"] = list(rules)
        write(path, json.dumps(cfg, indent=1))

    def failed_at_the_scan(self, report=None):
        res = self.gate_with_app(report)
        self.assertEqual((res["gate"], res["rounds_left"], self.scans(), self.times_run()), ("FAIL", 2, 1, 1), self.checks_md())
        self.assertIn("security scan: ", self.checks_md())
        return res

    def test_the_next_round_judges_the_reports_it_has_and_scans_nothing(self):
        self.failed_at_the_scan()
        self.accept(self.ACCEPT)
        res = self.gate_with_app()
        self.assertEqual(res["gate"], "PASS", self.checks_md(2))
        self.assertEqual((self.scans(), self.times_run()), (1, 1))  # neither the scan nor the checks ran a second time
        self.assertEqual(res["checks"]["reused"], True)
        self.assertEqual((res["zap"]["scanned_again"], res["zap"]["judged_again_from_round"]), (False, 1), res)
        line = [n for n in res["notes"] if n.startswith("zap: not scanned again")]
        self.assertTrue(line and "Round 1" in line[0] and "1 of 1 finding(s) accepted by a human" in line[0], res["notes"])
        rdir = os.path.join(self.f.jd, "round-2")
        self.assertIn(line[0], slurp(os.path.join(rdir, "security.md")))
        self.assertIn("accepted: the test input comes back", slurp(os.path.join(rdir, "security.md")))
        self.assertEqual(slurp(os.path.join(rdir, "zap", "zap.json")), self.SQLI)  # the round holds what it was judged by
        self.assertEqual(load_json(os.path.join(rdir, "zap", "judged-again.json"))["reports_of_round"], 1)
        # every quick scanner ran in the round itself, as always
        self.assertEqual([len(self.f.calls(t)) >= 2 for t in ("gitleaks", "semgrep", "osv-scanner")], [True] * 3)

    def test_the_round_that_failed_for_it_is_given_back(self):
        self.failed_at_the_scan()
        self.accept(self.ACCEPT)
        res = self.gate_with_app()
        self.assertEqual((res["gate"], res["rounds_given_back"], res["rounds_left"]), ("PASS", [1], 2), res)
        job = self.f.job()
        self.assertEqual((job["round"], job["rounds_returned"], fx.rounds_used(job)), (2, 1, 1))
        self.assertTrue(any("round 1 failed on this same commit" in n for n in res["notes"]), res["notes"])

    def test_a_finding_accepted_for_this_job_only_does_the_same(self):
        """fx override writes overrides/<job>.json and commits it: the gate reads what is committed there."""
        fac = self.f.factory
        write(os.path.join(fac, ".gitignore"), "jobs/\nbaseline.json\n")
        sh(["git", "init", "-q", "-b", "main"], fac)
        sh(["git", "add", "-A"], fac)
        sh(["git", "commit", "-q", "-m", "factory"], fac)
        self.failed_at_the_scan()
        code, res = self.fx("override", "T-1", "--accept", "zap-40018", "--path", "/q", "--reason", "a human looked")
        code, res = self.fx("override", "T-1", "--accept", "zap-40018", "--path", "/q", "--reason", "a human looked",
                            "--approved", "Linh said yes on 15/11", "--confirm", res["confirm"])
        self.assertEqual((code, res["written"]), (0, True), res)
        res = self.gate_with_app()
        self.assertEqual((res["gate"], self.scans(), self.times_run()), ("PASS", 1, 1), self.checks_md(2))
        self.assertIn("a human looked (for this job only)", slurp(os.path.join(self.f.jd, "round-2", "security.md")))

    def test_nothing_accepted_since_is_a_scan_as_before(self):
        self.failed_at_the_scan()
        res = self.gate_with_app()
        self.assertEqual((res["gate"], self.scans()), ("FAIL", 2), self.checks_md(2))
        self.assertFalse(any("not scanned again" in n for n in res["notes"]), res["notes"])
        self.assertNotIn("zap", res)

    def test_a_finding_that_still_blocks_is_scanned_again(self):
        self.failed_at_the_scan(self.BOTH)
        self.accept(self.ACCEPT)
        res = self.gate_with_app(self.BOTH)
        self.assertEqual((res["gate"], self.scans()), ("FAIL", 2), self.checks_md(2))
        self.assertTrue(any("still hold 1 finding(s) that block" in n and "scanned again" in n for n in res["notes"]), res["notes"])
        self.assertFalse(os.path.exists(os.path.join(self.f.jd, "round-2", "zap", "judged-again.json")))

    def test_another_commit_is_scanned_again(self):
        self.failed_at_the_scan()
        self.accept(self.ACCEPT)
        self.f.commit({"app.py": "x = 2\n"})
        res = self.gate_with_app()
        self.assertEqual((res["gate"], self.scans(), self.times_run()), ("PASS", 2, 2), self.checks_md(2))

    def test_other_scan_settings_are_scanned_again(self):
        self.failed_at_the_scan()
        self.accept(self.ACCEPT)
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"]["zap_minutes"] = 5
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.assertEqual((self.gate_with_app()["gate"], self.scans()), ("PASS", 2), self.checks_md(2))

    def test_other_test_data_is_scanned_again(self):
        """The builder's seed is what the app holds while ZAP looks at it, and it is no part of the commit."""
        write(os.path.join(self.f.jd, "ui", "seed.sh"), "exit 0\n")
        with mock.patch.object(fx, "run_seed", return_value=True):
            self.failed_at_the_scan()
            self.accept(self.ACCEPT)
            write(os.path.join(self.f.jd, "ui", "seed.sh"), "echo other data; exit 0\n")
            self.assertEqual((self.gate_with_app()["gate"], self.scans()), ("PASS", 2), self.checks_md(2))

    def test_reports_that_are_gone_or_old_are_scanned_again(self):
        self.failed_at_the_scan()
        self.accept(self.ACCEPT)
        os.remove(os.path.join(self.f.jd, "round-1", "zap", "zap.json"))
        self.assertEqual((self.gate_with_app()["gate"], self.scans()), ("PASS", 2), self.checks_md(2))

    def test_reports_older_than_the_limit_are_scanned_again(self):
        self.failed_at_the_scan()
        self.accept(self.ACCEPT)
        rounds = self.f.job()["gate_rounds"]
        rounds[-1]["zap"]["scanned"] = ago(25)
        self.f.save_job(gate_rounds=rounds)
        self.assertEqual((self.gate_with_app()["gate"], self.scans()), ("PASS", 2), self.checks_md(2))

    def test_a_round_that_failed_at_something_else_as_well_is_scanned(self):
        self.fail_next()
        res = self.gate_with_app()
        self.assertEqual((res["gate"], self.scans()), ("FAIL", 0), self.checks_md())  # it stopped at the checks
        self.fail_next(False)
        self.accept(self.ACCEPT)
        self.assertEqual((self.gate_with_app()["gate"], self.scans()), ("PASS", 1), self.checks_md(2))

    def test_with_reuse_turned_off_it_scans(self):
        self.failed_at_the_scan()
        self.accept(self.ACCEPT)
        self.fx("reuse", "--off")
        self.assertEqual((self.gate_with_app()["gate"], self.scans(), self.times_run()), ("PASS", 2, 2), self.checks_md(2))

    def test_the_reports_judged_again_serve_the_round_after_too(self):
        """Round 2 judged round 1's reports and failed on what a quick scanner found; a human accepts that as well.
        Round 3 judges the same reports once more: they are still of this commit, and no older than they were."""
        # what the quick scanner says comes from a file, so that the checks script's environment stays the same
        canned = os.path.join(self.tmp, "semgrep-out.json")
        with mock.patch.dict(os.environ, STUB_SEMGREP_FILE=canned):
            self.failed_at_the_scan()
            self.accept(self.ACCEPT)
            write(canned, json.dumps({"errors": [], "results": [{"check_id": "sqli", "path": "app.py", "start": {"line": 1},
                                                                  "extra": {"severity": "ERROR", "message": "m", "metadata": {}}}]}))
            res = self.gate_with_app()
            self.assertEqual((res["gate"], self.scans(), res["rounds_left"]), ("FAIL", 1, 1), self.checks_md(2))
            self.accept(self.ACCEPT, {"rule": "sqli", "path": "app.py", "reason": "a constant, not input: a human looked"})
            res = self.gate_with_app()
        self.assertEqual((res["gate"], self.scans(), self.times_run()), ("PASS", 1, 1), self.checks_md(3))
        self.assertEqual((res["zap"]["judged_again_from_round"], res["rounds_given_back"], res["rounds_left"]), (1, [1, 2], 2), res)


# --------------------------------------------------------------------------
# 3a. rounds that needed no builder are given back

class RoundsGivenBackTest(ChecksCase):
    def test_a_pass_on_the_same_commit_gives_the_failed_rounds_back(self):
        self.fail_next()
        self.assertEqual(self.gate()[1]["rounds_left"], 2)
        self.assertEqual(self.gate()[1]["rounds_left"], 1)
        self.fail_next(False)  # nothing of the job changed: what failed had failed by itself
        code, res = self.gate()
        self.assertEqual((res["gate"], res["round"], res["rounds_given_back"], res["rounds_left"]), ("PASS", 3, [1, 2], 2), res)
        job = self.f.job()
        self.assertEqual(([r.get("given_back") for r in job["gate_rounds"]], job["rounds_returned"]), ([3, 3, None], 2))
        row = self.fx("status")[1]["jobs"][0]
        self.assertEqual((row["round"], row["rounds_given_back"]), ("1/3", 2), row)
        self.assertEqual(fx.job_view(self.f.factory, "proj", job, {}, {})["round"], 1)
        code, prompt = self.fx("ctx", "T-1", "--agent", "approver")  # round numbers, and their folders, stay what they were
        self.assertIn("Review round: 3 (folder round-3/", prompt_text(prompt))
        learn = [x["finding"] for x in fx.job_findings(self.f.ctx(), job) if x["reviewer"] == "gate"]
        self.assertEqual(len(learn), 2)  # and whoever learns from failed gates is told the code was not at fault
        self.assertTrue(all("This round was given back" in x for x in learn), learn)

    def test_a_round_the_builder_mended_with_a_commit_counts(self):
        self.fail_next()
        self.gate()
        self.fail_next(False)
        self.f.commit({"app.py": "x = 'mended'\n"})
        code, res = self.gate()
        self.assertEqual((res["gate"], res["rounds_left"], "rounds_given_back" in res), ("PASS", 1, False), res)

    def test_so_does_one_mended_in_the_job_folder(self):
        """The UI check, its seed and the staged logic files are the builder's work too, and in no commit."""
        self.fail_next()
        self.gate()
        self.fail_next(False)
        write(os.path.join(self.f.jd, "ui", "seed.sh"), "exit 0\n")
        code, res = self.gate()
        self.assertEqual((res["gate"], res["rounds_left"], "rounds_given_back" in res), ("PASS", 1, False), res)

    def test_so_does_one_mended_in_the_factory_repository(self):
        """A lane holder mends its QA test, or records a missing image, in the factory repository: no commit of the
        project, and still the builder's work."""
        write(os.path.join(self.f.factory, "checks.sh"), 'echo ran >> "$FX_TEST_OUT/checks.ran"\nbash "$(dirname "$0")/qa/e2e.sh"\n')
        write(os.path.join(self.f.factory, "qa", "e2e.sh"), "echo 'screenshot missing'; exit 1\n")
        self.assertEqual(self.gate()[1]["gate"], "FAIL")
        write(os.path.join(self.f.factory, "qa", "e2e.sh"), "echo ok; exit 0\n")
        code, res = self.gate()
        self.assertEqual((res["gate"], res["rounds_left"], "rounds_given_back" in res), ("PASS", 1, False), res)
        self.assertNotIn("given_back", self.f.job()["gate_rounds"][0])

    def test_so_does_one_that_failed_on_files_left_uncommitted(self):
        write(os.path.join(self.f.wt, "notes.txt"), "left behind\n")
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", res)
        os.remove(os.path.join(self.f.wt, "notes.txt"))  # a builder had to come and take it away
        code, res = self.gate()
        self.assertEqual((res["gate"], res["rounds_left"], "rounds_given_back" in res), ("PASS", 1, False), res)

    def test_failing_again_on_the_same_commit_still_runs_out_of_rounds(self):
        """Only a pass gives rounds back. Otherwise a job nothing ever changes could go round for ever."""
        self.fail_next()
        self.assertEqual([self.gate()[1]["next"] for _ in range(3)], ["build", "build", "pr"])
        self.assertEqual((self.f.job()["stage"], self.gate()[0]), ("pr", 1))

    def test_a_rework_starts_with_all_its_rounds(self):
        self.assertEqual(fx.rounds_used({"round": 5, "round_start": 2, "rounds_returned": 2}), 1)
        self.fail_next()
        self.gate()
        self.fail_next(False)
        self.gate()
        self.f.save_job(stage="handed-off")
        code, res = self.fx("rework", "T-1", "change the button")
        self.assertEqual(code, 0, res)
        job = self.f.job()
        self.assertEqual((job["round"], job["round_start"], job["rounds_returned"], fx.rounds_left(job)), (2, 2, 0, 3))


LEASE_KIND = '''echo ran >> "$FX_TEST_OUT/checks.ran"
"%s" -c 'import json,os; st=json.load(open(os.path.join(os.environ["FACTORY_STATE_DIR"],"run","state.json"))); l=st["leases"].get(os.environ.get("FACTORY_PLACE",""),{}); print(l.get("kind"), l.get("over") is not None)' >> "$FX_TEST_OUT/kinds"
exit 0
''' % sys.executable


class LightGateTest(ChecksCase):
    """A gate is counted on the machine as one whose checks already passed when its commit has a pass: if something the
    checks read changed since, it runs them after all, and then they take a place of their own first."""
    script = LEASE_KIND

    def kinds(self):
        return [k for k in slurp(os.path.join(self.f.out, "kinds")).split("\n") if k]

    def test_a_gate_that_must_run_the_checks_after_all_waits_for_their_room(self):
        self.checks()
        write(os.path.join(self.f.factory, "qa", "new.spec.ts"), "test('x', () => {})\n")  # another job's lane work
        code, res = self.gate()
        self.assertEqual((res["gate"], res["checks"]["reused"], self.times_run()), ("PASS", False, 2), res)
        self.assertIn("files of the factory repository", res["checks"]["not_reused_because"])
        # the builder's run in its own place; the gate's run in a place of the checks' own kind, a command of the gate's
        self.assertEqual(self.kinds(), ["checks False", "checks True"])
        with open(os.path.join(os.environ["FACTORY_STATE_DIR"], "run", "state.json")) as f:
            self.assertEqual(json.load(f)["leases"], {})  # both places given back

    def test_a_gate_that_uses_the_pass_takes_no_other_place(self):
        self.checks()
        code, res = self.gate()
        self.assertEqual((res["gate"], res["checks"]["reused"], self.kinds()), ("PASS", True, ["checks False"]), res)

    def test_a_command_of_a_place_does_not_wait_for_that_place(self):
        """The line itself: the gate's own place is running and holds the room, and its command still starts."""
        cfg = dict(fx.ledger_settings())
        reading = {"at": time.time(), "host": {"total_mb": 1000.0, "avail_mb": 900.0, "pressure": 0, "pressure_source": "-"},
                   "docker": {"total_mb": 1000.0, "used_mb": 0.0, "cpus": 4}, "cpus": 4, "load1": 0.0, "by_lease": {}}
        gate = {"id": "g", "kind": fx.GATE_AFTER_CHECKS, "need": {"host_mb": 500.0, "docker_mb": 500.0, "cpu": 1.0}}
        ticket = {"id": "c", "kind": "checks", "seq": 1, "enqueued": time.time(), "seen": time.time(),
                  "need": {"host_mb": 600.0, "docker_mb": 600.0, "cpu": 1.0}}
        for over, starts in ((None, False), ("g", True)):
            t = dict(ticket, over=over) if over else ticket
            self.assertEqual(fx.place_decide(cfg, {"leases": {"g": gate}, "queue": [t]}, "c", reading)[0], starts, over)
            self.assertEqual(fx.place_alone({"leases": {"g": gate}, "queue": []}, t), starts, over)  # asked before it queues

    def test_a_packages_place_is_not_counted_twice_while_its_command_runs(self):
        cfg = dict(fx.ledger_settings())
        reading = {"at": time.time(), "host": {"total_mb": 10000.0, "avail_mb": 7000.0, "pressure": 0, "pressure_source": "-"},
                   "docker": {"total_mb": 10000.0, "used_mb": 3000.0, "cpus": 4}, "cpus": 4, "load1": 0.0,
                   "by_lease": {"cmd": {"docker_mb": 3000.0, "host_mb": 0.0, "cpu": 1.0}}}
        st = {"leases": {"pkg": {"id": "pkg", "kind": "package", "tie": {"wp": "A"}, "need": {"docker_mb": 2000.0}},
                         "cmd": {"id": "cmd", "kind": "package-run", "over": "pkg", "need": {"docker_mb": 1000.0}}}, "queue": []}
        acc, _cap = fx.pool_account(cfg, st, reading)
        self.assertEqual(acc["docker_mb"]["promised"], 3000.0)  # what the command's place sees, the stack included
        del st["leases"]["cmd"]
        reading["by_lease"] = {}
        acc, _cap = fx.pool_account(cfg, st, reading)
        self.assertEqual(acc["docker_mb"]["promised"], 2000.0)  # at rest, without a command: what its stack needs


# --------------------------------------------------------------------------
# 3b. a run that was stopped from outside says nothing about the code

STOPPABLE = '''echo ran >> "$FX_TEST_OUT/checks.ran"
if [ -e "$FX_TEST_OUT/stop" ]; then  # what Docker would say: a container of this step was asked to stop
  mkdir -p "$FACTORY_STATE_DIR/run/fake-stops"
  cat "$FX_TEST_OUT/stop" >> "$FACTORY_STATE_DIR/run/fake-stops/$FACTORY_PLACE.jsonl"
  echo "npm error signal SIGTERM"
  exit 1
fi
if [ -e "$FX_TEST_OUT/signal" ]; then kill -TERM $$; fi
if [ -e "$FX_TEST_OUT/fail" ]; then echo "a test failed"; exit 1; fi
exit 0
'''
FROM_OUTSIDE = {"container": "at-t-1-playwright-run-9f3c", "signal": "15"}


class OutsideStopTest(ChecksCase):
    script = STOPPABLE

    def stop_next(self, event=FROM_OUTSIDE):
        path = os.path.join(self.f.out, "stop")
        if event:
            write(path, json.dumps(event) + "\n")
        elif os.path.exists(path):
            os.remove(path)

    def test_the_round_does_not_count_and_no_builder_is_sent(self):
        self.stop_next()
        write(os.path.join(self.f.jd, "build.md"), "STATUS: READY\n")
        code, res = self.gate()
        self.assertEqual((code, res["void"], res["next"], res["round"]), (3, True, "gate", 1), res)
        self.assertIn("Docker was asked to stop at-t-1-playwright-run-9f3c", res["error"])
        self.assertIn("says nothing about the job's code", res["error"])
        job = self.f.job()
        self.assertEqual((job["round"], job["stage"], job.get("gate"), fx.rounds_left(job)), (0, "build", None, 3))
        self.assertEqual([(x["commit"], x["round"]) for x in job["outside_stops"]], [(self.head(), 1)])
        self.assertEqual([s.get("result") for s in job["steps"] if s["step"] == "gate"], ["void"])
        self.assertTrue(os.path.exists(os.path.join(self.f.jd, "build.md")))  # back where the gate reads it
        kept = load_json(os.path.join(res["kept"], "outside-stop.json"))  # what was seen is kept with the round
        self.assertEqual(res["kept"], os.path.join(self.f.jd, "round-1-void-1"))
        self.assertEqual((kept["stopped"][0]["container"], kept["commit"], kept["the_round_counts"]),
                         ("at-t-1-playwright-run-9f3c", self.head(), False), kept)
        self.assertIn("npm error signal SIGTERM", slurp(os.path.join(res["kept"], "checks.log")))
        self.stop_next(None)  # run again, as it says: the same round, and this time it counts
        code, res = self.gate()
        self.assertEqual((res["gate"], res["round"], res["rounds_left"], self.times_run()), ("PASS", 1, 2, 2), res)

    def test_it_happens_once_for_a_commit_then_the_failure_counts(self):
        self.stop_next()
        self.assertEqual(self.gate()[0], 3)
        code, res = self.gate()
        self.assertEqual((code, res["gate"], res["round"], res["rounds_left"], res["next"]), (0, "FAIL", 1, 2, "build"), res)
        md = self.checks_md()
        self.assertIn("stopped from outside again", md)
        self.assertIn("already put aside for that, so this one counts", md)
        self.assertTrue(load_json(os.path.join(self.f.jd, "round-1", "outside-stop.json"))["the_round_counts"])
        self.f.commit({"app.py": "x = 2\n"})  # another commit is another matter
        self.assertEqual(self.gate()[0], 3)
        self.assertEqual(len(self.f.job()["outside_stops"]), 2)

    def test_a_failure_of_the_code_is_a_failure(self):
        self.fail_next()
        code, res = self.gate()
        self.assertEqual((code, res["gate"], res["rounds_left"]), (0, "FAIL", 2), res)
        self.assertNotIn("outside_stops", self.f.job())
        self.assertFalse(os.path.exists(os.path.join(self.f.jd, "round-1", "outside-stop.json")))

    def test_what_the_checks_stop_themselves_is_their_own_business(self):
        """A script that takes its own containers down when a test failed: Docker is asked to kill them, by the script."""
        self.stop_next({"container": "at-t-1-e2e-web", "signal": "9", "own": ["docker rm -f at-t-1-e2e-web"]})
        code, res = self.gate()
        self.assertEqual((code, res["gate"], res["rounds_left"]), (0, "FAIL", 2), res)
        self.stop_next({"container": "at-t-1-e2e-web", "signal": "9", "own": None})  # fx could not tell who asked
        code, res = self.gate()
        self.assertEqual((code, res["gate"], res["rounds_left"]), (0, "FAIL", 1), res)

    def test_the_script_ended_by_a_signal_fx_did_not_send(self):
        write(os.path.join(self.f.out, "signal"), "")
        code, res = self.gate()
        self.assertEqual((code, res.get("void")), (3, True), res)
        self.assertIn("ended on signal 15 (SIGTERM), which fx did not send", res["error"])
        self.assertEqual(load_json(os.path.join(res["kept"], "outside-stop.json"))["signal"], 15)

    def test_a_run_fx_stopped_itself_because_its_time_was_up_counts(self):
        write(os.path.join(self.f.factory, "checks.sh"), 'echo ran >> "$FX_TEST_OUT/checks.ran"\nsleep 20\n')
        self.configure(gate_timeout_sec=1)
        code, res = self.gate()
        self.assertEqual((code, res["gate"], res["rounds_left"]), (0, "FAIL", 2), res)
        self.assertIn("checks.sh timed out after 1s", self.checks_md())

    def test_a_pass_stays_a_pass_and_a_stop_never_makes_one(self):
        path = os.path.join(self.f.out, "stop")
        write(os.path.join(self.f.factory, "checks.sh"), STOPPABLE.replace('  echo "npm error signal SIGTERM"\n  exit 1\n', ""))
        write(path, json.dumps(FROM_OUTSIDE) + "\n")  # asked to stop, and the checks passed all the same
        code, res = self.gate()
        self.assertEqual((code, res["gate"]), (0, "PASS"), res)
        self.assertNotIn("outside_stops", self.f.job())

    def test_the_builder_is_told_not_to_mend_what_is_not_broken(self):
        self.stop_next()
        code, res = self.checks()
        self.assertEqual((code, res["checks"]), (1, "FAIL"), res)
        self.assertIn("Docker was asked to stop", res["outside_stop"])
        self.assertIn("run this again", res["next"])
        self.assertIn("Docker was asked to stop", self.runs()[-1]["outside_stop"])
        code, res = self.checks()  # stopped from outside again on the same commit: now it is taken for a failure
        self.assertEqual((code, res["checks"]), (1, "FAIL"), res)
        self.assertIn("stopped from outside before as well", res["next"])
        self.assertIn("fix what fails", res["next"])

    def test_fx_pr_pushes_no_draft_for_it(self):
        self.assertEqual(self.gate()[1]["gate"], "PASS")
        self.f.save_job(stage="pr", checks_passed_at=None)
        job = self.f.job()
        job["checks_runs"] = []  # as if what will be pushed were a commit nothing passed on yet
        self.f.save_job(checks_runs=[])
        self.stop_next()
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual((code, res["void"], res["next"]), (3, True, "pr"), res)
        self.assertIn("Nothing was pushed", res["error"])
        job = self.f.job()
        self.assertEqual((job["stage"], job["incomplete"], job["needs_human"]), ("pr", False, False))
        self.assertEqual(sh(["git", "ls-remote", "--heads", self.f.origin, self.f.branch], self.tmp), "")
        self.assertTrue(os.path.exists(res["kept"]))
        code, res = self.fx("pr", "T-1", "--no-secret-scan")  # again on this commit: now it counts, as a draft
        self.assertEqual((code, res["draft"], self.f.job()["incomplete"]), (0, True, True), res)


class StopsWatchTest(unittest.TestCase):
    """What fx makes of Docker's own account on a real machine: the lines `docker events` prints when a container is
    asked to stop, and the processes of the checks' own group at that moment. No Docker is run: the lines are given."""
    CLONE = "/Users/me/code/proj-factory-work/C2P3-156"
    JOB_DIR = "/Users/me/code/proj-factory/jobs/C2P3-156"
    GROUP = 4242
    RUNNING_THE_TESTS = ("%d /usr/local/bin/docker compose -f /f/stack/docker/compose.yml --project-directory %s -p at-c2p3-156 "
                         "run --rm --quiet-pull -T playwright npm run e2e -- --update-snapshots=none" % (GROUP, CLONE))
    SOMEONE_ELSE = "  777 docker stop -t 10 at-c2p3-156-playwright-run-9f3c\n  778 docker compose -p other down"

    def event(self, name, signal="15", workdir="", project="", place=""):
        return {"status": "kill", "Type": "container", "Action": "kill", "time": 1794680000, "Actor": {"ID": "c0ffee", "Attributes": {
            "name": name, "signal": signal, "image": "mcr.microsoft.com/playwright:v1.57.0-noble", "fx.place": place,
            "com.docker.compose.project": project, "com.docker.compose.project.working_dir": workdir}}}

    def watch(self, events, ps):
        place = fx.Place({"id": "ab12cd34-0011", "projects": ["at-c2p3-156"]})
        place.names.add("at-c2p3-156-mariadb-1")
        w = fx.StopsWatch(place, [self.CLONE, self.JOB_DIR])
        w.group = self.GROUP
        w.proc = mock.Mock(stdout=[json.dumps(e) + "\n" for e in events] + ["not json\n"])
        with mock.patch.object(fx, "try_out", return_value=ps):
            w.read()
        return w

    def test_a_container_of_the_checks_that_something_else_stopped(self):
        """As it happened: the tests' container was stopped by another tool while the script was only running them."""
        w = self.watch([self.event("at-c2p3-156-playwright-run-9f3c", workdir=self.CLONE, project="at-c2p3-156")],
                       self.RUNNING_THE_TESTS + "\n" + self.SOMEONE_ELSE)
        self.assertEqual([(e["container"], e["signal"], e["own"]) for e in w.events], [("at-c2p3-156-playwright-run-9f3c", "15", [])])
        seen = fx.outside_stop(w, 1, False)  # npm turned the signal into exit 1: only Docker's account tells
        self.assertIn("Docker was asked to stop at-c2p3-156-playwright-run-9f3c while the checks ran (signal 15)", seen["why"])
        self.assertEqual((len(seen["stopped"]), seen["signal"], seen["exit"]), (1, None, 1))

    def test_other_jobs_containers_are_none_of_its_business(self):
        w = self.watch([self.event("at-c2p3-162-playwright-run-1", workdir="/Users/me/code/proj-factory-work/C2P3-162",
                                   project="at-c2p3-162"), self.event("some-database")], self.RUNNING_THE_TESTS)
        self.assertEqual(w.events, [])
        self.assertIsNone(fx.outside_stop(w, 1, False))

    def test_its_own_containers_are_known_in_four_ways(self):
        events = [self.event("a", workdir="/host_mnt" + self.CLONE + "/sub"),  # compose started it from the clone
                  self.event("b", place="ab12cd34-0011"),  # fx started it for this step (ZAP)
                  self.event("c", project="at-c2p3-156"),  # a compose project seen with the step
                  self.event("at-c2p3-156-mariadb-1"),  # a container seen with the step while it was measured
                  self.event("d", place="ab12cd34-9999"), self.event("e", project="at-c2p3-15")]
        self.assertEqual([e["container"] for e in self.watch(events, self.RUNNING_THE_TESTS).events],
                         ["a", "b", "c", "at-c2p3-156-mariadb-1"])

    def test_what_the_script_stops_itself_is_no_stop_from_outside(self):
        for own in ("docker rm -f at-c2p3-156-e2e-web", "/usr/local/bin/docker stop at-c2p3-156-e2e-web",
                    "/Users/me/.docker/cli-plugins/docker-compose compose -f x.yml -p at-c2p3-156 down --remove-orphans",
                    "docker compose -p at-c2p3-156 up -d --force-recreate", "docker-compose kill web"):
            w = self.watch([self.event("at-c2p3-156-e2e-web", signal="9", workdir=self.CLONE)],
                           self.RUNNING_THE_TESTS + "\n%d %s\n" % (self.GROUP, own))
            self.assertEqual(w.events[0]["own"], [own], own)
            self.assertIsNone(fx.outside_stop(w, 1, False), own)

    def test_running_a_container_is_not_stopping_one(self):
        for line in (self.RUNNING_THE_TESTS, "%d docker run --rm -v /x:/y img sh -c true" % self.GROUP,
                     "%d docker compose exec -T app php artisan test --stop-on-failure" % self.GROUP,
                     "%d bash /f/checks.sh" % self.GROUP, "%d node killer.js stop" % self.GROUP):
            w = self.watch([self.event("x", workdir=self.CLONE)], line)
            self.assertEqual(w.events[0]["own"], [], line)

    def test_when_nobody_can_say_who_asked_the_failure_stands(self):
        w = self.watch([self.event("x", workdir=self.CLONE)], None)  # the processes could not be read
        self.assertEqual(w.events[0]["own"], None)
        self.assertIsNone(fx.outside_stop(w, 1, False))
        w.group = None
        self.assertIsNone(w.own())

    def test_a_script_ended_by_a_signal(self):
        quiet = self.watch([], "")
        self.assertEqual([(fx.outside_stop(quiet, rc, False) or {}).get("signal") for rc in (-15, -9)], [15, 9])
        # 128 + a signal alone: a test container killed for memory, or the script's own `timeout -s KILL`
        self.assertEqual([fx.outside_stop(quiet, rc, False) for rc in (143, 137, 130, 129, 1, 2, 124, 127, 255)], [None] * 9)
        w = self.watch([self.event("x", workdir=self.CLONE)], self.RUNNING_THE_TESTS)
        self.assertIn("the script then ended with 137 (SIGKILL passed on)", fx.outside_stop(w, 137, False)["why"])

    def test_a_kill_in_the_scripts_last_seconds_is_its_own_clean_up(self):
        """A `docker compose down` in an EXIT trap may be over before fx looks at the script's processes."""
        w = self.watch([self.event("x", workdir=self.CLONE)], self.RUNNING_THE_TESTS)
        w.ended = w.events[0]["ts"] + 5
        self.assertIsNone(fx.outside_stop(w, 1, False))
        w.ended = w.events[0]["ts"] + 600  # ten minutes before the end: no clean-up of its own
        self.assertIsNotNone(fx.outside_stop(w, 1, False))

    def test_a_run_fx_stopped_itself_is_never_one(self):
        w = self.watch([self.event("x", workdir=self.CLONE)], self.RUNNING_THE_TESTS)
        self.assertIsNone(fx.outside_stop(w, None, True))  # its time was up: fx stopped the script and its containers


class CandidatesChecksTest(BoxCase):
    """Best-of-N: each candidate's builder checks its own clone, and the judge's preparation finds those passes."""
    COUNT = '#!/usr/bin/env bash\necho "$FACTORY_JOB" >> "$FX_TEST_OUT/checks.ran"\nexit 0\n'

    def test_a_candidate_its_builder_checked_is_not_checked_again(self):
        f = self.box.factory("cand", checks=self.COUNT)
        job = self.box.new(f, "CAN-1")["job"]
        self.box.edit(f, job, stage="build", best_of=2)
        code, res = self.box.fx(f, "candidates", job)
        self.assertEqual(code, 0, res)
        for c in self.box.job(f, job)["candidates"]:
            write(os.path.join(c["worktree"], "src", "new.py"), "x = %d\n" % c["k"])
            sh(["git", "add", "-A"], c["worktree"])
            subprocess.run(["git", "commit", "-q", "-m", "candidate %d" % c["k"]], cwd=c["worktree"], env=self.box.env, check=True)
            write(os.path.join(f, "jobs", job, "build-c%d.md" % c["k"]), "STATUS: READY\n")
        code, res = self.box.fx(f, "ctx", job, "--agent", "builder", "--candidate", "1")
        self.assertIn("checks %s --candidate 1` runs the whole checks script" % job, prompt_text(res))
        code, res = self.box.fx(f, "checks", job, "--candidate", "1")
        self.assertEqual((code, res["checks"], res["candidate"], res["kept_for_the_gate"]), (0, "PASS", 1, True), res)
        code, res = self.box.fx(f, "judge-prep", job)
        self.assertEqual((code, res["passing"]), (0, [1, 2]), res)
        self.assertEqual(slurp(os.path.join(self.box.out, "checks.ran")).split(), ["CAN-1-c1", "CAN-1-c2"])  # c1 once only
        runs = [(r["cand"], r["by"]) for r in self.box.job(f, job)["checks_runs"]]
        self.assertEqual(runs, [(1, "builder"), (2, "judge")])
        self.assertIn("was not run again", slurp(os.path.join(f, "jobs", job, "checks-c1.log")))


# --------------------------------------------------------------------------
# 3c. a run the machine was too small for says nothing about the code (#6)

def pw_failure(n, error):
    return ("  %d) [chromium] › e2e/page-%d.spec.ts:12:5 › page %d shows its list ──────────\n\n    %s\n\n"
            "      at e2e/page-%d.spec.ts:14:20\n\n" % (n, n, n, error, n))


CRASHED = "".join(pw_failure(n, "Error: browserContext.newPage: Target crashed") for n in (1, 2, 3))
CRASHABLE = '''echo ran >> "$FX_TEST_OUT/checks.ran"
if [ -e "$FX_TEST_OUT/report" ]; then cat "$FX_TEST_OUT/report"; echo "  3 failed"; echo "  890 passed (24.1m)"; exit 1; fi
exit 0
'''
OOM_LOG = ("2026-11-24T10:01:02+0000 fx-sandbox kernel: Out of memory: Killed process 4242 (chrome-headless) "
           "total-vm:1234kB\n"
           "2026-11-24T10:01:09+0000 fx-sandbox kernel: Memory cgroup out of memory: Killed process 77 (postgres)\n"
           "2026-11-24T10:02:00+0000 fx-sandbox kernel: Out of memory: Killed process 4250 (chrome-headless)\n")


class MachineShortTest(ChecksCase):
    script = CRASHABLE

    def report(self, text):
        write(os.path.join(self.f.out, "report"), text)

    def in_a_vm(self, kills, mem_gib=6):
        """The checks as they run in a job's box in a VM of mem_gib on a machine of 24 GiB, whose kernel killed kills."""
        real = fx.checks_execute

        def in_box(*a, **k):
            r = real(*a, **k)
            if not r["ok"]:
                r.update(vm_oom=kills, in_box=True)
            return r
        return [mock.patch.object(fx, "checks_execute", in_box),
                mock.patch.object(fx, "sandbox_vm_row", return_value={"name": "fx-sandbox", "memory": mem_gib * 2 ** 30,
                                                                      "cpus": 4}),
                mock.patch.object(fx, "meter_host", return_value={"total_mb": 24 * 1024.0, "avail_mb": 20000.0,
                                                                  "pressure": 0, "pressure_source": "-"})]

    def test_only_crashed_browsers_put_the_round_aside(self):
        self.report(CRASHED)
        write(os.path.join(self.f.jd, "build.md"), "STATUS: READY\n")
        code, res = self.gate()
        self.assertEqual((code, res.get("void"), res.get("next")), (3, True, "stop"), res)
        self.assertIn("every test that failed (3) is a browser that crashed", res["error"])
        self.assertEqual(self.times_run(), 2)  # the flaky retry still ran first, and crashed as well
        job = self.f.job()
        self.assertEqual((job["round"], fx.rounds_left(job)), (0, 3))
        self.assertEqual([x["commit"] for x in job["machine_short"]], [self.head()])
        self.assertEqual(load_json(os.path.join(res["kept"], "machine-short.json"))["crashed"], 3)
        code, res = self.gate()  # the same commit on the same machine: this time it counts
        self.assertEqual((code, res["gate"], res["rounds_left"]), (0, "FAIL", 2), res)
        self.assertIn("failed again for want of memory", self.checks_md())

    def test_a_test_that_failed_otherwise_is_a_failure(self):
        self.report(CRASHED + pw_failure(4, "Error: expect(received).toBe(expected)"))
        code, res = self.gate()
        self.assertEqual((code, res["gate"], res["rounds_left"]), (0, "FAIL", 2), res)
        self.assertNotIn("machine_short", self.f.job())

    def test_the_vm_out_of_memory_is_not_run_again_and_asks_for_a_size(self):
        self.report(pw_failure(1, "Error: page.goto: net::ERR_CONNECTION_REFUSED"))  # not a crash: the app was killed
        kills = [{"at": "2026-11-24T10:01:02", "process": "chrome-headless"},
                 {"at": "2026-11-24T10:02:00", "process": "chrome-headless"}]
        with contextlib.ExitStack() as st:
            for p in self.in_a_vm(kills):
                st.enter_context(p)
            code, res = self.gate()
            self.assertEqual((code, res.get("void"), res.get("next")), (3, True, "ask-size"), res)
            self.assertEqual(self.times_run(), 1)  # no second run in a VM short of memory
            self.assertIn("its kernel killed chrome-headless (2 times)", res["error"])
            self.assertIn("The sandbox VM has 6 GiB of memory (this machine 24.0 GiB); fx suggests 9 GiB", res["error"])
            self.assertIn("1 ran out of memory (at 6 GiB)", res["error"])
            self.assertIn("Ask the person how much the VM may have", res["error"])
            self.assertEqual(self.f.job()["machine_short"][0]["memory_gib"], 6)
        with contextlib.ExitStack() as st:  # the person gave it more: the same commit is put aside once more
            for p in self.in_a_vm(kills, mem_gib=12):
                st.enter_context(p)
            code, res = self.gate()
            self.assertEqual((code, res.get("void")), (3, True), res)
            self.assertIn("fx suggests 18 GiB", res["error"])  # it ran out at 12 as well

    def test_the_kernel_log_reads_only_the_vms_own_shortage(self):
        with mock.patch.object(fx, "vm_run", return_value=subprocess.CompletedProcess([], 0, OOM_LOG.encode(), b"")):
            kills = fx.vm_oom_kills(time.time())
        self.assertEqual([k["process"] for k in kills], ["chrome-headless", "chrome-headless"])  # not the cgroup's
        with mock.patch.object(fx, "vm_run", return_value=subprocess.CompletedProcess([], 1, b"", b"no")):
            self.assertIsNone(fx.vm_oom_kills(time.time()))


class Issue8Test(FxTestCase):
    """#8: the stack's own services stay out of the box's proxy, a failed service's output reaches setup.log, and a
    second freeze of the same factory commit does not copy a file onto itself."""

    def test_the_stacks_services_are_left_out_of_the_proxy(self):
        write(os.path.join(self.f.factory, "stack", "docker", "compose.yml"),
              "services:\n  app:\n    image: x\n    hostname: web-app\n  minio:\n    image: m\n"
              "  minio-init:\n    image: mc\n    container_name: fx-mc\n    environment:\n      A: 1\n"
              "volumes:\n  data: {}\n")
        write(os.path.join(self.f.wt, "docker-compose.yml"), "version: '3'\nservices:\n    db:\n        image: pg\n")
        names = fx.compose_service_names([self.f.factory, self.f.wt])
        self.assertEqual(names, {"app", "web-app", "minio", "minio-init", "fx-mc", "db"})
        box = {"no_proxy": ",".join(sorted(names)), "online": True, "proxy_ip": "10.0.0.3"}
        env = fx.box_env(box, {})
        self.assertIn("minio,minio-init", env["NO_PROXY"])
        self.assertTrue(env["NO_PROXY"].startswith("localhost,127.0.0.1,::1,host.docker.internal"))
        self.assertIn("minio-init", fx.box_client_proxy_line("10.0.0.3", fx.box_no_proxy(box)))

    def test_a_failed_service_is_named_and_its_lines_kept(self):
        died = "==== at-t-1-minio-init-1 exited 1\nmc: <ERROR> 403 Filtered\n"
        calls = []

        def vm_run(argv, **k):
            calls.append(argv[-1].splitlines()[0])
            out = died if "fx-box-died-collect" in argv[-1] else ""
            return subprocess.CompletedProcess(argv, 0, out.encode(), b"")
        box = {"name": "fxbox-x"}
        with mock.patch.object(fx, "vm_run", vm_run), mock.patch.object(fx, "box_sync_in", return_value=None), \
                mock.patch.object(fx, "box_sync_out"), mock.patch.object(fx, "box_open"), \
                mock.patch.object(fx, "box_cut"), mock.patch.object(fx, "job_box_keep"), \
                mock.patch.object(fx, "box_exec", return_value=fx.BoxRun(1, "compose failed\n")):
            r = fx.box_job_exec(self.f.ctx(), {"id": "T-1"}, box, "bash setup.sh", "/x", {}, 60, online=True)
        self.assertEqual(calls, ["# fx-box-died-watch", "# fx-box-died-collect"])
        self.assertIn("mc: <ERROR> 403 Filtered", r.stdout)
        self.assertEqual(fx.died_words(r), " at-t-1-minio-init-1 exited 1 (their last lines are at the end of the log)")

    def test_a_second_freeze_of_the_same_commit(self):
        if not os.path.isdir(os.path.join(self.f.factory, ".git")):
            sh(["git", "init", "-q"], self.f.factory)
        ctx = self.f.ctx()
        write(os.path.join(self.f.factory, "qa", "ui-qa", "baselines.lock.json"), "{}\n")
        sh(["git", "add", "-A"], self.f.factory)
        sh(["git", "commit", "-qm", "qa"], self.f.factory)
        head = sh(["git", "rev-parse", "HEAD"], self.f.factory).strip()
        with mock.patch.object(fx, "baselines_config", return_value={"repo": "factory",
                                                                     "manifest": "qa/ui-qa/baselines.lock.json"}), \
                mock.patch.object(fx, "lane_folders", return_value=[("qa", "qa")]), \
                mock.patch.dict(os.environ, {"FACTORY_NO_FREEZE": ""}):
            fx.factory_freeze(ctx, head)
            snap = ctx._frozen
            self.assertTrue(os.path.islink(os.path.join(snap, "qa")))
            fx.factory_freeze(self.f.ctx(), head)  # raised SameFileError before
        self.assertEqual(ctx._frozen, snap)


class SandboxSizeTest(FxTestCase):
    """The VM's size: the person's answer the first time, then what fx measured suggests (#6)."""

    def on(self, gib, ncpu=8):
        st = contextlib.ExitStack()
        st.enter_context(mock.patch.object(fx, "meter_host", return_value={"total_mb": gib * 1024.0}))
        st.enter_context(mock.patch.object(fx.os, "cpu_count", return_value=ncpu))
        self.addCleanup(st.close)

    def used(self, *rows):
        for r in rows:
            fx.sandbox_usage_add(dict({"at": fx.now(), "ts": time.time(), "what": "bash checks.sh", "vm_gib": 6,
                                       "peak_load": 2.0, "vm_cpus": 4, "oom": 0}, **r))

    def test_before_anything_was_measured_it_suggests_half_and_asks(self):
        self.on(24)
        offer = fx.sandbox_size_offer()
        self.assertEqual((offer["suggest"]["memory_gib"], offer["suggest"]["cpus"]), (12, 4))
        self.assertIn("nothing was measured", offer["suggest"]["why"])
        self.assertEqual(offer["change"], "no size was chosen for the VM on this machine yet")
        self.assertEqual(offer["options"][0]["memory_gib"], 12)
        self.assertEqual(offer["most_gib"], 20)  # it always leaves the machine 4 GiB
        self.on(8)  # the machine keeps its 4 GiB: less than the 6 the VM had, and said so
        sug = fx.sandbox_size_offer()["suggest"]
        self.assertEqual(sug["memory_gib"], 4)
        self.assertIn("at most 4 GiB", sug["why"])

    def test_the_first_install_asks_and_makes_no_vm(self):
        self.on(24)
        with mock.patch.object(fx, "sandbox_kvm", return_value=""):
            code, res = self.fx("sandbox", "install")
        self.assertEqual((code, res["next"], res["suggest"]["memory_gib"]), (2, "ask-size", 12), res)
        self.assertFalse([c for c in self.f.calls("limactl") if c["argv"][:1] in (["create"], ["start"])])
        with mock.patch.object(fx, "sandbox_kvm", return_value=""):
            code, res = self.fx("sandbox", "install", "--memory", "10", "--cpus", "4")
        self.assertEqual((code, res["ok"], res["size_chosen"]["memory_gib"]), (0, True, 10), res)
        with mock.patch.object(fx, "sandbox_kvm", return_value=""):  # kept: no question the next time
            code, res = self.fx("sandbox", "reset")
        self.assertEqual((code, res["size_chosen"]["memory_gib"], res["size_chosen"]["cpus"]), (0, 10, 4), res)

    def test_the_answer_is_checked_and_kept(self):
        self.on(24)
        self.assertEqual(fx.sandbox_choose(memory=16)["cpus"], 4)  # what was not said: the suggestion
        self.assertEqual((fx.sandbox_chosen()["memory_gib"], fx.sandbox_chosen()["cpus"]), (16, 4))
        self.assertEqual(fx.sandbox_choose(cpus=6)["memory_gib"], 16)  # what was said before stays
        with self.assertRaises(fx.FxError):
            fx.sandbox_choose(memory=23)
        with self.assertRaises(fx.FxError):
            fx.sandbox_choose(cpus=9)

    def test_it_suggests_what_was_measured_and_a_quarter_more(self):
        self.on(24)
        fx.sandbox_choose(memory=6, cpus=4)
        self.used({"peak_gib": 5.1}, {"peak_gib": 7.2})
        offer = fx.sandbox_size_offer({"memory_gib": 6, "cpus": 4})
        self.assertEqual(offer["suggest"]["memory_gib"], 9)  # 7.2 and a quarter
        self.assertIn("over 2 commands measured", offer["suggest"]["why"])
        self.assertIn("needs more than the VM has", offer["change"])
        self.assertEqual([o["memory_gib"] for o in offer["options"]], [9, 6, 14])

    def test_a_vm_that_ran_out_of_memory_gets_half_again(self):
        self.on(24)
        fx.sandbox_choose(memory=6, cpus=4)
        self.used({"peak_gib": 5.9, "oom": 3}, {"peak_gib": 4.0})
        offer = fx.sandbox_size_offer({"memory_gib": 6, "cpus": 4})
        self.assertEqual(offer["suggest"]["memory_gib"], 9)
        self.assertIn("1 ran out of memory (at 6 GiB)", offer["suggest"]["why"])

    def test_a_vm_much_bigger_than_it_needs_may_give_memory_back(self):
        self.on(32)
        fx.sandbox_choose(memory=16, cpus=4)
        self.used(*[{"peak_gib": 3.0, "vm_gib": 16}] * 20)
        offer = fx.sandbox_size_offer({"memory_gib": 16, "cpus": 4})
        self.assertEqual(offer["suggest"]["memory_gib"], 6)
        self.assertIn("could have the rest back", offer["change"])
        self.assertIsNone(fx.sandbox_size_offer({"memory_gib": 9, "cpus": 4})["change"])  # not that much bigger

    def test_a_machine_too_small_for_it_says_so(self):
        self.on(10)
        fx.sandbox_choose(memory=6, cpus=4)
        self.used({"peak_gib": 7.9, "oom": 1})
        sug = fx.sandbox_size_offer({"memory_gib": 6, "cpus": 4})["suggest"]
        self.assertEqual(sug["memory_gib"], 6)
        self.assertIn("at most 6 GiB", sug["why"])

    def test_what_runs_outside_the_vm_is_left_to_the_machine(self):
        """A mobile app's phones and build run on this machine itself: the VM's suggestion leaves them what they used."""
        self.on(24)
        fx.cost_learn("shop", "gate", {"host_mb": 8192.0, "docker_mb": 0.0, "cpu": 1.0}, 600)
        fx.sandbox_choose(memory=6, cpus=4)
        self.used({"peak_gib": 12.0})
        offer = fx.sandbox_size_offer({"memory_gib": 6, "cpus": 4})
        self.assertEqual((offer["outside_the_vm_gib"], offer["machine_keeps_gib"], offer["most_gib"]), (8.0, 12, 12))
        self.assertEqual(offer["suggest"]["memory_gib"], 12)
        self.assertIn("virtual phones or an app's build, used up to 8.0 GiB", offer["suggest"]["why"])

    def test_a_busy_vm_gets_more_cpus(self):
        self.on(24, ncpu=12)
        fx.sandbox_choose(memory=8, cpus=4)
        self.used({"peak_gib": 4.0, "peak_load": 7.5})
        self.assertEqual(fx.sandbox_size_offer({"memory_gib": 8, "cpus": 4})["suggest"]["cpus"], 8)

    def test_the_watch_keeps_the_peak_of_the_whole_vm(self):
        w = fx.VmWatch({"name": "fxbox-a-T-1"}, fx.command_label("bash /home/me/shop-factory/checks.sh --all"))
        for line in ("6000000 4000000 1.5 4\n", "6000000 900000 3.25 4\n", "garbage\n", "6000000 2000000 2.0 4\n"):
            w.take(line)
        with mock.patch.object(fx, "vm_oom_kills", return_value=[{"at": "t", "process": "chrome"}]) as kills:
            got = w.stop(1)
        kills.assert_called_once()
        self.assertEqual((got["peak_gib"], got["vm_gib"], got["peak_load"], got["oom"], got["what"]),
                         (4.86, 6, 3.25, 1, "bash checks.sh --all"))  # no folder of the project in the history
        self.assertEqual(fx.sandbox_usage()[-1]["peak_gib"], 4.86)
        self.assertIsNone(fx.VmWatch({"name": "b"}, "x").stop(0))  # a command too short to be measured

    def test_the_watch_ends_limactl_and_what_it_started(self):
        """limactl shell runs ssh as a child: ending limactl alone left it holding the pipe for 10 s, and the loop on."""
        bin_ = os.path.join(self.tmp, "watchbin")
        os.makedirs(bin_)
        marker = os.path.join(self.tmp, "child-alive")
        write(os.path.join(bin_, "limactl"), "#!/bin/sh\n# like limactl: the command runs in a child it waits for\n"
              "sh -c 'trap \"\" TERM; while :; do echo \"6000000 1000000 1.0 4\"; touch %s; sleep 0.2; done' \n"
              % marker)
        os.chmod(os.path.join(bin_, "limactl"), 0o755)
        with mock.patch.dict(os.environ, {"PATH": bin_ + os.pathsep + os.environ["PATH"], "FACTORY_VM_WATCH": "1",
                                          "FACTORY_VM_WATCH_AFTER": "0"}):
            w = fx.VmWatch({"name": "b"}, "x").start()
            deadline = time.time() + 10
            while not w.samples and time.time() < deadline:
                time.sleep(0.1)
            t0 = time.time()
            got = w.stop(0)
        self.assertLess(time.time() - t0, 6)
        self.assertEqual(got["peak_gib"], 4.77)
        os.remove(marker)
        time.sleep(0.6)
        self.assertFalse(os.path.exists(marker))  # the loop ended with it
        with mock.patch.dict(os.environ, {"FACTORY_VM_WATCH": "0", "FACTORY_FAKE_MACHINE": ""}):
            self.assertIsNone(fx.VmWatch({"name": "b"}, "x").start().thread)  # turned off

    def test_a_size_kept_by_3_12_and_a_vm_grown_by_hand(self):
        self.on(24)
        write(fx.sandbox_size_path(), json.dumps({"memory_gib": 14}))  # 3.12.0 kept only what was passed
        self.assertEqual((fx.sandbox_chosen()["memory_gib"], fx.sandbox_chosen()["cpus"]), (14, 4))
        self.assertEqual(fx.sandbox_choose(cpus=6)["memory_gib"], 14)
        res, calls = self.resize(20, {"memory_gib": 14, "cpus": 4}, asked=False)  # grown by hand: left as it is
        self.assertEqual((res, calls), (None, []))
        res, calls = self.resize(6, {"memory_gib": 14, "cpus": 4}, asked=False)  # smaller than chosen: grown
        self.assertEqual(res["resized"]["to"]["memory_gib"], 14)

    def test_the_template_gets_the_size(self):
        text = slurp(fx.sandbox_template({"memory_gib": 12, "cpus": 6}))
        self.assertIn('\nmemory: "12GiB"\n', text)
        self.assertIn("\ncpus: 6\n", text)
        self.assertNotIn("6GiB", text)

    def resize(self, has_gib, want, restart=False, boxes=(), asked=True):
        calls = []

        def run(argv, *a, **k):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        with mock.patch.object(fx, "sandbox_vm_row", return_value={"status": "Running", "memory": has_gib * 2 ** 30,
                                                                   "cpus": 4}), \
                mock.patch.object(fx, "sandbox_running_boxes", return_value=list(boxes)), \
                mock.patch.object(fx.subprocess, "run", run):
            return fx.sandbox_resize(want, restart, asked), calls

    def test_install_gives_the_vm_the_chosen_size(self):
        res, calls = self.resize(6, {"memory_gib": 12, "cpus": 4})
        self.assertEqual(res["resized"]["to"], {"memory_gib": 12, "cpus": 4})
        self.assertEqual([c[:2] for c in calls], [["limactl", "stop"], ["limactl", "edit"]])
        self.assertEqual(self.resize(12, {"memory_gib": 12, "cpus": 4}), (None, []))
        res, calls = self.resize(16, {"memory_gib": 12, "cpus": 4})  # smaller too, since the person said so
        self.assertEqual(res["resized"]["to"]["memory_gib"], 12)

    def test_running_boxes_wait_for_restart(self):
        res, calls = self.resize(6, {"memory_gib": 12, "cpus": 4}, boxes=["fxbox-a-T-1"])
        self.assertEqual((calls, res["boxes_running"], res["next"]), ([], ["fxbox-a-T-1"], "ask-restart"))
        self.assertIn("--restart", res["size_note"])
        res, calls = self.resize(6, {"memory_gib": 12, "cpus": 4}, restart=True, boxes=["fxbox-a-T-1"])
        self.assertEqual(res["boxes_stopped"], ["fxbox-a-T-1"])


# --------------------------------------------------------------------------
# 4. a package tests its own part; its heavy commands take their own place, and the package is counted at rest

class SplitCase(BoxCase):
    def split(self, name="pk", key="SPL-1", f=None, **conf):
        f = f or self.box.factory(name, **conf)
        job = self.box.new(f, key)["job"]
        self.box.edit(f, job, stage="build", setup_done=True)
        write(os.path.join(f, "jobs", job, "plan.md"), FOUR)
        code, res = self.box.fx(f, "split", job)
        self.assertEqual(code, 0, res)
        return f, job

    def finish_package(self, f, job, pid, rel, env=None):
        clone = self.box.job(f, job)["split"]["packages"][pid]["clone"]
        write(os.path.join(clone, rel), "changed by %s\n" % pid)
        sh(["git", "add", "-A"], clone)
        subprocess.run(["git", "commit", "-q", "-m", pid], cwd=clone, env=self.box.env, check=True)
        write(os.path.join(f, "jobs", job, "build-%s.md" % pid), "STATUS: READY\n")
        code, res = self.box.fx(f, "wp", "done", job, pid, env=env)
        self.assertEqual((code, res.get("status")), (0, "integrated"), res)

    def leases(self, kind="package"):
        return {l["name"]: l for l in self.box.ledger()["leases"].values() if l["kind"] == kind}

    def run_fx(self, f, *argv, env=None):
        return subprocess.run([sys.executable, FX_PATH, "--repo", f] + list(argv), env=env or self.box.env, cwd=self.tmp,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def costs(self, f, kind):
        path = os.path.join(self.box.env["FACTORY_STATE_DIR"], "run", "costs.json")
        return (load(path) if os.path.exists(path) else {}).get("%s|%s" % (f, kind)) or []


class PackageChecksTest(SplitCase):
    COUNT = '#!/usr/bin/env bash\necho ran >> "$FX_TEST_OUT/checks.ran"\nexit 0\n'

    def test_the_whole_checks_script_is_not_run_for_a_package(self):
        f, job = self.split(checks=self.COUNT)
        self.box.fx(f, "wp", "next", job)
        script = self.box.checks_script(f)
        for argv in (["run", "--job", job, "--wp", "wp0", "--", "bash", script], ["checks", job, "--wp", "wp0"]):
            p = self.run_fx(f, *argv)
            res = json.loads(p.stdout)
            self.assertEqual((p.returncode, res["ok"]), (1, False), p.stdout)
            self.assertIn("a package tests the files it owns", res["error"])
            self.assertIn("checks %s" % job, res["error"])  # and where the whole script does run: once, at integration
        self.assertFalse(os.path.exists(os.path.join(self.box.out, "checks.ran")))
        p = self.run_fx(f, "run", "--job", job, "--wp", "wp0", "--", "bash", "-c", "echo its own tests")
        self.assertEqual((p.returncode, p.stdout), (0, "its own tests\n"), p.stderr)

    def test_each_builder_is_told_what_is_its_to_run(self):
        f, job = self.split(checks=self.COUNT)
        self.box.fx(f, "wp", "next", job)
        code, res = self.box.fx(f, "ctx", job, "--agent", "builder", "--wp", "wp0")
        self.assertIn("Heavy commands (the tests of the files you own, a build, a browser run)", prompt_text(res))
        self.assertIn("run --job %s --wp wp0 -- <command>" % job, prompt_text(res))
        self.assertIn("fx does not run it for a package", prompt_text(res))
        self.assertNotIn("\nChecks: ", prompt_text(res))
        for pid, rel in (("wp0", "src/a.py"), ("wp1", "src/b.py"), ("wp2", "web/c.js"), ("wp3", "README.md")):
            self.finish_package(f, job, pid, rel)
        self.assertEqual(self.box.fx(f, "wp", "next", job)[1]["next"], "integrate")
        code, res = self.box.fx(f, "ctx", job, "--agent", "builder")  # the integrator: the whole, once
        self.assertTrue(res["task"].startswith("Integrate"), res["task"])
        self.assertIn("Checks: `python3 %s checks %s` runs the whole checks script" % (FX_PATH, job), prompt_text(res))
        self.assertIn("Commit first, then run it once", prompt_text(res))
        code, res = self.box.fx(f, "checks", job)
        self.assertEqual((code, res["checks"], res["kept_for_the_gate"]), (0, "PASS", True), res)
        self.assertEqual(slurp(os.path.join(self.box.out, "checks.ran")).split(), ["ran"])
        code, res = self.box.fx(f, "ctx", job, "--agent", "code")  # a reviewer is told the same command
        self.assertIn("It answers at once when the script already passed on this commit", prompt_text(res))


class PackagePlacesTest(SplitCase):
    def measuring(self):
        """fx as on a real machine: it measures what a step uses while it runs (the tests' machine says what that is)."""
        return dict(self.box.env, FACTORY_SAMPLE_SEC="0.2")

    def uses(self, lease_id, **use):
        path = os.path.join(self.box.env["FACTORY_STATE_DIR"], "run", "fake-usage", lease_id + ".json")
        write(path, json.dumps(use))
        return path

    def test_a_package_is_counted_at_what_its_stack_uses_at_rest(self):
        """Its builder's test run used 7000 MB for a minute; its stack uses 600 MB the rest of the hour. Up to 1.8 the
        next package was counted at 7000 MB for as long as it was built."""
        write(os.path.join(self.tmp, "machine", "machine.json"), json.dumps({"max_builders": 1}))
        self.box.set_machine(cpus=16, host={"total_mb": 64000}, docker={"total_mb": 32000, "cpus": 16})
        f, job = self.split()
        env = self.measuring()
        code, res = self.box.fx(f, "wp", "next", job, env=env)
        self.assertEqual([p["id"] for p in res["start"]], ["wp0"], res)
        rest = self.uses(self.leases()["wp0"]["id"], docker_mb=600, cpu=0.2)
        worker = os.path.join(self.tmp, "worker.py")
        write(worker, WORKER)
        report = os.path.join(self.tmp, "r.json")
        p = self.run_fx(f, "run", "--job", job, "--wp", "wp0", "--report", report, "--", sys.executable, worker, "suite", "1.2",
                        os.path.join(self.tmp, "log.txt"), "docker_mb=7000", "cpu=3", env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        ran = load(report)
        self.assertEqual((ran["of_a_package"], ran["kind"]), (True, "package-run"), ran)
        self.assertAlmostEqual(ran["peak"]["docker_mb"], 7000, delta=1)
        held = self.leases()["wp0"]
        self.assertAlmostEqual(held["peak"]["docker_mb"], 600, delta=1)  # the command's use is the command's
        self.finish_package(f, job, "wp0", "src/a.py", env=env)
        os.remove(rest)
        self.assertEqual([(round(r["docker_mb"]), r.get("at_rest")) for r in self.costs(f, "package")], [(600, True)])
        self.assertEqual([round(r["docker_mb"]) for r in self.costs(f, "package-run")], [7000])
        code, res = self.box.fx(f, "wp", "next", job, env=env)
        self.assertEqual([p["id"] for p in res["start"]], ["wp1"], res)
        need = self.leases()["wp1"]
        self.assertEqual(need["need_source"], "measured (1)")
        self.assertAlmostEqual(need["need"]["docker_mb"], 600 * 1.15, delta=1)

    def test_what_a_package_was_measured_at_before_no_longer_counts(self):
        f, job = self.split()
        old = [{"docker_mb": 7870.0, "host_mb": 900.0, "cpu": 2.0, "seconds": 3000, "at": int(time.time()) - 86400}] * 3
        write(os.path.join(self.box.env["FACTORY_STATE_DIR"], "run", "costs.json"), json.dumps({"%s|package" % f: old}))
        self.box.set_machine(cpus=10, host={"total_mb": 24576, "other_mb": 8344}, docker={"total_mb": 16110, "other_mb": 6402, "cpus": 10})
        code, res = self.box.fx(f, "wp", "next", job)
        self.assertEqual([p["id"] for p in res["start"]], ["wp0", "wp1"], res)  # at 9 GB each not even two would start
        need = self.leases()["wp0"]
        self.assertIn("first guess", need["need_source"])
        self.assertEqual(need["need"]["share"]["docker_mb"], 0, need)
        p = subprocess.run([sys.executable, FX_PATH, "--repo", f, "capacity"], env=self.box.env, cwd=self.tmp,
                           stdout=subprocess.PIPE, text=True)
        self.assertNotIn("package", json.loads(p.stdout)["measured_here"].get(os.path.basename(f), {}))

    def test_a_step_that_waits_for_the_packages_room_does_not_hold_the_packages_own_command_up(self):
        """The step waits for the package to end, and has waited so long that nothing may overtake it. The package ends
        when its builder is done, and its builder waits for this command: held up too, neither would ever go on."""
        write(os.path.join(self.tmp, "machine", "machine.json"), json.dumps({"starve_sec": 1}))
        f, job = self.split()
        self.box.set_machine(cpus=8, host={"total_mb": 16000, "other_mb": 12000})  # room for one package, and no more
        self.assertEqual([p["id"] for p in self.box.fx(f, "wp", "next", job)[1]["start"]], ["wp0"])
        other = subprocess.Popen([sys.executable, FX_PATH, "run", "--name", "another job's gate", "--need", "host_mb=1500",
                                  "--wait", "600", "--", "true"], env=self.box.env, cwd=self.tmp,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (other.kill(), other.wait()))
        self.wait_for(lambda: [q for q in self.box.ledger()["queue"] if not q.get("kept_until")], what="the other step to wait")
        time.sleep(1.5)  # longer than starve_sec
        self.assertIsNone(other.poll())
        p = self.run_fx(f, "run", "--job", job, "--wp", "wp0", "--wait", "3", "--need", "host_mb=1000", "--", "echo", "tested")
        self.assertEqual((p.returncode, p.stdout), (0, "tested\n"), p.stdout + p.stderr)
        p = self.run_fx(f, "run", "--job", job, "--wait", "1", "--need", "host_mb=100", "--", "true")
        self.assertEqual(p.returncode, 75, p.stdout)  # any other command, small as it is, waits behind that step
        self.assertIn("nothing overtakes it", json.loads(p.stdout)["why"])
        self.finish_package(f, job, "wp0", "src/a.py")  # the builder is done: the package's room is the other step's now
        self.assertEqual(other.wait(timeout=30), 0)


class AtRestTest(unittest.TestCase):
    """Which step a container is counted for, on a real machine (Docker here is what the test says it is)."""
    CLONE = "/work/J-1--wp0"

    def read(self, leases, containers):
        docker = {"total_mb": 16000.0, "cpus": 8, "system": "test", "used_mb": sum(c["mem_mb"] for c in containers),
                  "containers": containers}
        env = {k: v for k, v in os.environ.items() if k != "FACTORY_FAKE_MACHINE"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(fx, "meter_docker", return_value=docker), \
                mock.patch.object(fx, "proc_table", return_value={}), \
                mock.patch.object(fx, "meter_host", return_value={"total_mb": 32000.0, "avail_mb": 20000.0, "pressure": 0,
                                                                  "pressure_source": "test"}):
            return fx.meter_read(leases)["by_lease"]

    def container(self, name, mb, oneoff=False):
        return {"name": name, "project": "at-j-1--wp0", "workdir": self.CLONE, "place": "", "oneoff": oneoff,
                "mounts": [self.CLONE], "mem_mb": float(mb), "cpu": 0.5}

    def test_while_a_command_runs_the_clones_containers_are_the_commands(self):
        package = {"id": "p", "kind": "package", "folders": [self.CLONE], "started": 100.0, "tie": {"wp": "wp0"}}
        command = {"id": "c", "kind": "package-run", "folders": [self.CLONE, "/factory/jobs/J-1"], "started": 900.0, "pid": 1}
        stack, tests = self.container("at-j-1--wp0-app-1", 600), self.container("at-j-1--wp0-app-run-7", 6400, oneoff=True)
        by = self.read({"p": package, "c": command}, [stack, tests])
        self.assertEqual((by["c"]["docker_mb"], by["p"]["docker_mb"]), (7000.0, 0.0))  # though the package came first
        by = self.read({"p": package}, [stack])
        self.assertEqual(by["p"]["docker_mb"], 600.0)  # and at rest its stack is its own again

    def test_between_steps_that_run_the_first_still_comes_first(self):
        first = {"id": "a", "kind": "gate", "folders": [self.CLONE], "started": 100.0, "pid": 1}
        second = {"id": "b", "kind": "run", "folders": [self.CLONE], "started": 200.0, "pid": 1}
        by = self.read({"b": second, "a": first}, [self.container("x", 500)])
        self.assertEqual((by["a"]["docker_mb"], by["b"]["docker_mb"]), (500.0, 0.0))


# --------------------------------------------------------------------------
# 5. how long a step waited for room is kept, with why

class WaitsTest(BoxCase):
    def busy(self, seconds, need="host_mb=13000", name="another job's checks"):
        p = subprocess.Popen([sys.executable, FX_PATH, "run", "--name", name, "--kind", "checks", "--need", need, "--wait",
                              "600", "--", "sleep", str(seconds)], env=self.box.env, cwd=self.tmp,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        self.wait_for(lambda: self.box.ledger()["leases"], what="the other step to hold its place")
        return p

    def a_job(self, name="wait", key="WAI-1"):
        f = self.box.factory(name)
        job = self.box.new(f, key)["job"]
        self.box.to_build(f, job)
        return f, job

    def test_a_gate_that_waited_says_for_how_long_and_why(self):
        f, job = self.a_job()
        self.box.set_machine(cpus=8, host={"total_mb": 16000})
        self.busy(5)  # long enough that a slow start of fx on a busy machine still leaves two seconds to wait
        code, res = self.box.fx(f, "gate", job)
        self.assertEqual(res.get("gate"), "PASS", res)
        j = self.box.job(f, job)
        step = [s for s in j["steps"] if s["step"] == "gate"][-1]
        self.assertGreaterEqual(step["waited_sec"], 2, step)
        self.assertIn("the machine's memory", step["wait_why"])
        wait = j["waits"][-1]
        self.assertEqual((wait["step"], wait["behind"]), ("gate", ["checks another job's checks"]), wait)
        self.assertGreaterEqual(wait["waited_sec"], 2)
        self.assertLess(fx.iso_ts(wait["asked"]), fx.iso_ts(wait["started"]))
        self.assertIn("the machine's memory: it needs", wait["why"])
        code, res = self.box.fx(f, "status")
        row = res["jobs"][0]["waited_for_room"]
        self.assertEqual((row["times"], row["longest"][0]["step"], row["longest"][0]["behind"]),
                         (1, "gate", ["checks another job's checks"]), row)
        self.assertGreaterEqual(row["longest"][0]["seconds"], 2)
        view = fx.job_view(f, "wait", j, {}, {})  # and the board draws it on the job's timeline
        seg = [s for s in view["segments"] if s["kind"] == "queue"]
        self.assertEqual([(s["label"], s["start"], s["end"]) for s in seg], [("queue", wait["asked"], wait["started"])])
        self.assertTrue(seg[0]["status"].startswith("gate: the machine's memory"), seg)

    def test_a_step_that_started_at_once_keeps_nothing(self):
        f, job = self.a_job()
        code, res = self.box.fx(f, "gate", job)
        j = self.box.job(f, job)
        self.assertNotIn("waits", j)
        self.assertNotIn("waited_sec", j["steps"][-1])
        self.assertNotIn("waited_for_room", self.box.fx(f, "status")[1]["jobs"][0])

    def test_the_builders_own_checks_and_commands_are_kept_too(self):
        f, job = self.a_job()
        self.box.set_machine(cpus=8, host={"total_mb": 16000})
        self.busy(4)
        code, res = self.box.fx(f, "checks", job)
        self.assertEqual((code, res["checks"]), (0, "PASS"), res)
        self.assertGreaterEqual(res["waited_sec"], 1, res)
        self.busy(4)
        p = subprocess.run([sys.executable, FX_PATH, "--repo", f, "run", "--job", job, "--", "true"], env=self.box.env,
                           cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        j = self.box.job(f, job)
        self.assertEqual([w["step"] for w in j["waits"]], ["checks", "run"], j["waits"])
        checks = [s for s in j["steps"] if s["step"] == "checks"][-1]
        self.assertEqual(checks["result"], "PASS")
        self.assertGreaterEqual(checks["waited_sec"], 1)

    def test_a_wait_that_had_to_leave_the_line_and_came_back_is_one_wait(self):
        """An agent's command answers `next: wait` after a while and is run again: the job waited all that time."""
        f, job = self.a_job()
        self.box.set_machine(cpus=8, host={"total_mb": 16000})
        self.busy(7)
        argv = [sys.executable, FX_PATH, "--repo", f, "checks", job, "--wait", "1"]
        p = subprocess.run(argv, env=self.box.env, cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual((p.returncode, json.loads(p.stdout)["next"]), (75, "wait"), p.stdout)
        self.assertNotIn("waits", self.box.job(f, job))  # it has not started yet
        code, res = self.box.fx(f, "checks", job)
        self.assertEqual((code, res["checks"]), (0, "PASS"), res)
        waits = self.box.job(f, job)["waits"]
        self.assertEqual(len(waits), 1, waits)
        self.assertGreaterEqual(waits[0]["waited_sec"], 3, waits)  # from the first time it asked

    def test_fx_capacity_says_how_long_a_step_that_runs_had_waited(self):
        self.box.set_machine(cpus=8, host={"total_mb": 16000})
        self.busy(4)
        second = subprocess.Popen([sys.executable, FX_PATH, "run", "--name", "second", "--need", "host_mb=13000", "--wait",
                                   "600", "--", "sleep", "4"], env=self.box.env, cwd=self.tmp,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (second.kill(), second.wait()))
        self.wait_for(lambda: any(l.get("name") == "second" for l in self.box.ledger()["leases"].values()), what="second to run")
        p = subprocess.run([sys.executable, FX_PATH, "capacity"], env=self.box.env, cwd=self.tmp, stdout=subprocess.PIPE, text=True)
        row = [r for r in json.loads(p.stdout)["running"] if r["name"] == "second"][0]
        self.assertGreaterEqual(row["waited_sec"], 1, row)
        self.assertIn("the machine's memory", row["waited_because"])

    def test_waiting_for_a_lane_is_kept_like_waiting_for_room(self):
        lanes = [{"name": "ui", "surfaces": ["ui"], "paths": ["qa"], "why": "shared tests"}]
        f = self.box.factory("lane", lanes=lanes)
        a, b = self.box.new(f, "UI-1")["job"], self.box.new(f, "UI-2")["job"]
        for job in (a, b):
            self.box.edit(f, job, stage="build", surfaces=["ui"])
        self.assertEqual(self.box.fx(f, "lane", "take", a)[1]["taken_now"], ["ui"])
        waiter = self.box.popen(f, "lane", "take", b, "--wait")
        self.addCleanup(lambda: (waiter.kill(), waiter.communicate()))
        self.wait_for(lambda: any(q["kind"] == "lane" for q in self.box.ledger()["queue"]), what="UI-2 to wait for the lane")
        time.sleep(2.5)
        self.box.edit(f, a, stage="handed-off")
        code, res = self.box.result(waiter)
        self.assertEqual((code, res.get("taken_now")), (0, ["ui"]), res)
        wait = self.box.job(f, b)["waits"][-1]
        self.assertEqual((wait["step"], wait["name"]), ("lane", "ui"), wait)
        self.assertGreaterEqual(wait["waited_sec"], 1)
        self.assertIn("UI-1 has it", wait["why"])
        self.assertNotIn("behind", wait)  # it waited for a name, not behind whoever held room

    def test_a_package_that_waited_for_room_is_kept_with_its_name(self):
        f = self.box.factory("pkw")
        job = self.box.new(f, "PKW-1")["job"]
        self.box.edit(f, job, stage="build", setup_done=True)
        write(os.path.join(f, "jobs", job, "plan.md"), FOUR)
        self.assertEqual(self.box.fx(f, "split", job)[0], 0)
        self.box.set_machine(cpus=8, host={"total_mb": 16000})
        self.busy(4)
        code, res = self.box.fx(f, "wp", "next", job, "--wait")
        self.assertEqual(code, 0, res)
        waits = self.box.job(f, job)["waits"]
        self.assertEqual((waits[0]["step"], waits[0]["name"]), ("package", "wp0"), waits)

    def test_a_reviews_step_is_kept_in_the_reviews_own_file(self):
        """/auto-factory:review shares the line: its sandbox waits like a job's setup, and its job.json is under _reviews/."""
        f = self.box.factory("rev")
        rid = "_reviews/REV-1-mr7"
        write(os.path.join(f, "jobs", rid, "job.json"), json.dumps({"id": rid, "stage": "run", "round": 1, "issue_key": "REV-1"}))
        old = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.box.env)
        cwd = os.getcwd()
        os.chdir(f)
        try:
            self.box.set_machine(cpus=8, host={"total_mb": 16000})
            self.busy(2)
            ctx = fx.Ctx()
            with contextlib.redirect_stderr(io.StringIO()), \
                    fx.step_place(ctx, "sandbox", ctx.load_exact(rid), folders=[self.tmp], need={"host_mb": 13000}):
                pass
            job = load(os.path.join(f, "jobs", rid, "job.json"))
            self.assertEqual([(w["step"], w["waited_sec"] >= 1) for w in job["waits"]], [("sandbox", True)], job)
            self.assertEqual(fx.review_rows(ctx)[0]["waited_for_room"]["longest"][0]["step"], "sandbox")
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(old)


# --------------------------------------------------------------------------
# 6. a job that does not hold a lane reads the lane's folders as committed

READS_THE_VARIABLE = """#!/usr/bin/env bash
QA="${FACTORY_LANE_QA:-$(cd "$(dirname "$0")" && git rev-parse --show-toplevel)/qa}"  # the factory repository's qa/
echo "$QA" > "$FX_TEST_OUT/dir-$FACTORY_JOB.txt"
cat "$QA"/*.spec.ts > "$FX_TEST_OUT/seen-$FACTORY_JOB.txt"
echo ran >> "$FX_TEST_OUT/ran-$FACTORY_JOB"
exit 0
"""
READS_THE_FOLDER = """#!/usr/bin/env bash
QA="$(cd "$(dirname "$0")" && git rev-parse --show-toplevel)/qa"
env | grep '^FACTORY_LANE_' | cut -d= -f2- > "$FX_TEST_OUT/dir-$FACTORY_JOB.txt"  # what it is told; it reads none of it
cat "$QA"/*.spec.ts > "$FX_TEST_OUT/seen-$FACTORY_JOB.txt"
exit 0
"""


class LaneCopyTest(BoxCase):
    LANES = [{"name": "ui", "surfaces": ["ui"], "paths": ["qa"], "why": "the UI tests in qa/ are shared by every job"}]

    def factory(self, checks=READS_THE_VARIABLE, name="lane"):
        f = self.box.factory(name, checks=checks, lanes=self.LANES)
        write(os.path.join(f, "qa", "suite.spec.ts"), "// shared\n")
        self.commit(f, "qa")
        return f

    def commit(self, f, msg):
        sh(["git", "add", "-A"], f)
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=f, env=self.box.env, check=True)

    def job(self, f, key, surfaces):
        job = self.box.new(f, key)["job"]
        self.box.to_build(f, job)
        self.box.edit(f, job, surfaces=surfaces)
        return job

    def seen(self, job):
        return slurp(os.path.join(self.box.out, "seen-%s.txt" % job))

    def where(self, job):
        return slurp(os.path.join(self.box.out, "dir-%s.txt" % job)).strip()

    def times_run(self, job):
        return len(slurp(os.path.join(self.box.out, "ran-%s" % job)).split())

    def two_jobs(self, f):
        """UI-1 holds the lane and is half way through changing qa/; API-1 has nothing to do with the UI."""
        ui, api = self.job(f, "UI-1", ["ui"]), self.job(f, "API-1", ["api"])
        self.assertEqual(self.box.fx(f, "lane", "take", ui)[1]["taken_now"], ["ui"])
        write(os.path.join(f, "qa", "suite.spec.ts"), "// half done by UI-1\n")
        write(os.path.join(f, "qa", "new.spec.ts"), "// not committed yet\n")
        return ui, api

    def test_another_jobs_gate_no_longer_runs_on_the_holders_half_done_changes(self):
        f = self.factory()
        ui, api = self.two_jobs(f)
        code, res = self.box.fx(f, "gate", api)
        self.assertEqual(res.get("gate"), "PASS", res)
        copy = os.path.join(f, "jobs", api, "lanes", "qa")
        self.assertEqual((self.seen(api), self.where(api)), ("// shared\n", copy))
        self.assertEqual(sorted(os.listdir(copy)), ["suite.spec.ts"])
        head = sh(["git", "rev-parse", "HEAD"], f).strip()
        self.assertEqual(res["lanes"], {"qa": {"read_from": copy, "as_committed_at": head[:12], "variable": "FACTORY_LANE_QA"}})
        self.assertIn("`qa`: this job read it as committed (%s), from its own copy in %s" % (head[:12], copy),
                      slurp(os.path.join(f, "jobs", api, "round-1", "checks.md")))
        code, res = self.box.fx(f, "gate", ui)  # the job that holds the lane works in the folder itself
        self.assertEqual(res.get("gate"), "PASS", res)
        self.assertEqual((self.seen(ui), self.where(ui)), ("// not committed yet\n// half done by UI-1\n", os.path.join(f, "qa")))
        self.assertNotIn("lanes", res)
        self.assertFalse(os.path.exists(os.path.join(f, "jobs", ui, "lanes")))

    def test_until_a_script_names_the_variable_everything_is_as_it_was(self):
        f = self.factory(READS_THE_FOLDER, name="asbefore")
        ui, api = self.two_jobs(f)
        code, res = self.box.fx(f, "lane", "status")
        self.assertEqual([(d["path"], d["variable"], d["named_in"]) for d in res["lanes"][0]["folders"]],
                         [("qa", "FACTORY_LANE_QA", [])], res)
        code, res = self.box.fx(f, "gate", api)
        self.assertEqual(res.get("gate"), "PASS", res)
        self.assertEqual(self.seen(api), "// not committed yet\n// half done by UI-1\n")
        self.assertEqual(self.where(api), os.path.join(f, "qa"))  # told where the folder is, and no copy was made
        self.assertFalse(os.path.exists(os.path.join(f, "jobs", api, "lanes")))
        self.assertNotIn("lanes", res)

    def test_fx_says_which_script_reads_the_variable(self):
        f = self.factory()
        write(os.path.join(f, "README.md"), "Scripts read FACTORY_LANE_QA to find the QA suite.\n")  # a text reads nothing
        code, res = self.box.fx(f, "lane", "status")
        folder = res["lanes"][0]["folders"][0]
        self.assertEqual((folder["variable"], folder["named_in"]), ("FACTORY_LANE_QA", ["checks/lane.sh"]), res)
        rows = {c["check"]: c for c in self.box.fx(f, "doctor")[1]["checks"]}
        self.assertIn("qa: FACTORY_LANE_QA is read by checks/lane.sh", rows["other jobs read a lane's folder as committed (advice)"]["detail"])
        self.assertIn("for 24 hours", rows["a pass of the checks script is used again (reuse)"]["detail"])
        other = self.factory(READS_THE_FOLDER, name="notyet")
        rows = {c["check"]: c for c in self.box.fx(other, "doctor")[1]["checks"]}
        self.assertIn("no script names FACTORY_LANE_QA yet", rows["other jobs read a lane's folder as committed (advice)"]["detail"])

    def test_each_folder_has_one_variable(self):
        self.assertEqual(fx.lane_var("qa/e2e-tests"), "FACTORY_LANE_QA_E2E_TESTS")

        class Settings:
            def __init__(self, *paths):
                self.lanes = [{"name": "ui", "surfaces": ["ui"], "paths": list(paths)}]

            def c(self, dotted, default=None):
                return self.lanes if dotted == "lanes" else default

        self.assertEqual(fx.lane_folders(Settings("qa", "stack/ui/")), [("ui", "qa"), ("ui", "stack/ui")])
        self.assertEqual(fx.lane_folders(Settings("qa/e2e", "qa")), [("ui", "qa")])  # the folder around it covers it
        self.assertEqual(fx.lane_folders(Settings("qa-x", "qa_x")), [("ui", "qa-x")])  # one name, one folder
        named = "QA=$FACTORY_LANE_QA_E2E; echo ${FACTORY_LANE_QA_E2E}"  # the longer name is not the shorter one
        self.assertFalse(re.search(re.escape("FACTORY_LANE_QA") + r"(?![A-Za-z0-9_])", named))

    def test_the_holders_half_done_changes_cost_another_job_no_run_and_a_commit_there_does(self):
        f = self.factory()
        ui, api = self.two_jobs(f)
        code, res = self.box.fx(f, "checks", api)
        self.assertEqual((res["checks"], res["reused"], self.times_run(api)), ("PASS", False, 1), res)
        write(os.path.join(f, "qa", "suite.spec.ts"), "// UI-1 is still at it\n")
        code, res = self.box.fx(f, "checks", api)
        self.assertEqual((res["checks"], res["reused"], self.times_run(api)), ("PASS", True, 1), res)
        self.commit(f, "UI-1: the new tests")  # now it is what every job is checked with
        code, res = self.box.fx(f, "gate", api)
        self.assertEqual((res["gate"], res["checks"]["reused"], self.times_run(api)), ("PASS", False, 2), res)
        self.assertIn("what is committed in a lane's folder", res["checks"]["not_reused_because"])
        self.assertEqual(self.seen(api), "// not committed yet\n// UI-1 is still at it\n")

    def test_the_holders_own_changes_are_the_holders_inputs(self):
        f = self.factory()
        ui, api = self.two_jobs(f)
        self.assertEqual(self.box.fx(f, "checks", ui)[1]["reused"], False)
        self.assertEqual(self.box.fx(f, "checks", ui)[1]["reused"], True)
        write(os.path.join(f, "qa", "suite.spec.ts"), "// UI-1 changed its test again\n")
        code, res = self.box.fx(f, "checks", ui)
        self.assertEqual((res["reused"], self.times_run(ui)), (False, 2), res)

    def test_the_job_that_took_the_lane_reads_the_folder_whatever_becomes_of_the_machines_list(self):
        """Its unfinished tests are in the folder. Were it handed a copy as committed because the machine forgot who
        holds what, its gate would run without them."""
        f = self.factory()
        ui, api = self.two_jobs(f)
        os.remove(os.path.join(self.box.env["FACTORY_STATE_DIR"], "run", "state.json"))
        code, res = self.box.fx(f, "checks", ui)
        self.assertEqual((code, res["checks"]), (0, "PASS"), res)
        self.assertEqual((self.where(ui), self.seen(ui)), (os.path.join(f, "qa"), "// not committed yet\n// half done by UI-1\n"))
        code, res = self.box.fx(f, "lane", "release", ui)  # until a person gives the lane back for it
        self.assertEqual(res["released"], ["ui"], res)
        self.box.fx(f, "checks", ui, "--again")
        self.assertEqual(self.seen(ui), "// shared\n")

    def test_the_copy_follows_what_is_committed_and_keeps_what_the_checks_put_there(self):
        f = self.factory()
        api = self.job(f, "API-1", ["api"])
        self.box.fx(f, "checks", api)
        copy = os.path.join(f, "jobs", api, "lanes", "qa")
        write(os.path.join(copy, "node_modules", "pkg", "index.js"), "installed by the checks\n")
        write(os.path.join(copy, "suite.spec.ts"), "// a test run wrote over it\n")
        sh(["git", "mv", "qa/suite.spec.ts", "qa/renamed.spec.ts"], f)
        write(os.path.join(f, "qa", "e2e", "deep.spec.ts"), "// deeper\n")
        self.commit(f, "qa: moved")
        code, res = self.box.fx(f, "checks", api)
        self.assertEqual((code, res["checks"]), (0, "PASS"), res)
        self.assertEqual(sorted(os.listdir(copy)), ["e2e", "node_modules", "renamed.spec.ts"])
        self.assertEqual(slurp(os.path.join(copy, "renamed.spec.ts")), "// shared\n")
        self.assertEqual(slurp(os.path.join(copy, "node_modules", "pkg", "index.js")), "installed by the checks\n")
        write(os.path.join(copy, "renamed.spec.ts"), "// written over again\n")
        self.box.fx(f, "checks", api, "--again")
        self.assertEqual(self.seen(api), "// shared\n")  # made as committed again before every run

    def test_setup_and_the_builders_own_commands_read_the_same_copy(self):
        f = self.factory(name="same")
        conf = load(os.path.join(f, "factory.json"))
        conf["repos"][0]["setup"] = 'echo "$FACTORY_LANE_QA" > "$FX_TEST_OUT/setup-dir.txt"; cat "$FACTORY_LANE_QA/suite.spec.ts" > "$FX_TEST_OUT/setup-seen.txt"'
        write(os.path.join(f, "factory.json"), json.dumps(conf, indent=1))
        self.commit(f, "setup")
        api = self.box.new(f, "API-1")["job"]
        self.box.edit(f, api, surfaces=["api"])
        code, res = self.box.fx(f, "setup", api)
        self.assertEqual(code, 0, res)
        copy = os.path.join(f, "jobs", api, "lanes", "qa")
        self.assertEqual((slurp(os.path.join(self.box.out, "setup-dir.txt")).strip(), slurp(os.path.join(self.box.out, "setup-seen.txt"))),
                         (copy, "// shared\n"))
        p = subprocess.run([sys.executable, FX_PATH, "--repo", f, "run", "--job", api, "--", "bash", "-c", "echo $FACTORY_LANE_QA"],
                           env=self.box.env, cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, copy), p.stderr)

    def test_the_copy_goes_with_the_clone_and_is_never_sent_to_s3(self):
        f = self.factory()
        api = self.job(f, "API-1", ["api"])
        self.box.fx(f, "checks", api)
        copy = os.path.join(f, "jobs", api, "lanes")
        self.assertTrue(os.path.isdir(copy))
        self.assertTrue(fx.jobs_left_out("%s/lanes/" % api, fx.JOBS_ALWAYS_EXCLUDE))
        code, res = self.box.fx(f, "cleanup", api, "--force")
        self.assertEqual(code, 0, res)
        self.assertFalse(os.path.exists(copy))


class LaneBaselinesTest(BaselinesCase):
    """The reference images are not in git: a copy of the lane's folder gets them from its own committed list."""
    CHECKS = BASELINE_CHECKS.replace('exit 0\n', 'echo "${FACTORY_LANE_QA:-}" > "$FX_TEST_OUT/lane-dir.txt"\nexit 0\n')

    def prepare(self):
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["lanes"] = [{"name": "ui", "surfaces": ["ui"], "paths": ["qa"], "why": "the reference images are shared"}]
        write(path, json.dumps(cfg, indent=1))
        write(os.path.join(self.f.factory, "checks.sh"), self.CHECKS)
        super().prepare()

    def seen(self):
        return slurp(os.path.join(self.f.out, "seen.txt"))

    def another_job_holds_the_lane(self):
        holder = dict(self.f.job(), id="UI-9", issue_key="UI-9", surfaces=["ui"], stage="build")
        write(os.path.join(self.f.factory, "jobs", "UI-9", "job.json"), json.dumps(holder))
        code, res = self.fx("lane", "take", "UI-9")
        self.assertEqual((code, res["taken_now"]), (0, ["ui"]), res)

    def test_the_copy_is_filled_from_the_committed_list(self):
        self.fill()
        self.another_job_holds_the_lane()
        self.put_file("home.png", C)  # it is regenerating this image, and has not recorded it yet
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        copy = os.path.join(self.f.jd, "lanes", "qa")
        self.assertEqual(slurp(os.path.join(self.f.out, "lane-dir.txt")).strip(), copy)
        self.assertIn("FACTORY_BASELINES=%s\n" % os.path.join(copy, "baselines"), slurp(os.path.join(self.f.out, "checks.env")))
        self.assertEqual(self.seen(), "./home.png %s\n./sub/menu.png %s\n" % (A.decode(), B.decode()))  # as committed
        self.assertEqual(self.on_disk("home.png"), C)  # and the shared folder was left to whoever is changing it
        md = self.checks_md()
        self.assertIn("reads the reference images from its own copy of the folder as committed", md)
        self.assertNotIn("Baselines changed", md)
        self.assertEqual(res["baselines"]["counts"], {"changed": 0, "added": 0, "removed": 0})

    def test_fx_checks_makes_the_images_ready_as_the_gate_does_and_says_what_it_did(self):
        """A builder that regenerated an image and has not recorded it: fx checks compares with the list, like the gate,
        keeps the builder's version aside and says so, instead of passing on an image the gate will not use."""
        self.fill()
        self.f.save_job(lanes={"ui": "the place it was given"})  # this job holds the lane: it works in the folder itself
        self.put_file("home.png", C)
        code, res = self.fx("checks", "T-1")
        self.assertEqual((code, res["checks"]), (0, "PASS"), res)
        self.assertEqual(self.on_disk("home.png"), A)
        self.assertTrue(any("restored from the list" in n and "home.png" in n for n in res["notes"]), res)
        self.assertEqual(slurp(os.path.join(self.f.out, "lane-dir.txt")).strip(), os.path.join(self.f.factory, "qa"))

    def test_images_that_cannot_be_had_stop_the_checks_as_ever(self):
        self.s3.fail_status = 500
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", res)
        self.assertFalse(self.ran_checks())
        self.assertIn("baseline image(s) missing and not fetched", self.checks_md())

    def test_the_images_are_part_of_what_a_pass_is_kept_for(self):
        code, res = self.fx("checks", "T-1")
        self.assertEqual((code, res["checks"], res["kept_for_the_gate"]), (0, "PASS", True), res)
        self.teammate_changes({"home.png": C, "sub/menu.png": B})  # new images are committed and pushed
        code, res = self.gate()  # the gate pulls them, the copy follows, and the checks run on them
        self.assertEqual((res["gate"], res["checks"]["reused"]), ("PASS", False), self.checks_md())
        self.assertEqual(self.seen(), "./home.png %s\n./sub/menu.png %s\n" % (C.decode(), B.decode()))


class ProjectListTest(ProjectRepoCase):
    """A project that keeps its baselines list in its own repository: a job that changes images leaves the list
    uncommitted until they are uploaded. The list is then no leftover work, and counts by what it says."""

    def checks(self):
        return self.fx("checks", "T-1")

    def runs(self):
        return len(slurp(os.path.join(self.f.out, "checks.ran")).split("\n")) - 1

    def test_a_recorded_list_waiting_for_its_upload_keeps_no_pass_from_the_gate(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.assertEqual(self.bl("record")[0], 0)
        code, res = self.checks()
        self.assertEqual((code, res["checks"], res["kept_for_the_gate"]), (0, "PASS", True), res)
        code, res = self.gate()
        self.assertEqual((res["gate"], res["checks"]["reused"], self.runs()), ("PASS", True, 1), self.checks_md())

    def test_another_list_or_another_image_is_checked_again(self):
        self.bl("sync")
        self.assertEqual(self.checks()[1]["kept_for_the_gate"], True)
        self.put_file("home.png", C)
        self.assertEqual(self.bl("record")[0], 0)
        code, res = self.checks()
        self.assertEqual((res["reused"], self.runs()), (False, 2), res)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class BuildersRunHoldsNothingTest(BaselinesCase):
    """fx checks does not keep the factory repository from being pulled while it runs (25 to 60 minutes): every other
    job's gate would judge on an older checkout. A gate still does."""

    def test_only_the_gate_holds_the_factory_against_a_pull(self):
        held, real = [], fx.baselines_hold
        with mock.patch.object(fx, "baselines_hold", side_effect=lambda ctx: held.append("checks") or real(ctx)):
            code, res = self.fx("checks", "T-1")
        self.assertEqual((code, held), (0, []), res)
        with mock.patch.object(fx, "baselines_hold", side_effect=lambda ctx: held.append("gate") or real(ctx)):
            self.gate()
        self.assertIn("gate", held)


# --------------------------------------------------------------------------
# the e2e tests a job's change reaches, and the checks script's stages timed

E2E_SCRIPT = r'''echo "::fx-stage unit 1000000000"
echo "::fx-stage e2e 1000000200"
echo "full=${FACTORY_E2E_FULL:-} why=${FACTORY_E2E_WHY:-}"
if [ -n "${FACTORY_E2E_TESTS:-}" ]; then echo "picked:$(tr '\n' ' ' < "$FACTORY_E2E_TESTS")"; fi
echo "changed:$(tr '\n' ' ' < "$FACTORY_CHANGED_FILES") base:${FACTORY_BASE_REF:-}"
echo ran >> "$FX_TEST_OUT/checks.ran"
exit 0
'''
E2E_MAP = {"tests": ["e2e/**/*.spec.ts"], "shared": ["src/layout/**"], "none": ["docs/**", "app.py"],
           "map": [{"paths": ["src/cart/**"], "tests": ["e2e/cart/**"]},
                   {"paths": ["src/home/**"], "tests": ["e2e/home/*.spec.ts"]}]}


class E2eChoiceTest(ChecksCase):
    """A one-line fix of one static page waited about 28 minutes, every round, for some 970 browser tests of pages it
    could not reach: a job runs the e2e tests its change reaches, chosen by fx from the factory's map."""
    script = E2E_SCRIPT
    base_files = {"README.md": "p\n", "e2e/cart/buy.spec.ts": "t\n", "e2e/home/home.spec.ts": "t\n",
                  "e2e/new.spec.ts": "t\n", "src/cart/cart.py": "x = 1\n", "src/layout/base.html": "<main/>\n",
                  "docs/a.md": "a\n"}

    def put_map(self, m=E2E_MAP):
        path = os.path.join(fx.repo_knowledge_dir(self.f.ctx()), "e2e-map.json")
        write(path, m if isinstance(m, str) else json.dumps(m))

    def ran(self, *argv):
        code, res = self.checks(*argv)
        self.assertEqual((code, res["checks"]), (0, "PASS"), res)
        return res, slurp(res["log"])

    def test_no_map_runs_the_whole_suite(self):
        res, log = self.ran()
        self.assertIn("full=1 why=no e2e map", log)
        self.assertNotIn("picked:", log)
        self.assertIn("changed:app.py  base:origin/main", log)
        self.assertTrue(res["e2e"].startswith("the whole suite: no e2e map"), res)

    def test_a_change_runs_the_tests_of_its_pages_and_those_no_entry_lists(self):
        self.put_map()
        self.f.commit({"src/cart/cart.py": "x = 2\n"})
        res, log = self.ran()
        self.assertIn("picked:e2e/cart/buy.spec.ts e2e/new.spec.ts ", log)  # new.spec.ts: in no entry yet, so always
        self.assertIn("full= why=2 of 3 e2e test files", log)
        self.assertIn("1 the map does not list yet", res["e2e"])
        code, shown = self.fx("e2e", "show", "T-1")
        got = list(shown["repos"].values())[0]
        self.assertEqual((got["full"], got["tests"]), (False, ["e2e/cart/buy.spec.ts", "e2e/new.spec.ts"]), shown)
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertIn("- e2e: 2 of 3 e2e test files", self.checks_md())

    def test_a_test_the_job_changed_runs(self):
        self.put_map()
        self.f.commit({"e2e/home/home.spec.ts": "t2\n", "docs/a.md": "b\n"})
        self.assertIn("picked:e2e/home/home.spec.ts e2e/new.spec.ts ", self.ran()[1])

    def test_what_the_map_cannot_say_runs_the_whole_suite(self):
        self.put_map()
        for files, why in (({"src/layout/base.html": "<main id=x/>\n"}, "src/layout/base.html is shared by every page"),
                           ({"src/orders/o.py": "y = 1\n"}, "src/orders/o.py is in no entry of the e2e map")):
            with self.subTest(files=files):
                self.f.commit(files)
                log = self.ran()[1]
                self.assertIn("full=1 why=%s" % why, log)
                sh(["git", "reset", "-q", "--hard", "HEAD~1"], self.f.wt)
        self.put_map('{"tests": "e2e/**"}')
        self.assertIn("does not say what fx reads", self.ran()[1])

    def test_only_what_reaches_no_page_runs_only_the_tests_no_entry_lists(self):
        self.put_map(dict(E2E_MAP, map=E2E_MAP["map"] + [{"paths": ["src/x/**"], "tests": ["e2e/new.spec.ts"]}]))
        self.f.commit({"docs/a.md": "b\n"})
        log = self.ran()[1]
        self.assertIn("full= why=no e2e test: the change reaches no page", log)
        self.assertIn("picked:\n", log)  # the list is there, and empty
        self.assertEqual(slurp(fx.e2e_env(*fx.one_app(self.f.ctx(), self.f.job()))["FACTORY_E2E_TESTS"]), "")

    def test_asked_for_the_whole_suite_and_a_pass_of_one_choice_is_not_another(self):
        self.put_map()
        self.f.commit({"src/cart/cart.py": "x = 3\n"})
        self.ran()
        res, log = self.ran("--full")
        self.assertEqual((res["reused"], self.times_run()), (False, 2), res)  # the whole suite is not what passed
        self.assertIn("full=1 why=the whole suite was asked for", log)
        code, res = self.fx("gate", "T-1", "--full")
        self.assertEqual((res["gate"], res["checks"]["reused"]), ("PASS", True), res)  # the run with --full, kept
        self.assertIn("- e2e: the whole suite was asked for", self.checks_md())

    def test_fx_own_runs_get_the_whole_suite(self):
        env = fx.e2e_env(self.f.ctx(), {"id": "_selftest"})
        self.assertEqual(env["FACTORY_E2E_FULL"], "1")
        self.assertIn("fx selftest", env["FACTORY_E2E_WHY"])

    def test_the_map_is_checked_against_the_base_branch(self):
        self.put_map(dict(E2E_MAP, map=E2E_MAP["map"] + [{"paths": ["src/gone/**"], "tests": ["e2e/cart/**"]}]))
        code, res = self.fx("e2e", "check")
        self.assertEqual((code, res["ok"], res["tests"], res["unmapped"]), (1, False, 3, ["e2e/new.spec.ts"]), res)
        self.assertEqual(res["empty"], ["map[1].paths src/home/**", "map[2].paths src/gone/**"], res)
        self.assertIn("run in every job", res["unmapped_note"])

    def test_the_stages_are_timed_and_a_slow_one_named(self):
        res = self.ran()[0]
        self.assertEqual(res["stages"][0], {"name": "unit", "seconds": 200})
        self.assertNotIn("slow_stages", res)
        write(os.path.join(self.f.factory, "baseline.json"), json.dumps(
            {"steps": {"checks": {"stages": [{"name": "unit", "seconds": 20}, {"name": "lint", "seconds": 5}]}}}))
        res = self.ran("--again")[0]
        self.assertEqual(res["slow_stages"], [{"name": "unit", "seconds": 200, "selftest": 20}])
        self.assertIn("unit took 200s, 20s in fx selftest", res["slow_note"])
        self.assertIn("not your code", res["slow_note"])
        code, res = self.gate()
        self.assertIn("- stages: unit 200s, e2e ", self.checks_md())
        self.assertIn("slower than in fx selftest, 3x or more (the machine or the sandbox, not the code): unit took 200s",
                      self.checks_md())


class StagesTest(unittest.TestCase):
    def test_each_stage_ends_where_the_next_starts(self):
        out = ("x\n::fx-stage lint 1000000100\nok\n::fx-stage unit 1000000130\n::fx-stage odd\n"
               "::fx-stage e2e 1000000400\r\n  ::fx-stage not-a-mark 1000000500\n")
        self.assertEqual(fx.checks_stages(out, 1000001000), [
            {"name": "lint", "seconds": 30}, {"name": "unit", "seconds": None},  # the next mark has no time
            {"name": "odd", "seconds": None}, {"name": "e2e", "seconds": 600}])
        self.assertEqual(fx.checks_stages("no stages\n", 5), [])

    def test_slow_is_three_times_and_a_minute_more(self):
        before = [{"name": "a", "seconds": 10}, {"name": "b", "seconds": 100}, {"name": "c", "seconds": 0}]
        now_ = [{"name": "a", "seconds": 69}, {"name": "b", "seconds": 299}, {"name": "c", "seconds": 61},
                {"name": "d", "seconds": 999}, {"name": "a2", "seconds": None}]
        self.assertEqual(fx.stages_slow(now_, before), [{"name": "c", "seconds": 61, "selftest": 0}])
        self.assertEqual([s["name"] for s in fx.stages_slow([{"name": "a", "seconds": 70}, {"name": "b", "seconds": 300}],
                                                            before)], ["a", "b"])
