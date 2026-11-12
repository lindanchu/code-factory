"""Tests for what the security gate checks and blocks (standard library only). Run from the repository root:
    python3 -I -m unittest discover -s tests

Same fixtures as test_security.py: real git repositories, stub scanners that record their argv and print canned JSON.
"""

import http.server
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock

from test_security import FxTestCase, TEMPLATE, fx, load_json, not_on_this_machine, sh, slurp, write


def semgrep_out(*findings, errors=()):
    return json.dumps({"errors": list(errors), "results": [
        {"check_id": rule, "path": "app.py", "start": {"line": i + 1},
         "extra": {"severity": sev, "message": "%s found" % rule, "metadata": {}}}
        for i, (rule, sev) in enumerate(findings)]})


def osv_out(*vulns):
    """vulns: (id, max_severity for its group, database_specific severity)."""
    return json.dumps({"results": [{"source": {"path": "package-lock.json"}, "packages": [{
        "package": {"name": "lodash", "version": "4.17.0", "ecosystem": "npm"},
        "groups": [{"ids": [v[0]], "max_severity": v[1]} for v in vulns],
        "vulnerabilities": [{"id": v[0], "summary": "bad", "database_specific": {"severity": v[2]} if v[2] else {}}
                            for v in vulns]}]}]})


def zap_report(*alerts):
    """alerts: (pluginid, risk code, name, uris)."""
    return json.dumps({"site": [{"alerts": [
        {"pluginid": a[0], "riskcode": a[1], "alert": a[2], "cweid": "79", "instances": [{"uri": u} for u in a[3]]}
        for a in alerts]}]})


class CoverageCase(FxTestCase):
    def set_security(self, **security):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"] = security
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))

    def gate_with(self, **env):
        self.f.commit({"app.py": "x = 1\n"})
        with mock.patch.dict(os.environ, env):
            return self.gate()[1]

    def security_md(self):
        return slurp(os.path.join(self.f.jd, "round-1", "security.md"))


class BlockSeverityTest(CoverageCase):
    def test_medium_blocks_by_default(self):
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_out(("xss.raw-html", "WARNING")))
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("xss.raw-html", self.checks_md())

    def test_low_is_reported_but_does_not_block_by_default(self):
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_out(("csrf.hint", "INFO")))
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertIn("csrf.hint", self.security_md())

    def test_a_factory_can_raise_the_threshold(self):
        self.set_security(block_severity="high")
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_out(("xss.raw-html", "WARNING")))
        self.assertEqual(res["gate"], "PASS", self.checks_md())

    def test_a_factory_can_lower_the_threshold(self):
        self.set_security(block_severity="low")
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_out(("csrf.hint", "INFO")))
        self.assertEqual(res["gate"], "FAIL", self.checks_md())

    def test_a_threshold_fx_does_not_know_never_blocks_less(self):
        self.set_security(block_severity="hihg")
        self.assertEqual(fx.block_rank(self.f.ctx()), fx.SEV_RANK["medium"])

    def test_a_vulnerable_dependency_already_on_the_base_does_not_block(self):
        out = osv_out(("GHSA-1", "9.8", None))
        res = self.gate_with(STUB_OSV_OUT=out, STUB_OSV_BASE_OUT=out)
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertIn("GHSA-1", self.security_md())

    def test_a_vulnerable_dependency_the_job_adds_blocks(self):
        res = self.gate_with(STUB_OSV_OUT=osv_out(("GHSA-1", "5.0", None)))
        self.assertEqual(res["gate"], "FAIL", self.checks_md())


def semgrep_at(rule, path, severity="ERROR"):
    return json.dumps({"errors": [], "results": [{"check_id": rule, "path": path, "start": {"line": 12},
                                                  "extra": {"severity": severity, "message": "m", "metadata": {}}}]})


class ScannerRunsOutOfTimeTest(CoverageCase):
    def slow(self, constant, tool):
        limit = mock.patch.object(fx, constant, 0.2)
        limit.start()
        self.addCleanup(limit.stop)
        env = mock.patch.dict(os.environ, {"STUB_SLEEP_" + tool: "3"})
        env.start()
        self.addCleanup(env.stop)

    def assert_gate_says_so(self, scanner):
        self.f.commit({"a.py": "x\n"})
        code, res = self.gate()  # no traceback: an exception would end the test here
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("no result within", self.checks_md())
        self.assertIn(scanner, self.checks_md())

    def test_semgrep(self):
        self.slow("SEMGREP_TIMEOUT_SEC", "SEMGREP")
        self.assert_gate_says_so("semgrep")

    def test_osv(self):
        self.slow("OSV_TIMEOUT_SEC", "OSV_SCANNER")
        self.assert_gate_says_so("osv")

    def test_gitleaks(self):
        self.slow("GITLEAKS_TIMEOUT_SEC", "GITLEAKS")
        self.assert_gate_says_so("gitleaks")

    def test_not_required_it_goes_to_a_human(self):
        self.set_security(required=["gitleaks", "osv"])
        self.slow("SEMGREP_TIMEOUT_SEC", "SEMGREP")
        self.f.commit({"a.py": "x\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(self.f.job()["security_missed"], ["semgrep"])

    def test_audit_scan_still_writes_its_report(self):
        self.slow("OSV_TIMEOUT_SEC", "OSV_SCANNER")
        code, res = self.fx("audit-scan")
        self.assertEqual(code, 0, res)
        self.assertFalse(res["complete"])
        self.assertEqual(res["missed"], ["osv"])
        self.assertIn("no result within", slurp(res["report"]))


class SemgrepThatReadNothingTest(CoverageCase):
    BROKEN = json.dumps({"results": [], "paths": {"scanned": []},
                         "errors": [{"level": "error", "type": "InvalidRuleSchemaError", "long_msg": "bad rule at line 3"}]})

    def test_a_broken_rules_file_is_not_a_clean_scan(self):
        res = self.gate_with(STUB_SEMGREP_OUT=self.BROKEN)
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("bad rule at line 3", self.checks_md())

    def test_an_error_that_says_nothing_still_counts(self):
        out = json.dumps({"results": [], "errors": [{"level": "error", "type": "Something"}]})
        res = self.gate_with(STUB_SEMGREP_OUT=out)
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("Something", self.checks_md())

    def test_doctor_says_so_too(self):
        with mock.patch.dict(os.environ, {"STUB_SEMGREP_OUT": self.BROKEN}):
            code, res = self.fx("doctor")
        row = next(c for c in res["checks"] if c["check"] == "semgrep can load its rules")
        self.assertFalse(row["ok"])
        self.assertIn("bad rule at line 3", row["detail"])


class SemgrepLooksAtTheProjectTest(CoverageCase):
    def test_what_it_skips_comes_from_fx(self):
        self.gate_with()
        argv = self.f.calls("semgrep")[-1]["argv"]
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"], fx.SEMGREP_EXCLUDE)
        self.assertIn("--x-ignore-semgrepignore-files", argv)  # the builder's .semgrepignore stays out

    def test_a_factory_can_change_the_list(self):
        self.set_security(semgrep_exclude=["third_party"])
        self.gate_with()
        argv = self.f.calls("semgrep")[-1]["argv"]
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"], ["third_party"])

    def test_vendored_code_does_not_block(self):
        for path in ("vendor/a/b.py", "node_modules/x/a.js", "web/dist/app.js", "public/app.min.js"):
            self.assertTrue(fx.semgrep_skips(path, fx.SEMGREP_EXCLUDE), path)
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_at("sqli", "vendor/a/b.py"))
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertNotIn("vendor/a/b.py", self.security_md())

    def test_the_projects_own_code_still_blocks(self):
        self.assertFalse(fx.semgrep_skips("src/vendors.py", fx.SEMGREP_EXCLUDE))
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_at("sqli", "src/app.py"))
        self.assertEqual(res["gate"], "FAIL", self.checks_md())

    def test_a_warning_in_a_test_file_is_reported_without_blocking(self):
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_at("weak-hash", "tests/test_a.py", "WARNING"))
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        rows = [l for l in self.security_md().splitlines() if "weak-hash" in l]
        self.assertTrue(rows[0].startswith("| test code"), rows)

    def test_it_also_leaves_a_note(self):
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_at("weak-hash", "tests/test_a.py", "WARNING"))
        self.assertTrue(any("in test code do not block: tests/test_a.py" in n for n in res["notes"]), res["notes"])

    def test_code_in_folders_that_only_sound_like_tests_blocks(self):
        for path in ("src/factories/pay.py", "db/seeds/users.py", "lib/mocks/api.py", "lib/stubs/x.py"):
            self.assertFalse(fx.TEST_ONLY_RE.search(path), path)
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_at("weak-hash", "src/factories/pay.py", "WARNING"))
        self.assertEqual(res["gate"], "FAIL", self.checks_md())

    def test_something_serious_in_a_test_file_still_blocks(self):
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_at("sqli", "tests/test_a.py", "ERROR"))
        self.assertEqual(res["gate"], "FAIL", self.checks_md())

    def test_what_semgrep_itself_skips(self):
        # Checked against semgrep 1.183: no slash, anywhere on the way; a slash, from the start of the path.
        skips = fx.semgrep_skips
        self.assertTrue(skips("a/build/u.py", ["build"]))
        self.assertTrue(skips("web/c.bundle.js", ["*.bundle.js"]))
        self.assertTrue(skips("src/generated/g.py", ["src/generated"]))
        self.assertTrue(skips("a/build/u.py", ["a/build"]))
        self.assertFalse(skips("a/src/generated/g.py", ["src/generated"]))
        self.assertFalse(skips("x/src/generatedish/g.py", ["src/generated"]))
        self.assertFalse(skips("src/vendors.py", ["vendor"]))
        self.assertTrue(skips("a/b/generated/g.py", ["**/generated"]))
        self.assertTrue(skips("src/generated/g.py", ["/src/generated/"]))

    def job_in_a_skipped_place(self, path, severity="ERROR", **security):
        if security:
            self.set_security(**security)
        self.f.commit({path: "eval(x)\n"})
        return self.gate_with(STUB_SEMGREP_OUT=semgrep_at("sqli", path, severity))

    def assert_scanned(self, path):
        self.assertEqual(self.job_in_a_skipped_place(path)["gate"], "FAIL", self.checks_md())
        self.assertIn(path, self.security_md())

    def test_a_job_changes_a_file_in_a_build_folder(self):
        self.assert_scanned("app/payments/build/pay.py")

    def test_a_job_changes_a_bundle(self):
        self.assert_scanned("web/src/checkout.bundle.js")

    def test_a_job_changes_a_file_in_vendor(self):
        self.assert_scanned("vendor/mine.py")

    def test_a_folder_of_two_parts_too(self):
        res = self.job_in_a_skipped_place("src/generated/pay.py", semgrep_exclude=["src/generated"])
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertTrue(any("src/generated/pay.py" in n for n in res["notes"]), res["notes"])

    def test_they_are_named_and_nothing_is_excluded(self):
        self.job_in_a_skipped_place("vendor/mine.py")
        first, second = self.f.calls("semgrep")[-2:]
        self.assertIn("--exclude", first["argv"])
        self.assertNotIn("--exclude", second["argv"])
        self.assertEqual(second["argv"][-1], "./vendor/mine.py")
        self.assertIn("--baseline-commit", second["argv"], "what was there before is not this job's")
        self.assertIn("--disable-nosem", second["argv"])

    def test_a_file_the_job_deleted_is_not_named(self):
        self.f.commit({"vendor/gone.py": "x\n"})
        sh(["git", "push", "-q", "origin", "HEAD:refs/heads/main"], self.f.wt)
        sh(["git", "fetch", "-q", "origin"], self.f.wt)
        sh(["git", "rm", "-q", "vendor/gone.py"], self.f.wt)
        self.gate_with()
        self.assertEqual(len(self.f.calls("semgrep")), 1)

    def test_with_nothing_there_it_runs_once(self):
        self.gate_with()
        self.assertEqual(len(self.f.calls("semgrep")), 1)

    def test_what_the_job_did_not_change_stays_skipped(self):
        self.f.commit({"vendor/mine.py": "x\n"})
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_at("sqli", "vendor/other.py"))
        self.assertEqual(res["gate"], "PASS", self.checks_md())

    def test_a_human_can_accept_a_vendored_file(self):
        accepted = [{"rule": "sqli", "path": "vendor/**", "reason": "third party, looked at it"}]
        res = self.job_in_a_skipped_place("vendor/mine.py", accepted_rules=accepted)
        self.assertEqual(res["gate"], "PASS", self.checks_md())

    def test_many_files_go_in_batches(self):
        with mock.patch.object(fx, "SEMGREP_NAMED_BATCH", 2):
            self.f.commit({"vendor/%d.py" % i: "x\n" for i in range(5)})
            res = self.gate_with(STUB_SEMGREP_OUT=semgrep_at("sqli", "vendor/4.py"))
        calls = self.f.calls("semgrep")
        self.assertEqual(len(calls), 4, "the usual run and three batches")
        self.assertEqual(sorted(a for c in calls[1:] for a in c["argv"] if a.startswith("./")),
                         ["./vendor/%d.py" % i for i in range(5)])
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertEqual(self.f.job()["security_missed"], [])

    def test_a_link_is_not_named(self):
        """Semgrep stops with `Invalid scanning root` on a link, and committed vendor trees have them."""
        write(os.path.join(self.f.wt, "build", "real.py"), "x\n")
        os.symlink("real.py", os.path.join(self.f.wt, "build", "latest"))
        sh(["git", "add", "-Af", "build"], self.f.wt)
        sh(["git", "commit", "-q", "-m", "a link"], self.f.wt)
        self.assertEqual(self.gate_with()["gate"], "PASS", self.checks_md())
        self.assertEqual([a for a in self.f.calls("semgrep")[-1]["argv"] if a.startswith("./")], ["./build/real.py"])

    def test_no_size_limit_for_what_the_job_changes(self):
        """Above 1 MB semgrep passes over a file without a word."""
        self.gate_with()
        argv = self.f.calls("semgrep")[-1]["argv"]
        self.assertEqual(argv[argv.index("--max-target-bytes") + 1], "0")

    def test_a_failure_of_that_run_counts(self):
        real = fx.run_sep
        calls = []

        def second_fails(cmd, **k):
            calls.append(cmd)
            r = real(cmd, **k)
            if cmd[0] == "semgrep" and len(calls) > 1 and any(a.startswith("./") for a in cmd):
                r.returncode, r.stdout = 2, ""
            return r
        self.f.commit({"vendor/mine.py": "x\n"})
        with mock.patch.object(fx, "run_sep", second_fails):
            self.gate_with()
        self.assertEqual(self.f.job()["security_missed"], ["semgrep"])

    def test_a_file_in_a_test_folder_without_a_tests_name_goes_to_a_human(self):
        res = self.job_in_a_skipped_place("app/api/tests/handlers.py", "WARNING")
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertIn("app/api/tests/handlers.py", self.f.job()["flags"])
        self.assertTrue(any("a human must look" in n for n in res["notes"]), res["notes"])

    def test_a_tests_own_name_is_enough(self):
        for path in ("tests/test_a.py", "app/api/tests/conftest.py", "src/a.test.js", "src/a_test.go", "a/b.spec.ts",
                     "tests/Feature/UserTest.php", "src/test/java/FooTest.java", "spec/user_spec.rb", "__tests__/Button.js",
                     "e2e/login.cy.ts", "src/test/java/UserIT.java"):
            with self.subTest(path=path):
                self.assertTrue(fx.TEST_ONLY_RE.search(path) or path.endswith("_spec.rb"))
                self.assertTrue(fx.TEST_NAME_RE.search(path), path)
        for path in ("app/api/tests/handlers.py", "tests/helpers.py", "src/latest.py", "src/contest.py", "spec/support.rb",
                     "src/AUDIT.java"):
            with self.subTest(path=path):
                self.assertFalse(fx.TEST_NAME_RE.search(path), path)
        res = self.job_in_a_skipped_place("tests/test_x.py", "WARNING")
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(self.f.job()["flags"], [])

    def test_accepted_it_needs_nobody(self):
        accepted = [{"rule": "sqli", "path": "app/api/tests/*", "reason": "helper, looked at it"}]
        self.job_in_a_skipped_place("app/api/tests/handlers.py", "WARNING", accepted_rules=accepted)
        self.assertEqual(self.f.job()["flags"], [])

    def test_the_note_names_a_file_with_an_accent(self):
        self.f.commit({"vendor/caf\u00e9.py": "eval(x)\n"})
        res = self.gate_with()
        self.assertTrue(any("vendor/caf\u00e9.py" in n for n in res["notes"]), res["notes"])

    def test_audit_scan_skips_them_too(self):
        with mock.patch.dict(os.environ, {"STUB_SEMGREP_OUT": semgrep_at("sqli", "vendor/a/b.py")}):
            code, res = self.fx("audit-scan")
        self.assertEqual(res["counts"], {})


class AcceptedForAFileTest(CoverageCase):
    ENTRY = {"rule": "sqli", "path": "admin/*", "reason": "an admin report, checked by hand"}

    def test_in_that_file_it_does_not_block(self):
        self.set_security(accepted_rules=[self.ENTRY])
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_at("sqli", "admin/report.py"))
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertIn("accepted: an admin report", self.security_md())

    def test_in_another_file_it_still_does(self):
        self.set_security(accepted_rules=[self.ENTRY])
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_at("sqli", "api/public_search.py"))
        self.assertEqual(res["gate"], "FAIL", self.checks_md())

    def test_another_rule_in_that_file_still_does(self):
        self.set_security(accepted_rules=[self.ENTRY])
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_at("xss", "admin/report.py"))
        self.assertEqual(res["gate"], "FAIL", self.checks_md())

    def test_a_dependency_by_its_lockfile(self):
        self.set_security(accepted_rules=[{"rule": "GHSA-1", "path": "tools/*", "reason": "only a build tool"}])
        vuln = {"tool": "osv", "severity": "high", "rule": "GHSA-1", "path": "web/package-lock.json"}
        self.assertIsNone(fx.accepted_reason(fx.accepted_entries(self.f.ctx()), vuln))
        self.assertEqual(fx.accepted_reason(fx.accepted_entries(self.f.ctx()), dict(vuln, path="tools/package-lock.json")),
                         "only a build tool")

    def test_zap_only_if_every_page_matches(self):
        entries = [{"rule": "zap-1", "path": "/admin/*", "reason": "x"}]
        self.assertTrue(fx.accepted_reason(entries, {"rule": "zap-1", "paths": ["/admin/a", "/admin/b"]}))
        self.assertIsNone(fx.accepted_reason(entries, {"rule": "zap-1", "paths": ["/admin/a", "/shop"]}))
        self.assertIsNone(fx.accepted_reason(entries, {"rule": "zap-1", "paths": []}))

    def test_a_star_does_not_reach_into_folders_below(self):
        self.set_security(accepted_rules=[{"rule": "sqli", "path": "src/legacy/*.py", "reason": "old"}])
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_at("sqli", "src/legacy/new/deep/evil.py"))
        self.assertEqual(res["gate"], "FAIL", self.checks_md())

    def test_a_header_finding_can_be_accepted_for_its_path(self):
        entries = [{"rule": "clickjacking", "path": "/", "reason": "no frames here"}]
        self.assertEqual(fx.accepted_reason(entries, {"rule": "clickjacking", "paths": ["/"]}), "no frames here")

    def test_a_list_of_globs_is_refused_not_a_crash(self):
        self.assert_refused([{"rule": "sqli", "path": ["src/*.py"], "reason": "x"}])

    def test_a_secret_cannot_be_accepted_for_a_file(self):
        self.assertIsNone(fx.accepted_reason([{"rule": "k", "reason": "x"}], {"rule": "k", "secret": True, "path": "a"}))

    def test_the_older_form_covers_every_file_and_doctor_says_so(self):
        self.set_security(accepted_rules={"sqli": "admin only"})
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_at("sqli", "api/public_search.py"))
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        row = next(c for c in self.fx("doctor")[1]["checks"] if c["check"].startswith("accepted rules name their files"))
        self.assertIn("every file", row["detail"])

    def assert_refused(self, value):
        self.set_security(accepted_rules=value)
        self.f.commit({"a.py": "x\n"})
        with mock.patch.dict(os.environ, {"STUB_SEMGREP_OUT": semgrep_out(("sqli", "ERROR"))}):
            code, res = self.gate()
        self.assertEqual(code, 3, res)
        self.assertIn("security.accepted_rules must be", res["error"])
        row = next(c for c in self.fx("doctor")[1]["checks"] if c["check"] == "security.accepted_rules")
        self.assertFalse(row["ok"])

    def test_a_list_of_names_is_refused(self):
        self.assert_refused(["sqli"])

    def test_a_string_is_refused(self):
        self.assert_refused("sqli")

    def test_an_entry_with_no_reason_is_refused(self):
        self.assert_refused([{"rule": "sqli", "path": "a/*"}])

    def test_a_local_rule_means_the_same_on_every_machine(self):
        ctx = self.f.ctx()
        dotted = ctx.factory.strip(os.sep).replace(os.sep, ".")
        self.assertEqual(fx.semgrep_rule_id(ctx, dotted + ".rules.py-eval"), "rules.py-eval")
        self.assertEqual(fx.semgrep_rule_id(ctx, "python.lang.eval"), "python.lang.eval")
        write(os.path.join(ctx.factory, "rules", "ours.yml"), "rules: []\n")
        self.set_security(semgrep_config="rules/ours.yml", accepted_rules=[
            {"rule": "rules.py-eval", "path": "*", "reason": "x"}])
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_at(dotted + ".rules.py-eval", "a.py"))
        self.assertEqual(res["gate"], "PASS", self.checks_md())


class AcceptedRulesTest(CoverageCase):
    def test_an_accepted_rule_does_not_block_and_shows_why(self):
        self.set_security(accepted_rules={"xss.raw-html": "admin tool only, checked by hand"})
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_out(("xss.raw-html", "ERROR"), ("sqli.tainted", "ERROR")))
        self.assertEqual(res["gate"], "FAIL")  # the other rule still blocks
        md = self.security_md()
        self.assertIn("accepted: admin tool only, checked by hand", md)
        rows = [l for l in md.splitlines() if "sqli.tainted" in l]
        self.assertTrue(rows[0].startswith("| yes "), rows)

    def test_a_secret_cannot_be_accepted(self):
        self.set_security(accepted_rules={"aws-access-token": "no"})
        leak = json.dumps([{"RuleID": "aws-access-token", "File": "a", "StartLine": 1, "Commit": "abc"}])
        res = self.gate_with(STUB_GITLEAKS_REPORT=leak)
        self.assertEqual(res["gate"], "FAIL", self.checks_md())


class RequiredByDefaultTest(CoverageCase):
    tools = ("gitleaks", "osv-scanner")  # no semgrep

    def test_a_missing_semgrep_fails_the_gate(self):
        with not_on_this_machine("semgrep"):
            res = self.gate_with()
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("semgrep", self.checks_md())
        self.assertIn("required security scanner(s) did not run", self.checks_md())


WITH_DEPS = json.dumps({"dependencies": {"lodash": "4.17.0"}}) + "\n"


class DependencyGapsTest(CoverageCase):
    base_files = {"README.md": "x\n", "package.json": WITH_DEPS}

    def test_no_lockfile_is_not_a_clean_result(self):
        res = self.gate_with(STUB_OSV_SCANNER="nosources")
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("package.json has no lockfile", self.checks_md())

    def test_no_lockfile_and_not_required_goes_to_a_human(self):
        self.set_security(required=["gitleaks", "semgrep"])
        res = self.gate_with(STUB_OSV_SCANNER="nosources")
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(self.f.job()["security_missed"], ["osv"])
        self.assertTrue(any("lockfile" in s for s in res["skipped"]), res["skipped"])

    def test_some_manifests_already_without_a_lockfile_get_a_note(self):
        res = self.gate_with(STUB_OSV_OUT=json.dumps({"results": [{"packages": []}]}))
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertTrue(any("no lockfile it can read for package.json" in n for n in res["notes"]), res["notes"])

    def test_unpinned_packages_get_a_note(self):
        res = self.gate_with(STUB_OSV_STDERR="Filtered 2 local/unscannable package/s from the scan.\n")
        self.assertTrue(any("2 package(s) went unchecked" in n for n in res["notes"]), res["notes"])


class HidingFromOsvTest(CoverageCase):
    """Base: two manifests, each with its lockfile. A job must not get a library past osv by leaving the lockfile out."""
    base_files = {"README.md": "x\n", "composer.json": '{"require": {"a/b": "1.0"}}\n', "composer.lock": "{}\n",
                  "package.json": WITH_DEPS, "package-lock.json": "{}\n"}

    def assert_blocked_for(self, res, manifest):
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        rows = [l for l in self.security_md().splitlines() if "osv-no-lockfile" in l]
        self.assertTrue(rows and rows[0].startswith("| yes") and manifest in rows[0], self.security_md())

    def test_deleting_the_lockfile(self):
        sh(["git", "rm", "-q", "package-lock.json"], self.f.wt)
        res = self.gate_with()
        self.assert_blocked_for(res, "package.json")

    def test_a_new_manifest_with_no_lockfile(self):
        write(os.path.join(self.f.wt, "frontend", "package.json"), WITH_DEPS)
        sh(["git", "add", "-A"], self.f.wt)
        res = self.gate_with()
        self.assert_blocked_for(res, "frontend/package.json")

    def test_a_gitignored_lockfile_is_still_read(self):
        write(os.path.join(self.f.wt, ".gitignore"), "package-lock.json\n")
        sh(["git", "add", "-A"], self.f.wt)
        self.gate_with()
        args = [c["argv"] for c in self.f.calls("osv-scanner")]
        self.assertTrue(args and all("--no-ignore" in a for a in args), args)

    def test_only_what_is_committed_is_read(self):
        self.f.commit({".gitignore": "vendor/\n"})
        write(os.path.join(self.f.wt, "vendor", "acme", "composer.lock"), "{}\n")  # what an install leaves behind
        seen, real = [], fx.osv_scan_dir

        def spy(path, config, env=None):
            seen.append(os.listdir(path))
            return real(path, config, env)
        with mock.patch.object(fx, "osv_scan_dir", spy):
            self.gate_with()
        self.assertTrue(seen)
        for names in seen:
            self.assertNotIn("vendor", names)

    def export(self):
        dest = os.path.join(self.tmp, "export")
        self.assertIsNone(fx.export_commit(self.f.ctx(), self.f.wt, "HEAD", dest))
        return dest

    def test_export_ignore_does_not_leave_a_file_out(self):
        self.f.commit({".gitattributes": "package-lock.json export-ignore\n"})
        self.assertTrue(os.path.exists(os.path.join(self.export(), "package-lock.json")))

    def test_the_clone_cannot_re_encode_a_lockfile(self):
        self.f.commit({".gitattributes": "package-lock.json working-tree-encoding=UTF-16\n"})
        self.assertEqual(slurp(os.path.join(self.export(), "package-lock.json")), "{}\n")

    def test_a_filter_in_the_clone_never_runs(self):
        marker = os.path.join(self.tmp, "filter-ran")
        sh(["git", "config", "filter.x.smudge", "env > %s; cat" % marker], self.f.wt)
        write(os.path.join(self.f.wt, ".git", "info", "attributes"), "* filter=x\n")
        self.export()
        self.assertFalse(os.path.exists(marker))

    def test_replacing_an_object_changes_nothing(self):
        blob = sh(["git", "rev-parse", "HEAD:package-lock.json"], self.f.wt).strip()
        other = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.f.wt, input="replaced\n", text=True,
                               stdout=subprocess.PIPE).stdout.strip()
        sh(["git", "replace", blob, other], self.f.wt)
        self.assertEqual(slurp(os.path.join(self.export(), "package-lock.json")), "{}\n")

    def test_a_tree_cannot_write_outside_the_export(self):
        self.f.commit({"ok.txt": "x\n"})
        self.assertTrue(os.path.exists(os.path.join(self.export(), "ok.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "export", ".git")))

    def test_a_lockfile_osv_cannot_read_counts_as_none(self):
        self.f.commit({"package-lock.json": "unreadable\n"})
        res = self.gate_with()
        self.assert_blocked_for(res, "package.json")

    def test_a_symlinked_lockfile_counts_as_none(self):
        os.remove(os.path.join(self.f.wt, "package-lock.json"))
        os.symlink("/dev/null", os.path.join(self.f.wt, "package-lock.json"))
        sh(["git", "add", "-A"], self.f.wt)
        res = self.gate_with()
        self.assert_blocked_for(res, "package.json")

    def test_a_dependency_added_with_no_change_to_a_lockfile_blocks(self):
        self.f.commit({"package.json": json.dumps({"dependencies": {"lodash": "4.17.0", "left-pad": "1.0.0"}}) + "\n"})
        res = self.gate_with()
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("osv-lock-stale", self.security_md())

    def test_other_changes_to_a_manifest_do_not(self):
        self.f.commit({"package.json": json.dumps({"dependencies": {"lodash": "4.17.0"}, "scripts": {"t": "x"}}) + "\n"})
        self.assertEqual(self.gate_with()["gate"], "PASS", self.checks_md())

    def test_removing_a_dependency_does_not(self):
        self.f.commit({"package.json": "{}\n"})
        self.assertEqual(self.gate_with()["gate"], "PASS", self.checks_md())

    def test_a_lockfile_kept_is_no_finding(self):
        self.assertEqual(self.gate_with()["gate"], "PASS", self.checks_md())


class BytesThatAreNotUtf8Test(CoverageCase):
    """git prints names and file contents as they are: the first such byte used to end the gate with a traceback."""
    NAME = b"caf\xe9.txt"

    def commit_the_name(self):
        """A real file where this machine allows it. A Mac refuses the name, so there it is only in the commit."""
        wt = self.f.wt
        try:
            with open(os.path.join(os.fsencode(wt), self.NAME), "wb") as f:
                f.write(b"x\n")
            sh(["git", "add", "-A"], wt)
            on_disk = True
        except OSError:
            blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=wt, input=b"x\n", stdout=subprocess.PIPE,
                                  check=True).stdout.decode().strip()
            subprocess.run(["git", "update-index", "--add", "--index-info"], cwd=wt, check=True,
                           input=b"100644 " + blob.encode() + b"\t" + self.NAME + b"\n")
            on_disk = False
        sh(["git", "commit", "-q", "-m", "a name"], wt)
        return on_disk

    def test_a_file_name_does_not_stop_the_gate(self):
        on_disk = self.commit_the_name()
        code, res = self.gate()
        self.assertEqual(code, 0, res)
        self.assertIn(res["gate"], ("PASS", "FAIL"))
        if on_disk:
            self.assertEqual(res["gate"], "PASS", self.checks_md())
        else:
            self.assertIn("Uncommitted changes", self.checks_md())  # the clone cannot hold it, and git says it is gone
        self.assertTrue(load_json(os.path.join(self.f.jd, "job.json")))

    def test_contents_do_not_stop_the_gate(self):
        with open(os.path.join(self.f.wt, "notes.txt"), "wb") as f:
            f.write(b"caf\xe9 eval(x)\n")
        sh(["git", "add", "-A"], self.f.wt)
        sh(["git", "commit", "-q", "-m", "latin-1"], self.f.wt)
        self.assertEqual(self.gate()[1]["gate"], "PASS", self.checks_md())

    def test_a_manifest_in_other_bytes(self):
        with open(os.path.join(self.f.wt, "package.json"), "wb") as f:
            f.write(b'{"description": "caf\xe9", "dependencies": {"lodash": "1.0.0"}}\n')
        sh(["git", "add", "-A"], self.f.wt)
        sh(["git", "commit", "-q", "-m", "manifest"], self.f.wt)
        res = self.gate()[1]
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("osv-no-lockfile", self.security_md())

    def commit_only(self, name):
        """A file only in the commit, in a clone git calls clean: git's own view of a name a checkout would refuse."""
        wt = self.f.wt
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=wt, input=b"x\n", stdout=subprocess.PIPE,
                              check=True).stdout.decode().strip()
        subprocess.run(["git", "update-index", "--add", "--index-info"], cwd=wt, check=True,
                       input=b"100644 " + blob.encode() + b"\t" + name + b"\n")
        subprocess.run(["git", "update-index", "--skip-worktree", "--stdin"], cwd=wt, input=name + b"\n", check=True)
        sh(["git", "commit", "-q", "-m", "only in the commit"], wt)

    def test_a_file_that_cannot_be_written_is_no_clean_scan(self):
        """A lockfile the export cannot write used to pass as if it were not there."""
        name = b"x" * 300 + b"/requirements.txt"
        self.commit_only(name)
        res = self.gate()[1]
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("could not write", self.checks_md())
        self.assertEqual(self.f.job()["security_missed"], ["osv"])

    def test_on_a_mac_a_name_git_can_hold_gets_as_far_as_the_scanners(self):
        if sys.platform != "darwin":
            self.skipTest("only a Mac refuses this name")
        self.commit_only(self.NAME)
        res = self.gate()[1]
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("could not write", self.checks_md())

    def test_commands_keep_such_bytes(self):
        out = fx.run_sep([sys.executable, "-I", "-c", "import os; os.write(1, b'caf\\xe9')"])
        self.assertEqual(out.stdout.encode("utf-8", "surrogateescape"), b"caf\xe9")
        path = os.path.join(self.tmp, "raw.txt")
        with open(path, "wb") as f:
            f.write(b"caf\xe9\n")
        self.assertEqual(fx.read(path).encode("utf-8", "surrogateescape"), b"caf\xe9\n")

    def test_reports_and_output_take_them(self):
        name = self.NAME.decode("utf-8", "surrogateescape")
        path = os.path.join(self.tmp, "report.md")
        fx.write_atomic(path, "about %s\n" % name)
        self.assertEqual(fx.read(path), "about caf\\udce9.txt\n")
        buf = io.BytesIO()
        stdout = io.TextIOWrapper(buf, encoding="utf-8")
        with mock.patch.object(fx.sys, "stdout", stdout):
            fx.out({"file": name})
        stdout.flush()
        self.assertEqual(json.loads(buf.getvalue().decode("utf-8")), {"file": name})


class SameVulnerabilityInSeveralLockfilesTest(CoverageCase):
    """The same library in two lockfiles used to be one finding, named after whichever was read last."""

    @staticmethod
    def report(root, where):
        pkg = {"package": {"name": "lodash", "version": "4.17.0", "ecosystem": "npm"},
               "groups": [{"ids": ["GHSA-1"], "max_severity": "9.8"}],
               "vulnerabilities": [{"id": "GHSA-1", "summary": "bad"}]}
        return {"results": [{"source": {"path": os.path.join(root, w, "package-lock.json")}, "packages": [pkg]} for w in where]}

    def gate_on(self, base, head, **security):
        if security:
            self.set_security(required=[], scanners={"zap": False, "headers": False}, **security)
        self.f.commit({"api/package-lock.json": "{}\n", "tools/package-lock.json": "{}\n"})

        def scan(path, config, env=None):
            return self.report(path, base if "fx-osv-base-" in path else head), None
        with mock.patch.object(fx, "osv_scan_dir", scan):
            return self.gate()[1]["gate"]

    def rows(self, where):
        return [l for l in self.security_md().splitlines() if "GHSA-1" in l and where in l]

    def test_accepting_it_for_one_place_does_not_hide_the_other(self):
        accepted = [{"rule": "GHSA-1", "path": "tools/*", "reason": "only a build tool"}]
        self.assertEqual(self.gate_on([], ["api", "tools"], accepted_rules=accepted), "FAIL", self.checks_md())
        self.assertTrue(self.rows("api/package-lock.json")[0].startswith("| yes"), self.security_md())
        self.assertTrue(self.rows("tools/package-lock.json")[0].startswith("| accepted: only a build tool"), self.security_md())

    def test_accepted_for_every_place_it_does_not_block(self):
        accepted = [{"rule": "GHSA-1", "path": "*/package-lock.json", "reason": "looked at it"}]
        self.assertEqual(self.gate_on([], ["api", "tools"], accepted_rules=accepted), "PASS", self.checks_md())

    def test_a_job_that_brings_an_old_one_to_a_new_place_brought_it(self):
        self.assertEqual(self.gate_on(["tools"], ["tools", "api"]), "FAIL", self.checks_md())
        self.assertTrue(self.rows("api/package-lock.json")[0].startswith("| yes"), self.security_md())
        self.assertFalse(self.rows("tools/package-lock.json")[0].startswith("| yes"), self.security_md())

    def test_where_it_already_was_it_does_not_block(self):
        self.assertEqual(self.gate_on(["tools", "api"], ["tools", "api"]), "PASS", self.checks_md())


class DependencyNotInItsLockTest(CoverageCase):
    """Base: a package.json listing lodash and a composer.json, each with its lockfile. What a job adds must be in the
    lockfile that speaks for it; that some lockfile somewhere changed proves nothing."""
    MORE = json.dumps({"dependencies": {"lodash": "4.17.0", "left-pad": "1.3.0"}}) + "\n"
    base_files = {"README.md": "x\n", "package.json": WITH_DEPS, "package-lock.json": "pkg:lodash\n",
                  "composer.json": '{"require": {"a/b": "1.0"}}\n', "composer.lock": "pkg:a/b\n"}

    def assert_stale(self, files, name="left-pad"):
        self.f.commit(files)
        res = self.gate_with()
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        rows = [l for l in self.security_md().splitlines() if "osv-lock-stale" in l]
        self.assertTrue(rows and rows[0].startswith("| yes") and name in rows[0], self.security_md())

    def assert_fine(self, files):
        self.f.commit(files)
        res = self.gate_with()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertNotIn("osv-lock-stale", self.security_md())

    def test_nothing_else_changed(self):
        self.assert_stale({"package.json": self.MORE})

    def test_a_blank_line_in_its_lockfile(self):
        self.assert_stale({"package.json": self.MORE, "package-lock.json": "pkg:lodash\n\n"})

    def test_a_new_lockfile_somewhere_else(self):
        self.assert_stale({"package.json": self.MORE, "docs/yarn.lock": "pkg:left-pad\n"})

    def test_another_ecosystems_lockfile(self):
        self.assert_stale({"package.json": self.MORE, "composer.lock": "pkg:a/b\npkg:left-pad\n"})

    def test_a_wider_version_range_adds_nothing(self):
        self.assert_fine({"package.json": json.dumps({"dependencies": {"lodash": "^4.0.0"}})})

    def test_a_package_in_the_lockfile_is_fine(self):
        self.assert_fine({"package.json": self.MORE, "package-lock.json": "pkg:lodash\npkg:left-pad\n"})

    def test_a_workspace_member_with_the_lockfile_above(self):
        self.f.commit({"package.json": json.dumps({"workspaces": ["apps/*"], "dependencies": {"lodash": "4.17.0"}})})
        member = json.dumps({"name": "web", "dependencies": {"chalk": "5.0.0"}})
        self.assert_fine({"apps/web/package.json": member, "package-lock.json": "pkg:lodash\npkg:chalk\n"})

    def test_and_with_the_lockfile_above_not_listing_it(self):
        self.f.commit({"package.json": json.dumps({"workspaces": ["apps/*"], "dependencies": {"lodash": "4.17.0"}})})
        member = json.dumps({"name": "web", "dependencies": {"chalk": "5.0.0"}})
        self.assert_stale({"apps/web/package.json": member}, "chalk")

    def test_an_alias_is_looked_up_by_the_real_name(self):
        self.assert_fine({"package.json": json.dumps({"dependencies": {"lodash": "4.17.0", "al": "npm:left-pad@1.3.0"}}),
                          "package-lock.json": "pkg:lodash\npkg:left-pad\n"})

    def test_a_scoped_alias_too(self):
        self.assert_fine({"package.json": json.dumps({"dependencies": {"lodash": "4.17.0", "al": "npm:@x/y@1.0.0"}}),
                          "package-lock.json": "pkg:lodash\npkg:@x/y\n"})

    def test_and_a_scoped_alias_not_listed(self):
        self.assert_stale({"package.json": json.dumps({"dependencies": {"lodash": "4.17.0", "al": "npm:@x/y@1.0.0"}})}, "al")

    def test_what_is_on_this_machine_needs_no_lockfile(self):
        self.assert_fine({"package.json": json.dumps({"dependencies": {
            "lodash": "4.17.0", "a": "file:../a", "b": "workspace:*", "c": "link:../c", "d": "../d", "e": "./e"}})})

    def spec(self, spec):
        return {"package.json": json.dumps({"dependencies": {"lodash": "4.17.0", "foo": spec}})}

    def test_a_tarball_added_with_no_change_to_the_lockfile(self):
        self.assert_stale(self.spec("https://evil.example/evil.tgz"), "foo")

    def test_a_git_dependency_added_with_no_change_to_the_lockfile(self):
        for spec in ("git+https://github.com/u/foo.git", "github:u/foo", "u/foo#v1", "git@github.com:u/foo.git"):
            with self.subTest(spec=spec):
                self.assertEqual(fx.git_repo_name(spec), "github.com/u/foo")
        self.assert_stale(self.spec("git+https://github.com/u/foo.git"), "foo")

    def test_a_git_dependency_listed_by_its_repository_is_fine(self):
        """As the real osv-scanner lists one from a package-lock.json."""
        for spec in ("u/foo#v1", "git@github.com:u/foo.git", "ssh://git@github.com/u/foo.git", "github:u/foo",
                     "git+https://github.com/u/foo.git", "https://github.com/u/foo"):
            with self.subTest(spec=spec):
                what = fx.dependency_identity("foo", spec)
                listed = fx.git_repo_name("https://github.com/u/foo")
                self.assertEqual(what, listed)
        self.assert_fine(dict(self.spec("u/foo#v1"), **{"package-lock.json": "pkg:lodash\npkg:https://github.com/u/foo\n"}))

    def test_a_tarball_listed_under_its_own_name_is_fine(self):
        self.assert_fine(dict(self.spec("https://x.example/foo.tgz"), **{"package-lock.json": "pkg:lodash\npkg:foo\n"}))

    def test_an_old_name_pointed_at_another_package(self):
        self.assert_stale({"package.json": json.dumps({"dependencies": {"lodash": "npm:evil-package@1.0.0"}})}, "lodash")

    def test_an_old_name_pointed_at_a_repository(self):
        self.assert_stale({"package.json": json.dumps({"dependencies": {"lodash": "github:evil/lodash"}})}, "lodash")

    def test_a_workspace_written_with_dot_slash(self):
        self.f.commit({"package.json": json.dumps({"workspaces": ["./apps/*"], "dependencies": {"lodash": "4.17.0"}})})
        member = json.dumps({"name": "web", "dependencies": {"chalk": "5.0.0"}})
        self.assert_fine({"apps/web/package.json": member, "package-lock.json": "pkg:lodash\npkg:chalk\n"})

    def test_a_workspaces_own_package_may_only_be_linked(self):
        """pnpm resolves it to link:, and osv-scanner lists nothing for it."""
        self.f.commit({"pnpm-workspace.yaml": "packages:\n  - 'apps/*'\n", "apps/b/package.json": json.dumps({"name": "@acme/b"}),
                       "pnpm-lock.yaml": "pkg:lodash\n"})
        self.assert_fine({"apps/a/package.json": json.dumps({"name": "a", "dependencies": {"@acme/b": "^1.0.0"}})})

    def test_composer_may_ask_for_what_a_package_provides(self):
        lock = json.dumps({"packages": [{"name": "monolog/monolog", "provide": {"psr/log-implementation": "1.0"}}], "x": "pkg:a/b"})
        self.assert_fine({"composer.json": '{"require": {"a/b": "1.0", "psr/log-implementation": "*"}}\n', "composer.lock": lock})

    def test_or_replaces(self):
        lock = json.dumps({"packages": [{"name": "s/s", "replace": {"s/http": "self.version"}}], "x": "pkg:a/b"})
        self.assert_fine({"composer.json": '{"require": {"a/b": "1.0", "s/http": "*"}}\n', "composer.lock": lock})

    def test_composer_asks_for_what_nothing_provides(self):
        lock = json.dumps({"packages": [{"name": "m/m", "provide": {"x/y": "1.0"}}], "x": "pkg:a/b"})
        self.assert_stale({"composer.json": '{"require": {"a/b": "1.0", "c/d": "*"}}\n', "composer.lock": lock}, "c/d")

    def test_a_peer_is_the_installers_to_find(self):
        self.assert_fine({"package.json": json.dumps({"dependencies": {"lodash": "4.17.0"},
                                                      "peerDependencies": {"react": "18.0.0"}})})

    def test_composer_by_name_too(self):
        self.assert_stale({"composer.json": '{"require": {"a/b": "1.0", "c/d": "1.0"}}\n', "composer.lock": "pkg:a/b\n\n"},
                          "c/d")

    def test_composer_names_in_any_case(self):
        self.assert_fine({"composer.json": '{"require": {"a/b": "1.0", "C/D": "1.0"}}\n',
                          "composer.lock": "pkg:a/b\npkg:c/d\n"})

    def test_a_php_extension_belongs_to_the_machine(self):
        self.assert_fine({"composer.json": '{"require": {"a/b": "1.0", "ext-json": "*", "php": ">=8.1"}}\n'})


class ComposerOnlyAsksForTheMachineTest(CoverageCase):
    """No package at all: osv-scanner finds 0 in such a composer.lock and exits 128, which is no reason to fail a gate."""
    ONLY = json.dumps({"require": {"php": ">=8.1", "ext-json": "*", "lib-curl": "*", "composer-plugin-api": "^2.0"},
                       "require-dev": {"composer-runtime-api": "^2.0"}}) + "\n"
    base_files = {"README.md": "x\n", "composer.json": ONLY, "composer.lock": '{"packages": [], "packages-dev": []}\n'}

    def test_the_gate_passes(self):
        with mock.patch.dict(os.environ, STUB_OSV_SCANNER="nosources"):
            res = self.gate_with()
        self.assertEqual(res["gate"], "PASS", self.checks_md())

    def test_it_declares_nothing(self):
        self.assertFalse(fx.manifest_declares_dependencies("composer.json", self.ONLY))

    def test_a_real_package_still_counts(self):
        self.assertTrue(fx.manifest_declares_dependencies("composer.json", '{"require": {"php": "8", "a/b": "1"}}'))
        self.assertTrue(fx.manifest_declares_dependencies("composer.json", '{"require-dev": {"a/b": "1"}}'))

    def test_what_makes_no_sense_counts_as_declaring(self):
        self.assertTrue(fx.manifest_declares_dependencies("composer.json", '{"require": "a/b"}'))
        self.assertTrue(fx.manifest_declares_dependencies("composer.json", "not json"))


class WhichManifestsTest(CoverageCase):
    def unlocked(self, files):
        self.f.commit(files)
        return fx.unlocked_manifests(self.f.wt)

    def test_a_lockfile_next_to_it(self):
        self.assertEqual(self.unlocked({"web/package.json": WITH_DEPS, "web/yarn.lock": "\n"}), [])

    def test_a_lockfile_above_it_only_counts_in_a_workspace(self):
        self.assertEqual(self.unlocked({"package-lock.json": "{}\n", "package.json": WITH_DEPS,
                                        "web/package.json": WITH_DEPS}), ["web/package.json"])
        self.assertEqual(self.unlocked({"package.json": '{"workspaces": ["web"], "dependencies": {"a": "1"}}\n'}), [])

    def test_and_only_for_the_folders_the_workspace_names(self):
        root = json.dumps({"workspaces": ["packages/*"], "scripts": {"x": "workspace"}, "dependencies": {"a": "1"}})
        self.assertEqual(self.unlocked({"package-lock.json": "{}\n", "package.json": root,
                                        "packages/a/package.json": WITH_DEPS, "tools/evil/package.json": WITH_DEPS}),
                         ["tools/evil/package.json"])

    def test_other_workspace_files(self):
        self.assertEqual(self.unlocked({"pnpm-lock.yaml": "x\n", "pnpm-workspace.yaml": "packages:\n  - 'apps/*'\n",
                                        "package.json": WITH_DEPS, "apps/a/package.json": WITH_DEPS}), [])
        self.assertEqual(self.unlocked({"Cargo.lock": "x\n", "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
                                        "crates/a/Cargo.toml": "[package]\n", "other/Cargo.toml": "[package]\n"}),
                         ["other/Cargo.toml"])

    def test_gradle_subprojects_share_the_root_lockfile_with_settings(self):
        self.assertEqual(self.unlocked({"gradle.lockfile": "\n", "build.gradle": "x\n", "settings.gradle": "x\n",
                                        "app/build.gradle": "x\n"}), [])

    def test_no_dependencies_nothing_to_lock(self):
        self.assertEqual(self.unlocked({"pyproject.toml": "[tool.ruff]\nline-length = 100\n", "package.json": "{}\n",
                                        "composer.json": '{"name": "a/b"}\n'}), [])

    def test_python_dependencies_are_recognised(self):
        self.assertEqual(self.unlocked({"pyproject.toml": '[project]\nname = "x"\ndependencies = [\n  "flask",\n]\n'}),
                         ["pyproject.toml"])
        self.assertEqual(self.unlocked({"pyproject.toml": "[tool.poetry.dependencies]\nflask = \"*\"\n"}), ["pyproject.toml"])
        self.assertEqual(self.unlocked({"pyproject.toml": '[project]\nname = "x"\ndependencies = []\n'}), [])

    def test_in_doubt_a_pyproject_declares(self):
        for text in ('[project]\ndependencies = [\n  # web\n  "flask",\n]\n', '[dependency-groups]\ndev = ["pytest"]\n',
                     '[tool.poetry.group.dev.dependencies]\npytest = "*"\n', '[tool.uv]\ndev-dependencies = ["pytest"]\n'):
            self.assertEqual(self.unlocked({"pyproject.toml": text}), ["pyproject.toml"], text)

    def test_what_is_not_the_project_does_not_count(self):
        self.assertEqual(self.unlocked({"docs/package.json": WITH_DEPS, "examples/a/package.json": WITH_DEPS,
                                        "tests/fixtures/x/package.json": WITH_DEPS, "vendor/y/composer.json": '{"require": {"a": "1"}}'}), [])

    def test_a_lockfile_osv_got_nothing_from_does_not_count(self):
        self.f.commit({"package.json": WITH_DEPS, "package-lock.json": "{}\n"})
        self.assertEqual(fx.unlocked_manifests(self.f.wt, "HEAD", set()), ["package.json"])
        self.assertEqual(fx.unlocked_manifests(self.f.wt, "HEAD", {"package-lock.json"}), [])

    def test_read_from_the_commit_not_the_folder(self):
        self.f.commit({"package.json": WITH_DEPS, "package-lock.json": "{}\n"})
        os.remove(os.path.join(self.f.wt, "package-lock.json"))
        self.assertEqual(fx.unlocked_manifests(self.f.wt), [])


class OsvOfAnOlderVersionTest(CoverageCase):
    """v1 calls the option --experimental-all-packages (read in the source of v1.9.2; exit 127 on any other error)."""

    def test_the_gate_says_what_to_do(self):
        res = self.gate_with(STUB_OSV_VERSION="1.9.2")
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("update to v2 or newer", self.checks_md())

    def test_install_counts_it_as_too_old(self):
        with mock.patch.dict(os.environ, STUB_OSV_VERSION="1.9.2"):
            tool = next(t for t in self.fx("install", "status")[1]["tools"] if t["name"] == "osv-scanner")
        self.assertFalse(tool["ok"], tool)

    def test_v2_is_fine(self):
        tool = next(t for t in self.fx("install", "status")[1]["tools"] if t["name"] == "osv-scanner")
        self.assertTrue(tool["ok"], tool)

    def test_doctor_says_so(self):
        with mock.patch.dict(os.environ, STUB_OSV_VERSION="1.9.2"):
            checks = {c["check"]: c for c in self.fx("doctor")[1]["checks"]}
        row = checks["required scanner osv installed"]
        self.assertFalse(row["ok"], row)
        self.assertIn("v2 or newer", row["detail"])


class TriedBeforeTheFirstGateTest(CoverageCase):
    base_files = {"README.md": "x\n", "package.json": WITH_DEPS}

    def doctor(self, **env):
        with mock.patch.dict(os.environ, env):
            return self.fx("doctor")[1]

    def row(self, res, name):
        return next(c for c in res["checks"] if c["check"].startswith(name))

    def test_semgrep_with_no_rules_fails_doctor_and_says_what_to_do(self):
        row = self.row(self.doctor(STUB_SEMGREP_OUT=""), "semgrep can load its rules")
        self.assertFalse(row["ok"])
        self.assertIn("no network", row["detail"])
        self.assertIn("semgrep_config", row["detail"])

    def test_not_required_it_is_only_advice(self):
        self.set_security(required=["gitleaks", "osv"])
        row = self.row(self.doctor(STUB_SEMGREP_OUT=""), "semgrep can load its rules (advice)")
        self.assertTrue(row["ok"])
        self.assertIn("no network", row["detail"])

    def test_osv_with_nothing_to_read_fails_doctor(self):
        row = self.row(self.doctor(STUB_OSV_SCANNER="nosources"), "osv-scanner can read the dependencies")
        self.assertFalse(row["ok"])
        self.assertIn("package.json has no lockfile", row["detail"])

    def test_some_dependencies_unreadable_is_advice(self):
        row = self.row(self.doctor(), "osv-scanner can read every dependency (advice)")
        self.assertTrue(row["ok"])
        self.assertIn("package.json", row["detail"])

    def test_selftest_says_the_same(self):
        with mock.patch.dict(os.environ, {"STUB_SEMGREP_OUT": ""}):
            code, res = self.fx("selftest", "--no-app")
        self.assertFalse(res["ok"])
        self.assertFalse(res["steps"]["scanners"]["ok"])
        self.assertIn("no network", res["steps"]["scanners"]["error"])

    def test_only_fx_doctor_itself_tries_them(self):
        for session in (True, False):
            checks, _, _ = fx.doctor_checks(self.f.ctx(), session=session)
            self.assertFalse([c for c in checks if c["check"].startswith("semgrep can load")])


class SemgrepOfflineTest(CoverageCase):
    def run_it(self, stdout="", stderr="", code=2):
        with mock.patch.object(fx, "run_sep", return_value=subprocess.CompletedProcess([], code, stdout, stderr)):
            return fx.scan_semgrep(self.f.ctx(), self.f.job(), None, self.tmp, [])

    def test_the_hint_survives_the_warning_about_our_own_option(self):
        found, err = self.run_it(stderr="[WARNING]: !!! You're using one or more options starting with '--x-'.\n")
        self.assertIsNone(found)
        self.assertIn("no network", err)
        self.assertIn("p/default", err)

    def test_local_rules_do_not_blame_the_network(self):
        write(os.path.join(self.f.factory, "rules.yml"), "rules: []\n")
        self.set_security(semgrep_config="rules.yml")
        found, err = self.run_it(stderr="[WARNING]: options starting with '--x-'\nbad rule on line 3\n")
        self.assertNotIn("no network", err)
        self.assertIn("bad rule on line 3", err)
        self.assertNotIn("--x-", err)


class PathGlobTest(unittest.TestCase):
    def test_a_star_stays_in_its_folder(self):
        self.assertTrue(fx.path_glob("src/legacy/a.py", "src/legacy/*.py"))
        self.assertFalse(fx.path_glob("src/legacy/new/deep/evil.py", "src/legacy/*.py"))

    def test_two_stars_cross_folders(self):
        self.assertTrue(fx.path_glob("src/legacy/new/deep/a.py", "src/legacy/**"))
        self.assertTrue(fx.path_glob("a.py", "**/a.py"))
        self.assertTrue(fx.path_glob("x/y/a.py", "**/a.py"))
        self.assertFalse(fx.path_glob("x/y/b.py", "**/a.py"))


class NothingToScanTest(CoverageCase):
    def test_a_project_without_dependencies_is_clean(self):
        res = self.gate_with(STUB_OSV_SCANNER="nosources")
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(self.f.job()["security_missed"], [])


class UnratedVulnerabilityTest(unittest.TestCase):
    def severities(self, *vulns):
        return {v["rule"]: (v["severity"], v["message"]) for v in fx.osv_vulns(json.loads(osv_out(*vulns))).values()}

    def test_a_score_or_a_word_is_used(self):
        got = self.severities(("A", "9.8", None), ("B", "", "MODERATE"), ("C", "3.0", None))
        self.assertEqual(got["A"][0], "critical")
        self.assertEqual(got["B"][0], "medium")
        self.assertEqual(got["C"][0], "low")

    def test_no_rating_counts_as_high_and_says_so(self):
        got = self.severities(("A", "", None), ("B", "", "UNKNOWN"))
        for rule in "AB":
            self.assertEqual(got[rule][0], "high")
            self.assertIn("no severity rating yet", got[rule][1])


class SemgrepRulesTest(CoverageCase):
    def configs(self):
        argv = self.f.calls("semgrep")[-1]["argv"]
        return [argv[i + 1] for i, a in enumerate(argv) if a == "--config"]

    def test_the_default_is_the_whole_list(self):
        self.gate_with()
        self.assertEqual(self.configs(), fx.SEMGREP_RULES)

    def test_a_single_set_still_works(self):
        self.set_security(semgrep_config="p/ci")
        self.gate_with()
        self.assertEqual(self.configs(), ["p/ci"])

    def test_a_list_can_mix_the_registry_and_the_factory(self):
        write(os.path.join(self.f.factory, "rules.yml"), "rules: []\n")
        self.set_security(semgrep_config=["p/ci", "rules.yml"])
        self.gate_with()
        self.assertEqual(self.configs(), ["p/ci", os.path.join(self.f.factory, "rules.yml")])

    def test_files_it_could_not_read_get_a_note(self):
        warn = {"level": "warn", "path": "ci.yml", "message": "syntax"}
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_out(errors=[warn]))
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertTrue(any("could not read all of ci.yml" in n for n in res["notes"]), res["notes"])

    def test_a_real_error_is_still_a_failure(self):
        res = self.gate_with(STUB_SEMGREP_OUT=semgrep_out(errors=[{"level": "error", "message": "boom"}]))
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("boom", self.checks_md())


class HeadersTest(CoverageCase):
    def serve(self, **headers):
        class H(http.server.BaseHTTPRequestHandler):
            seen = {}

            def do_GET(self):
                H.seen = dict(self.headers.items())
                self.send_response(200)
                for k, v in headers.items():
                    self.send_header(k.replace("_", "-"), v)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with mock.patch.dict(os.environ, {"no_proxy": "*"}):
            found, err = fx.scan_headers(None, None, "http://127.0.0.1:%d/" % srv.server_address[1])
        self.assertIsNone(err)
        self.assertEqual(H.seen.get("Origin"), fx.CORS_PROBE)
        return {f["rule"]: f for f in found}

    def test_cors_that_trusts_any_origin_with_credentials(self):
        got = self.serve(Access_Control_Allow_Origin=fx.CORS_PROBE, Access_Control_Allow_Credentials="true")
        self.assertEqual(got["cors-any-origin"]["severity"], "high")
        self.assertIn("with credentials", got["cors-any-origin"]["message"])

    def test_cors_that_trusts_any_origin_without_credentials(self):
        got = self.serve(Access_Control_Allow_Origin=fx.CORS_PROBE)
        self.assertEqual(got["cors-any-origin"]["severity"], "medium")

    def test_a_wildcard_is_only_low(self):
        got = self.serve(Access_Control_Allow_Origin="*")
        self.assertEqual(got["cors-wildcard"]["severity"], "low")
        self.assertNotIn("cors-any-origin", got)

    def test_a_named_origin_is_fine(self):
        got = self.serve(Access_Control_Allow_Origin="https://app.example.com")
        self.assertFalse([r for r in got if r.startswith("cors")])

    def test_what_runs_here_is_low(self):
        got = self.serve(X_Powered_By="Express")
        self.assertEqual(got["version-leak"]["severity"], "low")

    def test_server_without_a_version_says_nothing(self):
        class Quiet(http.server.BaseHTTPRequestHandler):
            server_version, sys_version = "nginx", ""

            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        found, _ = fx.scan_headers(None, None, "http://127.0.0.1:%d/" % srv.server_address[1])
        self.assertFalse([f for f in found if f["rule"] == "version-leak"], found)


class BaselineTest(CoverageCase):
    cfg = {"app": {"up": "true"}, "security": {"required": [], "scanners": {"zap": False}}}
    finding = {"tool": "headers", "severity": "medium", "rule": "clickjacking", "location": "u", "message": "no frame"}

    def gate_app(self):
        self.f.commit({"app.py": "x = 1\n"})
        with mock.patch.object(fx, "app_start", return_value={"url": "http://127.0.0.1:5100/"}), \
                mock.patch.object(fx, "app_stop"), \
                mock.patch.object(fx, "scan_headers", lambda c, j, u: ([dict(self.finding)], None)):
            return self.gate()[1]

    def baseline(self, security, signature=None):
        b = {"security": {"headers": security}}
        if signature is not False:
            b["security_scan"] = {"headers": signature or fx.scan_signature(self.f.ctx(), "headers")}
        write(os.path.join(self.f.factory, "baseline.json"), json.dumps(b))

    def test_no_baseline_reports_without_blocking(self):
        res = self.gate_app()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertTrue(any(s.startswith("headers: no baseline") for s in res["skipped"]), res["skipped"])

    def test_a_medium_finding_on_the_baseline_does_not_block(self):
        self.baseline([self.finding])
        self.assertEqual(self.gate_app()["gate"], "PASS", self.checks_md())

    def test_a_new_medium_finding_blocks(self):
        self.baseline([dict(self.finding, rule="other")])
        self.assertEqual(self.gate_app()["gate"], "FAIL", self.checks_md())

    def test_a_baseline_from_older_scans_does_not_count(self):
        self.baseline([dict(self.finding, rule="other")], signature=False)
        res = self.gate_app()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertTrue(any(s.startswith("headers: no baseline") for s in res["skipped"]), res["skipped"])

    def assert_new_signature_after(self, **security):
        self.set_security()
        before = fx.scan_signature(self.f.ctx(), "zap")
        self.set_security(**security)
        self.assertNotEqual(fx.scan_signature(self.f.ctx(), "zap"), before)

    def test_what_changes_the_signature(self):
        self.assert_new_signature_after(zap_mode="full")
        self.assert_new_signature_after(zap_openapi="/o.json")
        self.assert_new_signature_after(zap_auth={"command": "echo x"})
        self.assert_new_signature_after(zap_image="other/zap:1@sha256:" + "a" * 64)

    def test_a_newer_stable_changes_it_too(self):
        before = fx.scan_signature(self.f.ctx(), "zap")
        with mock.patch.dict(os.environ, {"STUB_ZAP_IMAGE_ID": "sha256:newer"}):
            self.assertNotEqual(fx.scan_signature(self.f.ctx(), "zap"), before)

    def test_zap_settings_do_not_touch_the_headers_signature(self):
        before = fx.scan_signature(self.f.ctx(), "headers")
        self.set_security(zap_mode="full", zap_auth={"command": "echo x"})
        self.assertEqual(fx.scan_signature(self.f.ctx(), "headers"), before)

    def test_the_stamp_is_taken_after_zap_has_pulled_its_image(self):
        self.set_security(required=[])
        pulled = []

        def zap(*a, **k):
            pulled.append(1)
            return [], None
        with mock.patch.object(fx, "app_start", return_value={"url": "http://127.0.0.1:5100/"}), \
                mock.patch.object(fx, "app_stop"), mock.patch.object(fx, "scan_headers", lambda c, j, u: ([], None)), \
                mock.patch.object(fx, "scan_zap", zap), \
                mock.patch.object(fx, "zap_image_id", lambda c: "sha256:pulled" if pulled else "the-tag"):
            self.fx("selftest")
        stamp = load_json(os.path.join(self.f.factory, "baseline.json"))["security_scan"]["zap"]
        self.assertTrue(stamp.endswith("sha256:pulled"), stamp)

    def test_selftest_records_what_made_the_baseline(self):
        with mock.patch.object(fx, "app_start", return_value={"url": "http://127.0.0.1:5100/"}), \
                mock.patch.object(fx, "app_stop"), mock.patch.object(fx, "scan_headers", lambda c, j, u: ([], None)):
            self.fx("selftest")
        self.assertEqual(fx.baseline_state(self.f.ctx(), "headers")[0], "ok")

    def old_baseline(self):
        write(os.path.join(self.f.factory, "baseline.json"), json.dumps({"security": {"headers": []}}))

    def test_a_baseline_from_before_says_what_to_do(self):
        self.old_baseline()
        res = self.gate_app()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertTrue(any("baseline from an older version: run fx selftest" in n for n in res["notes"]), res["notes"])
        self.assertIn("baseline from an older version", self.checks_md())

    def test_doctor_fails_on_it(self):
        self.old_baseline()
        row = next(c for c in self.fx("doctor")[1]["checks"] if c["check"] == "headers baseline usable")
        self.assertFalse(row["ok"])
        self.assertIn("run fx selftest", row["detail"])

    def test_a_baseline_with_an_older_signature_counts_as_other_scans(self):
        write(os.path.join(self.f.factory, "baseline.json"),
              json.dumps({"security": {"headers": []}, "security_scan": "2/baseline/web"}))
        self.assertEqual(fx.baseline_state(self.f.ctx(), "headers")[0], "other")

    def test_a_good_baseline_has_no_such_row(self):
        self.baseline([])
        self.assertFalse([c for c in self.fx("doctor")[1]["checks"] if c["check"] == "headers baseline usable"])

    def test_a_serious_finding_on_a_new_path_is_new(self):
        known = {("zap-1", "XSS"): {"paths": {"/old"}, "libraries": set()}}
        new = lambda f: fx.app_finding_is_new(dict(f, rule="zap-1", message="XSS"), known, fx.SEV_RANK["medium"])
        self.assertTrue(new({"severity": "high", "paths": ["/old", "/new"]}))
        self.assertFalse(new({"severity": "high", "paths": ["/old"]}))

    def test_a_kind_of_finding_the_baseline_never_had_is_new_at_any_level(self):
        self.assertTrue(fx.app_finding_is_new({"rule": "zap-3", "message": "SQL", "severity": "low", "paths": []}, {},
                                              fx.SEV_RANK["medium"]))


class NewPageSameOldFindingTest(CoverageCase):
    cfg = {"app": {"up": "true"}, "security": {"required": [], "scanners": {"headers": False}}}
    CSRF = {"rule": "zap-10202", "message": "Absence of Anti-CSRF Tokens", "severity": "medium"}
    JS_LIB = {"rule": "zap-10003", "message": "Vulnerable JS Library", "severity": "medium"}
    CSP = {"rule": "zap-10038", "message": "Content Security Policy (CSP) Header Not Set", "severity": "medium"}

    def gate_finding(self, finding, base_paths, paths, **security):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"].update(security)
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        write(os.path.join(self.f.factory, "baseline.json"), json.dumps({
            "security": {"zap": [dict(finding, tool="zap", location="u", paths=base_paths)]},
            "security_scan": {"zap": fx.scan_signature(self.f.ctx(), "zap")}}))
        self.f.commit({"page.py": "x = 1\n"})
        now_ = dict(finding, tool="zap", location="u", paths=paths)
        with mock.patch.object(fx, "app_start", return_value={"url": "http://127.0.0.1:5100/"}), \
                mock.patch.object(fx, "app_stop"), mock.patch.object(fx, "scan_zap", lambda *a, **k: ([now_], None)):
            return self.gate()[1]["gate"]

    def test_a_new_form_without_a_token_blocks(self):
        self.assertEqual(self.gate_finding(self.CSRF, ["/login"], ["/login", "/transfer"]), "FAIL", self.checks_md())

    def test_a_new_vulnerable_library_blocks(self):
        self.assertEqual(self.gate_finding(self.JS_LIB, ["/old.js"], ["/old.js", "/new.js"]), "FAIL", self.checks_md())

    def test_a_rebuilt_asset_is_the_same_page(self):
        self.assertEqual(self.gate_finding(self.JS_LIB, ["/assets/app.abc123.js"], ["/assets/app.def456.js"]), "PASS",
                         self.checks_md())

    def test_shapes_of_paths(self):
        shape = fx.app_path_shape
        self.assertEqual(shape("/a/index-BwqjK2lA.js"), shape("/a/index-C9dkE1xZ.js"))
        self.assertEqual(shape("/item/123/edit"), shape("/item/456/edit"))
        self.assertEqual(shape("/u/3f2b1c9e-1111-4222-8333-abcdefabcdef"), shape("/u/9a8b7c6d-1111-4222-8333-abcdefabcdef"))
        self.assertNotEqual(shape("/transfer"), shape("/login"))
        self.assertNotEqual(shape("/a/app.js"), shape("/a/admin.js"))

    def test_shapes_of_built_files(self):
        shape = fx.app_path_shape
        self.assertEqual(shape("/_nuxt/DlAUqK2U.js"), shape("/_nuxt/Xy9pQw3Z.js"))
        self.assertEqual(shape("/static/js/main.1a2b3c4d.chunk.js"), shape("/static/js/main.9f8e7d6c.chunk.js"))
        self.assertEqual(shape("/assets/index-a1b2c3d4.js"), shape("/assets/index-e5f6a7b8.js"))
        self.assertEqual(shape("/assets/index-a1b2c3d4.js.map"), shape("/assets/index-e5f6a7b8.js.map"))
        self.assertEqual(shape("/_next/static/AbC123xyz/_buildManifest.js"), shape("/_next/static/Zz9Yy8Xx7/_buildManifest.js"))
        self.assertEqual(shape("/_nuxt/Dl-UqK2U.js"), shape("/_nuxt/Xy_pQw3Z.js"))
        self.assertNotEqual(shape("/static/js/main.1a2b3c4d.chunk.js"), shape("/static/js/admin.1a2b3c4d.chunk.js"))

    def test_only_built_files_lose_their_names(self):
        shape = fx.app_path_shape
        self.assertNotEqual(shape("/oauth2callback"), shape("/login2factor"))
        self.assertEqual(shape("/oauth2callback"), "/oauth2callback")
        self.assertEqual(shape("/oauth2callback/start"), "/oauth2callback/start")
        self.assertNotEqual(shape("/assets/admin.js"), shape("/assets/orders.js"), "no digit in it: a name, not a hash")

    def test_the_same_pages_do_not(self):
        self.assertEqual(self.gate_finding(self.CSRF, ["/login"], ["/login"]), "PASS", self.checks_md())

    def test_a_site_without_a_csp_does_not_stop_every_new_page(self):
        self.assertEqual(self.gate_finding(self.CSP, ["/"], ["/", "/new"]), "PASS", self.checks_md())

    def test_below_the_threshold_a_new_page_does_not_block(self):
        self.assertEqual(self.gate_finding(self.CSRF, ["/login"], ["/login", "/transfer"], block_severity="high"), "PASS",
                         self.checks_md())


def library_report(*found):
    """As the real ZAP writes it: found is (file, library, version, evidence)."""
    return json.dumps({"site": [{"alerts": [{
        "pluginid": "10003", "riskcode": "2", "alert": "Vulnerable JS Library", "name": "Vulnerable JS Library", "cweid": "1395",
        "otherinfo": "<p>The identified library %s, version %s is vulnerable.</p><p>CVE-1</p>" % (found[0][1], found[0][2]),
        "instances": [{"uri": "http://host.docker.internal:5100" + f, "evidence": e,
                       "otherinfo": "The identified library %s, version %s is vulnerable.\nCVE-1\n" % (l, v)}
                      for f, l, v, e in found]}]}]})


class SameLibraryInAnotherBundleTest(CoverageCase):
    """ZAP calls every vulnerable library the same, so the rule's name and the file's shape cannot tell them apart."""
    cfg = {"app": {"up": "true"}, "security": {"required": [], "scanners": {"headers": False}}}
    JQUERY = ("/assets/index-a1b2c3d4.js", "jquery", "1.12.4", "/*! jQuery v1.12.4")

    def findings(self, report):
        with mock.patch.dict(os.environ, STUB_ZAP_REPORT=report), mock.patch.object(fx.sys, "platform", "darwin"):
            found, err = fx.scan_zap(self.f.ctx(), self.f.job(), "http://127.0.0.1:5100/", os.path.join(self.tmp, "z"))
        self.assertIsNone(err)
        return found

    def gate_report(self, base, now, **security):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"].update(security)
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        write(os.path.join(self.f.factory, "baseline.json"), json.dumps({
            "security": {"zap": self.findings(base)}, "security_scan": {"zap": fx.scan_signature(self.f.ctx(), "zap")}}))
        self.f.commit({"page.py": "x = 1\n"})
        with mock.patch.dict(os.environ, STUB_ZAP_REPORT=now), mock.patch.object(fx, "app_start", return_value={"url": "http://127.0.0.1:5100/"}), \
                mock.patch.object(fx, "app_stop"), mock.patch.object(fx, "docker_running", return_value=True):
            return self.gate()[1]["gate"]

    def test_what_it_reads_from_a_report(self):
        found = self.findings(library_report(self.JQUERY, ("/assets/v.js", "bootstrap", "3.3.7", "* Bootstrap v3.3.7")))
        self.assertEqual(found[0]["libraries"], ["bootstrap@3.3.7", "jquery@1.12.4"])
        self.assertIn("jquery@1.12.4", found[0]["location"])

    def test_the_same_library_in_a_rebuilt_bundle_is_not_new(self):
        now = library_report(("/assets/index-e5f6a7b8.js",) + self.JQUERY[1:])
        self.assertEqual(self.gate_report(library_report(self.JQUERY), now), "PASS", self.checks_md())

    def test_the_same_library_in_a_file_of_another_kind_is_not_new(self):
        now = library_report(("/_nuxt/DlAUqK2U.js",) + self.JQUERY[1:])
        self.assertEqual(self.gate_report(library_report(self.JQUERY), now), "PASS", self.checks_md())

    def test_another_library_in_the_rebuilt_bundle_is_new(self):
        now = library_report(("/assets/index-e5f6a7b8.js", "bootstrap", "3.3.7", "* Bootstrap v3.3.7"), self.JQUERY)
        self.assertEqual(self.gate_report(library_report(self.JQUERY), now), "FAIL", self.checks_md())
        self.assertIn("bootstrap@3.3.7", self.checks_md())

    def test_another_version_is_new(self):
        now = library_report(("/assets/index-a1b2c3d4.js", "jquery", "1.11.0", "/*! jQuery v1.11.0"))
        self.assertEqual(self.gate_report(library_report(self.JQUERY), now), "FAIL", self.checks_md())

    def test_below_the_threshold_it_does_not_block(self):
        now = library_report(("/assets/index-e5f6a7b8.js", "bootstrap", "3.3.7", "* Bootstrap v3.3.7"))
        self.assertEqual(self.gate_report(library_report(self.JQUERY), now, block_severity="high"), "PASS", self.checks_md())

    def test_without_zaps_words_it_goes_by_what_was_matched(self):
        report = json.loads(library_report(self.JQUERY))
        del report["site"][0]["alerts"][0]["instances"][0]["otherinfo"]
        self.assertEqual(self.findings(json.dumps(report))[0]["libraries"], ["/*! jQuery v1.12.4"])

    def test_with_nothing_to_go_by_it_goes_by_the_file(self):
        report = json.loads(library_report(self.JQUERY))
        del report["site"][0]["alerts"][0]["instances"][0]["otherinfo"]
        del report["site"][0]["alerts"][0]["instances"][0]["evidence"]
        self.assertEqual(self.findings(json.dumps(report))[0]["libraries"], [])
        now = library_report(("/assets/new.js",) + self.JQUERY[1:])
        report2 = json.loads(now)
        del report2["site"][0]["alerts"][0]["instances"][0]["otherinfo"]
        del report2["site"][0]["alerts"][0]["instances"][0]["evidence"]
        self.assertEqual(self.gate_report(json.dumps(report), json.dumps(report2)), "FAIL", self.checks_md())

    @staticmethod
    def rated(*found):
        """One alert per risk, as the real ZAP groups them: found is (risk code, file, library, version)."""
        alerts = []
        for risk, f, lib, ver in found:
            alerts += json.loads(library_report((f, lib, ver, "x")))["site"][0]["alerts"]
            alerts[-1]["riskcode"] = risk
        return json.dumps({"site": [{"alerts": alerts}]})

    def test_a_library_keeps_its_own_severity(self):
        found = self.findings(self.rated(("3", "/a.js", "lodash", "4.17.0"), ("2", "/b.js", "jquery", "1.12.4")))
        self.assertEqual({f["severity"]: f["libraries"] for f in found},
                         {"high": ["lodash@4.17.0"], "medium": ["jquery@1.12.4"]})

    def test_a_new_library_below_the_threshold_beside_an_old_serious_one(self):
        old = self.rated(("3", "/a.js", "lodash", "4.17.0"))
        now = self.rated(("3", "/a.js", "lodash", "4.17.0"), ("2", "/b.js", "bootstrap", "3.3.7"))
        self.assertEqual(self.gate_report(old, now, block_severity="high"), "PASS", self.checks_md())

    def test_a_new_serious_library_beside_an_old_lesser_one(self):
        old = self.rated(("2", "/a.js", "jquery", "1.12.4"))
        now = self.rated(("2", "/a.js", "jquery", "1.12.4"), ("3", "/b.js", "lodash", "4.17.0"))
        self.assertEqual(self.gate_report(old, now, block_severity="high"), "FAIL", self.checks_md())
        self.assertIn("lodash@4.17.0", self.checks_md())

    def test_other_rules_are_left_as_they_were(self):
        report = zap_report(("10202", "2", "Absence of Anti-CSRF Tokens", ["http://h/login"]))
        self.assertNotIn("libraries", self.findings(report)[0])


class LoginAddedAfterTheBaselineTest(CoverageCase):
    cfg = {"app": {"up": "true"}, "security": {"required": [], "scanners": {"headers": False}}}

    def test_a_job_that_only_changes_docs_is_not_blocked(self):
        write(os.path.join(self.f.factory, "baseline.json"),
              json.dumps({"security": {"zap": []}, "security_scan": {"zap": fx.scan_signature(self.f.ctx(), "zap")}}))
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"]["zap_auth"] = {"command": "echo x"}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.f.commit({"docs.md": "docs\n"})
        old = {"tool": "zap", "severity": "medium", "rule": "zap-10202", "location": "http://h/account",
               "message": "Absence of Anti-CSRF Tokens", "paths": ["/account"]}
        with mock.patch.object(fx, "app_start", return_value={"url": "http://127.0.0.1:5100/"}), \
                mock.patch.object(fx, "app_stop"), mock.patch.object(fx, "scan_zap", lambda *a, **k: ([old], None)):
            code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertTrue(any("run fx selftest" in n for n in res["notes"]), res["notes"])


class ZapImageMovedTest(CoverageCase):
    """What project code run by checks.sh can do: docker tag or docker build. It used to turn ZAP's blocking off."""
    cfg = {"app": {"up": "true"}, "security": {"required": [], "scanners": {"headers": False}}}
    SELFTEST = "sha256:selftest"
    SQLI = zap_report(("40018", "3", "SQL Injection", ["http://127.0.0.1:5100/q"]))

    def baseline(self):
        with mock.patch.dict(os.environ, STUB_ZAP_IMAGE_ID=self.SELFTEST):
            write(os.path.join(self.f.factory, "baseline.json"), json.dumps({
                "security": {"zap": []}, "security_scan": {"zap": fx.scan_signature(self.f.ctx(), "zap")}}))

    def gate_with_image(self, **env):
        self.baseline()
        self.f.commit({"page.py": "x = 1\n"})
        env = dict({"STUB_ZAP_REPORT": self.SQLI}, **env)
        with mock.patch.dict(os.environ, env), mock.patch.object(fx, "app_start", return_value={"url": "http://127.0.0.1:5100/"}), \
                mock.patch.object(fx, "app_stop"), mock.patch.object(fx, "docker_running", return_value=True):
            return self.gate()[1]

    def ran(self):
        return [c["argv"] for c in self.f.calls("docker") if c["argv"][0] == "run"]

    def test_the_same_image_blocks(self):
        res = self.gate_with_image(STUB_ZAP_IMAGE_ID=self.SELFTEST)
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertFalse(any("moved" in n or "names another" in n for n in res["notes"]), res["notes"])

    def test_a_moved_tag_still_blocks(self):
        res = self.gate_with_image(STUB_ZAP_IMAGE_ID="sha256:other")
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertEqual(self.f.job()["security_missed"], [])
        self.assertTrue(any("names another image" in n for n in res["notes"]), res["notes"])

    def test_it_runs_the_image_selftest_used_not_the_tag(self):
        self.gate_with_image(STUB_ZAP_IMAGE_ID="sha256:other")
        argv = self.ran()[-1]
        self.assertIn(self.SELFTEST, argv)
        self.assertFalse([a for a in argv if a.startswith("ghcr.io/")], argv)

    def test_the_image_gone_is_a_scan_that_did_not_run(self):
        res = self.gate_with_image(STUB_ZAP_IMAGE_ID="sha256:other", STUB_ZAP_IMAGE_GONE=self.SELFTEST)
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(self.f.job()["security_missed"], ["zap"])
        self.assertIn("no longer on this machine", self.checks_md())
        self.assertEqual(self.ran(), [])
        self.f.save_job(stage="approve", flags=[])
        write(os.path.join(self.f.jd, "decision.md"), "APPROVE\n\nfine\n")
        code, res = self.fx("result", "T-1", "decision")
        self.assertEqual(code, 0, res)
        self.assertTrue(self.f.job()["needs_human"], "APPROVE turns into a human's decision")

    def test_required_it_fails_the_gate(self):
        self.set_security(required=["zap"], scanners={"headers": False})
        res = self.gate_with_image(STUB_ZAP_IMAGE_GONE=self.SELFTEST)
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("no longer on this machine", self.checks_md())

    def test_doctor_says_so(self):
        self.baseline()
        with mock.patch.dict(os.environ, STUB_ZAP_IMAGE_GONE=self.SELFTEST), \
                mock.patch.object(fx, "docker_running", return_value=True):
            checks = {c["check"]: c for c in self.fx("doctor")[1]["checks"]}
        self.assertFalse(checks["zap baseline usable"]["ok"])

    def test_changed_settings_are_still_only_a_note(self):
        self.baseline()
        self.set_security(required=[], scanners={"headers": False}, zap_mode="full")
        self.f.commit({"page.py": "x = 1\n"})
        with mock.patch.dict(os.environ, STUB_ZAP_REPORT=self.SQLI), mock.patch.object(fx, "app_start", return_value={"url": "http://127.0.0.1:5100/"}), \
                mock.patch.object(fx, "app_stop"), mock.patch.object(fx, "docker_running", return_value=True):
            res = self.gate()[1]
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertTrue(any("run fx selftest" in n for n in res["notes"]), res["notes"])
        self.assertEqual(self.f.job()["security_missed"], [])

    def test_a_different_image_named_in_the_settings_counts_as_changed_settings(self):
        self.baseline()
        self.set_security(required=[], scanners={"headers": False}, zap_image="ghcr.io/zaproxy/zaproxy:2.16.0")
        self.assertEqual(fx.baseline_state(self.f.ctx(), "zap")[0], "other")
        self.assertIsNone(fx.zap_baseline_image(self.f.ctx()))

    def test_an_image_pinned_by_digest_needs_no_asking(self):
        image = "ghcr.io/zaproxy/zaproxy@sha256:" + "a" * 64
        self.set_security(required=[], scanners={"headers": False}, zap_image=image)
        self.baseline()
        with mock.patch.dict(os.environ, STUB_ZAP_IMAGE_GONE=image):
            self.assertEqual(fx.zap_pinned(self.f.ctx(), []), (image, None))

    def test_a_baseline_that_does_not_count_pins_nothing(self):
        self.assertEqual(fx.zap_pinned(self.f.ctx(), []), (None, None))

    def test_selftest_itself_runs_the_tag_so_it_can_move_on(self):
        self.baseline()
        fx.scan_zap(self.f.ctx(), self.f.job(), "http://127.0.0.1:5100/", os.path.join(self.tmp, "z"))
        self.assertIn("ghcr.io/zaproxy/zaproxy:stable", self.ran()[-1])


class ZapRunsTest(CoverageCase):
    def scan(self, **env):
        rdir = os.path.join(self.tmp, "zap")
        notes = []
        with mock.patch.object(fx.sys, "platform", "darwin"), mock.patch.dict(os.environ, env):
            found, err = fx.scan_zap(self.f.ctx(), self.f.job(), "http://127.0.0.1:5100/", rdir, notes)
        return found, err, rdir, [c for c in self.f.calls("docker") if c["argv"][0] == "run"]

    @staticmethod
    def script(argv):
        return next(a for a in argv if "zap-" in a and ".py" in a)

    def test_baseline_only_watches_but_crawls_with_a_browser(self):
        _, err, _, runs = self.scan()
        self.assertIsNone(err)
        self.assertEqual(len(runs), 1)
        argv = runs[0]["argv"]
        self.assertIn("zap-baseline.py", self.script(argv))
        self.assertIn("-a", argv)
        self.assertIn("-j", argv)

    def test_full_attacks_and_has_a_limit(self):
        self.set_security(zap_mode="full", zap_minutes=7)
        _, _, _, runs = self.scan()
        argv = runs[0]["argv"]
        self.assertIn("zap-full-scan.py", self.script(argv))
        self.assertIn("-config scanner.maxScanDurationInMins=7", argv)

    def test_every_script_starts_in_a_folder_it_can_write(self):
        # The full and API scripts open zap.out in the folder they start from; /zap is not writable for this uid.
        self.set_security(zap_mode="full")
        _, _, _, runs = self.scan()
        self.assertIn("cd /tmp/zap-user &&", self.script(runs[0]["argv"]))

    def test_an_api_description_gets_its_own_run(self):
        self.set_security(zap_openapi="/openapi.json")
        _, _, rdir, runs = self.scan()
        self.assertEqual(len(runs), 2)
        argv = runs[1]["argv"]
        self.assertIn("zap-api-scan.py", self.script(argv))
        self.assertEqual(argv[argv.index("-t") + 1], "http://host.docker.internal:5100/openapi.json")
        self.assertEqual(argv[argv.index("-O") + 1], "http://host.docker.internal:5100")
        self.assertIn("-S", argv)  # baseline mode: no attacks here either
        self.assertEqual(argv[argv.index("-J") + 1], "zap-api.json")

    def test_a_full_api_run_attacks(self):
        self.set_security(zap_openapi="openapi.json", zap_mode="full")
        _, _, _, runs = self.scan()
        self.assertNotIn("-S", runs[1]["argv"])

    def test_the_login_value_reaches_only_the_container_and_no_file(self):
        secret = "Bearer sEcReT-tOkEn-123"
        self.set_security(zap_auth={"command": "echo noise; echo '%s'" % secret})
        report = zap_report(("40012", 3, "XSS seen with %s" % secret, ["http://h/a"]))
        found, err, rdir, runs = self.scan(STUB_ZAP_REPORT=report, STUB_ZAP_LOG="started with " + secret)
        self.assertIsNone(err)
        self.assertEqual(runs[0]["zap_auth"], secret)
        argv = runs[0]["argv"]
        self.assertNotIn(secret, " ".join(argv))
        self.assertIn("ZAP_AUTH_HEADER_VALUE", argv)
        self.assertEqual(argv[argv.index("ZAP_AUTH_HEADER_VALUE") - 1], "-e")
        for name in ("zap.json", "zap.log"):
            self.assertNotIn(secret, slurp(os.path.join(rdir, name)), name)
        self.assertNotIn(secret, found[0]["message"])

    def test_the_login_value_goes_only_to_the_apps_host(self):
        self.set_security(zap_auth={"command": "echo tok"})
        _, err, _, runs = self.scan()
        self.assertIsNone(err)
        self.assertEqual(runs[0]["zap_auth_site"], "host.docker.internal")
        self.assertEqual(runs[0]["argv"][runs[0]["argv"].index("ZAP_AUTH_HEADER_SITE") - 1], "-e")

    def test_the_host_follows_where_zap_reaches_the_app(self):
        self.set_security(zap_auth={"command": "echo tok"})
        with mock.patch.object(fx, "zap_network", return_value=(["--network", "host"], "http://127.0.0.1:5100/", None)):
            _, _, _, runs = self.scan()
        self.assertEqual(runs[0]["zap_auth_site"], "127.0.0.1")

    def test_a_login_command_that_fails_stops_the_scan(self):
        self.set_security(zap_auth={"command": "exit 3"})
        found, err, _, runs = self.scan()
        self.assertIsNone(found)
        self.assertIn("zap_auth", err)
        self.assertEqual(runs, [])

    def test_findings_say_where_and_merge_across_runs(self):
        self.set_security(zap_openapi="/openapi.json")
        report = zap_report(("40018", 3, "SQL Injection", ["http://h/b?id=1", "http://h/a?id=1"]))
        found, _, _, _ = self.scan(STUB_ZAP_REPORT=report)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "high")
        self.assertEqual(found[0]["location"], "http://h/a?id=1, http://h/b?id=1")
        self.assertEqual(found[0]["paths"], ["/a", "/b"])

    def test_how_fx_itself_runs_the_app_is_not_a_finding(self):
        found, _, _, _ = self.scan(STUB_ZAP_REPORT=zap_report(("10106", 2, "HTTP Only Site", ["http://h/"])))
        self.assertEqual(found, [])

    def test_a_report_from_an_earlier_run_is_not_read_again(self):
        rdir = os.path.join(self.tmp, "zap")
        write(os.path.join(rdir, "zap.json"), zap_report(("1", 3, "old", [])))
        found, _, _, _ = self.scan()
        self.assertEqual(found, [])


class AuditScanTest(CoverageCase):
    def scan(self, *args, **env):
        with mock.patch.dict(os.environ, env):
            return self.fx("audit-scan", *args)

    def test_it_looks_at_everything_not_only_a_change(self):
        code, res = self.scan()
        self.assertEqual(code, 0, res)
        argv = self.f.calls("gitleaks")[-1]["argv"]
        self.assertEqual(argv[argv.index("--log-opts") + 1], "HEAD")
        self.assertNotIn("--baseline-commit", self.f.calls("semgrep")[-1]["argv"])
        self.assertFalse([a for a in self.f.calls("osv-scanner") if any("fx-osv-base-" in x for x in a["argv"])])
        self.assertTrue(res["complete"])

    def test_old_findings_become_tasks_grouped_by_rule(self):
        out = semgrep_out(("sqli.tainted", "ERROR"), ("sqli.tainted", "ERROR"), ("xss.raw", "WARNING"), ("hint", "INFO"))
        code, res = self.scan(STUB_SEMGREP_OUT=out)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["counts"], {"high": 2, "medium": 1, "low": 1})
        self.assertEqual([(r["rule"], r["places"]) for r in res["to_fix"]], [("sqli.tainted", 2), ("xss.raw", 1)])
        task = slurp(res["to_fix"][0]["task_file"])
        self.assertIn("smallest safe change", task)
        self.assertIn("app.py:2", task)
        self.assertIn("hint", slurp(res["report"]))  # below the threshold: in the report, no task

    def test_a_secret_task_says_to_replace_it(self):
        leak = json.dumps([{"RuleID": "aws-access-token", "File": "a", "StartLine": 1, "Commit": "abc"}])
        _, res = self.scan(STUB_GITLEAKS_REPORT=leak)
        self.assertIn("replace it first", slurp(res["to_fix"][0]["task_file"]))
        self.assertIn("--redact", self.f.calls("gitleaks")[-1]["argv"])

    def test_it_leaves_no_clone_and_needs_no_job(self):
        self.scan()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "proj-factory-work", "_audit")))
        self.assertFalse(os.path.exists(os.path.join(self.f.factory, "jobs", "_audit")))

    def test_a_scanner_that_could_not_run_is_said(self):
        _, res = self.scan(STUB_SEMGREP="x", STUB_SEMGREP_OUT="not json")
        self.assertFalse(res["complete"])
        self.assertEqual(res["missed"], ["semgrep"])

    def test_another_branch_and_a_missing_one(self):
        code, res = self.scan("--base", "nope")
        self.assertEqual(code, 3, res)

    def test_the_running_app_is_scanned_when_there_is_one(self):
        self.set_security(scanners={"zap": False})
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["app"] = {"up": "true"}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        seen = []

        def headers(c, j, url):
            seen.append(url)
            return [{"tool": "headers", "severity": "medium", "rule": "clickjacking", "location": url, "message": "m"}], None
        with mock.patch.object(fx, "app_start", return_value={"url": "http://127.0.0.1:1/"}), \
                mock.patch.object(fx, "app_stop") as stop, mock.patch.object(fx, "scan_headers", headers):
            _, res = self.scan()
            self.assertTrue(stop.called)
            self.assertEqual(res["to_fix"][0]["rule"], "clickjacking")  # no baseline here: everything counts
            self.scan("--no-app")
        self.assertEqual(len(seen), 1)

    def test_an_app_that_will_not_start_is_said(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["app"] = {"up": "true"}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        with mock.patch.object(fx, "app_start", side_effect=fx.FxError("no port")), mock.patch.object(fx, "app_stop"):
            code, res = self.scan()
        self.assertEqual(code, 0, res)
        self.assertTrue(any("could not start" in s for s in res["skipped"]), res["skipped"])


class GitleaksRedactTest(CoverageCase):
    def test_the_report_never_holds_the_secret(self):
        self.gate_with()
        self.assertIn("--redact", self.f.calls("gitleaks")[-1]["argv"])


class SecurityReportTest(unittest.TestCase):
    def test_the_cut_never_hides_a_blocking_finding(self):
        low = [{"tool": "t", "severity": "high", "rule": "r%d" % i, "new": False} for i in range(fx.SECURITY_ROWS + 5)]
        blocking = {"tool": "t", "severity": "low", "rule": "the-one", "new": True}
        md = fx.security_markdown({"findings": low + [blocking], "blocking": [blocking], "skipped": []})
        self.assertIn("the-one", md)
        self.assertIn("6 more findings", md)


class GapsTest(CoverageCase):
    def gaps(self):
        return " | ".join(fx.security_gaps(self.f.ctx()))

    def test_a_new_factory_only_lacks_the_app(self):
        self.assertEqual(self.gaps(), "no app.up: nothing looks at the running app (headers, ZAP)")

    def test_it_names_what_a_factory_turned_down(self):
        self.set_security(block_severity="high", required=["gitleaks"], semgrep_config="p/default",
                          scanners={"osv": False}, accepted_rules=[{"rule": "r", "path": "a/*", "reason": "why"}])
        got = self.gaps()
        self.assertIn("block_severity is high", got)
        self.assertIn("semgrep not in security.required", got)
        self.assertNotIn("osv not in", got)  # turned off is said once
        self.assertIn("turned off in security.scanners: osv", got)
        self.assertIn("leaves out p/owasp-top-ten", got)
        self.assertIn("1 rule(s) accepted", got)

    def test_local_rules_are_not_called_narrow(self):
        self.set_security(semgrep_config=["/rules/ours.yml"])
        self.assertNotIn("leaves out", self.gaps())

    def test_with_an_app_it_names_what_zap_cannot_see(self):
        self.set_security()
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["app"] = {"up": "true"}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        got = self.gaps()
        for words in ("until fx selftest runs", "does not try attacks", "need no login", "API routes"):
            self.assertIn(words, got)
        cfg["security"] = {"zap_mode": "full", "zap_openapi": "/o.json", "zap_auth": {"command": "echo x"}}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        got = self.gaps()
        for words in ("does not try attacks", "need no login", "API routes"):
            self.assertNotIn(words, got)

    def test_no_baseline_is_not_said_when_nothing_would_use_one(self):
        self.set_security(scanners={"headers": False, "zap": False})
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["app"] = {"up": "true"}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.assertNotIn("until fx selftest runs", self.gaps())

    def test_doctor_shows_it_without_failing(self):
        code, res = self.fx("doctor")
        row = [c for c in res["checks"] if c["check"].startswith("what the security gate leaves out")]
        self.assertEqual(len(row), 1)
        self.assertTrue(row[0]["ok"])
        self.assertIn("no app.up", row[0]["detail"])


class TemplateAgreesTest(unittest.TestCase):
    def test_the_template_and_the_defaults_are_the_same(self):
        sec = load_json(TEMPLATE)["security"]
        self.assertEqual(sec["block_severity"], fx.BLOCK_SEVERITY)
        self.assertEqual(sec["required"], fx.REQUIRED_SCANNERS)
        self.assertEqual(sec["semgrep_config"], fx.SEMGREP_RULES)
        self.assertEqual(sec["zap_mode"], "baseline")  # attacking the app is only ever a human's choice
        self.assertEqual(sec["accepted_rules"], [])
        self.assertEqual(sec["semgrep_exclude"], fx.SEMGREP_EXCLUDE)

    def test_the_jira_mode_is_left_to_init(self):
        cfg = load_json(TEMPLATE)
        self.assertEqual(cfg["jira"]["mode"], "")  # init writes mcp or token; until then fx reads it as no mode
        self.assertNotIn(cfg["jira"]["mode"], fx.JIRA_MODES)
        for words in ("mcp", "token", fx.JIRA_ENV, "JIRA_EMAIL", "JIRA_API_TOKEN", "keys_folder",
                      "fx jira children, finished and statuses", "Confluence is read through the connector"):
            self.assertIn(words, cfg["jira"]["_doc"])

    def test_the_s3_rule_is_described(self):
        cfg = load_json(TEMPLATE)
        self.assertNotIn("storage", cfg)
        self.assertNotIn("jobs_store", cfg)  # a factory without S3 keeps its jobs on its machine
        self.assertIn("fx where prints it as keys_folder", cfg["_doc_storage"])
        self.assertIn("fx doctor fails on storage without jobs_store", cfg["_doc_jobs_store"])


if __name__ == "__main__":
    unittest.main()
