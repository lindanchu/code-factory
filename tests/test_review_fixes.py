"""Tests for the fixes of 3.9 (standard library only). Run from the repository root:
    python3 -I -m unittest discover -s tests

The factory brought up to its remote before it works; logins checked with the headers the real calls send; a job's
staged logic checked against its own clone; a limits.env a box can read; the scope of the code; shared limits; the
logic index; a repository's additions to shared knowledge; how far a gate is; the box's installs through Docker; a
Jira issue that fixes a limit; logins saved where they are read and tried before they are saved; one JSON answer per
command; read clones of the repositories another one needs; a gate tried without counting; notes on the skill itself
posted as a GitHub issue with nothing private in them; long files split into an index and parts.
"""

import contextlib
import http.server
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import types
import unittest
from unittest import mock

from test_project_repos import ProjectCase
from test_project_gate import GateCase
from test_security import fx, load_json, sh, slurp, write

LOGIC_README = ("# Logic\n\n## Modules\n| Module | Name |\n|---|---|\n"
                "| [billing/](billing/README.md) | Billing |\n<!-- index:end -->\n\n"
                "## Overall log\n| Date | File | Change |\n|---|---|---|\n<!-- log:end -->\n")
MODULE = "# Billing\n\n| Feature | File |\n|---|---|\n| Invoices | [invoices.md](invoices.md) |\n\n" \
         "## 1. Rules\nNone.\n\n## 2. Parameters\n| Parameter | Value | Code | Env | Test value |\n" \
         "|---|---|---|---|---|\n%s\n"
FEATURE = ("# %s\n\n**Ticket:** SHOP-%d · - · -\n\n## 1. Input\n%s\n\n## 2. BE\nx\n\n## 3. FE\nx\n\n## 4. Nav\nx\n\n"
           "## 5. Open\nx\n\n```mermaid\nflowchart LR\n  a-->b\n```\n")


def ctx_in(case):
    return case.ctx()


# --------------------------------------------------------------------------
# 1. the factory repository is brought up to its remote first

class FactorySyncTest(ProjectCase):
    def setUp(self):
        super().setUp()
        self.remote = os.path.join(self.tmp, "factory.git")
        sh(["git", "clone", "-q", "--bare", self.factory, self.remote], self.tmp)
        sh(["git", "remote", "add", "origin", self.remote], self.factory)
        sh(["git", "fetch", "-q", "origin"], self.factory)
        sh(["git", "branch", "-q", "--set-upstream-to=origin/main"], self.factory)
        other = os.path.join(self.tmp, "other-machine")
        sh(["git", "clone", "-q", self.remote, other], self.tmp)
        write(os.path.join(other, "checks", "be.sh"), "#!/usr/bin/env bash\necho newer\n")
        sh(["git", "commit", "-qam", "from another machine"], other)
        sh(["git", "push", "-q", "origin", "HEAD:main"], other)
        fx._FACTORY_SYNC.clear()
        self.addCleanup(fx._FACTORY_SYNC.clear)

    def test_a_clean_factory_behind_is_pulled_and_said(self):
        fx.factory_sync(self.ctx())
        self.assertIn("newer", slurp(os.path.join(self.factory, "checks", "be.sh")))
        self.assertTrue(any("pulled" in n for n in fx._FACTORY_SYNC["notes"]), fx._FACTORY_SYNC)

    def test_uncommitted_changes_stop_and_ask(self):
        write(os.path.join(self.factory, "checks", "be.sh"), "#!/usr/bin/env bash\necho mine\n")
        with self.assertRaises(fx.FxError) as e:
            fx.factory_sync(self.ctx())
        self.assertEqual(e.exception.code, 3)
        self.assertEqual(e.exception.extra["next"], "ask")
        self.assertEqual((e.exception.extra["behind"], e.exception.extra["ahead"]), (1, 0))
        self.assertEqual(e.exception.extra["both_changed"], ["checks/be.sh"])
        self.assertIn("mine", slurp(os.path.join(self.factory, "checks", "be.sh")), "nothing stashed or merged")

    def test_no_remote_is_only_a_note(self):
        sh(["git", "remote", "set-url", "origin", os.path.join(self.tmp, "gone.git")], self.factory)
        fx.factory_sync(self.ctx())
        self.assertTrue(any("could not check" in n for n in fx._FACTORY_SYNC["notes"]))


# --------------------------------------------------------------------------
# 2. the permission table's login check sends the user's own headers

class _Platform(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.headers.get("X-Platform") == "admin" and "sess=1" in (self.headers.get("Cookie") or "")
        self.send_response(200 if ok else 401)
        self.end_headers()

    def log_message(self, *a):
        pass


class LoginCheckHeadersTest(unittest.TestCase):
    def test_the_users_headers_go_with_the_check(self):
        srv = http.server.HTTPServer(("127.0.0.1", 0), _Platform)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        url = "http://127.0.0.1:%d" % srv.server_address[1]
        conf = {"check": "/api/v2/admin/profile"}
        self.assertIn("401", fx.login_check(url, conf, "Cookie", "sess=1"))
        self.assertIsNone(fx.login_check(url, conf, "Cookie", "sess=1", extra={"X-Platform": "admin"}))


# --------------------------------------------------------------------------
# 12c. a job of one repository in a project of several: its staged logic is checked against its own clone

class StagedLogicOwnCloneTest(GateCase):
    def test_a_new_file_of_the_job_is_found(self):
        write(os.path.join(self.factory, "logic", "README.md"), LOGIC_README)
        write(os.path.join(self.factory, "logic", "billing", "README.md"), MODULE % "")
        write(os.path.join(self.factory, "logic", "billing", "invoices.md"), FEATURE % ("Invoices", 1, "x"))
        write(os.path.join(self.factory, "logic", "limits.json"), "{}\n")
        self.commit_factory()
        job = self.job_for("be")
        self.commit_in(self.work(job, "be"), "config/ratelimit.php", "<?php\nreturn ['x' => 1];\n")
        write(os.path.join(self.factory, "jobs", job, "logic", "billing", "invoices.md"),
              FEATURE % ("Invoices", 1, "Limit: `be:config/ratelimit.php:1-2`"))
        code, res = self.gate(job)
        self.assertEqual(code, 0, res)
        self.assertNotIn("does not exist", slurp(self.checks_md(job) and self.round_file("checks.md", job)))
        self.assertEqual(res["gate"], "PASS", self.checks_md(job))


# --------------------------------------------------------------------------
# 14. a box gets the limits.env its app reads

class BoxEnvFilesTest(unittest.TestCase):
    def test_limits_env_is_copied_at_its_path(self):
        put = []
        with mock.patch.object(fx, "box_put", lambda box, paths, exclude=(): put.append(list(paths))):
            fx.box_put_env_files({"name": "b"}, {"FACTORY_LIMITS_ENV": __file__, "PORT": "1"})
            fx.box_put_env_files({"name": "b"}, {"FACTORY_LIMITS_ENV": "/nowhere/limits.env"})
        self.assertEqual(put, [[__file__], []])


# --------------------------------------------------------------------------
# 17. what fx reads of run.md and review files

class ReviewFormatTest(unittest.TestCase):
    def test_a_dotfile_is_a_path(self):
        self.assertTrue(fx.REVIEW_LOCATION_RE.search("`.env.example:29-31`"))
        self.assertTrue(fx.REVIEW_LOCATION_RE.search("src/a.php:3"))
        self.assertFalse(fx.REVIEW_LOCATION_RE.search("http://x:80"))


# --------------------------------------------------------------------------
# 3. scope, 5. shared limits, 6. the logic index, 12b. factory paths and marks

class LogicCase(ProjectCase):
    top = {"logic_modules": ["billing"]}
    extra = {"fe": {"scope": {"exclude": ["src/legacy/**"], "note": "v2 only"}}}

    def setUp(self):
        super().setUp()
        write(os.path.join(self.factory, "logic", "README.md"), LOGIC_README)
        write(os.path.join(self.factory, "logic", "billing", "README.md"), MODULE % "")
        write(os.path.join(self.factory, "logic", "billing", "invoices.md"), FEATURE % ("Invoices", 1, "x"))


class ScopeTest(LogicCase):
    def test_in_scope_and_the_prompt_block(self):
        ctx = self.ctx()
        self.assertFalse(fx.in_scope(ctx, "fe", "src/legacy/a.vue"))
        self.assertFalse(fx.in_scope(ctx, "fe", "src/legacy"))
        self.assertTrue(fx.in_scope(ctx, "fe", "src/views/a.vue"))
        self.assertTrue(fx.in_scope(ctx, "be", "app/x.php"))
        text = "\n".join(fx.scope_lines(ctx))
        self.assertIn("fe: all of it; not src/legacy/** (v2 only)", text)
        self.assertIn("only the modules billing", text)

    def test_the_module_map_leaves_out_what_is_out_of_scope(self):
        code = os.path.join(self.tmp, "fe-code")
        for d in ("src/legacy/old", "src/views/new"):
            for i in range(6):
                write(os.path.join(code, d, "f%d.vue" % i), "x\n")
        kdir = os.path.join(self.tmp, "k")
        write(os.path.join(kdir, "architecture.md"), "Modules: `src/views/new`\n")
        res = fx.knowledge_check(self.ctx(), kdir, {"fe": code}, modules_code=code, scope_repo="fe")
        self.assertFalse([m for m in res["missing_modules"] if "legacy" in m], res)

    def test_logic_of_another_module_is_refused(self):
        write(os.path.join(self.factory, "logic", "chat", "README.md"), MODULE % "")
        res = fx.logic_check(self.ctx(), os.path.join(self.factory, "logic"), {"be": self.tmp, "fe": self.tmp})
        self.assertTrue(any("chat/: not one of factory.json's logic_modules" in p for p in res["problems"]), res)
        code, res = self.fx("logic", "ctx", "--module", "chat")
        self.assertEqual(code, 3, res)

    def test_routes_out_of_scope_need_no_row(self):
        tech = {"out_of_scope": {"* /api/v2/chat/**": "chat"}}
        self.assertEqual(fx.perm_out_of_scope(tech, "GET /api/v2/chat/rooms/{id}"), "chat")
        self.assertIsNone(fx.perm_out_of_scope(tech, "GET /api/v2/billing"))


class SharedLimitsTest(LogicCase):
    def test_one_limit_cited_twice_with_two_names_is_refused(self):
        rows = "| api calls | 6000 | `be:Base/Route.php:62-64` | %s | 1000000 |"
        write(os.path.join(self.factory, "logic", "billing", "README.md"), MODULE % (rows % "API_THROTTLE_PER_MINUTE"))
        write(os.path.join(self.factory, "logic", "shared.md"), "# Shared\n\n## Limits\n"
              "| Parameter | Value | Code | Env | Test value |\n|---|---|---|---|---|\n"
              + rows % "API_RATE_LIMIT_PER_MINUTE" + "\n")
        res = fx.limits_check(self.ctx())
        self.assertIn("API_RATE_LIMIT_PER_MINUTE", res["env"], "logic/shared.md's Limits table counts")
        self.assertTrue(any("cite the same code" in p for p in res["problems"]), res)

    def test_a_vietnamese_test_value_column(self):
        write(os.path.join(self.factory, "logic", "billing", "README.md"),
              "# B\n\n## 2. Bảng thông số\n| Tham số | Giá trị | Mã | Env | Giá trị test |\n|---|---|---|---|---|\n"
              "| x | 5 | `be:a.php:1` | X_LIMIT | 99 |\n")
        res = fx.limits_check(self.ctx())
        self.assertEqual(res["unchecked"], [])
        self.assertEqual(res["env"].get("X_LIMIT"), "99")


class LogicIndexTest(LogicCase):
    def test_a_new_feature_gets_a_row_in_its_modules_table(self):
        ld = os.path.join(self.factory, "logic")
        write(os.path.join(ld, "billing", "refunds.md"), FEATURE % ("Refunds", 2, "x"))
        res = self.ok("logic", "index", "--log", "init wrote the logic")
        self.assertEqual(res["added"], ["billing/refunds.md"])
        mod = slurp(os.path.join(ld, "billing", "README.md"))
        self.assertIn("| Invoices | [invoices.md](invoices.md) |\n| Refunds | [refunds.md](refunds.md) |\n\n## 1.", mod)
        self.assertNotIn("refunds", slurp(os.path.join(ld, "README.md")), "the top index lists modules only")
        self.assertIn("init wrote the logic", slurp(os.path.join(ld, "README.md")))
        self.assertEqual(self.ok("logic", "index")["added"], [], "only what is not there yet")

    def test_a_new_module_gets_a_row_and_a_feature_table(self):
        ld = os.path.join(self.factory, "logic")
        write(os.path.join(ld, "tax", "README.md"), "# Module Tax rules (tax)\n\n**Parent ticket:** -\n\n## 1. Rules\nx\n")
        write(os.path.join(ld, "tax", "vat.md"), FEATURE % ("VAT", 3, "x"))
        res = self.ok("logic", "index")
        self.assertEqual((res["added"], res["modules_added"]), (["tax/vat.md"], ["tax"]))
        self.assertIn("| [tax/](tax/README.md) | Tax rules |\n<!-- index:end -->", slurp(os.path.join(ld, "README.md")))
        self.assertIn("**Parent ticket:** -\n\n| Feature | File |\n| ------- | ---- |\n| VAT | [vat.md](vat.md) |\n\n## 1.",
                      slurp(os.path.join(ld, "tax", "README.md")))

    def test_check_names_what_the_index_lacks_and_index_mends_it(self):
        ld = os.path.join(self.factory, "logic")
        write(os.path.join(ld, "billing", "refunds.md"), FEATURE % ("Refunds", 2, "x"))
        os.remove(os.path.join(ld, "billing", "invoices.md"))
        probs = fx.logic_check(self.ctx(), ld, {"be": self.tmp, "fe": self.tmp})["problems"]
        self.assertTrue(any("billing/refunds.md: not in its module's feature table" in p for p in probs), probs)
        self.assertTrue(any("lists invoices.md, which does not exist" in p for p in probs), probs)
        res = self.ok("logic", "index")
        self.assertEqual((res["added"], res["removed"]), (["billing/refunds.md"], ["billing/invoices.md"]))
        probs = fx.logic_check(self.ctx(), ld, {"be": self.tmp, "fe": self.tmp})["problems"]
        self.assertFalse([p for p in probs if "fx logic index" in p], probs)

    def test_an_index_of_every_feature_becomes_a_list_of_modules(self):
        # an older index: one row per feature, Vietnamese headers; the module README has no feature table yet
        ld = os.path.join(self.factory, "logic")
        write(os.path.join(ld, "README.md"), "# Logic\n\n## Mục lục\n| Module | File | Tính năng | Ticket |\n|---|---|---|---|\n"
              "| billing | [billing/invoices.md](billing/invoices.md) | Hoá đơn | SHOP-1 |\n"
              "| billing | [billing/refunds.md](billing/refunds.md) | Hoàn tiền | SHOP-2 |\n<!-- index:end -->\n\n## Quy ước\nx\n")
        write(os.path.join(ld, "billing", "README.md"), "# Module Thanh toán (billing)\n\n## 1. Rules\nx\n\n## 2. P\nx\n")
        write(os.path.join(ld, "billing", "refunds.md"), FEATURE % ("Hoàn tiền", 2, "x"))
        probs = fx.logic_check(self.ctx(), ld, {"be": self.tmp, "fe": self.tmp})["problems"]
        self.assertTrue(any("as an older version kept it" in p for p in probs), probs)
        res = self.ok("tidy", "--force")
        self.assertTrue(any("now a list of 1 modules" in m["what"] for m in res["logic_moved"]), res["logic_moved"])
        idx = slurp(os.path.join(ld, "README.md"))
        self.assertIn("## Mục lục\n| Module | Name |\n| ------ | ---- |\n| [billing/](billing/README.md) | Thanh toán |\n"
                      "<!-- index:end -->\n\n## Quy ước", idx)
        mod = slurp(os.path.join(ld, "billing", "README.md"))
        self.assertIn("| Invoices | [invoices.md](invoices.md) |\n| Hoàn tiền | [refunds.md](refunds.md) |", mod)
        probs = fx.logic_check(self.ctx(), ld, {"be": self.tmp, "fe": self.tmp})["problems"]
        self.assertFalse([p for p in probs if "fx logic index" in p], probs)

    def test_apply_puts_a_new_feature_in_its_modules_table(self):
        ctx = self.ctx()
        job = {"id": "SHOP-8", "issue_key": "SHOP-8", "summary": "s", "repos": {}}
        sdir = os.path.join(self.factory, "jobs", "SHOP-8", "logic")
        write(os.path.join(sdir, "billing", "refunds.md"), FEATURE % ("Refunds", 8, "x"))
        with mock.patch.object(fx, "logic_check", lambda *a, **k: {"problems": []}):
            fx.apply_logic(ctx, job, ["billing/refunds.md"], force=True)
        self.assertIn("[refunds.md](refunds.md)", slurp(os.path.join(self.factory, "logic", "billing", "README.md")))

    def test_apply_keeps_a_staged_index(self):
        ctx = self.ctx()
        job = {"id": "SHOP-9", "issue_key": "SHOP-9", "summary": "s", "repos": {}}
        sdir = os.path.join(self.factory, "jobs", "SHOP-9", "logic")
        write(os.path.join(sdir, "README.md"), LOGIC_README.replace("# Logic", "# Logic, staged"))
        write(os.path.join(sdir, "billing", "credits.md"), FEATURE % ("Credits", 3, "x"))
        with mock.patch.object(fx, "logic_check", lambda *a, **k: {"problems": []}):
            fx.apply_logic(ctx, job, ["README.md", "billing/credits.md"], force=True)
        idx = slurp(os.path.join(self.factory, "logic", "README.md"))
        self.assertIn("# Logic, staged", idx)
        self.assertIn("[credits.md](credits.md)", slurp(os.path.join(self.factory, "logic", "billing", "README.md")))


class FactoryPathsTest(LogicCase):
    def test_a_factory_file_is_not_a_citation(self):
        write(os.path.join(self.factory, "logic", "billing", "README.md"),
              MODULE % "" + "\n## Permissions\nSee `stack/permissions-be.json`.\n")
        res = fx.logic_check(self.ctx(), os.path.join(self.factory, "logic"), {"be": self.tmp, "fe": self.tmp},
                             check_index=False)
        self.assertFalse([p for p in res["problems"] if "stack/" in p], res)

    def test_a_mark_needs_clean_logic(self):
        write(os.path.join(self.factory, "logic", "billing", "README.md"), "# no sections\n")
        with mock.patch.object(fx, "knowledge_codes", lambda c: {"be": self.tmp, "fe": self.tmp}):
            with self.assertRaises(fx.FxError) as e:
                fx.logic_clean_or_stop(self.ctx(), "the limits")
        self.assertIn("missing sections", str(e.exception))


# --------------------------------------------------------------------------
# 7. a repository's addition to a shared knowledge file

class RepoAdditionTest(ProjectCase):
    def test_an_existing_addition_is_applied_a_new_one_refused(self):
        write(os.path.join(self.factory, "knowledge", "fe", "domain.md"), "# FE domain\n")
        ctx = self.ctx()
        stage = os.path.join(self.tmp, "stage")
        src, dst = fx.staged_target(ctx, stage, "fe", "fe/domain.md")
        self.assertEqual(dst, os.path.join(self.factory, "knowledge", "fe", "domain.md"))
        with self.assertRaises(fx.FxError):
            fx.staged_target(ctx, stage, "fe", "fe/rules.md")


# --------------------------------------------------------------------------
# 8. how far a gate is

class ProgressTest(ProjectCase):
    def test_the_permission_table_says_cells_done(self):
        place = fx.Place({"id": "l1", "kind": "gate: app and scans", "factory": self.factory, "job": "SHOP-1"})
        fx._PLACES.append(place)
        self.addCleanup(fx._PLACES.remove, place)
        fx._PROGRESS.clear()
        fx.progress("checks")
        fx.progress("permissions", 312, 856)
        p = fx.progress_read(self.factory, "SHOP-1")
        self.assertEqual(p["says"], "permissions 312/856")
        self.assertEqual(p["steps"], ["checks", "permissions"])
        self.assertIn("left_min", p)
        data = load_json(os.path.join(self.factory, "jobs", "SHOP-1", "progress.json"))
        data["pid"] = 999999
        write(os.path.join(self.factory, "jobs", "SHOP-1", "progress.json"), json.dumps(data))
        self.assertIsNone(fx.progress_read(self.factory, "SHOP-1"), "a gate that is not running says nothing")


# --------------------------------------------------------------------------
# 9. installing in a box

class BoxInstallTest(unittest.TestCase):
    def test_default_hosts(self):
        hosts = fx.sandbox_allow(None)
        for h in ("api.github.com", "dl-cdn.alpinelinux.org", "deb.debian.org", "archive.ubuntu.com"):
            self.assertIn(h, hosts)

    def test_docker_client_proxy_by_address(self):
        line = fx.box_client_proxy_line("172.25.0.2")
        self.assertIn("/root/.docker/config.json", line)
        self.assertIn("http://172.25.0.2:8888", line)
        self.assertIn("rm -f /root/.docker/config.json", fx.box_client_proxy_line(None))

    def test_a_refused_host_is_named(self):
        line, why = fx.net_failure(None, "ok\nfetch https://repo.example.org/x.tar.gz\n"
                                         "curl: (56) CONNECT tunnel failed, response 403 https://repo.example.org/x\n")
        self.assertIn("CONNECT tunnel failed", line)
        self.assertIn("repo.example.org is not in sandbox.allow", why)
        self.assertIn("no way out", fx.net_failure(None, "proxyconnect tcp: no route to host", offline=True)[1])
        self.assertEqual(fx.net_failure(None, "all good\n"), (None, None))

    def test_node_gets_the_proxy_while_installing(self):
        env = fx.box_env({"online": True, "proxy_ip": "10.0.0.2"}, {})
        self.assertEqual(env["NODE_USE_ENV_PROXY"], "1")


# --------------------------------------------------------------------------
# 10. a Jira issue that is the fix of a MISSING limit

class FixesLimitTest(LogicCase):
    def test_the_named_missing_limit_lets_it_start(self):
        write(os.path.join(self.factory, "logic", "billing", "README.md"),
              MODULE % "| api calls | 60 | `be:a.php:1` | MISSING: API_LIMIT | 1000000 |")
        write(os.path.join(self.factory, "logic", "limits.json"), "{}\n")
        ctx = self.ctx()
        a = types.SimpleNamespace(local=False, issue_file=None, fixes_limit=["billing/api calls"])
        found = fx.limits_gate_new(ctx, a)
        self.assertEqual([m["parameter"] for m in found], ["api calls"])
        a.fixes_limit = ["billing/nothing"]
        with self.assertRaises(fx.FxError):
            fx.limits_gate_new(ctx, a)


# --------------------------------------------------------------------------
# 11. a login saved where its repository reads it; 12. tried before it is saved

class LoginSaveTest(ProjectCase):
    extra = {"fe": {"security": {"zap_auth": {}}}}
    top = {"security": {"zap_auth": {"command": "echo top", "check": "/"}}}

    def test_saved_into_the_repositorys_own_block(self):
        ctx = self.ctx()
        where = fx.login_save_factory(ctx, "web", {"command": "echo fe", "check": "/me"}, "fe")
        self.assertEqual(where, "repos[fe].security.zap_auth")
        cfg = load_json(os.path.join(self.factory, "factory.json"))
        self.assertEqual(cfg["repos"][1]["security"]["zap_auth"]["command"], "echo fe")
        self.assertEqual(cfg["security"]["zap_auth"]["command"], "echo top")
        fx.login_save_factory(ctx, "web", {"command": "echo be", "check": "/"}, "be")
        self.assertEqual(load_json(os.path.join(self.factory, "factory.json"))["security"]["zap_auth"]["command"],
                         "echo be", "a repository without a block of its own reads the project's")

    def test_an_empty_one_hiding_the_projects_is_said(self):
        rows = []
        fx.doctor_repos.__wrapped__ if hasattr(fx.doctor_repos, "__wrapped__") else None
        with mock.patch.object(fx, "remote_heads", lambda url: ({"main": "x"}, None)):
            fx.doctor_repos(self.ctx(), lambda n, ok, d="": rows.append((n, ok, d)))
        hit = [r for r in rows if "fe: its" in r[0]]
        self.assertTrue(hit and not hit[0][1], rows)
        rctx = fx.repo_ctx(self.ctx(), "fe")
        self.assertIn("which is empty", fx.login_where_words(rctx, "web"))

    def test_untested_needs_a_reason(self):
        code, res = self.fx("login", "--kind", "web", "--command", "echo x", "--check", "/", "--save", "factory",
                            "--untested", "--repo", "be")
        self.assertEqual(code, 3, res)
        self.assertIn("--why", res["error"])
        res = self.ok("login", "--kind", "web", "--command", "echo x", "--check", "/", "--save", "factory",
                      "--untested", "--repo", "be", "--why", "its setup needs a VPN")
        self.assertEqual(res["untested_because"], "its setup needs a VPN")

    def test_several_repositories_need_repo(self):
        code, res = self.fx("login", "--kind", "web", "--command", "echo x", "--check", "/", "--save", "factory",
                            "--untested", "--why", "w")
        self.assertEqual(code, 3, res)
        self.assertIn("--repo", res["error"])


class LoginRunTest(ProjectCase):
    def test_what_the_command_said_is_kept(self):
        r = types.SimpleNamespace(returncode=0, stdout="", stderr="POST /login -> 429\n")
        job = {"id": "SHOP-1", "worktree": self.tmp, "port": 5100}
        with mock.patch.object(fx, "app_env", lambda c, j: ({}, "db")), \
                mock.patch.object(fx, "proj_run", lambda *a, **k: r):
            with self.assertRaises(fx.ScannerLogin) as e:
                fx.login_run(self.ctx(), job, {"command": "x", "_kind": "web"})
        self.assertIn("429", e.exception.why)
        self.assertIn("429", slurp(os.path.join(self.factory, "jobs", "SHOP-1", "login.log")))


# --------------------------------------------------------------------------
# 18. one JSON answer per command

class OneAnswerTest(ProjectCase):
    def test_a_second_answer_goes_to_stderr(self):
        def twice(ctx, a):
            fx.out({"ok": True, "first": 1})
            raise fx.FxError("while cleaning up", code=3)
        err = io.StringIO()
        buf = io.StringIO()
        with mock.patch.object(fx, "cmd_status", twice), contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(err):
            os.chdir(self.factory)
            try:
                fx.main(["status"])
            except SystemExit as e:
                self.assertEqual(e.code, 3)
            finally:
                os.chdir(self.cwd)
        self.assertEqual(json.loads(buf.getvalue()), {"ok": True, "first": 1})
        self.assertIn("while cleaning up", err.getvalue())


# --------------------------------------------------------------------------
# 13. the factory's scripts; 19 and S2. what a repository needs of another

class NeedsTest(ProjectCase):
    extra = {"fe": {"needs": ["be@develop"]}}

    def test_a_read_clone_of_what_it_needs(self):
        ctx = self.ctx()
        self.assertEqual(fx.repo_needs(ctx, "fe"), [("be", "develop")])
        self.assertEqual(fx.needed_repos(ctx, ["fe"]), ["be"])
        self.assertEqual(fx.needs_branch(ctx, ["fe"], "be"), "develop")
        env = fx.clone_env({"repos": {"be": {"worktree": "/w/be"}}, "clones": {"api-x": {"worktree": "/w/x"}}})
        self.assertEqual(env, {"FACTORY_CLONE_BE": "/w/be", "FACTORY_CLONE_API_X": "/w/x"})

    def test_a_job_of_fe_gets_be_on_the_branch_named(self):
        job = self.new("--repos", "fe")["job"]
        raw = self.raw(job)
        self.assertEqual(raw["repos"]["be"]["role"], "run")
        self.assertEqual(raw["repos"]["be"]["base_branch"], "develop")
        self.assertTrue(os.path.exists(os.path.join(raw["repos"]["be"]["worktree"], "only-on-develop.txt")))


class DoctorScriptsTest(ProjectCase):
    def test_syntax_and_fetching(self):
        write(os.path.join(self.factory, "checks", "be.sh"), "#!/usr/bin/env bash\nif then\n")
        write(os.path.join(self.factory, "checks", "fe.sh"),
              "#!/usr/bin/env bash\ngit clone git@gitlab.com:team/be.git ../be\n")
        rows = []
        fx.doctor_scripts(self.ctx(), lambda n, ok, d="": rows.append((n, ok, d)))
        parse = [r for r in rows if "bash -n" in r[0]][0]
        self.assertFalse(parse[1])
        self.assertIn("checks/be.sh", parse[2])
        fetch = [r for r in rows if "fetch nothing" in r[0]][0]
        self.assertIn("checks/fe.sh:2 git gitlab.com", fetch[2])


# --------------------------------------------------------------------------
# 19. a gate tried without counting

class DryRunGateTest(GateCase):
    def test_the_round_is_put_aside(self):
        job = self.job_for("be")
        self.commit_in(self.work(job, "be"), "be.txt", "work\n")
        before = self.raw(job)
        code, res = self.fx("gate", job, "--dry-run")
        self.assertEqual(code, 3, res)
        self.assertTrue(res.get("void"), res)
        self.assertIn("a dry run of the gate", res["error"])
        after = self.raw(job)
        self.assertEqual(after["stage"], "build")
        self.assertEqual(fx.rounds_left(after), fx.rounds_left(before))


# --------------------------------------------------------------------------
# the light lane: a small job leaves out what adds nothing, and the diff puts back what it needs

INTAKE_LIGHT = ("INTAKE: ACCEPT\nRISK: low\nSURFACES: -\nREPOS: be\nLANE: light\nSKIP: spec, planner, reviewers, approver\n\n"
                "## Summary\nRemove an unused import.\n\n## Reason\nSmall.\n\n## Lane\n- spec: one line, criteria below\n"
                "- planner: one builder\n- reviewers: the checks prove it\n- approver: nothing sensitive\n\n"
                "## Acceptance criteria\n1. The lint passes.\n2. Nothing else changes.\n")


class LightLaneTest(GateCase):
    def light_job(self):
        job = self.new("--repos", "be")["job"]
        write(os.path.join(self.factory, "jobs", job, "intake.md"), INTAKE_LIGHT)
        res = self.ok("result", job, "intake")
        self.assertEqual(res["lane"]["skip"], ["approver", "planner", "reviewers", "spec"])
        return job

    def test_spec_and_planner_are_written_by_fx(self):
        job = self.light_job()
        res = self.ok("ctx", job, "--agent", "spec")
        self.assertTrue(res["skipped"])
        spec = slurp(os.path.join(self.factory, "jobs", job, "spec.md"))
        self.assertTrue(spec.startswith("SPEC: READY"))
        self.assertIn("1. The lint passes.", spec)
        self.assertEqual(self.ok("result", job, "spec")["next"], "build")

    def test_a_small_diff_goes_straight_to_the_pull_request(self):
        job = self.light_job()
        self.ok("ctx", job, "--agent", "spec")
        self.ok("result", job, "spec")
        self.commit_in(self.work(job, "be"), "a.txt", "one line\n")
        code, res = self.gate(job)
        self.assertEqual((code, res["gate"]), (0, "PASS"), res)
        self.assertEqual(res["next"], "approve")
        self.assertEqual(res["reviewers"], [])
        self.assertIn("Light lane", res["lane"])
        res = self.ok("ctx", job, "--agent", "approver")
        self.assertTrue(res["skipped"], res)
        self.assertEqual(self.ok("result", job, "decision")["next"], "pr")

    def test_a_big_diff_goes_the_whole_way(self):
        job = self.light_job()
        self.ok("ctx", job, "--agent", "spec")
        self.ok("result", job, "spec")
        self.commit_in(self.work(job, "be"), "big.txt", "x\n" * 80)
        code, res = self.gate(job)
        self.assertEqual(res["next"], "review", res)
        self.assertIn("security", res["reviewers"])
        self.assertIn("every step ran", res["lane"])

    def test_a_high_risk_job_is_never_light(self):
        job = self.new("--repos", "be")["job"]
        write(os.path.join(self.factory, "jobs", job, "intake.md"), INTAKE_LIGHT.replace("RISK: low", "RISK: high"))
        res = self.ok("result", job, "intake")
        self.assertNotIn("lane", res)
        self.assertIn("RISK: low", res["lane_note"])


# --------------------------------------------------------------------------
# a gate reads the factory as committed: another session's edits stop nothing

class FrozenFactoryTest(GateCase):
    def test_an_uncommitted_edit_does_not_stop_the_gate(self):
        job = self.job_for("be")
        self.commit_in(self.work(job, "be"), "be.txt", "work\n")
        write(os.path.join(self.factory, "checks", "be.sh"), "#!/usr/bin/env bash\necho EDITED\nexit 1\n")
        code, res = self.gate(job)
        self.assertEqual((code, res.get("gate")), (0, "PASS"), res)
        self.assertNotIn("EDITED", slurp(self.round_file("checks.log", job)) if os.path.exists(
            self.round_file("checks.log", job)) else "")
        self.assertTrue(any("not committed" in n for n in res["notes"]), res["notes"])
        frozen = os.path.join(self.tmp, "shop-factory-work", "_factory")
        self.assertTrue(os.listdir(frozen))


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# notes on the skill itself, posted as a public GitHub issue

GH_STUB = """#!%(python)s
import json, os, sys
args = sys.argv[1:]
log = os.environ["GH_LOG"]
with open(log, "a") as f:
    f.write(json.dumps(args) + "\\n")
    if "--body-file" in args:
        f.write("BODY:" + json.dumps(open(args[args.index("--body-file") + 1]).read()) + "\\n")
if args[:2] == ["auth", "status"]:
    sys.exit(int(os.environ.get("GH_AUTH", "0")))
if args[:2] == ["issue", "list"]:
    print(os.environ.get("GH_LIST", "[]"))
elif args[:2] == ["issue", "create"]:
    print("https://github.com/lindanchu/code-factory/issues/7")
elif args[:2] == ["issue", "comment"]:
    print("https://github.com/lindanchu/code-factory/issues/3#issuecomment-1")
"""

NOTE = ("## Problem\nThe gate of SHOP-12 in shop-factory waited 50 minutes on a box with no network.\n\n"
        "## Evidence\nfx said so in /Users/somebody/work/shop-factory/jobs/SHOP-12/gate.log; the app at "
        "https://git.shop.example/team/shop-api failed. Mail ops@shop.example, token ghp_abcdefghijklmnopqrstuvwxyz0123. "
        "UTF-8 and CVE-2024-1234 stay. POST /quan-ly/cua-hang/{id}/key/{key}/huy-bo?tab=keys revoked it; "
        "GET /api/v1/ping and /tmp/zap-user stay.\n\n"
        "## Change\nVoid the round when the box is offline.\n")


class SkillNoteTest(ProjectCase):
    def setUp(self):
        super().setUp()
        self.gh_log = os.path.join(self.tmp, "gh.log")
        path = os.path.join(self.bin, "gh")
        write(path, GH_STUB % {"python": sys.executable})
        os.chmod(path, 0o755)
        os.environ["GH_LOG"] = self.gh_log
        self.note = os.path.join(self.tmp, "note.md")
        write(self.note, NOTE)

    def note_args(self, *more, title="Void a gate round when the box is offline"):
        return ("skill-note", "--job", "SHOP-12", "--agent", "builder", "--title", title,
                "--where", "fx gate", "--file", self.note) + more

    def calls(self):
        if not os.path.exists(self.gh_log):
            return [], ""
        lines = open(self.gh_log).read().splitlines()
        return ([json.loads(l) for l in lines if not l.startswith("BODY:")],
                "".join(json.loads(l[5:]) for l in lines if l.startswith("BODY:")))

    def test_a_note_is_posted_with_nothing_private(self):
        res = self.ok(*self.note_args())
        self.assertTrue(res["posted"], res)
        self.assertEqual(res["sent"][0]["url"], "https://github.com/lindanchu/code-factory/issues/7")
        calls, body = self.calls()
        create = next(c for c in calls if c[:2] == ["issue", "create"])
        self.assertIn("lindanchu/code-factory", create)
        self.assertTrue(create[create.index("--title") + 1].startswith("[skill] "))
        for private in ("SHOP-12", "shop-factory", "somebody", "git.shop.example", "ops@", "ghp_", "shop-api",
                        "quan-ly", "cua-hang", "huy-bo", "tab=keys"):
            self.assertNotIn(private, body)
        for kept in ("UTF-8", "CVE-2024-1234", "`fx gate`", "Void the round", "fx-note: ", fx.VERSION,
                     "/<part>/<part>/{id}/key/{key}/<part>?<query>", "/api/v1/ping", "/tmp/zap-user"):
            self.assertIn(kept, body)
        for machine in ("Darwin", "macOS", "Linux", "Machine", self.tmp):
            self.assertNotIn(machine, body)
        self.assertEqual(res["waiting"], 0)

    def test_not_logged_in_says_why_and_the_note_waits(self):
        os.environ["GH_AUTH"] = "1"
        res = self.ok(*self.note_args())
        self.assertFalse(res["posted"])
        self.assertEqual(res["waiting"], 1)
        self.assertEqual(res["command"], "gh auth login")
        for words in ("public issue", "nothing about the user", "never sees the password", "cannot be turned off"):
            self.assertIn(words, res["ask_user"])
        self.assertTrue(res["next"].startswith("ask"))
        os.environ["GH_AUTH"] = "0"
        res = self.ok("skill-note", "--push")
        self.assertTrue(res["posted"], res)
        self.assertEqual(res["waiting"], 0)

    def test_the_same_note_comments_on_its_open_issue(self):
        os.environ["GH_LIST"] = json.dumps([{"number": 3, "url": "https://github.com/lindanchu/code-factory/issues/3"}])
        res = self.ok(*self.note_args())
        self.assertEqual(res["sent"][0]["as"], "comment")
        calls, _ = self.calls()
        self.assertFalse([c for c in calls if c[:2] == ["issue", "create"]])
        self.assertTrue([c for c in calls if c[:3] == ["issue", "comment", "3"]])

    def test_a_job_posts_a_note_once_and_three_at_most(self):
        self.ok(*self.note_args())
        self.assertTrue(self.ok(*self.note_args()).get("duplicate"))
        self.ok(*self.note_args(title="second"))
        self.ok(*self.note_args(title="third"))
        code, res = self.fx(*self.note_args(title="fourth"))
        self.assertEqual(code, 1, res)
        self.assertIn("3 notes", res["error"])

    def test_a_note_names_the_skill_and_has_its_parts(self):
        code, res = self.fx("skill-note", "--title", "x", "--where", "src/app.py", "--file", self.note)
        self.assertEqual(code, 1)
        self.assertIn("names no file of the plugin", res["error"])
        write(self.note, "## Problem\nonly this\n")
        code, res = self.fx("skill-note", "--title", "x", "--where", "skills/code/SKILL.md", "--file", self.note)
        self.assertEqual(code, 1)
        self.assertIn("Evidence", res["error"])

    def test_there_is_no_way_to_turn_it_off(self):
        os.environ["FACTORY_SKILL_FEEDBACK"] = "off"
        cfg = load_json(os.path.join(self.factory, "factory.json"))
        cfg["skill_feedback"] = False
        self.write_config(cfg)
        res = self.ok(*self.note_args())
        self.assertTrue(res["posted"], res)

    def test_gh_missing_is_installed_by_fx(self):
        os.remove(os.path.join(self.bin, "gh"))
        def install(name):
            self.assertEqual(name, "gh")
            path = os.path.join(self.bin, "gh")
            write(path, GH_STUB % {"python": sys.executable})
            os.chmod(path, 0o755)
            return True, "installed with brew"
        real = shutil.which  # a gh this machine has in /usr/bin is not the test's: only the test's folder counts

        def which(name, *a, **k):
            return real(name, path=self.bin) if name == "gh" else real(name, *a, **k)
        with mock.patch.object(fx, "install_one", install), mock.patch.object(fx.shutil, "which", which):
            res = self.ok(*self.note_args())
        self.assertTrue(res["posted"], res)
        self.assertTrue(any("installed gh" in n for n in res["notes"]))

    def test_every_agent_is_told_how(self):
        line = fx.skill_note_line({"id": "SHOP-12"}, "builder")
        self.assertIn("skill-note --job SHOP-12 --agent builder", line)
        self.assertIn("really need changing", line)


# --------------------------------------------------------------------------
# long files: knowledge split into an index and parts, logic's history and open points apart

def big(title, n):
    return "## %s\n\n### %s one\n%s\n" % (title, title, ("Rule of %s, said at length. " % title) * n)


class LongFilesTest(LogicCase):
    top = {"logic_modules": ["billing"], "learning": {"ask_after": "never", "file_max_kb": 2}}

    def setUp(self):
        super().setUp()
        kd = os.path.join(self.factory, "knowledge")
        write(os.path.join(kd, "README.md"), "# Knowledge\n\n- [style of be](be/style.md)\n")
        write(os.path.join(kd, "be", "README.md"), "# be\n")
        write(os.path.join(kd, "be", "style.md"), "# Style of be\n\nNew code follows this.\n\n" +
              big("Controller", 40) + big("Migration", 40) + big("Test", 40))
        write(os.path.join(kd, "be", "testing.md"), "# Testing\n\nSee `style.md#migration` and [style](style.md).\n")
        write(os.path.join(kd, "fe", "README.md"), "# fe\n")
        write(os.path.join(kd, "fe", "style.md"), "# Style of fe\n\nshort\n")
        write(os.path.join(self.factory, "logic", "README.md"), LOGIC_README)
        write(os.path.join(self.factory, "logic", "billing", "README.md"), MODULE.replace(
            "|\n\n## 1.", "|\n| Credit | [credit.md](credit.md) |\n\n## 1.") % "" +
              "\n## 3. Open points\n1. Is a credit note a refund?\n\n## Change history\n| Date | Change | Ticket |\n"
              "|---|---|---|\n| 01.11.2026 | written | SHOP-1 |\n")
        write(os.path.join(self.factory, "logic", "billing", "credit.md"), FEATURE % ("Credit", 2, "Long text. " * 300) +
              "\n## Change history\n| Date | Change | Ticket |\n|---|---|---|\n| 02.11.2026 | added | SHOP-2 |\n")

    def test_tidy_splits_knowledge_and_moves_logic_history(self):
        res = self.ok("tidy", "--force")
        kd = os.path.join(self.factory, "knowledge")
        self.assertEqual([x["file"] for x in res["split"]], ["knowledge/be/style.md"])
        self.assertFalse(os.path.exists(os.path.join(kd, "be", "style.md")))
        idx = slurp(os.path.join(kd, "be", "style", "README.md"))
        self.assertIn("<!-- parts of style.md:", idx)
        self.assertIn("[controller.md](controller.md)", idx)
        self.assertIn("New code follows this.", idx)
        self.assertEqual(sorted(os.listdir(os.path.join(kd, "be", "style"))),
                         ["README.md", "controller.md", "migration.md", "test.md"])
        self.assertIn("be/style/README.md", slurp(os.path.join(kd, "README.md")))
        testing = slurp(os.path.join(kd, "be", "testing.md"))
        self.assertIn("`style/migration.md#migration`", testing)
        self.assertIn("[style](style/README.md)", testing)
        self.assertTrue(os.path.exists(os.path.join(kd, "fe", "style.md")))  # short: left alone
        ctx = self.ctx()
        files = [os.path.relpath(f, kd) for f in fx.knowledge_files(ctx, "builder", repos=["be"])]
        self.assertIn("be/style/README.md", files)
        self.assertNotIn("be/style/controller.md", files)
        self.assertIn("style/controller.md", fx.knowledge_names(os.path.join(kd, "be")))
        ld = os.path.join(self.factory, "logic")
        self.assertNotIn("Overall log", slurp(os.path.join(ld, "README.md")))
        self.assertIn("<!-- log:end -->", slurp(os.path.join(ld, "history.md")))
        self.assertEqual(fx.logic_log_path(ld), os.path.join(ld, "history.md"))
        mod = slurp(os.path.join(ld, "billing", "README.md"))
        self.assertIn("## 3. Open points", mod)
        self.assertIn("[open.md](open.md)", mod)
        self.assertNotIn("credit note", mod)
        self.assertNotIn("Change history", mod)
        self.assertIn("credit note a refund", slurp(os.path.join(ld, "billing", "open.md")))
        hist = slurp(os.path.join(ld, "billing", "history.md"))
        self.assertIn("## README.md", hist)
        self.assertIn("## credit.md", hist)
        self.assertIn("SHOP-2", hist)
        self.assertNotIn("Change history", slurp(os.path.join(ld, "billing", "credit.md")))
        self.assertEqual([x["file"] for x in res["logic_too_long"]], ["logic/billing/credit.md"])
        check = fx.logic_check(ctx, ld, {"be": self.tmp, "fe": self.tmp}, check_index=True)
        self.assertFalse([p for p in check["problems"] if "history.md" in p or "open.md" in p], check["problems"])
        self.assertEqual(check["too_long"], ["billing/credit.md"])

    def test_history_headings_in_both_languages(self):
        for h in ("## Overall log", "## Overall history", "## Nhật ký tổng", "## 4. Nhật ký thay đổi", "## Nhật ký",
                  "## Lịch sử thay đổi", "## Changelog"):
            self.assertTrue(fx.HISTORY_HEAD_RE.match(h), h)
        for h in ("## Nhật ký đăng nhập", "## Overall rules"):
            self.assertFalse(fx.HISTORY_HEAD_RE.match(h), h)

    def test_tidy_wants_the_files_committed_first(self):
        code, res = self.fx("tidy")
        self.assertEqual(code, 3, res)
        self.assertIn("commit them first", res["error"])

    def test_a_split_file_takes_staged_parts_and_learned_rules(self):
        self.ok("tidy", "--force")
        ctx = self.ctx()
        stage = os.path.join(self.tmp, "stage")
        write(os.path.join(stage, "be", "style", "controller.md"), "## Controller\nnew\n")
        src, dst = fx.staged_target(ctx, stage, "be", "be/style/controller.md")
        self.assertEqual(dst, os.path.join(self.factory, "knowledge", "be", "style", "controller.md"))
        self.assertIn("be/style/controller.md", fx.staged_knowledge(ctx, stage, "be"))
        with self.assertRaises(fx.FxError):
            fx.staged_target(ctx, stage, "be", "be/other/x.md")

    def test_the_writer_is_told_to_split_a_long_feature(self):
        self.ok("tidy", "--force")
        res = self.ok("logic", "ctx", "--split", "logic/billing/credit.md")
        text = slurp(res["prompt_file"])
        self.assertIn("Split only: logic/billing/credit.md", text)
        self.assertIn("Task: Split billing/credit.md into sub-features.", text)
        code, res = self.fx("logic", "ctx", "--split", "logic/billing/history.md")
        self.assertEqual(code, 1)

    def test_lessons_reach_agents_without_their_notes(self):
        text = ("# Lessons learned\n\nIntro.\n\n## 2026-11-05 (from SHOP-1)\n\n"
                "- [L1] [code] Paginate every list. <!-- 2026-11-05; from SHOP-1 -->\n")
        short = fx.lessons_compact(text)
        self.assertIn("- [L1] [code] Paginate every list.", short)
        self.assertNotIn("<!--", short)
        self.assertNotIn("## 2026", short)


class GithubLoginRequiredTest(ProjectCase):
    """Notes on the skill are part of every install: no init, no doctor pass and no new job before gh auth login."""

    def test_init_doctor_and_a_new_job_wait_for_the_login(self):
        os.environ["STUB_GH"] = "logged-out"
        for argv in (("init", "--repo", "be=%s" % self.origin["be"]),
                     ("new", "--local", "a job", "--issue-file", self.issue)):
            code, res = self.fx(*argv)
            self.assertEqual(code, 3, (argv, res))
            self.assertTrue(res["github_login"], res)
            self.assertEqual(res["command"], "gh auth login")
            self.assertIn("cannot be turned off", res["ask_user"])
        code, res = self.fx("doctor")
        row = next(c for c in res["checks"] if c["check"].startswith("GitHub login"))
        self.assertFalse(row["ok"], row)
        os.environ["STUB_GH"] = ""
        code, res = self.fx("doctor")
        self.assertTrue(next(c for c in res["checks"] if c["check"].startswith("GitHub login"))["ok"])

    def test_a_job_already_running_goes_on(self):
        job = self.ok("new", "--local", "a job", "--issue-file", self.issue)["job"]
        os.environ["STUB_GH"] = "logged-out"
        code, res = self.fx("status")
        self.assertEqual(code, 0, res)
        self.assertIn(job, json.dumps(res))


class LongFilesLeftTest(LongFilesTest):
    # what LongFilesTest itself tries runs there, once
    test_tidy_splits_knowledge_and_moves_logic_history = test_tidy_wants_the_files_committed_first = None
    test_a_split_file_takes_staged_parts_and_learned_rules = test_the_writer_is_told_to_split_a_long_feature = None
    test_lessons_reach_agents_without_their_notes = None

    def test_a_long_table_with_no_headings_is_cut_between_rows(self):
        kd = os.path.join(self.factory, "knowledge", "be")
        rows = "".join("| mod%03d | `src/m%03d` | what module %d does, said at some length |\n" % (i, i, i)
                       for i in range(120))
        write(os.path.join(kd, "architecture.md"), "# Module map\n\n| Module | Path | Does |\n|---|---|---|\n" + rows)
        self.ok("tidy", "--force")
        folder = os.path.join(kd, "architecture")
        parts = [n for n in os.listdir(folder) if n != "README.md"]
        self.assertGreater(len(parts), 1)
        limit = fx.doc_max_bytes(self.ctx())
        for n in parts:
            text = slurp(os.path.join(folder, n))
            self.assertLessEqual(len(text.encode()), limit, n)
            self.assertIn("| Module | Path | Does |", text)
        whole = fx.doc_text(os.path.join(kd, "architecture.md"))
        self.assertEqual(whole.count("| mod"), 120)

    def test_tidy_names_the_split_commands_and_doctor_fails_until_done(self):
        res = self.ok("tidy", "--force")
        self.assertEqual([c.split(" logic ctx --split ")[1] for c in res["logic_split"]], ["logic/billing/credit.md"])
        self.assertIn("not done yet", res["next_logic"])
        code, res = self.fx("doctor")
        row = next(c for c in res["checks"] if c["check"].startswith("knowledge and logic files under"))
        self.assertFalse(row["ok"], row)
        self.assertIn("logic/billing/credit.md", row["detail"])
