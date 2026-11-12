"""Tests for the two ways a factory reaches Jira (standard library only). Run from the repository root:
    python3 -I -m unittest discover -s tests

mcp: Claude's own Atlassian connector does the Jira work, and fx only queues it. token: fx calls Jira's REST API itself,
with the email and API token in jira.env, in this machine's keys folder; it also does the Jira reads of /factory feature
and /factory init (fx jira children, finished and statuses). A fake Jira site on 127.0.0.1 answers, checks the
credentials of every request and keeps what was posted.
"""

import base64
import contextlib
import http.server
import io
import json
import os
import threading
import unittest
import urllib.parse
from unittest import mock

from test_jobs_store import PushCase
from test_security import FxTestCase, fx, load_json, slurp, write

EMAIL = "dev@example.com"
TOKEN = "ATATT3xFfGF0-jira-token-for-tests-0123456789"
KEY = "PROJ-1"
IMAGE = b"\x89PNG not really"


class FakeJira:
    """One issue with an image attached, its transitions, and every request with the credentials and query it came
    with. A link under /media/ is signed, like Jira Cloud's media host: it needs no token. redirects sends a path
    elsewhere. searches answers GET /rest/api/3/search/jql: each JQL with its issues, page_size issues a page at most
    (Jira may give fewer than maxResults). fail answers a path with that HTTP status."""

    def __init__(self, email=EMAIL, token=TOKEN):
        self.auth = "Basic " + base64.b64encode(("%s:%s" % (email, token)).encode()).decode()
        self.log, self.comments, self.moves = [], [], []
        self.attachment, self.redirects, self.fail = None, {}, {}
        self.searches, self.page_size = {}, None
        self.transitions = [{"id": "31", "name": "Start", "to": {"name": "In Progress"}}]
        jira = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, obj=None, raw=None, location=None):
                body = raw if raw is not None else json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def search(self, query):
                found = jira.searches.get((query.get("jql") or [""])[0])
                if found is None:
                    return self.reply(400, {"errorMessages": ["Error in the JQL Query."]})
                start = int((query.get("nextPageToken") or ["after-0"])[0].split("-")[1])
                size = min(int(query["maxResults"][0]), jira.page_size or len(found) + 1)
                body = {"issues": found[start:start + size], "isLast": start + size >= len(found)}
                if not body["isLast"]:
                    body["nextPageToken"] = "after-%d" % (start + size)
                return self.reply(200, body)

            def handle_any(self):
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n)) if n else None
                path, _, query = self.path.partition("?")
                query = urllib.parse.parse_qs(query)
                jira.log.append({"method": self.command, "path": path, "auth": self.headers.get("Authorization"),
                                 "query": {k: v[0] for k, v in query.items()}})
                if self.command == "GET" and path.startswith("/media/"):
                    return self.reply(200, raw=IMAGE)
                if self.headers.get("Authorization") != jira.auth:
                    return self.reply(401, {"errorMessages": ["The credentials are wrong."]})
                if path in jira.redirects:
                    return self.reply(302, location=jira.redirects[path])
                if path in jira.fail:
                    return self.reply(jira.fail[path], {"errorMessages": ["Something went wrong."]})
                if self.command == "GET" and path == "/rest/api/3/search/jql":
                    return self.search(query)
                issue = "/rest/api/3/issue/%s" % KEY
                if self.command == "GET" and path == issue:
                    return self.reply(200, {"key": KEY, "fields": {
                        "summary": "Reset password", "status": {"name": "To Do"}, "issuetype": {"name": "Story"},
                        "project": {"key": "PROJ", "name": "Project"}, "components": [{"name": "backend"}],
                        "labels": ["api"], "description": "Users reset their own password.", "comment": {"comments": []},
                        "attachment": [{"filename": "screen.png", "mimeType": "image/png", "size": len(IMAGE),
                                        "content": jira.attachment or jira.url + "/secure/attachment/1/screen.png"}]}})
                if self.command == "GET" and path == "/secure/attachment/1/screen.png":
                    return self.reply(200, raw=IMAGE)
                if self.command == "POST" and path == issue + "/comment":
                    jira.comments.append(data["body"])
                    return self.reply(201, {"id": "10"})
                if self.command == "GET" and path == issue + "/transitions":
                    return self.reply(200, {"transitions": jira.transitions})
                if self.command == "POST" and path == issue + "/transitions":
                    jira.moves.append(data["transition"]["id"])
                    return self.reply(204)
                return self.reply(404, {"errorMessages": ["Issue does not exist."]})

            do_GET = do_POST = handle_any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class JiraCase(FxTestCase):
    """The factory's job T-1 is about the issue PROJ-1 of the fake site, which is this factory's Jira site
    (jira.base_url). FxTestCase's environment holds JIRA_API_TOKEN."""
    cfg = {"jira": {"transitions": {"start": "In Progress", "review": "In Review"}},
           "setup": 'env > "$FX_TEST_OUT/setup.env"'}

    def setUp(self):
        super().setUp()
        self.jira = FakeJira()
        self.addCleanup(self.jira.stop)
        self.set_jira("base_url", self.jira.url)
        self.f.save_job(jira_base=self.jira.url, issue_key=KEY, issue_url="%s/browse/%s" % (self.jira.url, KEY))

    def set_jira(self, name, value):
        """jira.<name> in factory.json; None takes the key out."""
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["jira"].pop(name, None)
        if value is not None:
            cfg["jira"][name] = value
        write(path, json.dumps(cfg, indent=1))

    def mode(self, value):
        """jira.mode in factory.json; None takes the key out."""
        self.set_jira("mode", value)

    def env_path(self):
        return fx.jira_env_path(self.f.ctx())

    def put_env(self, text="JIRA_EMAIL=%s\nJIRA_API_TOKEN=%s\n" % (EMAIL, TOKEN), perms=0o600):
        path = self.env_path()
        write(path, text)
        os.chmod(path, perms)
        return path

    def note(self, text):
        path = os.path.join(self.tmp, "note.md")
        write(path, text)
        return path

    def rows(self, *argv):
        code, res = self.fx("doctor", *argv)
        self.assertNotIn(TOKEN, json.dumps(res))
        return {c["check"]: c for c in res["checks"]}, res


# --------------------------------------------------------------------------
# mcp: Claude's connector does the Jira work

class McpTest(JiraCase):
    def setUp(self):
        super().setUp()
        self.mode("mcp")

    def test_fx_only_queues_the_work_even_with_a_token(self):
        self.put_env()
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual((code, res["next"]), (0, "fetch"), res)
        code, res = self.fx("jira", "comment", "T-1", self.note("Work has started.\n"))
        self.assertEqual(code, 0, res)
        self.assertIn("queued", res["jira"])
        code, res = self.fx("jira", "transition", "T-1", "start")
        self.assertIn("queued", res["jira"])
        self.assertEqual([i["action"] for i in res["jira_todo"]], ["comment", "transition"])
        self.assertEqual(self.jira.log, [], "fx never called Jira")

    def test_a_new_job_waits_for_the_orchestrator_to_read_the_issue(self):
        self.put_env()
        code, res = self.fx("new", "%s/browse/%s" % (self.jira.url, KEY))
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "fetch")
        self.assertIn("--issue-file", res["then"])
        self.assertEqual(self.jira.log, [])

    def test_jira_env_is_never_opened(self):
        self.put_env()
        watched, opened, real_open = os.path.realpath(self.env_path()), [], open

        def watch(path, *a, **kw):
            if isinstance(path, (str, bytes, os.PathLike)) and os.path.realpath(path) == watched:
                opened.append(path)
            return real_open(path, *a, **kw)
        with mock.patch("builtins.open", watch):
            ctx = self.f.ctx()
            self.assertNotIn(TOKEN, fx.local_secret_values(ctx))
            self.assertEqual(fx.scrub_text(ctx, "the token is %s" % TOKEN)[0], "the token is %s" % TOKEN)
            self.assertEqual(fx.jira_mode(ctx), "mcp")
            for argv in (("jira", "fetch", "T-1"), ("jira", "comment", "T-1", self.note("Started.\n")),
                         ("jira", "transition", "T-1", "start"), ("new", "%s/browse/%s" % (self.jira.url, KEY)),
                         ("doctor", "--issue", "%s/browse/%s" % (self.jira.url, KEY)), ("install", "status"),
                         ("where",), ("jira", "children", KEY), ("jira", "finished", "PROJ"),
                         ("jira", "statuses", "%s/browse/%s" % (self.jira.url, KEY))):
                code, res = self.fx(*argv)
                self.assertEqual(code, 0, (argv, res))
        self.assertEqual(opened, [])
        self.assertEqual(self.jira.log, [])

    def test_doctor_has_one_jira_row_and_tries_no_issue(self):
        rows, _ = self.rows("--issue", "%s/browse/%s" % (self.jira.url, KEY))
        jira_rows = sorted(n for n in rows if n.startswith(("Jira access", "Jira read", "jira.env", "Jira mode")))
        self.assertEqual(jira_rows, ["Jira access"])
        self.assertTrue(rows["Jira access"]["ok"])
        self.assertIn("Atlassian connector does the Jira work", rows["Jira access"]["detail"])
        self.assertIn("fx cannot check it", rows["Jira access"]["detail"])
        self.assertEqual(self.jira.log, [])

    def test_doctor_says_a_left_over_token_file_is_not_used(self):
        self.assertNotIn("jira.env", self.rows()[0]["Jira access"]["detail"])
        path = self.put_env()
        detail = self.rows()[0]["Jira access"]["detail"]
        self.assertIn("fx does not use %s" % path, detail)
        self.assertIn("delete it", detail)

    def test_install_reports_the_mode_and_nothing_of_the_file(self):
        self.put_env()
        code, res = self.fx("install", "status")
        self.assertEqual(res["jira"], {"mode": "mcp", "jira_env": None})


# --------------------------------------------------------------------------
# token: fx calls Jira with jira.env

class TokenTest(JiraCase):
    def setUp(self):
        super().setUp()
        self.mode("token")

    def test_fx_calls_jira_with_the_file(self):
        self.put_env()
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual(code, 0, res)
        issue = slurp(res["issue"])
        self.assertIn("# %s: Reset password" % KEY, issue)
        with open(os.path.join(self.f.jd, "attachments", "screen.png"), "rb") as f:
            self.assertEqual(f.read(), IMAGE, "mode token downloads the images")
        code, res = self.fx("jira", "comment", "T-1", self.note("Work has started.\n"))
        self.assertEqual(code, 0, res)
        code, res = self.fx("jira", "transition", "T-1", "start")
        self.assertEqual(res["jira"], {"moved": "To Do -> In Progress"})
        self.assertNotIn("jira_todo", res)
        self.assertIn("Work has started.", json.dumps(self.jira.comments))
        self.assertEqual(self.jira.moves, ["31"])
        issue = "/rest/api/3/issue/" + KEY
        self.assertEqual([(c["method"], c["path"]) for c in self.jira.log], [
            ("GET", issue), ("GET", "/secure/attachment/1/screen.png"), ("POST", issue + "/comment"),
            ("GET", issue), ("GET", issue + "/transitions"), ("POST", issue + "/transitions")])

    def test_a_new_job_reads_the_issue_itself(self):
        self.put_env()
        code, res = self.fx("new", "%s/browse/%s" % (self.jira.url, KEY))
        self.assertEqual(code, 0, res)
        self.assertEqual((res["job"], res["jira_mode"]), (KEY, "token"))
        self.assertIn("Reset password", slurp(res["issue"]))

    def test_the_file_may_say_export_and_quote_its_values(self):
        self.put_env("# Jira, for fx\nexport JIRA_EMAIL='%s'\nexport JIRA_API_TOKEN=\"%s\"\n" % (EMAIL, TOKEN))
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual(code, 0, res)

    def test_without_the_file_fx_stops_with_where_and_what(self):
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual(code, 4, res)
        msg = res["error"]
        self.assertIn("%s is missing" % self.env_path(), msg)
        for words in ("JIRA_EMAIL=<", "JIRA_API_TOKEN=<", fx.JIRA_TOKEN_PAGE, "mkdir -p -m 700", "chmod 600",
                      "never through the conversation"):
            self.assertIn(words, msg)
        self.assertEqual(self.jira.log, [])

    def test_a_new_job_stops_before_anything_is_made(self):
        code, res = self.fx("new", "%s/browse/%s" % (self.jira.url, KEY))
        self.assertEqual(code, 4, res)
        self.assertIn("jira.env", res["error"])
        self.assertFalse(os.path.exists(os.path.join(self.f.factory, "jobs", KEY)))

    def test_an_incomplete_file_is_named_without_its_values(self):
        self.put_env("JIRA_EMAIL=%s\n" % EMAIL)
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual(code, 4, res)
        self.assertIn("has no JIRA_API_TOKEN.", res["error"])
        self.assertNotIn(EMAIL, json.dumps(res))
        self.put_env("JIRA_API_TOKEN=%s\nJIRA_EMAIL=\n" % TOKEN)
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual(code, 4, res)
        self.assertIn("has no JIRA_EMAIL.", res["error"])
        self.assertNotIn(TOKEN, json.dumps(res))
        self.assertEqual(self.jira.log, [])

    def test_a_refused_token_names_the_file_never_the_token(self):
        self.put_env("JIRA_EMAIL=%s\nJIRA_API_TOKEN=revoked-token-0123456789\n" % EMAIL)
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual(code, 4, res)
        self.assertIn("Jira refused the request (401)", res["error"])
        self.assertIn(self.env_path(), res["error"])
        self.assertNotIn("revoked-token-0123456789", json.dumps(res))

    def test_the_environment_is_never_read(self):
        with mock.patch.dict(os.environ, {"JIRA_EMAIL": EMAIL, "JIRA_API_TOKEN": TOKEN}):
            code, res = self.fx("jira", "fetch", "T-1")
            self.assertEqual(code, 4, res)
            self.assertIn("is missing", res["error"])
            self.mode(None)
            code, res = self.fx("jira", "fetch", "T-1")
            self.assertEqual(res["next"], "fetch", "without jira.env, no mode is mcp, whatever the environment says")
        self.assertEqual(self.jira.log, [])

    def test_the_token_is_put_in_no_environment(self):
        self.put_env()
        self.f.save_job(setup_done=False)
        for argv in (("jira", "fetch", "T-1"), ("setup", "T-1")):
            code, res = self.fx(*argv)
            self.assertEqual(code, 0, res)
        self.assertNotIn(TOKEN, os.environ.values())
        self.assertNotIn(TOKEN, slurp(os.path.join(self.f.out, "setup.env")))
        self.assertNotIn(EMAIL, slurp(os.path.join(self.f.out, "setup.env")))

    def test_the_token_is_redacted_from_a_posted_comment(self):
        self.put_env()
        code, res = self.fx("jira", "comment", "T-1", self.note("Ask %s: the token %s works.\n" % (EMAIL, TOKEN)))
        self.assertEqual(code, 0, res)
        self.assertEqual(res["jira"]["redacted"], ["local-env-value"])
        posted = json.dumps(self.jira.comments)
        self.assertNotIn(TOKEN, posted)
        self.assertIn(fx.REDACTED, posted)
        self.assertIn(EMAIL, posted, "an email is an address, not a secret")
        self.assertNotIn(TOKEN, json.dumps(res))

    def test_it_is_a_local_secret_and_the_email_is_not(self):
        self.put_env()
        values = fx.local_secret_values(self.f.ctx())
        self.assertIn(TOKEN, values)
        self.assertNotIn(EMAIL, values)

    def test_doctor_checks_the_file_and_reads_the_issue(self):
        path = self.put_env()
        rows, _ = self.rows("--issue", "%s/browse/%s" % (self.jira.url, KEY))
        self.assertTrue(rows["Jira access"]["ok"])
        self.assertIn(path, rows["Jira access"]["detail"])
        self.assertTrue(rows["jira.env file private"]["ok"])
        self.assertEqual(rows["Jira read %s" % KEY]["detail"], "Reset password")
        self.assertNotIn("Jira mode chosen (advice)", rows)

    def test_doctor_fails_without_the_file_and_tries_no_issue(self):
        rows, res = self.rows("--issue", "%s/browse/%s" % (self.jira.url, KEY))
        self.assertFalse(rows["Jira access"]["ok"])
        self.assertFalse(res["ok"])
        self.assertIn("JIRA_API_TOKEN=<", rows["Jira access"]["detail"])
        self.assertNotIn("jira.env file private", rows)
        self.assertFalse([n for n in rows if n.startswith("Jira read")])
        self.assertEqual(self.jira.log, [])

    def test_doctor_wants_the_file_private(self):
        path = self.put_env(perms=0o644)
        row = self.rows()[0]["jira.env file private"]
        self.assertFalse(row["ok"])
        self.assertIn("chmod 600 %s" % path, row["detail"])

    def test_install_reports_the_mode_and_whether_the_file_is_there(self):
        code, res = self.fx("install", "status")
        self.assertEqual(res["jira"], {"mode": "token", "jira_env": False})
        self.put_env()
        code, res = self.fx("install", "status")
        self.assertEqual(res["jira"], {"mode": "token", "jira_env": True})
        self.assertNotIn(TOKEN, json.dumps(res))

    def test_a_file_that_is_not_utf8_is_named_without_a_crash(self):
        path = self.env_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"JIRA_EMAIL=d\xe9v@example.com\nJIRA_API_TOKEN=" + TOKEN.encode() + b"\n")
        os.chmod(path, 0o600)
        for argv in (("jira", "fetch", "T-1"), ("new", "%s/browse/%s" % (self.jira.url, KEY))):
            code, res = self.fx(*argv)
            self.assertEqual(code, 4, (argv, res))
            self.assertIn("%s is not UTF-8 text" % path, res["error"])
            self.assertNotIn(TOKEN, json.dumps(res))
        rows, _ = self.rows("--issue", "%s/browse/%s" % (self.jira.url, KEY))
        self.assertFalse(rows["Jira access"]["ok"])
        self.assertIn("is not UTF-8 text", rows["Jira access"]["detail"])
        self.assertEqual(self.jira.log, [])

    def test_a_byte_order_mark_hides_no_line(self):
        self.put_env("﻿JIRA_EMAIL=%s\r\nJIRA_API_TOKEN=%s\r\n" % (EMAIL, TOKEN))
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual(code, 0, res)

    def test_doctor_wants_agents_kept_from_the_keys_folder(self):
        name = "agents kept from reading the keys folder (advice)"
        self.put_env()
        rows, _ = self.rows()
        self.assertIn("jira.env", rows[name]["detail"])
        write(os.path.join(self.f.factory, ".claude", "settings.local.json"),
              json.dumps({"permissions": {"deny": fx.key_deny_rules(self.f.ctx())}}))
        self.assertNotIn(name, self.rows()[0])
        os.remove(os.path.join(self.f.factory, ".claude", "settings.local.json"))
        self.mode("mcp")
        self.assertNotIn(name, self.rows()[0], "nothing of this factory is in the keys folder")


# --------------------------------------------------------------------------
# token: the token goes to this factory's Jira site and nowhere else

class TokenSiteTest(JiraCase):
    """Whatever a link, the environment, a job's files or a redirect say. Another fake site stands for anyone else."""

    def setUp(self):
        super().setUp()
        self.mode("token")
        self.put_env()
        self.other = FakeJira()
        self.addCleanup(self.other.stop)

    def link(self, site):
        return "%s/browse/%s" % (site, KEY)

    def saved_image(self):
        path = os.path.join(self.f.jd, "attachments", "screen.png")
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def test_a_link_to_another_site_gets_nothing(self):
        rows, _ = self.rows("--issue", self.link(self.other.url))
        self.assertFalse(rows["Jira read"]["ok"])
        self.assertIn("%s is not this factory's Jira site %s" % (self.other.url, self.jira.url),
                      rows["Jira read"]["detail"])
        code, res = self.fx("new", self.link(self.other.url))
        self.assertEqual(code, 4, res)
        self.assertFalse(os.path.exists(os.path.join(self.f.factory, "jobs", KEY)))
        self.assertEqual(self.other.log, [])

    def test_nor_does_a_job_that_names_another_site(self):  # a job's files can come back from S3
        self.f.save_job(jira_base=self.other.url)
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual(code, 4, res)
        code, res = self.fx("jira", "comment", "T-1", self.note("Started.\n"))
        self.assertIn("is not this factory's Jira site", res["jira"]["error"])
        self.assertEqual(self.other.log, [])

    def test_the_environment_decides_nothing(self):
        with mock.patch.dict(os.environ, {"JIRA_BASE_URL": self.other.url, "FX_JIRA_API_OVERRIDE": self.other.url}):
            for ref in (KEY, self.link(self.jira.url)):
                rows, _ = self.rows("--issue", ref)
                self.assertEqual(rows["Jira read %s" % KEY]["detail"], "Reset password", ref)
        self.assertEqual(self.other.log, [])

    def test_without_jira_base_url_the_token_goes_nowhere(self):
        self.set_jira("base_url", None)
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual(code, 4, res)
        self.assertIn("factory.json has no jira.base_url", res["error"])
        self.assertIn("/factory init", res["error"])
        self.assertFalse(self.rows()[0]["Jira access"]["ok"])
        self.assertEqual(self.jira.log, [])

    def test_only_over_https(self):
        site = "http://jira.example.invalid"
        self.set_jira("base_url", site)
        self.f.save_job(jira_base=site)
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual(code, 4, res)
        self.assertIn("is not an https address", res["error"])
        self.assertEqual(fx.url_origin("https://Team.atlassian.net:443/x"), ("https", "team.atlassian.net", 443))

    def test_an_attachment_on_another_site_is_not_fetched_with_it(self):
        self.jira.attachment = self.other.url + "/secure/attachment/1/screen.png"
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual(code, 0, res)
        self.assertIn("Reset password", slurp(res["issue"]))
        self.assertIsNone(self.saved_image())
        self.assertEqual(self.other.log, [])

    def test_a_redirect_to_another_site_goes_without_it(self):
        self.jira.attachment = self.jira.url + "/secure/attachment/2/screen.png"
        self.jira.redirects["/secure/attachment/2/screen.png"] = self.other.url + "/media/2?token=signed"
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.saved_image(), IMAGE, "the signed link needs no token")
        self.assertEqual([(c["path"], c["auth"]) for c in self.other.log], [("/media/2", None)])

    def test_a_redirect_on_the_same_site_keeps_it(self):
        self.jira.attachment = self.jira.url + "/secure/attachment/3/screen.png"
        self.jira.redirects["/secure/attachment/3/screen.png"] = self.jira.url + "/secure/attachment/1/screen.png"
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.saved_image(), IMAGE)
        self.assertEqual(self.other.log, [])


# --------------------------------------------------------------------------
# the Jira reads of /factory feature and /factory init: fx jira children, finished and statuses

FIELDS = "summary,issuetype,status,project,components,labels"
CHILDREN_JQL = 'parent = "%s"' % KEY
FINISHED_JQL = 'project = "PROJ" AND statusCategory = Done ORDER BY resolved DESC, updated DESC'
SEARCH = "/rest/api/3/search/jql"


def an_issue(key, summary, components=(), labels=(), status="To Do", kind="Story"):
    """An issue as Jira's search gives it, with the fields fx jira children asks for."""
    return {"key": key, "fields": {"summary": summary, "issuetype": {"name": kind}, "status": {"name": status},
                                   "project": {"key": key.split("-")[0]},
                                   "components": [{"name": c} for c in components], "labels": list(labels)}}


def a_finished_issue(n, description=None):
    """A finished issue of project PROJ, its description in Atlassian's document format unless given."""
    text = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Done %d." % n}]}]}
    return {"key": "PROJ-%d" % n, "fields": {"summary": "Finished %d" % n, "issuetype": {"name": "Bug"},
                                             "description": text if description is None else description}}


class JiraReadsCase(JiraCase):
    """Mode token with jira.env: the issue PROJ-1 has three child issues, and project PROJ 40 finished issues, the newest
    first (PROJ-100 down to PROJ-61)."""

    def setUp(self):
        super().setUp()
        self.mode("token")
        self.put_env()
        self.jira.searches[CHILDREN_JQL] = [
            an_issue("PROJ-10", "FE: the reset form", ["web"], ["ui"]),
            an_issue("PROJ-2", "BE: the reset endpoint", ["backend"], status="In Progress"),
            an_issue("PROJ-3", "Mobile: the reset screen", kind="Sub-task")]
        self.jira.searches[FINISHED_JQL] = [a_finished_issue(n) for n in range(100, 60, -1)]

    def calls(self):
        return [(c["method"], c["path"], c["query"]) for c in self.jira.log]

    def pages(self):
        """maxResults and nextPageToken of each search request."""
        return [(c["query"]["maxResults"], c["query"].get("nextPageToken")) for c in self.jira.log if c["path"] == SEARCH]

    def jobs_files(self):
        found = {}
        for root, _, names in os.walk(os.path.join(self.f.factory, "jobs")):
            for name in names:
                with open(os.path.join(root, name), "rb") as f:
                    found[os.path.join(root, name)] = f.read()
        return found


class ChildIssuesTest(JiraReadsCase):
    def test_the_issue_and_its_children(self):
        before = self.jobs_files()
        code, res = self.fx("jira", "children", KEY)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["issue"], {"key": KEY, "summary": "Reset password", "type": "Story", "status": "To Do",
                                        "project": "PROJ", "components": ["backend"], "labels": ["api"]})
        self.assertEqual([c["key"] for c in res["children"]], ["PROJ-2", "PROJ-3", "PROJ-10"])
        self.assertEqual(res["children"][0], {"key": "PROJ-2", "summary": "BE: the reset endpoint", "type": "Story",
                                              "status": "In Progress", "project": "PROJ", "components": ["backend"],
                                              "labels": []})
        self.assertEqual(res["children"][2]["labels"], ["ui"])
        self.assertEqual((res["jql"], res["more"]), (CHILDREN_JQL, False))
        self.assertEqual(self.calls(), [
            ("GET", "/rest/api/3/issue/" + KEY, {"fields": FIELDS}),
            ("GET", SEARCH, {"jql": CHILDREN_JQL, "fields": FIELDS, "maxResults": "100"})])
        self.assertTrue(all(c["auth"] == self.jira.auth for c in self.jira.log))
        self.assertEqual(self.jobs_files(), before, "nothing is written into the job folders")
        self.assertNotIn(TOKEN, json.dumps(res))

    def test_a_link_or_a_key_in_any_case(self):
        board = "%s/jira/software/projects/PROJ/boards/1?selectedIssue=%s" % (self.jira.url, KEY)
        for ref in ("%s/browse/%s" % (self.jira.url, KEY), board, KEY.lower()):
            code, res = self.fx("jira", "children", ref)
            self.assertEqual((code, res.get("issue_key"), len(res.get("children", []))), (0, KEY, 3), (ref, res))

    def test_no_children(self):
        self.jira.searches[CHILDREN_JQL] = []
        code, res = self.fx("jira", "children", KEY)
        self.assertEqual((code, res["children"], res["more"]), (0, [], False))
        self.assertEqual(res["issue"]["key"], KEY)

    def test_more_children_than_fx_takes(self):
        self.jira.searches[CHILDREN_JQL] = [an_issue("PROJ-%d" % n, "Part %d" % n) for n in range(2, 132)]
        self.jira.page_size = 60
        code, res = self.fx("jira", "children", KEY)
        self.assertEqual(code, 0, res)
        self.assertEqual(len(res["children"]), fx.CHILDREN_MAX)
        self.assertEqual(res["children"][-1]["key"], "PROJ-101")
        self.assertTrue(res["more"])
        self.assertIn("more than 100 child issues of %s" % KEY, res["notes"][0])
        self.assertEqual(self.pages(), [("100", None), ("40", "after-60")])

    def test_the_token_is_redacted_from_what_fx_prints(self):
        self.jira.searches[CHILDREN_JQL] = [an_issue("PROJ-2", "Call the API with %s" % TOKEN)]
        code, res = self.fx("jira", "children", KEY)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["children"][0]["summary"], "Call the API with %s" % fx.REDACTED)
        self.assertEqual(res["redacted"], ["local-env-value"])


class FinishedIssuesTest(JiraReadsCase):
    def test_the_newest_finished_issues(self):
        before = self.jobs_files()
        code, res = self.fx("jira", "finished", "PROJ")
        self.assertEqual(code, 0, res)
        self.assertEqual((res["project"], res["limit"], res["jql"], res["more"]), ("PROJ", 15, FINISHED_JQL, True))
        self.assertEqual([i["key"] for i in res["issues"]], ["PROJ-%d" % n for n in range(100, 85, -1)])
        self.assertEqual(res["issues"][0], {"key": "PROJ-100", "summary": "Finished 100", "type": "Bug",
                                            "description": "Done 100.", "cut": False})
        self.assertEqual(self.calls(), [
            ("GET", SEARCH, {"jql": FINISHED_JQL, "fields": "summary,issuetype,description", "maxResults": "15"})])
        self.assertEqual(self.jobs_files(), before, "nothing is written into the job folders")

    def test_pages_are_followed_up_to_the_limit(self):
        self.jira.page_size = 7  # Jira may give fewer than it was asked for
        code, res = self.fx("jira", "finished", "proj", "--limit", "30")
        self.assertEqual(code, 0, res)
        self.assertEqual([i["key"] for i in res["issues"]], ["PROJ-%d" % n for n in range(100, 70, -1)])
        self.assertTrue(res["more"])
        self.assertEqual(self.pages(), [("30", None), ("23", "after-7"), ("16", "after-14"), ("9", "after-21"),
                                        ("2", "after-28")])

    def test_the_last_page_ends_it(self):
        self.jira.searches[FINISHED_JQL] = [a_finished_issue(n) for n in (3, 2, 1)]
        self.jira.page_size = 2
        code, res = self.fx("jira", "finished", "PROJ")
        self.assertEqual((code, [i["key"] for i in res["issues"]], res["more"]), (0, ["PROJ-3", "PROJ-2", "PROJ-1"], False))
        self.assertEqual(self.pages(), [("15", None), ("13", "after-2")])

    def test_the_limit_is_1_to_30(self):
        for limit in ("0", "31", "-1", "100"):
            code, res = self.fx("jira", "finished", "PROJ", "--limit", limit)
            self.assertEqual(code, 3, (limit, res))
            self.assertIn("--limit takes 1 to 30", res["error"])
        self.assertEqual(self.jira.log, [])
        for limit in ("1", "30"):
            code, res = self.fx("jira", "finished", "PROJ", "--limit", limit)
            self.assertEqual((code, len(res["issues"]), self.pages()[-1][0]), (0, int(limit), limit))

    def test_descriptions_are_markdown_cut_at_2000_characters(self):
        long_one = {"type": "doc", "version": 1, "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Goal"}]},
            {"type": "bulletList", "content": [{"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Users reset it"}]}]}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "x" * 3000}]}]}
        self.jira.searches[FINISHED_JQL] = [a_finished_issue(2, long_one), a_finished_issue(1, "Short, as text.")]
        code, res = self.fx("jira", "finished", "PROJ")
        self.assertEqual(code, 0, res)
        first, second = res["issues"]
        self.assertTrue(first["description"].startswith("## Goal\n\n- Users reset it\n\nxxx"), first["description"][:40])
        self.assertEqual((len(first["description"]), first["cut"]), (fx.DESCRIPTION_MAX, True))
        self.assertEqual((second["description"], second["cut"]), ("Short, as text.", False))

    def test_the_token_is_redacted_before_the_cut(self):
        self.jira.searches[FINISHED_JQL] = [a_finished_issue(1, "y" * 1980 + " " + TOKEN)]  # the cut falls inside it
        code, res = self.fx("jira", "finished", "PROJ")
        self.assertEqual(code, 0, res)
        self.assertNotIn(TOKEN[:10], json.dumps(res), "no part of it is left by the cut")
        self.assertTrue(res["issues"][0]["description"].endswith(fx.REDACTED))
        self.assertEqual(res["redacted"], ["local-env-value"])


class StatusesTest(JiraReadsCase):
    def test_the_transitions_of_the_issue_now(self):
        self.jira.transitions = [
            {"id": "31", "name": "Start", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
            {"id": "41", "name": "Ready for review", "to": {"name": "In Review"}}]
        code, res = self.fx("jira", "statuses", "%s/browse/%s" % (self.jira.url, KEY))
        self.assertEqual(code, 0, res)
        self.assertEqual(res["issue_key"], KEY)
        self.assertEqual(res["transitions"], [{"name": "Start", "to": "In Progress"},
                                              {"name": "Ready for review", "to": "In Review"}])
        self.assertEqual(self.calls(), [("GET", "/rest/api/3/issue/%s/transitions" % KEY, {})])
        self.assertEqual(self.jira.moves, [], "reading the statuses moves nothing")


class ReadsRefuseWhatIsNotAKeyTest(JiraReadsCase):
    def test_only_a_key_reaches_jql_or_a_request(self):
        board = "%s/jira/software/projects/PROJ/boards/1?selectedIssue=PROJ-1%%20OR%%201%%3D1" % self.jira.url
        for argv in (("children", 'PROJ-1" OR project = "OTHER'), ("children", "PROJ-1 OR 1=1"), ("children", ""),
                     ("children", board), ("children", "%s/browse/nothing" % self.jira.url),
                     ("statuses", "../../myself"), ("statuses", "PROJ"), ("finished", "PROJ-1"), ("finished", "P"),
                     ("finished", 'PROJ" OR project = "OTHER'), ("finished", "1PROJ"), ("finished", "")):
            code, res = self.fx("jira", *argv)
            self.assertEqual(code, 3, (argv, res))
        self.assertEqual(self.jira.log, [])

    def test_nothing_after_the_key_and_no_limit_but_for_finished(self):
        for argv in (("children", KEY, "more"), ("statuses", KEY, "start"), ("finished", "PROJ", "PROJ"),
                     ("children", KEY, "--limit", "5"), ("statuses", KEY, "--limit", "5"),
                     ("fetch", "T-1", "--limit", "5")):
            code, res = self.fx("jira", *argv)
            self.assertEqual(code, 3, (argv, res))
        self.assertEqual(self.jira.log, [])


class ReadsWithoutJiraEnvTest(JiraCase):
    READS = (("children", KEY), ("finished", "PROJ"), ("statuses", KEY))

    def setUp(self):
        super().setUp()
        self.mode("token")

    def test_a_missing_file_says_where_and_what(self):
        for argv in self.READS:
            code, res = self.fx("jira", *argv)
            self.assertEqual(code, 4, (argv, res))
            self.assertIn("%s is missing" % self.env_path(), res["error"])
            for words in ("JIRA_EMAIL=<", "JIRA_API_TOKEN=<", fx.JIRA_TOKEN_PAGE, "never through the conversation"):
                self.assertIn(words, res["error"])
        self.assertEqual(self.jira.log, [])

    def test_an_incomplete_file_is_named_without_its_values(self):
        for text, missing, value in (("JIRA_EMAIL=%s\n" % EMAIL, "JIRA_API_TOKEN", EMAIL),
                                     ("JIRA_API_TOKEN=%s\n" % TOKEN, "JIRA_EMAIL", TOKEN)):
            self.put_env(text)
            for argv in self.READS:
                code, res = self.fx("jira", *argv)
                self.assertEqual(code, 4, (argv, res))
                self.assertIn("%s has no %s." % (self.env_path(), missing), res["error"])
                self.assertNotIn(value, json.dumps(res))
        self.assertEqual(self.jira.log, [])


class ReadsJiraErrorsTest(JiraReadsCase):
    def test_every_jira_failure_is_exit_4(self):
        self.jira.fail[SEARCH] = 500
        for argv in (("children", KEY), ("finished", "PROJ")):
            code, res = self.fx("jira", *argv)
            self.assertEqual(code, 4, (argv, res))
            self.assertIn("Jira HTTP 500", res["error"])
        self.jira.fail[SEARCH] = 200  # an answer without issues: a proxy page, or another API
        code, res = self.fx("jira", "finished", "PROJ")
        self.assertEqual(code, 4, res)
        self.assertIn("no list of issues", res["error"])
        del self.jira.fail[SEARCH]
        for argv in (("children", "PROJ-404"), ("statuses", "PROJ-404")):
            code, res = self.fx("jira", *argv)
            self.assertEqual(code, 4, (argv, res))
            self.assertIn("Jira: not found", res["error"])
        del self.jira.searches[FINISHED_JQL]
        code, res = self.fx("jira", "finished", "PROJ")
        self.assertEqual(code, 4, res)
        self.assertIn("Jira HTTP 400", res["error"])

    def test_a_refused_token_names_the_file_never_the_token(self):
        self.put_env("JIRA_EMAIL=%s\nJIRA_API_TOKEN=revoked-token-0123456789\n" % EMAIL)
        for argv in (("children", KEY), ("finished", "PROJ"), ("statuses", KEY)):
            code, res = self.fx("jira", *argv)
            self.assertEqual(code, 4, (argv, res))
            self.assertIn("Jira refused the request (401)", res["error"])
            self.assertIn(self.env_path(), res["error"])
            self.assertNotIn("revoked-token-0123456789", json.dumps(res))


class ReadsSiteTest(JiraReadsCase):
    """The token goes to jira.base_url only, whatever a link, the last job or the environment say."""

    def setUp(self):
        super().setUp()
        self.other = FakeJira()
        self.addCleanup(self.other.stop)
        self.other.searches = dict(self.jira.searches)

    def test_a_link_to_another_site_gets_nothing(self):
        for action in ("children", "statuses"):
            code, res = self.fx("jira", action, "%s/browse/%s" % (self.other.url, KEY))
            self.assertEqual(code, 4, (action, res))
            self.assertIn("%s is not this factory's Jira site %s" % (self.other.url, self.jira.url), res["error"])
        self.assertEqual(self.other.log + self.jira.log, [])

    def test_a_bare_key_goes_to_jira_base_url(self):
        self.f.save_job(jira_base=self.other.url)  # the last job's site
        with mock.patch.dict(os.environ, {"JIRA_BASE_URL": self.other.url}):
            for argv in (("children", KEY), ("finished", "PROJ"), ("statuses", KEY)):
                code, res = self.fx("jira", *argv)
                self.assertEqual(code, 0, (argv, res))
        self.assertEqual(self.other.log, [])
        self.assertEqual(len(self.jira.log), 4)
        self.assertTrue(all(c["auth"] == self.jira.auth for c in self.jira.log))

    def test_without_jira_base_url_nothing_is_sent(self):
        self.set_jira("base_url", None)
        for argv in (("children", KEY), ("finished", "PROJ"), ("statuses", KEY),
                     ("children", "%s/browse/%s" % (self.jira.url, KEY))):
            code, res = self.fx("jira", *argv)
            self.assertEqual(code, 4, (argv, res))
            self.assertIn("factory.json has no jira.base_url", res["error"])
        self.assertEqual(self.jira.log + self.other.log, [])


class ReadsInModeMcpTest(JiraCase):
    """Claude's Atlassian connector reads: fx says what to read, and calls no Jira and opens no jira.env."""

    def setUp(self):
        super().setUp()
        self.mode("mcp")
        self.put_env()

    def test_fx_says_what_to_read(self):
        code, children = self.fx("jira", "children", KEY)
        self.assertEqual(code, 0, children)
        code, finished = self.fx("jira", "finished", "proj", "--limit", "20")
        self.assertEqual(code, 0, finished)
        code, statuses = self.fx("jira", "statuses", "%s/browse/%s" % (self.jira.url, KEY))
        self.assertEqual(code, 0, statuses)
        for res in (children, finished, statuses):
            self.assertEqual((res["ok"], res["next"]), (True, "use your Atlassian tools"))
            self.assertEqual(res["site"], fx.site_of(self.jira.url))
            for words in ("issue", "children", "issues", "transitions", "redacted"):
                self.assertNotIn(words, res)
        self.assertEqual((children["issue_key"], children["jql"]), (KEY, CHILDREN_JQL))
        self.assertIn("the issue %s and its child issues" % KEY, children["read"])
        self.assertIn("status, project, components and labels", children["read"])
        self.assertEqual((finished["project"], finished["limit"], finished["jql"]), ("PROJ", 20, FINISHED_JQL))
        self.assertIn("the 20 most recently finished issues of project PROJ", finished["read"])
        self.assertIn("description", finished["read"])
        self.assertEqual(statuses["issue_key"], KEY)
        self.assertIn("the transitions of %s now" % KEY, statuses["read"])
        self.assertEqual(self.jira.log, [])

    def test_no_site_known_is_no_problem(self):
        self.set_jira("base_url", None)
        self.f.save_job(jira_base=None)
        for argv in (("children", KEY), ("finished", "PROJ"), ("statuses", KEY)):
            code, res = self.fx("jira", *argv)
            self.assertEqual((code, res["next"]), (0, "use your Atlassian tools"), (argv, res))
            self.assertNotIn("site", res)

    def test_what_is_not_a_key_is_refused_here_too(self):
        for argv in (("children", "PROJ-1 OR 1=1"), ("finished", "PROJ-1"), ("statuses", "x"),
                     ("finished", "PROJ", "--limit", "31")):
            code, res = self.fx("jira", *argv)
            self.assertEqual(code, 3, (argv, res))


class JiraHelpTest(unittest.TestCase):
    def test_the_help_says_what_each_action_takes(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            fx.build_parser().parse_args(["jira", "--help"])
        text = " ".join(buf.getvalue().split())
        for words in ("{fetch,comment,transition,children,finished,statuses}", "fetch, comment and transition: the job",
                      "children and statuses: an issue key or link",
                      "finished: the Jira project key, the part before the dash in its issue keys",
                      "(default 15, at most 30)"):
            self.assertIn(words, text)


# --------------------------------------------------------------------------
# what an older factory.json says

class OlderValuesTest(JiraCase):
    def test_each_value(self):
        full = "JIRA_EMAIL=%s\nJIRA_API_TOKEN=%s\n" % (EMAIL, TOKEN)
        for value, env, want in (("agent", full, "mcp"), ("api", None, "token"), ("auto", full, "token"),
                                 ("auto", None, "mcp"), (None, full, "token"), (None, None, "mcp"), ("", full, "token"),
                                 ("auto", "JIRA_API_TOKEN=%s\n" % TOKEN, "mcp"), ("tokens", full, "token"),
                                 ("tokens", None, "mcp"), (" MCP ", full, "mcp"), ("Token", None, "token")):
            self.mode(value)
            if env is None:
                if os.path.exists(self.env_path()):
                    os.remove(self.env_path())
            else:
                self.put_env(env)
            self.assertEqual(fx.jira_mode(self.f.ctx()), want, (value, env))

    def test_agent_queues_and_api_calls_jira(self):
        self.put_env()
        self.mode("agent")
        self.assertEqual(self.fx("jira", "fetch", "T-1")[1]["next"], "fetch")
        self.assertEqual(self.jira.log, [])
        self.mode("api")
        code, res = self.fx("jira", "fetch", "T-1")
        self.assertEqual(code, 0, res)
        self.assertTrue(self.jira.log)

    def test_doctor_advises_choosing(self):
        for value, words in (("agent", "an older version's name for mcp"), ("api", "an older version's name for token"),
                             (None, "not chosen"), ("auto", "not chosen"), ("tokens", "neither mcp nor token")):
            self.mode(value)
            row = self.rows()[0]["Jira mode chosen (advice)"]
            self.assertTrue(row["ok"])
            self.assertIn(words, row["detail"])
            self.assertIn("choose mcp or token with /factory init", row["detail"])
        for value in ("mcp", "token"):
            self.mode(value)
            self.assertNotIn("Jira mode chosen (advice)", self.rows()[0])

    def test_install_without_a_mode(self):
        self.mode(None)
        self.assertEqual(self.fx("install", "status")[1]["jira"], {"mode": "mcp", "jira_env": False})
        self.put_env()
        self.assertEqual(self.fx("install", "status")[1]["jira"], {"mode": "token", "jira_env": True})
        self.assertEqual(fx.jira_machine_status(None), {"mode": None, "jira_env": None})


# --------------------------------------------------------------------------
# JIRA_EMAIL and JIRA_API_TOKEN in the environment

class EnvironmentWarningTest(JiraCase):
    def row(self):
        return self.rows()[0].get("Jira keys out of the environment (advice)")

    def test_doctor_warns_and_never_prints_the_value(self):
        row = self.row()  # FxTestCase's environment holds JIRA_API_TOKEN
        self.assertTrue(row["ok"])
        self.assertIn("JIRA_API_TOKEN is set in this environment", row["detail"])
        self.assertIn("code-factory does not read it there", row["detail"])
        self.assertIn("any agent's shell can", row["detail"])
        self.assertNotIn(os.environ["JIRA_API_TOKEN"], json.dumps(self.fx("doctor")[1]))

    def test_both(self):
        with mock.patch.dict(os.environ, {"JIRA_EMAIL": EMAIL}):
            row = self.row()
        self.assertIn("JIRA_EMAIL and JIRA_API_TOKEN are set", row["detail"])
        self.assertNotIn(EMAIL, row["detail"])

    def test_no_row_when_neither_is_set(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("JIRA_API_TOKEN", None)
            os.environ.pop("JIRA_EMAIL", None)
            self.assertIsNone(self.row())


# --------------------------------------------------------------------------
# the jobs store keeps a file with the token on this machine

class TokenInTheJobsStoreTest(PushCase):
    def setUp(self):
        super().setUp()
        path = fx.jira_env_path(self.f.ctx())
        write(path, "JIRA_EMAIL=%s\nJIRA_API_TOKEN=%s\n" % (EMAIL, TOKEN))
        os.chmod(path, 0o600)
        self.write_job("T-1", {"note.md": "the token is %s\n" % TOKEN, "people.md": "ask %s\n" % EMAIL})

    def test_the_file_stays_here(self):
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        snap = self.snapshot()
        self.assertIn({"path": "note.md", "reason": "secret: local-secret"}, snap["skipped"])
        self.assertIn("people.md", [f["path"] for f in snap["files"]], "an email is not a secret")
        self.assertFalse([k for k, d in self.s3.objects.items() if TOKEN.encode() in d])
        self.assertNotIn(TOKEN, json.dumps(res))


if __name__ == "__main__":
    unittest.main()
