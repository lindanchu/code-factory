#!/usr/bin/env python3
"""The permission table: fx calls every route as every kind of user on the job's test stack and compares what happens
with what the table says (reads, refused writes that must leave the data alone, allowed writes that must change it,
refused ids that must look like ids that exist nowhere), tells the base branch's mistakes from the job's, and starts no
job before the user confirmed the table."""

import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import unittest.mock as mock

from test_security import fx, slurp, write
from test_parallel import BoxCase


class FakeApp(http.server.BaseHTTPRequestHandler):
    """A tiny app with two projects (1 is the viewer's, 2 someone else's), a viewer, an admin and an admin-only write.
    bugs: viewer_sees_others (C), viewer_writes_admin (writes although it answers 404), tells_it_exists (a refused id
    answers otherwise than a missing one)."""
    bugs = set()
    db = None

    def log_message(self, *a):
        pass

    def who(self):
        auth = self.headers.get("Authorization") or ""
        return auth.split(" ", 1)[1] if auth.startswith("Bearer ") else None

    def send(self, code, body="", location=None):
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        data = body.encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def load(self):
        return json.loads(slurp(self.db))

    def do_GET(self):
        who = self.who()
        if self.path == "/me":
            return self.send(200, "me") if who else self.send(302, location="/login")
        if self.path == "/api/me":
            if "api_redirects" in self.bugs and not who:
                return self.send(302, location="/login")
            return self.send(200, "{}") if who else self.send(401, "{}")
        if self.path.startswith("/media/"):
            if not who:
                return self.send(302, location="/login")
            pid, _, q = self.path[len("/media/"):].partition("?run=")
            p = self.load()["projects"].get(pid)
            if p is None or q != "r" + pid:
                return self.send(404, "No media %s" % pid)
            return self.send(200, "media") if who == "admin" or p["owner"] == who else self.send(404, "No media %s" % pid)
        if self.path.startswith("/projects/"):
            if not who:
                return self.send(302, location="/login")
            pid = self.path.rsplit("/", 1)[1]
            p = self.load()["projects"].get(pid)
            if p is None:
                return self.send(404, "Not found")
            if who == "admin" or p["owner"] == who or "viewer_sees_others" in self.bugs:
                return self.send(200, "project %s" % pid)
            return self.send(404, "Not found" if "tells_it_exists" not in self.bugs else "Not yours")
        return self.send(404, "Not found")

    def do_QUERY(self):
        return self.send(501, "Unsupported method")

    def do_POST(self):
        who = self.who()
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/admin/projects":
            if not who:
                return self.send(302, location="/login")
            if who != "admin" and "viewer_writes_admin" not in self.bugs:
                return self.send(404, "Not found")
            d = self.load()
            d["projects"][str(len(d["projects"]) + 1)] = {"owner": who}
            write(self.db, json.dumps(d))
            return self.send(404, "Not found") if who != "admin" else self.send(302, location="/admin")
        return self.send(404, "Not found")


ROUTES = json.dumps([{"method": "GET|HEAD", "uri": "me"}, {"method": "GET|HEAD", "uri": "projects/{id}"},
                     {"method": "POST", "uri": "admin/projects"}])
ROWS = {"GET /me": {"module": "auth", "cells": {"guest": "no", "viewer": "yes", "admin": "yes"}},
        "GET /projects/{id}": {"module": "projects", "cells": {"guest": "no", "viewer": "own", "admin": "yes"}},
        "POST /admin/projects": {"module": "projects", "cells": {"guest": "no", "viewer": "no", "admin": "yes"}}}


class MatrixCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="fx-perm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "db.json")
        self.start = json.dumps({"projects": {"1": {"owner": "viewer"}, "2": {"owner": "other"}}})
        write(self.db, self.start)
        handler = type("App", (FakeApp,), {"bugs": set(), "db": self.db})
        self.app = handler
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:%d/" % self.server.server_address[1]
        self.job = {"id": "T-1", "worktree": self.tmp}

        class Ctx:
            def c(self, key, default=None):
                return default
        self.ctx = Ctx()
        for p in (mock.patch.object(fx, "app_env", return_value=(dict(os.environ), "db")),
                  mock.patch.object(fx, "app_fill", side_effect=lambda cmd, job, db, strict=None: cmd)):
            p.start()
            self.addCleanup(p.stop)

    def tech(self, **more):
        t = {"routes": "echo '%s'" % ROUTES,
             "snapshot": "cp db.json db.snap", "restore": "cp db.snap db.json", "fingerprint": "cksum db.json",
             "who": {"guest": {"login": None, "deny": {"status": [302], "location": "/login"}},
                     "viewer": {"login": {"command": "echo 'Bearer viewer'", "check": "/me"},
                                "deny": {"status": [404], "same_as_missing": True}},
                     "admin": {"login": {"command": "echo 'Bearer admin'", "check": "/me"},
                               "deny": {"status": [404]}}},
             "requests": {"GET /projects/{id}": {"own": {"id": "1"}, "other": {"id": "2"}, "missing": {"id": "999"}},
                          "POST /admin/projects": {"body": {"name": "x"}}}}
        t.update(more)
        return t

    def run_matrix(self, rows=None, **more):
        return fx.perm_matrix(self.ctx, self.job, self.url, self.tech(**more), rows or ROWS)


class MatrixTest(MatrixCase):
    def test_an_app_that_does_what_the_table_says(self):
        res = self.run_matrix()
        self.assertEqual(res["mismatches"], [], res["mismatches"])
        # guest: 3 routes, the id once; viewer and admin: /me, own and other ids, the write
        self.assertEqual(len(res["cells"]), 3 + 4 + 4)
        self.assertEqual((res["unrowed"], res["stale"]), ([], []))
        self.assertEqual(slurp(self.db), self.start)  # the admin's write happened and was put back

    def test_a_viewer_who_sees_someone_elses_project(self):
        self.app.bugs.add("viewer_sees_others")
        res = self.run_matrix()
        self.assertEqual([(c["route"], c["who"], c["data"]) for c in res["mismatches"]],
                         [("GET /projects/{id}", "viewer", "other")])

    def test_a_refusal_that_tells_the_data_exists(self):
        self.app.bugs.add("tells_it_exists")
        res = self.run_matrix()
        self.assertEqual(len(res["mismatches"]), 1, res["mismatches"])
        self.assertIn("exists nowhere", res["mismatches"][0]["why"])

    def test_a_refused_write_that_still_writes(self):
        self.app.bugs.add("viewer_writes_admin")
        res = self.run_matrix()
        bad = [c for c in res["mismatches"] if c["route"] == "POST /admin/projects"]
        self.assertEqual([c["who"] for c in bad], ["viewer"], res["mismatches"])
        self.assertIn("the data changed although it was refused", bad[0]["why"])
        self.assertEqual(slurp(self.db), self.start)  # put back

    def test_routes_without_a_row_and_rows_without_a_route(self):
        rows = dict(ROWS)
        rows.pop("GET /me")
        rows["DELETE /projects/{id}"] = {"module": "projects", "cells": {"admin": "yes"}}
        res = self.run_matrix(rows)
        self.assertEqual((res["unrowed"], res["stale"]), (["GET /me"], ["DELETE /projects/{id}"]))
        res = self.run_matrix(rows, not_app={"GET /me": "a development page"})
        self.assertEqual(res["unrowed"], [])

    def test_what_the_technical_part_lacks_stops_before_any_call(self):
        t = self.tech()
        t["requests"]["POST /admin/projects"] = {}
        del t["requests"]["GET /projects/{id}"]["other"]
        with self.assertRaises(fx.PermsSetup) as e:
            fx.perm_matrix(self.ctx, self.job, self.url, t, ROWS)
        self.assertIn("body", e.exception.why)
        self.assertIn(".other: the value of id", e.exception.why)

    def test_a_user_that_cannot_log_in(self):
        t = self.tech()
        t["who"]["viewer"]["login"]["command"] = "echo 'Bearer nobody'; exit 0"
        t["who"]["viewer"]["login"]["check"] = "/nowhere"
        with self.assertRaises(fx.PermsSetup) as e:
            fx.perm_matrix(self.ctx, self.job, self.url, t, ROWS)
        self.assertIn("viewer cannot log in", e.exception.why)

    def test_everyone_logs_in_once_and_a_fingerprint_is_used_again(self):
        t = self.tech(fingerprint="echo x >> fps; cksum db.json")
        for who in ("viewer", "admin"):
            t["who"][who]["login"]["command"] = "echo %s >> logins; echo 'Bearer %s'" % (who, who)
        res = fx.perm_matrix(self.ctx, self.job, self.url, t, ROWS)
        self.assertEqual(res["mismatches"], [])
        # before the snapshot, and proved on /me after each restore instead of made again
        self.assertEqual(sorted(slurp(os.path.join(self.tmp, "logins")).split()), ["admin", "viewer"])
        # the snapshot's, one before the writes, after each of the three writes (guest, viewer, admin), after each of
        # the two restores: each write after the first starts from the fingerprint taken after the one before (9 before)
        self.assertEqual(len(slurp(os.path.join(self.tmp, "fps")).split()), 7)

    def test_a_login_lost_with_the_restore_is_made_again(self):
        t = self.tech()
        t["who"]["admin"]["login"]["command"] = "echo admin >> logins; echo 'Bearer admin'"
        rows = dict(ROWS)
        rows["POST /admin/archive"] = {"module": "projects", "cells": {"guest": "no", "viewer": "no", "admin": "yes"}}
        t["requests"]["POST /admin/archive"] = {"body": {"name": "y"}}
        routes = json.loads(ROUTES) + [{"method": "POST", "uri": "admin/archive"}]
        with mock.patch.object(fx, "perm_still_in", return_value=False):
            res = fx.perm_matrix(self.ctx, self.job, self.url, dict(t, routes="echo '%s'" % json.dumps(routes)), rows)
        self.assertEqual(len(slurp(os.path.join(self.tmp, "logins")).split()), 2, res["mismatches"])

    def test_a_restore_that_does_not_bring_the_data_back(self):
        with self.assertRaises(fx.PermsSetup) as e:
            self.run_matrix(restore="true")
        self.assertIn("does not bring the data back", e.exception.why)


class DenyByPrefixTest(MatrixCase):
    """Who is not logged in is sent to the login page by the web and answered 401 by the API: one rule per part."""
    API_ROUTES = json.dumps([{"method": "GET|HEAD", "uri": "me"}, {"method": "GET|HEAD", "uri": "api/me"}])
    API_ROWS = {"GET /me": {"module": "auth", "cells": {"guest": "no", "admin": "yes"}},
                "GET /api/me": {"module": "auth", "cells": {"guest": "no", "admin": "yes"}}}

    def guest(self, deny):
        t = self.tech(routes="echo '%s'" % self.API_ROUTES)
        t["who"] = {"guest": {"login": None, "deny": deny}, "admin": t["who"]["admin"]}
        return fx.perm_matrix(self.ctx, self.job, self.url, t, self.API_ROWS)

    RULES = [{"prefix": "/api/", "status": [401]}, {"status": [302], "location": "/login"}]

    def test_each_part_its_own_refusal(self):
        self.assertEqual(self.guest(self.RULES)["mismatches"], [])
        self.assertEqual([c["route"] for c in self.guest({"status": [302], "location": "/login"})["mismatches"]],
                         ["GET /api/me"])

    def test_an_api_route_that_redirects_is_caught(self):
        self.app.bugs.add("api_redirects")
        bad = self.guest(self.RULES)["mismatches"]
        self.assertEqual([c["route"] for c in bad], ["GET /api/me"])
        self.assertIn("a refusal is 401", bad[0]["why"])

    def test_query_is_a_read_and_an_unknown_method_a_write(self):
        rows = {"QUERY /api/v1/{path}": {"module": "api", "cells": {"guest": "no"}},
                "PURGE /cache": {"module": "api", "cells": {"guest": "no"}}}
        routes = fx.perm_routes_parse("QUERY /api/v1/{path}\nPURGE /cache\n")
        t = self.tech()
        t["requests"] = {"QUERY /api/v1/{path}": {"other": {"path": "x"}}}
        plan, lacks, _ = fx.perm_plan(t, rows, routes)
        self.assertEqual(lacks, [])
        self.assertIn("QUERY", fx.PERM_READ)
        self.assertNotIn("PURGE", fx.PERM_READ)

    def test_a_list_of_rules_is_checked_like_one(self):
        self.assertEqual(fx.perm_deny({"deny": self.RULES}, "/api/v1/x")["status"], [401])
        self.assertEqual(fx.perm_deny({"deny": self.RULES}, "/projects/1")["location"], "/login")
        self.assertEqual(fx.perm_deny({"deny": [{"prefix": "/api/", "status": [401]}]}, "/x"), {})


class ValuesTest(MatrixCase):
    MEDIA = json.dumps([{"method": "GET|HEAD", "uri": "me"}, {"method": "GET|HEAD", "uri": "media/{id}"},
                        {"method": "QUERY", "uri": "api/{path}"}])
    ROWS = {"GET /me": {"module": "m", "cells": {"viewer": "yes", "admin": "yes"}},
            "GET /media/{id}": {"module": "m", "cells": {"viewer": "own", "admin": "yes"}},
            "QUERY /api/{path}": {"module": "m", "cells": {"viewer": "no"}}}

    def run_media(self):
        t = self.tech(routes="echo '%s'" % self.MEDIA)
        t["who"] = {k: t["who"][k] for k in ("viewer", "admin")}
        t["requests"] = {"GET /media/{id}": {"own": {"id": "1", "run": "r1"}, "other": {"id": "2", "run": "r2"},
                                             "missing": {"id": "999", "run": "r999"}},
                         "QUERY /api/{path}": {"own": {"path": "x"}, "other": {"path": "y"}, "missing": {"path": "z"}}}
        return fx.perm_matrix(self.ctx, self.job, self.url, t, self.ROWS)

    def test_each_kind_of_data_with_its_own_query_and_a_refusal_that_repeats_the_id(self):
        res = self.run_media()
        self.assertEqual(res["mismatches"], [], res["mismatches"])
        media = [c for c in res["cells"] if c["route"] == "GET /media/{id}"]
        self.assertEqual(sorted((c["who"], c["data"], c["want"]) for c in media),
                         [("admin", "other", "allow"), ("admin", "own", "allow"), ("viewer", "other", "deny"),
                          ("viewer", "own", "allow")])

    def test_a_method_the_server_does_not_know_is_not_checked(self):
        res = self.run_media()
        self.assertEqual(sorted((c["route"], c["data"], c["ok"]) for c in res["unchecked"]), [("QUERY /api/{path}", "other", None), ("QUERY /api/{path}", "own", None)])
        self.assertIn("never reached the app", res["unchecked"][0]["why"])
        self.assertIn("## Not checked", fx.perm_markdown(res))

    def test_the_caller_s_values_are_left_out_of_the_comparison(self):
        self.assertEqual(fx.perm_norm_body("No feature with id 5 here", {"id": "5"}),
                         fx.perm_norm_body("No feature with id 999 here", {"id": "999"}))
        self.assertNotEqual(fx.perm_norm_body("No feature with id 5 in project vacc", {"id": "5"}),
                            fx.perm_norm_body("No feature with id 999 here", {"id": "999"}))
        self.assertEqual(fx.perm_norm_body("id 15 and 5", {"id": "5"}), "id 15 and {sent}")
        both = {"a": "2", "b": "999"}  # the gate takes out the values of both calls from both answers
        self.assertEqual(fx.perm_norm_body('<p class="p-2">No 2</p>', both), fx.perm_norm_body('<p class="p-2">No 999</p>', both))


class TablesTest(unittest.TestCase):
    README = ("# Module projects\n\n## 1. Module-wide rules\n| Rule | Code | Test |\n|---|---|---|\n\n"
              "## 2. Parameters\n| Parameter | Value | Code |\n|---|---|---|\n\n"
              "## Quyền\n| Method | Route | guest | viewer | admin | Ghi chú |\n|---|---|---|---|---|---|\n"
              "| GET | `/projects/{id}` | không | riêng | có | chỉ dự án được gán |\n"
              "| PUT, PATCH | /projects/{id}/ | no | no | yes | |\n"
              "| DELETE | /projects/{id} | no | maybe | yes | |\n\n"
              "## 3. Open points\n1. none\n")

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="fx-perm-t-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        write(os.path.join(self.tmp, "logic", "README.md"), "# Logic\n")
        write(os.path.join(self.tmp, "logic", "projects", "README.md"), self.README)

        class Ctx:
            factory = self.tmp
            scope = None

            def repo_names(self):
                return ["app"]

            def c(self, key, default=None):
                return "true" if key == "app.up" else default
        self.ctx = Ctx()
        mock.patch.object(fx, "repo_ctx", side_effect=lambda ctx, n: ctx).start()
        self.addCleanup(mock.patch.stopall)

    def test_rows_cells_and_words_of_the_team(self):
        tab = fx.perm_tables(self.ctx)
        self.assertEqual(tab["rows"]["GET /projects/{id}"]["cells"], {"guest": "no", "viewer": "own", "admin": "yes"})
        self.assertIn("PUT /projects/{id}", tab["rows"])
        self.assertIn("PATCH /projects/{id}", tab["rows"])
        self.assertEqual(tab["whos"], {"guest", "viewer", "admin"})
        self.assertEqual(len(tab["problems"]), 1, tab["problems"])
        self.assertIn("'maybe' is not yes, no or own", tab["problems"][0])

    def test_a_job_s_staged_logic_replaces_the_module(self):
        over = os.path.join(self.tmp, "job-logic")
        write(os.path.join(over, "projects", "README.md"), self.README.replace("| DELETE | /projects/{id} | no | maybe",
                                                                               "| DELETE | /projects/{id} | no | no"))
        tab = fx.perm_tables(self.ctx, over=over)
        self.assertEqual(tab["problems"], [])
        self.assertEqual(tab["rows"]["DELETE /projects/{id}"]["cells"]["viewer"], "no")

    def test_routes_from_laravel_and_from_lines(self):
        got = fx.perm_routes_parse(json.dumps([{"method": "GET|HEAD", "uri": "/", "action": "Closure"},
                                               {"method": "PUT|PATCH", "uri": "projects/{id}",
                                                "action": "App\\Http\\Controllers\\ProjectController@update"}]))
        self.assertEqual(set(got), {"GET /", "PUT /projects/{id}", "PATCH /projects/{id}"})
        self.assertEqual(fx.perm_routes_parse("GET /a\nPOST /b/{id} Ctl@x\nnot a route\n"),
                         {"GET /a": "", "POST /b/{id}": "Ctl@x"})


class GateJudgeTest(unittest.TestCase):
    """What the gate makes of the table's answers: the base branch's mistakes against the job's own."""

    def judge(self, res, base, staged_rows=None, factory_rows=None, changed=()):
        class Ctx:
            scope = None

            def job_dir(self, job_id):
                return "/nowhere"
        ctx = Ctx()
        staged_rows = staged_rows if staged_rows is not None else ROWS
        patches = [mock.patch.object(fx, "perm_needed", return_value=True),
                   mock.patch.object(fx, "perm_tech", return_value=({"who": {}}, [])),
                   mock.patch.object(fx, "perm_state", return_value={"confirmed": True}),
                   mock.patch.object(fx, "baseline_of", return_value={"permissions": base}),
                   mock.patch.object(fx, "perm_tables", side_effect=lambda rc, over=None: {
                       "rows": staged_rows if over else (factory_rows or ROWS), "problems": [], "whos": set()}),
                   mock.patch.object(fx, "perm_matrix", return_value=dict(
                       {"cells": [], "calls": 0, "seconds": 0, "unrowed": [], "stale": [], "mismatches": []}, **res)),
                   mock.patch.object(fx, "changed_files", return_value=[("M", [p]) for p in changed]),
                   mock.patch.object(fx, "perm_scope", return_value=None),  # the scope's own tests: ScopeTest
                   mock.patch.object(fx, "write_atomic")]
        for p in patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        return fx.permissions_gate(ctx, {"id": "T-1"}, {"id": "T-1"}, ctx, "http://x/", "/nowhere")

    BAD = {"route": "GET /projects/{id}", "who": "viewer", "data": "other", "want": "deny", "ok": False,
           "why": "answered 200"}
    ROUTES = {"GET /projects/{id}": "App\\Http\\Controllers\\ProjectController@show", "GET /me": "",
              "POST /admin/projects": ""}

    def test_a_mismatch_the_job_brings_goes_to_the_builder(self):
        o = self.judge({"mismatches": [self.BAD], "routes": self.ROUTES}, {"mismatches": [], "routes": self.ROUTES})
        self.assertEqual(len(o["problems"]), 1)
        self.assertIn("permissions: GET /projects/{id} as viewer", o["problems"][0])
        self.assertEqual((o["void"], o["base"]), ([], []))

    def test_one_already_on_the_base_stops_no_job_but_one_that_touches_it(self):
        base = {"mismatches": [self.BAD], "routes": self.ROUTES}
        o = self.judge({"mismatches": [self.BAD], "routes": self.ROUTES}, base)
        self.assertEqual((o["problems"], o["void"], len(o["base"])), ([], [], 1))
        self.assertIn("tell the user", o["notes"][0])
        o = self.judge({"mismatches": [self.BAD], "routes": self.ROUTES}, base,
                       changed=["app/Http/Controllers/ProjectController.php"])
        self.assertEqual(len(o["void"]), 1)
        self.assertIn("waits for the fix", o["void"][0])

    def test_a_route_the_job_adds_without_a_row_waits_for_its_rows(self):
        o = self.judge({"unrowed": ["POST /projects"], "routes": dict(self.ROUTES, **{"POST /projects": ""})},
                       {"mismatches": [], "routes": self.ROUTES, "unrowed": []})
        self.assertEqual(len(o["void"]), 1)
        self.assertIn("never the builder", o["void"][0])
        o = self.judge({"unrowed": ["GET /me"], "routes": self.ROUTES},
                       {"mismatches": [], "routes": self.ROUTES, "unrowed": ["GET /me"]})
        self.assertEqual((o["void"], len(o["base"])), ([], 1))

    def test_a_row_the_job_changes_is_a_route_it_touches(self):
        staged = dict(ROWS, **{"GET /projects/{id}": {"module": "projects",
                                                      "cells": {"guest": "no", "viewer": "yes", "admin": "yes"}}})
        o = self.judge({"mismatches": [self.BAD], "routes": self.ROUTES},
                       {"mismatches": [self.BAD], "routes": self.ROUTES}, staged_rows=staged)
        self.assertEqual(len(o["void"]), 1)

    def test_cells_the_app_limited_put_the_round_aside(self):
        cell = {"route": "GET /me", "who": "viewer", "data": "any", "want": "allow", "ok": None,
                "why": "the app answered 429 (too many calls) even after waiting"}
        o = self.judge({"unchecked": [cell], "cells": [cell], "routes": self.ROUTES},
                       {"mismatches": [], "routes": self.ROUTES})
        self.assertEqual(len(o["void"]), 1)
        self.assertIn("never judged", o["void"][0])

    def test_before_the_table_was_run_on_the_base_it_is_skipped(self):
        with mock.patch.object(fx, "perm_state", return_value={"confirmed": True}), \
                mock.patch.object(fx, "perm_needed", return_value=True), \
                mock.patch.object(fx, "perm_tech", return_value=({"who": {}}, [])), \
                mock.patch.object(fx, "baseline_of", return_value={}):
            o = fx.permissions_gate(None, {"id": "T-1"}, {}, None, "http://x/", "/nowhere")
        self.assertIn("skipped", o)


class NoJobBeforeTheTableTest(BoxCase):
    def factory_with_app(self, name):
        f = self.box.factory(name, app={"up": "true"})
        write(os.path.join(f, "logic", "README.md"), "# Logic\n")
        write(os.path.join(f, "logic", "limits.json"), "{}")
        return f

    def test_no_job_starts_before_the_table_is_confirmed_and_run_on_the_base(self):
        f = self.factory_with_app("perm0")
        code, res = self.box.fx(f, *self.box.new_args(f, "PERM-1"))
        self.assertEqual((code, res.get("next")), (3, "permissions"), res)
        self.assertIn("never worked out", res["error"])
        code, res = self.box.fx(f, "permissions", "check")
        self.assertFalse(res["ok"])
        self.assertTrue(any("stack/permissions.json does not exist" in w for w in res["words"]), res["words"])

    def test_an_app_with_no_route_of_its_own(self):
        f = self.factory_with_app("perm1")
        write(os.path.join(f, "stack", "permissions.json"), json.dumps({"none": "it only calls the shop's API"}))
        code, res = self.box.fx(f, "permissions", "mark")
        self.assertEqual(code, 0, res)
        code, res = self.box.fx(f, "permissions", "check")
        self.assertTrue(res["ok"], res)
        code, res = self.box.fx(f, *self.box.new_args(f, "PERM-2"))
        self.assertEqual(code, 0, res)

    def test_confirmed_but_never_run_on_the_base(self):
        f = self.factory_with_app("perm2")
        write(os.path.join(f, "stack", "permissions.json"), json.dumps({
            "routes": "true", "snapshot": "true", "restore": "true", "fingerprint": "true",
            "who": {"guest": {"login": None, "deny": {"status": [302]}}}}))
        code, res = self.box.fx(f, "permissions", "mark")
        self.assertEqual(code, 0, res)
        code, res = self.box.fx(f, *self.box.new_args(f, "PERM-3"))
        self.assertEqual((code, res.get("next")), (3, "permissions"), res)
        self.assertIn("fx selftest", res["error"])

    def test_the_fix_tasks_start_while_others_wait(self):
        f = self.factory_with_app("perm3")
        task = os.path.join(f, "jobs", "_permissions", "get-projects-id.md")
        write(task, "# Permissions of GET /projects/{id}\n\nA viewer sees other projects.\n")
        code, res = self.box.fx(f, "new", "--local", "Permissions of GET /projects/{id}", "--issue-file", task,
                                "--base", "main")
        self.assertEqual(code, 0, res)

    def test_the_base_branch_s_mistakes_become_fix_tasks_to_start_first(self):
        f = self.factory_with_app("perm4")
        name = json.loads(slurp(os.path.join(f, "factory.json")))["repos"][0]["name"]
        bad = {"route": "GET /projects/{id}", "who": "viewer", "data": "other", "want": "deny", "ok": False,
               "why": "answered 200"}
        write(os.path.join(f, "baseline.json"), json.dumps({"repos": {name: {"permissions": {
            "routes": {}, "mismatches": [bad], "unrowed": ["GET /me"], "stale": []}}}}))
        code, res = self.box.fx(f, "permissions", "tasks")
        self.assertEqual(code, 0, res)
        self.assertEqual([t["route"] for t in res["tasks"]], ["GET /projects/{id}"])
        self.assertIn("as viewer (other data): answered 200", slurp(res["tasks"][0]["task_file"]))
        self.assertEqual([a["route"] for a in res["ask"]], ["GET /me"])

    def test_a_merged_job_s_requests_join_the_factory_s(self):
        f = self.factory_with_app("perm5")
        write(os.path.join(f, "stack", "permissions.json"), json.dumps({"requests": {"GET /a": {}}}))
        jd = os.path.join(f, "jobs", "PERM-6")
        write(os.path.join(jd, "job.json"), json.dumps({"id": "PERM-6", "issue_key": "PERM-6", "stage": "handed-off"}))
        write(os.path.join(jd, "permissions.json"), json.dumps({"requests": {"POST /b": {"body": {"n": 1}}}}))
        code, res = self.box.fx(f, "permissions", "apply", "PERM-6")
        self.assertEqual(code, 0, res)
        got = json.loads(slurp(os.path.join(f, "stack", "permissions.json")))
        self.assertEqual(set(got["requests"]), {"GET /a", "POST /b"})
        self.assertFalse(os.path.exists(os.path.join(jd, "permissions.json")))


class ScopeTest(MatrixCase):
    """A job's round calls only the rows its change can reach (#7)."""

    ACTIONS = {"GET /me": "App\\Http\\Controllers\\MeController@show",
               "GET /projects/{id}": "App\\Http\\Controllers\\ProjectController@show",
               "POST /admin/projects": "App\\Http\\Controllers\\Admin\\ProjectAdminController@store"}

    def setUp(self):
        super().setUp()
        self.wt = os.path.join(self.tmp, "clone")
        files = {"app/Http/Controllers/MeController.php": "class MeController { config('services.mail.from'); }\n",
                 "app/Http/Controllers/ProjectController.php": "class ProjectController { InvoiceService $s; }\n",
                 "app/Http/Controllers/Admin/ProjectAdminController.php": "class ProjectAdminController {}\n",
                 "app/Services/InvoiceService.php": "class InvoiceService {}\n",
                 "app/Http/Middleware/Authenticate.php": "class Authenticate { TokenService $t; }\n",
                 "app/Services/TokenService.php": "class TokenService {}\n",
                 "config/services.php": "<?php return ['mail' => ['from' => 'a@b', 'name' => 'x'], 'sms' => 1];\n",
                 "config/app.php": "<?php return ['timezone' => 'UTC', 'locale' => 'en'];\n",
                 "tests/Feature/MeTest.php": "MeController\n", "docs/api.md": "ProjectController\n"}
        for rel, text in files.items():
            write(os.path.join(self.wt, rel), text)
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "x"]):
            subprocess.run(cmd, cwd=self.wt, check=True)
        self.base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.wt, check=True, stdout=subprocess.PIPE,
                                   text=True).stdout.strip()
        self.view = {"id": "T-1", "worktree": self.wt, "base_ref": self.base}
        self.jobfile = os.path.join(self.tmp, "permissions.json")
        for p in (mock.patch.object(fx, "perm_job_file", return_value=self.jobfile),
                  mock.patch.object(fx, "baseline_of", return_value={"permissions": {"routes": self.ACTIONS}})):
            p.start()
            self.addCleanup(p.stop)

    def scope(self, *changed, routes=None, staged=None, **conf):
        class Ctx:
            def c(self, key, default=None):
                return conf.get(key, default)
        with mock.patch.object(fx, "changed_files", return_value=[("M", [p]) for p in changed]):
            fn = fx.perm_scope(Ctx(), self.view, self.view, staged or ROWS, ROWS)
        sc = fn(routes or self.ACTIONS)
        return sc["kind"], sorted(sc["only"]) if sc["only"] is not None else None, sc["why"]

    def test_a_controller_and_what_uses_a_changed_class(self):
        self.assertEqual(self.scope("app/Http/Controllers/MeController.php")[:2], ("affected", ["GET /me"]))
        kind, only, why = self.scope("app/Services/InvoiceService.php")  # ProjectController uses it
        self.assertEqual((kind, only), ("affected", ["GET /projects/{id}"]))
        self.assertIn("1 changed file of the code, and 1 that uses them", why)

    def test_tests_and_documents_reach_no_row(self):
        kind, only, why = self.scope("tests/Feature/MeTest.php", "docs/api.md", "README.md")
        self.assertEqual((kind, only), ("affected", []))
        self.assertIn("only tests, documents and the like", why)

    def test_a_layer_every_route_goes_through_calls_the_whole_table(self):
        for path in ("app/Http/Middleware/Authenticate.php", "routes/web.php", "composer.lock",
                     "app/Providers/AuthServiceProvider.php", "config/auth.php"):
            kind, only, why = self.scope(path)
            self.assertEqual((kind, only), ("full", None), path)
            self.assertIn(path, why)
        self.assertEqual(self.scope("app/Services/InvoiceService.php", **{"permissions.scope": "full"})[0], "full")
        with mock.patch.dict(os.environ, {"FX_PERM_FULL": "1"}):
            self.assertEqual(self.scope("docs/api.md")[0], "full")
        self.assertEqual(self.scope("app/x.php", **{"permissions.full_paths": ["app/x.php"]})[0], "full")

    def commit(self, rel, text):
        write(os.path.join(self.wt, rel), text)
        for cmd in (["git", "add", "-A"], ["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "c"]):
            subprocess.run(cmd, cwd=self.wt, check=True)

    def test_a_config_file_is_followed_by_its_keys(self):
        self.commit("config/services.php", "<?php return ['mail' => ['from' => 'c@d', 'name' => 'x'], 'sms' => 1];\n")
        self.assertEqual(self.scope("config/services.php")[:2], ("affected", ["GET /me"]))  # config('services.mail.from')
        self.commit("config/app.php", "<?php return ['timezone' => 'Asia/Ho_Chi_Minh', 'locale' => 'en'];\n")
        kind, only, why = self.scope("config/app.php")  # nothing of the project reads app.timezone: the framework does
        self.assertEqual((kind, only), ("full", None))
        self.assertIn("nothing of the project reads what the job changed in config/app.php", why)

    def test_what_a_shared_layer_uses_reaches_every_route(self):
        kind, only, why = self.scope("app/Services/TokenService.php")
        self.assertEqual((kind, only), ("full", None))
        self.assertIn("reaches app/Http/Middleware/Authenticate.php", why)

    def test_code_that_looks_like_a_test_or_a_document_is_code(self):
        for path in ("app/Models/Contest.php", "app/Http/Controllers/Docs/PageController.php",
                     "app/Services/Spec/Builder.php", "public/logo.svg", "app/Latest.php"):
            self.assertFalse(fx.perm_ignored(path), path)
        for path in ("tests/Feature/MeTest.php", "app/Models/ContestTest.php", "docs/api.md", "web/src/a.spec.ts",
                     "packages/x/tests/helper.php", "e2e/login.ts", "README.md", "public/logo.png"):
            self.assertTrue(fx.perm_ignored(path), path)

    def test_an_entry_point_or_a_server_setting_calls_the_whole_table(self):
        for path in ("src/main.ts", "public/.htaccess", "next.config.js", "docker/nginx/default.conf",
                     "src/app.module.ts", "routes/web.php", "app/Providers/AppServiceProvider.php"):
            self.assertEqual(self.scope(path)[0], "full", path)

    def test_a_route_file_that_names_a_class_is_not_followed(self):
        self.commit("routes/api.php", "Route::get('me', [MeController::class, 'show']);\n"
                                      "Route::get('projects/{id}', [ProjectController::class, 'show']);\n")
        self.commit("app/Providers/AppServiceProvider.php", "bind(InvoiceService::class);\n")
        self.assertEqual(self.scope("app/Http/Controllers/MeController.php")[:2], ("affected", ["GET /me"]))
        self.assertEqual(self.scope("app/Services/InvoiceService.php")[:2], ("affected", ["GET /projects/{id}"]))

    def test_handlers_of_other_stacks(self):
        routes = {"GET /posts/{id}": "blog.views.detail", "GET /projects/{id}": "projects#show",
                  "GET /users/{id}": "UsersController.findOne", "GET /me": "auth_views.me"}
        p = mock.patch.object(fx, "baseline_of", return_value={"permissions": {"routes": routes}})  # none of them new
        p.start()
        self.addCleanup(p.stop)
        self.commit("blog/views.py", "def detail(): pass\n")
        self.commit("app/controllers/projects_controller.rb", "def show; end\n")
        self.assertEqual(self.scope("blog/views.py", routes=routes)[1], ["GET /posts/{id}"])
        self.assertEqual(self.scope("app/controllers/projects_controller.rb", routes=routes)[1], ["GET /projects/{id}"])
        kind, only, why = self.scope("app/Http/Controllers/MeController.php", routes={k: "" for k in routes})
        self.assertEqual((kind, only), ("full", None))  # no handler named: fx cannot tell
        self.assertIn("names no handler", why)

    def test_short_names_are_followed_and_a_one_letter_one_calls_the_whole_table(self):
        self.commit("app/db.php", "<?php\n")
        self.commit("app/Http/Controllers/MeController.php", "class MeController { db::get(); }\n")
        self.assertEqual(self.scope("app/db.php")[:2], ("affected", ["GET /me"]))
        self.assertEqual(self.scope("app/x.php")[0], "full")

    def test_a_service_bound_to_an_interface_is_followed_through_it(self):
        """A provider binds LoginServiceInterface to LoginService and the controller asks for the interface; a login
        service is no layer every route goes through."""
        self.commit("app/Contracts/LoginServiceInterface.php", "interface LoginServiceInterface {}\n")
        self.commit("app/Services/LoginService.php", "class LoginService implements LoginServiceInterface, ShouldQueue"
                                                     " { LoginThrottle $t; }\n")
        self.commit("app/Support/LoginThrottle.php", "class LoginThrottle {}\n")
        self.commit("app/Providers/AppServiceProvider.php", "bind(LoginServiceInterface::class, LoginService::class);\n")
        self.commit("app/Http/Controllers/MeController.php", "class MeController { LoginServiceInterface $l; }\n")
        kind, only, why = self.scope("app/Support/LoginThrottle.php")
        self.assertEqual((kind, only), ("affected", ["GET /me"]))
        self.assertEqual(fx.project_interfaces(self.wt, ["app/Services/LoginService.php"]), {"LoginServiceInterface"})

    def test_a_handler_is_its_class_not_its_namespace(self):
        self.assertEqual(fx.handler_names("Modules\\Account\\Http\\Controllers\\ProjectAdminController@store"),
                         {"ProjectAdminController", "ProjectAdmin"})
        self.assertEqual(fx.handler_names("App\\Http\\Controllers\\MeController"), {"MeController", "Me"})
        self.assertEqual(fx.handler_names("app.api.endpoints.users.read_user"), {"users"})

    def test_rows_it_changes_new_routes_and_its_own_requests(self):
        staged = dict(ROWS, **{"GET /me": {"module": "auth", "cells": {"guest": "no", "viewer": "no", "admin": "yes"}}})
        self.assertEqual(self.scope(staged=staged)[1], ["GET /me"])
        routes = dict(self.ACTIONS, **{"GET /reports": "App\\Http\\Controllers\\ReportController@index"})
        self.assertEqual(self.scope(routes=routes)[1], ["GET /reports"])  # not on the base branch
        write(self.jobfile, json.dumps({"requests": {"POST /admin/projects": {"body": {"name": "y"}}}}))
        self.assertEqual(self.scope()[1], ["POST /admin/projects"])
        write(self.jobfile, json.dumps({"who": {"auditor": {}}, "requests": {}}))
        self.assertEqual(self.scope()[0], "full")

    def test_an_app_that_limits_the_calls_is_not_a_wrong_permission(self):
        real = fx.perm_http
        tries = []

        def limited(url, method, headers, body=None, kind="form"):
            if url.endswith("/me"):
                tries.append(url)
                return {"status": 429, "location": "", "body": "slow down", "error": None}
            return real(url, method, headers, body, kind)
        with mock.patch.object(fx, "perm_http", limited), mock.patch.object(fx, "PERM_LIMITED_WAITS", (0, 0)):
            res = self.run_matrix()
        self.assertEqual(res["mismatches"], [])
        me = [c for c in res["unchecked"] if c["route"] == "GET /me"]
        self.assertEqual(len(me), 3)  # guest, viewer, admin: none of them judged
        self.assertIn("429", me[0]["why"])
        self.assertEqual(len(tries), 3 * 3)  # each called again twice after waiting

    def test_only_the_scope_is_called_and_the_rest_neither_passes_nor_fails(self):
        res = fx.perm_matrix(self.ctx, self.job, self.url, self.tech(), ROWS,
                             scope=lambda routes: {"kind": "affected", "why": "w", "only": {"GET /me"}})
        self.assertEqual({c["route"] for c in res["cells"]}, {"GET /me"})
        self.assertEqual((res["scope"]["rows"], res["scope"]["of"], res["scope"]["routes"]), (1, 3, ["GET /me"]))
        self.assertIn("Scope: the rows this job's change can reach, 1 of 3", fx.perm_markdown(res))
        res = fx.perm_matrix(self.ctx, self.job, self.url, self.tech(snapshot="false"), ROWS,
                             scope=lambda routes: {"kind": "affected", "why": "w", "only": set()})
        self.assertEqual((res["cells"], res["calls"]), ([], 0))  # nothing to call: not even the snapshot ran


class LimitsLoadedTest(unittest.TestCase):
    """limits_file_loaded reads what git has, once per process (#7)."""

    def setUp(self):
        self.f = os.path.realpath(tempfile.mkdtemp(prefix="fx-lim-"))
        self.addCleanup(shutil.rmtree, self.f, True)
        write(os.path.join(self.f, ".gitignore"), "qa/test-results/\n")
        write(os.path.join(self.f, "qa", "test-results", "trace.txt"), "FACTORY_LIMITS_ENV\n")  # output, not a script
        write(os.path.join(self.f, "stack", "up.sh"), "docker compose up\n")
        subprocess.run(["git", "init", "-q"], cwd=self.f, check=True)
        fx._LIMITS_LOADED.clear()
        f = self.f

        class Ctx:
            factory = f

            def c(self, key, default=None):
                return default
        self.ctx = Ctx()

    def test_ignored_output_is_not_read_and_the_answer_is_kept(self):
        real, reads = fx.read, []

        def read(path, *a, **k):
            reads.append(os.path.relpath(path, self.f))
            return real(path, *a, **k)
        with mock.patch.object(fx, "read", read):
            self.assertFalse(fx.limits_file_loaded(self.ctx))
            self.assertNotIn("qa/test-results/trace.txt", reads)
            n = len(reads)
            self.assertFalse(fx.limits_file_loaded(self.ctx))
            self.assertEqual(len(reads), n)  # the second command reads nothing
        write(os.path.join(self.f, "stack", "up.sh"), 'docker run --env-file "$FACTORY_LIMITS_ENV" app\n')
        self.assertTrue(fx.limits_file_loaded(self.ctx))  # a script that changed is read again


class GateMarkdownTest(unittest.TestCase):
    def test_checks_md_gets_a_permissions_part(self):
        md = fx.perm_gate_md({"": {"summary": "permissions: 12 cell(s), 0 not as the table says (3s)",
                                   "base": ["GET /me has no row in the permission table"]}, "x": None})
        self.assertEqual(md[0], "## Permissions")
        self.assertIn("see permissions.md", md[2])
        self.assertIn("already on the base branch: GET /me", md[3])

    def test_skipped_and_problems_go_where_the_gate_keeps_them(self):
        job = {"skipped_checks": []}
        got = fx.perm_gate_take(None, job, 1, "/nowhere", None, {"": {"skipped": "permissions: later"},
                                                                 "b": {"problems": ["permissions: a"]}})
        self.assertEqual((got, job["skipped_checks"]), (["permissions: a"], ["permissions: later"]))

    def test_the_csrf_page_is_asked_for_as_html_whatever_the_user_accepts(self):
        seen = []

        def fake(url, method, headers, body=None, kind="form"):
            seen.append(headers)
            return {"status": 200, "location": "", "cookies": ["s=1"], "error": None,
                    "body": '<input type="hidden" name="_token" value="t&amp;1">'}
        with mock.patch.object(fx, "perm_http", side_effect=fake):
            h, form = fx.perm_csrf("http://x/", "key", {"csrf": {"from": "/login"}},
                                   {"Accept": "application/json", "Authorization": "Bearer k"})
        self.assertEqual(seen[0]["Accept"], "text/html,application/xhtml+xml")
        self.assertEqual((form, h["Accept"], h["Cookie"]), ({"_token": "t&1"}, "application/json", "s=1"))

    def test_bodies_compare_without_tokens_and_cookies_merge(self):
        self.assertEqual(fx.perm_norm_body("<p>Not  found</p><meta content=\"%s\">" % ("a" * 40)),
                         fx.perm_norm_body("<p>Not found</p><meta content=\"%s\">" % ("b" * 40)))
        self.assertEqual(fx.perm_cookie_merge("a=1; b=2", ["b=3; Path=/; HttpOnly", "c=4"]), "a=1; b=3; c=4")


if __name__ == "__main__":
    unittest.main()
