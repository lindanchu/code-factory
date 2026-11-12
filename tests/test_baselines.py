"""Tests for the baselines on S3 (standard library only). Run from the repository root:
    python3 -I -m unittest discover -s tests

A fake S3 server checks every request's AWS signature against its own implementation, keeps a read and a write key
apart, lists objects a page at a time, and can fail, or serve bytes that do not match what was asked for. Each test has a factory repository with its
own bare remote, so "a commit landed on the remote" is real git.
"""

import base64
import hashlib
import hmac
import html
import http.server
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import unicodedata
import unittest
import urllib.parse
from unittest import mock

from test_security import FX_PATH, FxTestCase, fx, load_json, sh, slurp, write

READ_ID, READ_SECRET = "READKEYIDFORTEST0001", "read-secret-value-for-tests-0001"
WRITE_ID, WRITE_SECRET = "WRITEKEYIDFORTEST001", "write-secret-value-for-tests-001"
DIR, MANIFEST = "qa/baselines", "qa/baselines.lock.json"
BUCKET, PREFIX = "test-bucket", "team/proj"
A, B, C = b"png-a", b"png-b-longer", b"png-c-longest-of-all"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def entries(images):
    return {p: {"sha256": sha(d), "size": len(d)} for p, d in images.items()}


class FakeS3:
    def __init__(self):
        self.objects, self.log = {}, []
        self.keys = {READ_ID: (READ_SECRET, "read"), WRITE_ID: (WRITE_SECRET, "write")}
        self.fail_status, self.corrupt, self.truncate, self.echo_key = None, False, False, False
        self.page_size, self.fail_puts_after = 1000, None
        s3 = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body=b"", length=None):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body) if length is None else length))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def error_body(self, code):
                detail = "<AWSAccessKeyId>%s</AWSAccessKeyId>" % self.key_id if s3.echo_key else ""
                return ("<Error><Code>%s</Code>%s</Error>" % (code, detail)).encode()

            def check_signature(self):
                """The request's signature, recomputed here without any of fx's code."""
                m = re.match(r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request,"
                             r"SignedHeaders=([^,]+),Signature=([0-9a-f]{64})$", self.headers.get("Authorization", ""))
                self.key_id = m.group(1) if m else "?"
                if not m or m.group(1) not in s3.keys:
                    return False
                date, region, signed, got = m.group(2), m.group(3), m.group(4), m.group(5)
                names = signed.split(";")
                if not {"host", "x-amz-date", "x-amz-content-sha256"} <= set(names):
                    return False
                canon = "".join("%s:%s\n" % (n, (self.headers.get(n) or "").strip()) for n in names)
                body = self.headers.get("x-amz-content-sha256")
                path, _, raw = self.path.partition("?")
                query = "&".join("%s=%s" % (urllib.parse.quote(k, safe="-_.~"), urllib.parse.quote(v, safe="-_.~"))
                                 for k, v in sorted(urllib.parse.parse_qsl(raw, keep_blank_values=True)))
                creq = "\n".join([self.command, path, query, canon, signed, body])
                scope = "%s/%s/s3/aws4_request" % (date, region)
                sts = "\n".join(["AWS4-HMAC-SHA256", self.headers.get("x-amz-date"), scope,
                                 hashlib.sha256(creq.encode()).hexdigest()])
                k = ("AWS4" + s3.keys[m.group(1)][0]).encode()
                for part in (date, region, "s3", "aws4_request"):
                    k = hmac.new(k, part.encode(), hashlib.sha256).digest()
                return hmac.new(k, sts.encode(), hashlib.sha256).hexdigest() == got

            def list_objects(self, q):
                """ListObjectsV2: names in order, a page at a time, with a token that needs encoding in the next query."""
                names = sorted(k for k in s3.objects if k.startswith(q.get("prefix", "")))
                if "continuation-token" in q:
                    after = base64.b64decode(q["continuation-token"])[4:].decode()
                    names = [k for k in names if k > after]
                size = min(s3.page_size, int(q.get("max-keys", 1000)))
                page, more = names[:size], len(names) > size
                rows = "".join("<Contents><Key>%s</Key><Size>%d</Size><ETag>&quot;x&quot;</ETag></Contents>"
                               % (html.escape(k), len(s3.objects[k])) for k in page)
                token = "<NextContinuationToken>%s</NextContinuationToken>" % base64.b64encode(
                    b"?>?/" + page[-1].encode()).decode() if more else ""
                self.reply(200, ('<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="http://s3.amazonaws.com/'
                                 'doc/2006-03-01/"><IsTruncated>%s</IsTruncated>%s%s</ListBucketResult>'
                                 % ("true" if more else "false", token, rows)).encode())

            def handle_any(self):
                path, _, raw = self.path.partition("?")
                q = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
                key = urllib.parse.unquote(path).split("/", 2)[-1]
                self.key_id = "?"
                signed = self.check_signature()
                listing = self.command == "GET" and q.get("list-type") == "2"
                s3.log.append({"method": "LIST" if listing else self.command, "key": q.get("prefix", "") if listing else key,
                               "id": self.key_id})
                data = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.command == "PUT" else b""
                if not signed:
                    return self.reply(403, self.error_body("SignatureDoesNotMatch"))
                if s3.fail_status:
                    return self.reply(s3.fail_status, self.error_body("InternalError"))
                kind = s3.keys[self.key_id][1]
                if listing:
                    if kind == "jobs-write":
                        return self.reply(403, self.error_body("AccessDenied"))
                    return self.list_objects(q)
                if self.command == "PUT":
                    if kind not in ("write", "jobs-write") or (kind == "jobs-write" and "/jobs/" not in "/" + key):
                        return self.reply(403, self.error_body("AccessDenied"))
                    if s3.fail_puts_after is not None:
                        if s3.fail_puts_after <= 0:
                            return self.reply(503, self.error_body("SlowDown"))
                        s3.fail_puts_after -= 1
                    if self.headers.get("x-amz-content-sha256") != sha(data):
                        return self.reply(400, self.error_body("XAmzContentSHA256Mismatch"))
                    if key in s3.objects and self.headers.get("If-None-Match") == "*":
                        return self.reply(412, self.error_body("PreconditionFailed"))
                    s3.objects[key] = data
                    return self.reply(200)
                if kind == "jobs-write":
                    return self.reply(403, self.error_body("AccessDenied"))  # s3:PutObject on <prefix>/jobs/* and no more
                if key not in s3.objects:
                    return self.reply(404, self.error_body("NoSuchKey"))
                body = s3.objects[key]
                if self.command == "HEAD":
                    return self.reply(200, length=len(body))
                if s3.corrupt:
                    body = bytes(reversed(body))  # same size, other bytes
                if s3.truncate:
                    body = body[:-1]
                self.reply(200, body)

            do_GET = do_HEAD = do_PUT = handle_any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.thread:
            self.server.shutdown()
            self.server.server_close()
            self.thread = None

    def key_of(self, data):
        h = sha(data)
        return "%s/baselines/sha256/%s/%s" % (PREFIX, h[:2], h)

    def put(self, data):
        self.objects[self.key_of(data)] = data

    def calls(self, method):
        return [c for c in self.log if c["method"] == method]


CHECKS = '''env > "$FX_TEST_OUT/checks.env"
echo "checks ran" >> "$FX_TEST_OUT/checks.ran"
if [ -d "$FACTORY_BASELINES" ]; then
  (cd "$FACTORY_BASELINES" && find . -type f | sort | while read -r f; do printf '%s ' "$f"; cat "$f"; echo; done) > "$FX_TEST_OUT/seen.txt"
fi
exit 0
'''


class BaselinesCase(FxTestCase):
    repo = None
    extra_store = {}
    images = {"home.png": A, "sub/menu.png": B}

    def setUp(self):
        super().setUp()
        self.s3 = FakeS3()
        self.addCleanup(self.s3.stop)
        self.gitignore = "jobs/\nlocal/\nbaseline.json\n.claude/settings.local.json\n%s/\n" % DIR
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"] = {"required": [], "scanners": {"zap": False, "headers": False}}
        cfg["storage"] = dict({"type": "s3", "bucket": BUCKET, "region": "eu-west-1",
                               "endpoint": "http://127.0.0.1:%d" % self.s3.port, "prefix": PREFIX}, **self.extra_store)
        cfg["baselines"] = {"dir": DIR, "manifest": MANIFEST}
        if self.repo:
            cfg["baselines"]["repo"] = self.repo
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg, indent=1))
        write(os.path.join(self.f.factory, "checks.sh"), CHECKS)
        write(self.key_path("read"),
              "AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\n" % (READ_ID, READ_SECRET))
        write(self.key_path("write"),
              "export AWS_ACCESS_KEY_ID=%s\nexport AWS_SECRET_ACCESS_KEY='%s'\n" % (WRITE_ID, WRITE_SECRET))
        for mode in ("read", "write"):
            os.chmod(self.key_path(mode), 0o600)
        write(os.path.join(self.f.factory, ".gitignore"), self.gitignore)
        self.folder = os.path.join(self.f.factory, DIR)
        self.list_path = os.path.join(self.f.factory, MANIFEST)
        self.f.commit({"app.py": "x = 1\n"})
        self.prepare()

    def prepare(self):
        """A factory repository with a list in git, S3 with its images, and an empty folder: a new machine."""
        for data in self.images.values():
            self.s3.put(data)
        write(self.list_path, fx.baselines_text(entries(self.images)))
        fac = self.f.factory
        sh(["git", "init", "-q", "-b", "main"], fac)
        sh(["git", "add", "-A"], fac)
        sh(["git", "commit", "-q", "-m", "factory"], fac)
        self.remote = os.path.join(self.tmp, "factory-origin.git")
        sh(["git", "clone", "-q", "--bare", fac, self.remote], self.tmp)
        sh(["git", "remote", "add", "origin", self.remote], fac)
        sh(["git", "fetch", "-q", "origin"], fac)
        sh(["git", "branch", "-q", "--set-upstream-to=origin/main", "main"], fac)
        self.other = os.path.join(self.tmp, "teammate")
        sh(["git", "clone", "-q", self.remote, self.other], self.tmp)

    def teammate_changes(self, images, extra=None):
        """Another machine uploads new images, commits the list and pushes."""
        for data in images.values():
            self.s3.put(data)
        write(os.path.join(self.other, MANIFEST), fx.baselines_text(entries(images)))
        for rel, text in (extra or {}).items():
            write(os.path.join(self.other, rel), text)
        sh(["git", "add", "-A"], self.other)
        sh(["git", "commit", "-q", "-m", "teammate: new baselines"], self.other)
        sh(["git", "push", "-q", "origin", "main"], self.other)

    def key_path(self, mode):
        return os.path.join(fx.baselines_key_dir(self.f.ctx()), "s3-%s.env" % mode)

    def on_disk(self, rel="home.png"):
        with open(os.path.join(self.folder, rel), "rb") as f:
            return f.read()

    def fill(self):
        for rel, data in self.images.items():
            p = os.path.join(self.folder, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)

    def put_file(self, rel, data):
        p = os.path.join(self.folder, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def ran_checks(self):
        return os.path.exists(os.path.join(self.f.out, "checks.ran"))

    def bl(self, *argv):
        return self.fx("baselines", *argv)

    def cached(self, data):
        cfg = fx.baselines_config(self.f.ctx())
        return os.path.exists(fx.baselines_cache_path(cfg, sha(data)))

    def check(self, res, name):
        return next(c for c in res["checks"] if c["check"] == name)


# --------------------------------------------------------------------------
# 1. a new machine, an empty folder

class NewMachineTest(BaselinesCase):
    def test_sync_fetches_everything(self):
        code, res = self.bl("sync")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["fetched"], 2)
        self.assertEqual(self.on_disk(), A)
        self.assertEqual(self.on_disk("sub/menu.png"), B)
        self.assertTrue(all(c["id"] == READ_ID for c in self.s3.log), "sync only uses the read key")

    def test_gate_fetches_and_runs(self):
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(res["baselines"]["fetched"], 2)
        self.assertIn("./home.png png-a", slurp(os.path.join(self.f.out, "seen.txt")))

    def test_only_what_is_missing_is_fetched(self):
        self.put_file("home.png", A)
        code, res = self.bl("sync")
        self.assertEqual(res["fetched"], 1)
        self.assertEqual([c["key"] for c in self.s3.calls("GET")], [self.s3.key_of(B)])
        self.s3.log.clear()
        code, res = self.bl("sync")
        self.assertEqual(res["fetched"], 0)
        self.assertEqual(self.s3.log, [])

    def test_the_local_cache_saves_a_second_download(self):
        self.bl("sync")
        shutil.rmtree(self.folder)
        self.s3.log.clear()
        code, res = self.bl("sync")
        self.assertEqual(res["fetched"], 2)
        self.assertEqual(self.s3.calls("GET"), [])
        self.assertEqual(self.on_disk(), A)

    def test_object_names(self):
        self.assertEqual(sorted(self.s3.objects), sorted(self.s3.key_of(d) for d in self.images.values()))
        self.assertTrue(all(re.match(r"^team/proj/baselines/sha256/[0-9a-f]{2}/[0-9a-f]{64}$", k) for k in self.s3.objects))

    def test_status_lists_what_differs(self):
        self.put_file("home.png", b"edited")
        self.put_file("new.png", b"new")
        code, res = self.bl("status")
        self.assertEqual(code, 0, res)
        self.assertFalse(res["ok"])
        self.assertEqual(res["missing"], ["sub/menu.png"])
        self.assertEqual(res["wrong"], ["home.png"])
        self.assertEqual(res["unrecorded"], ["new.png"])
        self.assertEqual(res["remote"]["behind"], 0)
        self.assertEqual(self.s3.log, [], "status does not talk to S3")

    def test_parallel_sessions_do_not_collide(self):
        env = self.f.env()
        cmd = [sys.executable, "-I", FX_PATH, "baselines", "sync"]
        procs = [subprocess.Popen(cmd, env=env, cwd=self.f.factory, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                 for _ in range(4)]
        for p in procs:
            out_, _ = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, out_)
        self.assertEqual(self.on_disk(), A)
        self.assertEqual(self.on_disk("sub/menu.png"), B)
        names = [n for _, _, fs in os.walk(self.folder) for n in fs]
        self.assertEqual(sorted(names), ["home.png", "menu.png"], "no temporary file left behind")


# --------------------------------------------------------------------------
# 2. all images here, no network

class OfflineTest(BaselinesCase):
    def test_runs_with_a_warning_and_no_download(self):
        self.fill()
        self.assertEqual(self.gate()[1]["gate"], "PASS", self.checks_md())  # once S3 has confirmed the images
        self.s3.stop()
        os.rename(self.remote, self.remote + ".gone")
        self.s3.log.clear()
        self.f.save_job(stage="build")
        code, res = self.gate()
        self.assertEqual(res.get("gate"), "PASS", res)
        self.assertTrue(self.ran_checks())
        self.assertEqual(self.s3.calls("GET"), [])
        self.assertTrue(any("could not check the factory repository" in n for n in res["notes"]), res["notes"])
        self.assertIn("could not check the factory repository", self.checks_md(2))

    def test_images_s3_never_confirmed_cannot_pass_without_it(self):
        self.fill()
        self.s3.stop()
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("could not check that S3", self.checks_md())
        self.assertFalse(self.ran_checks())

    def test_sync_warns_too(self):
        self.fill()
        self.s3.stop()
        os.rename(self.remote, self.remote + ".gone")
        code, res = self.bl("sync")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["fetched"], 0)
        self.assertTrue(any("could not check" in w for w in res["warnings"]), res)

    def test_a_dead_network_costs_one_failure_not_one_per_image(self):
        lazy = mock.MagicMock()
        lazy.down = None
        cfg = fx.baselines_config(self.f.ctx())
        real = fx._LazyStore(self.f.ctx(), cfg)
        self.s3.stop()
        with self.assertRaises(fx.StoreError):
            fx.baselines_fetch(cfg, real, sha(A), len(A))
        self.assertIsNotNone(real.down)
        with self.assertRaises(fx.StoreError) as again:
            real.get()
        self.assertEqual(again.exception.kind, "network")


# --------------------------------------------------------------------------
# 3. images missing and the store failing

class StoreFailureTest(BaselinesCase):
    def test_gate_fails_naming_the_images_and_why(self):
        self.s3.fail_status = 500
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        md = self.checks_md()
        self.assertIn("home.png", md)
        self.assertIn("sub/menu.png", md)
        self.assertIn("HTTP 500 (InternalError)", md)
        self.assertFalse(self.ran_checks(), "checks.sh never runs without its images")

    def test_store_down_fails_the_gate(self):
        self.s3.stop()
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("cannot reach the store", self.checks_md())
        self.assertFalse(self.ran_checks())

    def test_no_read_key_fails_the_gate(self):
        os.remove(self.key_path("read"))
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("no read key", self.checks_md())
        self.assertFalse(self.ran_checks())

    def test_wrong_key_says_so(self):
        write(self.key_path("read"),
              "AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=not-the-secret\n" % READ_ID)
        code, res = self.bl("sync")
        self.assertEqual(code, 3)
        self.assertIn("HTTP 403 (SignatureDoesNotMatch)", res["error"])

    def test_sync_exits_3(self):
        self.s3.fail_status = 503
        code, res = self.bl("sync")
        self.assertEqual(code, 3)
        self.assertEqual(sorted(f["path"] for f in res["failed"]), ["home.png", "sub/menu.png"])

    def test_selftest_stops_before_the_checks(self):
        self.s3.fail_status = 500
        code, res = self.fx("selftest", "--no-app")
        self.assertEqual(code, 0, res)
        self.assertFalse(res["ok"])
        self.assertFalse(res["steps"]["baselines"]["ok"])
        self.assertIn("HTTP 500", res["steps"]["baselines"]["error"])
        self.assertNotIn("checks", res["steps"])
        self.assertFalse(self.ran_checks())


class SelftestTest(BaselinesCase):
    def test_selftest_fetches_then_checks(self):
        code, res = self.fx("selftest", "--no-app")
        self.assertEqual(code, 0, res)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["steps"]["baselines"]["fetched"], 2)
        self.assertIn("./home.png png-a", slurp(os.path.join(self.f.out, "seen.txt")))


# --------------------------------------------------------------------------
# 4. an edited image

class EditedImageTest(BaselinesCase):
    def test_sync_detects_by_hash_restores_and_keeps_the_edit(self):
        self.fill()
        self.put_file("home.png", b"edit")
        code, res = self.bl("sync")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.on_disk(), A)
        self.assertEqual(res["unrecorded"], ["home.png"])
        self.assertEqual(slurp(os.path.join(res["kept_in"], "home.png")), "edit")

    def test_same_size_edit_is_still_caught(self):
        self.fill()
        self.put_file("home.png", b"PNG-A")
        self.assertEqual(len(b"PNG-A"), len(A))
        code, res = self.bl("sync")
        self.assertEqual(self.on_disk(), A)

    def test_gate_keeps_the_edit_in_the_job_restores_and_reports(self):
        self.fill()
        self.put_file("home.png", b"builder hides a bug")
        self.put_file("sneaky.png", b"not on the list")
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(self.on_disk(), A)
        self.assertFalse(os.path.exists(os.path.join(self.folder, "sneaky.png")))
        kept = os.path.join(self.f.jd, "round-1", "baselines", "unrecorded")
        self.assertEqual(slurp(os.path.join(kept, "home.png")), "builder hides a bug")
        self.assertEqual(slurp(os.path.join(kept, "sneaky.png")), "not on the list")
        self.assertEqual(res["baselines"]["unrecorded"], ["home.png", "sneaky.png"])
        md = self.checks_md()
        self.assertIn("without fx baselines record", md)
        self.assertIn("sneaky.png", md)
        self.assertIn("./home.png png-a", slurp(os.path.join(self.f.out, "seen.txt")))
        self.assertTrue(any("without fx baselines record" in n for n in res["notes"]), res["notes"])
        code, prompt = self.fx("ctx", "T-1", "--agent", "code")
        self.assertIn("restored by the gate: home.png, sneaky.png", prompt["prompt"])

    def test_deleting_an_image_does_not_get_through_the_gate(self):
        self.fill()
        os.remove(os.path.join(self.folder, "home.png"))
        self.s3.objects.clear()
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", "it is not generated from the code under test either")
        self.assertIn("home.png", self.checks_md())
        self.assertFalse(self.ran_checks())

    def test_deleting_the_list_does_not_get_through_the_gate(self):
        self.fill()
        os.remove(self.list_path)
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("is gone", self.checks_md())
        self.assertFalse(self.ran_checks())


# --------------------------------------------------------------------------
# 5. a bad download

class BadDownloadTest(BaselinesCase):
    def assert_rejected(self):
        self.put_file("home.png", b"what is here now")
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("does not match the list", self.checks_md())
        self.assertEqual(self.on_disk(), b"what is here now", "the image in place is not overwritten")
        self.assertFalse(self.cached(A), "nor does a bad copy reach the cache")
        self.assertFalse(os.path.exists(os.path.join(self.folder, "sub", "menu.png")))
        names = [n for _, _, fs in os.walk(self.folder) for n in fs]
        self.assertEqual(names, ["home.png"], "no temporary file left behind")
        self.assertFalse(self.ran_checks())

    def test_wrong_hash(self):
        self.s3.corrupt = True
        self.assert_rejected()

    def test_wrong_size(self):
        self.s3.truncate = True
        self.assert_rejected()

    def test_a_damaged_cache_entry_is_replaced(self):
        self.bl("sync")
        cfg = fx.baselines_config(self.f.ctx())
        with open(fx.baselines_cache_path(cfg, sha(A)), "wb") as f:
            f.write(b"bit rot")
        shutil.rmtree(self.folder)
        code, res = self.bl("sync")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.on_disk(), A)
        self.assertEqual(len(self.s3.calls("GET")), 3)


# --------------------------------------------------------------------------
# 6. new commits on the remote

class NewCommitsTest(BaselinesCase):
    def test_clean_repository_pulls_and_fetches_the_new_images(self):
        self.fill()
        self.teammate_changes({"home.png": C, "sub/menu.png": B})
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(res["baselines"]["pulled"]["commits"], 1)
        self.assertEqual(res["baselines"]["fetched"], 1)
        self.assertEqual(self.on_disk(), C)
        self.assertEqual(sh(["git", "rev-parse", "HEAD"], self.f.factory), sh(["git", "rev-parse", "HEAD"], self.other))
        self.assertEqual(res["baselines"]["counts"]["changed"], 0, "a teammate's change is not this job's change")
        self.assertEqual(res["baselines"]["unrecorded"], [])

    def test_checks_sh_from_the_pull_is_what_the_gate_runs(self):
        self.fill()
        self.teammate_changes(self.images, {"checks.sh": 'echo new-checks > "$FX_TEST_OUT/new.txt"\nexit 0\n'})
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(slurp(os.path.join(self.f.out, "new.txt")).strip(), "new-checks")

    def test_factory_json_from_the_pull_is_what_the_gate_uses(self):
        self.fill()
        cfg = load_json(os.path.join(self.other, "factory.json"))
        cfg["max_rounds"] = 7
        cfg["gate_marker"] = "from the teammate"
        self.teammate_changes(self.images, {"factory.json": json.dumps(cfg)})
        code, res = self.gate()
        self.assertEqual(self.f.ctx().c("gate_marker"), "from the teammate")
        self.assertEqual(res["gate"], "PASS", self.checks_md())

    def assert_stopped(self, code, res, before):
        self.assertEqual(code, 3, res)
        self.assertIn("did not pull", res["error"])
        self.assertIn("uncommitted changes", res["error"])
        self.assertEqual(sh(["git", "rev-parse", "HEAD"], self.f.factory), before, "no pull, no stash, no merge")
        self.assertEqual(sh(["git", "stash", "list"], self.f.factory), "")

    def test_uncommitted_changes_stop_the_gate_with_exit_3(self):
        self.fill()
        self.teammate_changes({"home.png": C})
        write(os.path.join(self.f.factory, "lessons-draft.md"), "x\n")
        sh(["git", "add", "lessons-draft.md"], self.f.factory)
        sh(["git", "commit", "-q", "-m", "draft"], self.f.factory)
        sh(["git", "reset", "-q", "--soft", "HEAD~1"], self.f.factory)
        before = sh(["git", "rev-parse", "HEAD"], self.f.factory)
        code, res = self.gate()
        self.assert_stopped(code, res, before)
        self.assertEqual(self.f.job()["round"], 0, "a stop of this kind does not use up a round")
        self.assertFalse(self.ran_checks())

    def test_a_recorded_list_stops_the_gate_when_behind(self):
        self.fill()
        self.teammate_changes({"home.png": C})
        self.put_file("home.png", b"mine")
        self.bl("record", "--allow-removed")
        before = sh(["git", "rev-parse", "HEAD"], self.f.factory)
        code, res = self.gate()
        self.assert_stopped(code, res, before)

    def test_sync_and_new_stop_too(self):
        self.fill()
        self.teammate_changes({"home.png": C})
        with open(self.list_path, "a") as f:
            f.write(" ")
        before = sh(["git", "rev-parse", "HEAD"], self.f.factory)
        self.assert_stopped(*self.bl("sync"), before)
        issue = os.path.join(self.tmp, "issue.md")
        write(issue, "Do it\n")
        code, res = self.fx("new", "--local", "a job", "--issue-file", issue)
        self.assert_stopped(code, res, before)
        self.assertFalse(os.path.exists(os.path.join(self.f.factory, "jobs", "LOCAL-1")), "no half-made job")

    def test_diverged_repository_stops_with_exit_3(self):
        self.fill()
        self.teammate_changes({"home.png": C})
        sh(["git", "commit", "-q", "--allow-empty", "-m", "mine"], self.f.factory)
        code, res = self.gate()
        self.assertEqual(code, 3)
        self.assertIn("cannot fast-forward", res["error"])

    def hold_lock(self, mode):
        lock = os.path.join(self.f.factory, "jobs", ".baselines-gate.lock")
        p = subprocess.Popen([sys.executable, "-I", "-c",
                              "import fcntl,sys,time\nf=open(sys.argv[1],'w')\nfcntl.flock(f,fcntl.%s)\n"
                              "print('held',flush=True)\ntime.sleep(60)\n" % mode, lock],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), "held")
        self.addCleanup(p.stdout.close)

    def test_no_pull_while_another_gate_is_running(self):
        self.fill()
        self.hold_lock("LOCK_SH")
        self.teammate_changes({"home.png": C, "sub/menu.png": B},
                              {"checks.sh": 'echo new-checks > "$FX_TEST_OUT/new.txt"\nexit 0\n'})
        before = sh(["git", "rev-parse", "HEAD"], self.f.factory)
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(sh(["git", "rev-parse", "HEAD"], self.f.factory), before)
        self.assertTrue(any("another gate or selftest" in n for n in res["notes"]), res["notes"])
        self.assertEqual(self.on_disk(), A)
        self.assertFalse(os.path.exists(os.path.join(self.f.out, "new.txt")))

    def test_a_running_gate_holds_off_any_pull(self):
        self.fill()
        probe = ('import fcntl,sys\nf=open(sys.argv[1],"w")\n'
                 'try:\n    fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);print("free")\n'
                 'except OSError:\n    print("held")\n')
        write(os.path.join(self.f.factory, "probe.py"), probe)
        write(os.path.join(self.f.factory, "checks.sh"),
              '"%s" -I "%s" "%s" > "$FX_TEST_OUT/lock.txt"\nexit 0\n' % (
                  sys.executable, os.path.join(self.f.factory, "probe.py"),
                  os.path.join(self.f.factory, "jobs", ".baselines-gate.lock")))
        sh(["git", "add", "-A"], self.f.factory)
        sh(["git", "commit", "-q", "-m", "probe"], self.f.factory)
        sh(["git", "push", "-q", "origin", "main"], self.f.factory)
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(slurp(os.path.join(self.f.out, "lock.txt")).strip(), "held")
        self.assertEqual(fx._BASELINE_GATE_LOCK, [], "and lets go when fx is done")

    def test_new_job_pulls_and_fills_in(self):
        self.teammate_changes({"home.png": C, "sub/menu.png": B})
        self.put_file("sub/menu.png", b"someone's work in progress")
        issue = os.path.join(self.tmp, "issue.md")
        write(issue, "Do it\n")
        code, res = self.fx("new", "--local", "a job", "--issue-file", issue)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["baselines"]["pulled"]["commits"], 1)
        self.assertEqual(self.on_disk(), C)
        self.assertEqual(self.on_disk("sub/menu.png"), b"someone's work in progress", "only a gate restores")
        self.assertTrue(any("gate will restore" in w for w in res["baselines"]["warnings"]))

    def test_new_job_survives_a_store_outage(self):
        self.s3.fail_status = 500
        issue = os.path.join(self.tmp, "issue.md")
        write(issue, "Do it\n")
        code, res = self.fx("new", "--local", "a job", "--issue-file", issue)
        self.assertEqual(code, 0, res)
        self.assertTrue(any("HTTP 500" in w for w in res["baselines"]["warnings"]), res["baselines"])

    def test_no_upstream_is_no_problem(self):
        sh(["git", "branch", "-q", "--unset-upstream"], self.f.factory)
        code, res = self.bl("sync")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["warnings"], [])
        self.assertEqual(self.bl("status")[1]["remote"]["state"], "no-upstream")


# --------------------------------------------------------------------------
# 7. reverting the list

class RevertTest(BaselinesCase):
    def test_revert_brings_the_old_images_back(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record")
        self.bl("push", "--commit")
        self.assertEqual(self.on_disk(), C)
        sh(["git", "revert", "--no-edit", "HEAD"], self.f.factory)
        code, res = self.bl("sync")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.on_disk(), A)
        self.assertEqual(res["unrecorded"], [], "a version S3 has needs no keeping")
        sh(["git", "revert", "--no-edit", "HEAD"], self.f.factory)
        self.bl("sync")
        self.assertEqual(self.on_disk(), C)

    def test_an_old_branch_keeps_its_own_images(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record")
        self.bl("push", "--commit")
        old = sh(["git", "rev-parse", "HEAD~1"], self.f.factory).strip()
        sh(["git", "checkout", "-q", old], self.f.factory)
        self.bl("sync")
        self.assertEqual(self.on_disk(), A)


# --------------------------------------------------------------------------
# 8. the list names a hash S3 does not have

class UnpushedHashTest(BaselinesCase):
    def commit_without_push(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record")
        sh(["git", "add", MANIFEST], self.f.factory)
        sh(["git", "commit", "-q", "-m", "oops, list before images"], self.f.factory)

    def test_gate_finds_it(self):
        self.commit_without_push()
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("not on S3", self.checks_md())
        self.assertIn("home.png", self.checks_md())
        self.assertFalse(self.ran_checks())

    def test_doctor_finds_it(self):
        self.commit_without_push()
        code, res = self.fx("doctor")
        check = self.check(res, "baselines images on S3")
        self.assertFalse(check["ok"])
        self.assertIn("home.png", check["detail"])

    def test_pr_finds_it(self):
        self.commit_without_push()
        self.f.commit({"src/a.txt": "a\n"})
        self.f.save_job(stage="pr", incomplete=True, needs_human=True, handoff_reason="test")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 3)
        self.assertIn("not on S3", res["error"])
        self.assertEqual(sh(["git", "ls-remote", "--heads", self.f.origin, self.f.branch], self.tmp), "")

    def test_push_clears_it(self):
        self.commit_without_push()
        code, res = self.bl("push")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["uploaded"], 1)
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())

    def test_uploaded_but_not_committed_is_only_advice_in_doctor(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record")
        code, res = self.fx("doctor")
        self.assertTrue(self.check(res, "baselines images on S3")["ok"])
        self.assertIn("home.png", self.check(res, "baselines recorded, not uploaded yet (advice)")["detail"])


# --------------------------------------------------------------------------
# 9. a poisoned list

class PoisonedListTest(BaselinesCase):
    def poison(self, path, sha256=None, size=None):
        write(self.list_path, json.dumps({"version": 1, "files": [
            {"path": path, "sha256": sha256 or sha(A), "size": size if size is not None else len(A)}]}))

    def test_bad_paths_are_refused_and_nothing_is_written(self):
        victim = os.path.join(self.tmp, "victim.txt")
        for path in ("../../../victim.txt", "/tmp/fx-evil.txt", "a/../../b.png", "a/./b.png", "a//b.png", "C:\\evil.png",
                     "~/evil.png", "..", ".git/hooks/pre-commit", "a\\..\\b.png", "new\nline.png", ""):
            self.poison(path)
            code, res = self.bl("sync")
            self.assertEqual(code, 3, path)
            self.assertIn("unsafe path", res["error"], path)
            code, res = self.bl("status")
            self.assertEqual(code, 3, path)
        self.assertFalse(os.path.exists(victim))
        self.assertFalse(os.path.exists("/tmp/fx-evil.txt"))
        self.assertEqual(self.s3.log, [])

    def test_gate_fails_before_the_checks(self):
        self.poison("../../../victim.txt")
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("unsafe path", self.checks_md())
        self.assertFalse(self.ran_checks())
        self.assertEqual(self.s3.log, [])

    def test_a_symlink_out_of_the_folder_is_refused(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.makedirs(self.folder)
        os.symlink(outside, os.path.join(self.folder, "link"))
        self.poison("link/evil.png")
        code, res = self.bl("sync")
        self.assertEqual(code, 3)
        self.assertIn("symlink", res["error"])
        self.assertEqual(os.listdir(outside), [])

    def test_a_symlinked_file_out_of_the_folder_is_refused(self):
        outside = os.path.join(self.tmp, "secret.txt")
        write(outside, "keep me\n")
        os.makedirs(self.folder)
        os.symlink(outside, os.path.join(self.folder, "home.png"))
        code, res = self.bl("sync")
        self.assertEqual(code, 3)
        self.assertIn("symlink", res["error"])
        self.assertEqual(slurp(outside), "keep me\n")

    def test_a_folder_that_leads_out_of_the_repository_is_refused(self):
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside)
        os.makedirs(os.path.dirname(self.folder), exist_ok=True)
        os.symlink(outside, self.folder)
        code, res = self.bl("sync")
        self.assertEqual(code, 3)
        self.assertIn("through a symlink", res["error"])
        self.assertEqual(os.listdir(outside), [])

    def test_a_symlink_inside_the_folder_becomes_the_real_file(self):
        self.put_file("real.png", A)
        os.symlink(os.path.join(self.folder, "real.png"), os.path.join(self.folder, "home.png"))
        code, res = self.bl("sync")
        self.assertEqual(code, 0, res)
        self.assertFalse(os.path.islink(os.path.join(self.folder, "home.png")))
        self.assertEqual(self.on_disk(), A)
        self.assertFalse(os.path.exists(os.path.join(self.folder, "real.png")))

    def test_a_folder_symlink_inside_the_folder_is_refused_too(self):
        os.makedirs(os.path.join(self.folder, "real"))
        os.symlink(os.path.join(self.folder, "real"), os.path.join(self.folder, "link"))
        self.poison("link/a.png")
        code, res = self.bl("sync")
        self.assertEqual(code, 3)
        self.assertIn("symlink", res["error"])

    def test_stray_symlinks_are_removed_by_a_sync(self):
        self.fill()
        os.symlink(os.path.join(self.tmp, "gitconfig"), os.path.join(self.folder, "stray.png"))
        code, res = self.bl("sync")
        self.assertEqual(code, 0, res)
        self.assertFalse(os.path.lexists(os.path.join(self.folder, "stray.png")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "gitconfig")))

    def test_a_bad_hash_cannot_steer_the_object_name(self):
        for bad in ("../../other", "A" * 64, "abc", sha(A)[:-1] + "g"):
            self.poison("home.png", sha256=bad)
            code, res = self.bl("sync")
            self.assertEqual(code, 3, bad)
            self.assertIn("valid sha256", res["error"])
        self.assertEqual(self.s3.log, [])

    def test_other_malformed_lists_are_refused(self):
        for text in ("{", "[]", '{"version": 2, "files": []}', '{"version": 1, "files": [1]}',
                     json.dumps({"version": 1, "files": [{"path": "a", "sha256": sha(A), "size": -1}]}),
                     json.dumps({"version": 1, "files": [{"path": "a", "sha256": sha(A), "size": True}]}),
                     json.dumps({"version": 1, "files": [{"path": "a", "sha256": sha(A), "size": 1},
                                                         {"path": "a", "sha256": sha(A), "size": 1}]})):
            write(self.list_path, text)
            code, res = self.bl("sync")
            self.assertEqual(code, 3, text)


# --------------------------------------------------------------------------
# 10. no baselines key: everything as before

class NoBaselinesTest(FxTestCase):
    def test_the_gate_is_as_before(self):
        write(os.path.join(self.f.factory, "checks.sh"), 'env > "$FX_TEST_OUT/checks.env"\nexit 0\n')
        self.f.commit({"app.py": "x = 1\n"})
        code, res = self.gate()
        self.assertEqual(sorted(res), ["flags", "gate", "job", "models", "next", "notes", "report", "reviewers",
                                       "round", "rounds_left", "skipped"])
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertNotIn("Baselines", self.checks_md())
        self.assertNotIn("FACTORY_BASELINES", slurp(os.path.join(self.f.out, "checks.env")))
        self.assertFalse(os.path.exists(os.path.join(self.f.factory, "jobs", ".baselines-gate.lock")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "state", "baselines")))
        self.assertNotIn("baselines", self.f.job())
        self.assertNotIn("Baselines", self.fx("ctx", "T-1", "--agent", "builder")[1]["prompt"])

    def test_a_tracked_folder_of_images_stays_ordinary_files(self):
        self.f.commit({"qa/baselines/home.png": "png\n", "app.py": "x = 1\n"})
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())

    def test_the_command_says_it_is_not_in_use(self):
        code, res = self.fx("baselines", "status")
        self.assertEqual(code, 3)
        self.assertIn("no baselines block", res["error"])

    def test_doctor_and_install_are_as_before(self):
        code, res = self.fx("doctor")
        self.assertFalse([c for c in res["checks"] if "baselines" in c["check"] and "checks baseline" not in c["check"]])
        code, res = self.fx("install", "status")
        self.assertNotIn("aws", [t["name"] for t in res["tools"]])

    def test_new_job_output_is_as_before(self):
        issue = os.path.join(self.tmp, "issue.md")
        write(issue, "Do it\n")
        code, res = self.fx("new", "--local", "a job", "--issue-file", issue)
        self.assertEqual(code, 0, res)
        self.assertNotIn("baselines", res)
        self.assertNotIn("baselines_start", load_json(os.path.join(self.f.factory, "jobs", res["job"], "job.json")))

    def test_local_secret_values_ignore_key_files_without_the_block(self):
        write(os.path.join(self.f.factory, "local", "s3-read.env"), "AWS_SECRET_ACCESS_KEY=abcdef123456\n")
        self.assertEqual(fx.local_secret_values(self.f.ctx()), set())


class ImagesWithoutJobsTest(BaselinesCase):
    """Reference images on S3 and no jobs_store: with S3, jobs always go there too."""

    def test_doctor_asks_for_the_jobs_store(self):
        code, res = self.fx("doctor")
        self.assertTrue(self.check(res, "baselines settings")["ok"])
        row = self.check(res, "jobs store settings")
        self.assertFalse(row["ok"])
        self.assertIn("S3 is set up but jobs are not kept there", row["detail"])


class SettingsLostTest(BaselinesCase):
    """factory.json overwritten from an old copy: the blocks are gone, and the list and the ignored folder are still there."""

    def lose_the_settings(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        del cfg["storage"], cfg["baselines"]
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))

    def test_doctor_fails_and_says_why(self):
        self.lose_the_settings()
        code, res = self.fx("doctor")
        row = self.check(res, "baselines settings")
        self.assertFalse(row["ok"])
        self.assertFalse(res["ok"])
        self.assertIn("may have been lost", row["detail"])
        self.assertIn(MANIFEST, row["detail"])

    def test_the_command_says_so_and_not_plain_files_in_git(self):
        self.lose_the_settings()
        code, res = self.bl("status")
        self.assertEqual(code, 3)
        self.assertIn("may have been lost", res["error"])
        self.assertNotIn("plain files in git", res["error"])

    def test_only_a_block_gone_is_named_too(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        del cfg["baselines"]
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.assertIn("may have been lost", self.bl("status")[1]["error"])

    def test_another_json_file_is_no_list(self):
        self.lose_the_settings()
        os.remove(self.list_path)
        write(os.path.join(self.f.factory, "qa", "other.json"), json.dumps({"version": 1, "items": []}))
        write(os.path.join(self.f.factory, "qa", "broken.json"), "{nope")
        write(os.path.join(self.f.factory, "qa", "empty.json"), json.dumps({"version": 1, "files": []}))
        code, res = self.fx("doctor")
        self.assertFalse([c for c in res["checks"] if c["check"] == "baselines settings"])
        self.assertIn("plain files in git", self.bl("status")[1]["error"])

    def test_a_copy_in_a_job_s_folder_is_not_counted(self):
        self.lose_the_settings()
        os.remove(self.list_path)
        write(os.path.join(self.f.jd, "copy.json"), fx.baselines_text(entries(self.images)))
        self.assertIn("plain files in git", self.bl("status")[1]["error"])


# --------------------------------------------------------------------------
# 11. keys stay out of everything

class KeySecrecyTest(BaselinesCase):
    KEYS = (READ_ID, READ_SECRET, WRITE_ID, WRITE_SECRET)

    def assert_no_key(self, text, where):
        for key in self.KEYS:
            self.assertNotIn(key, text, where)

    def test_checks_sh_and_the_files_of_a_gate_never_see_them(self):
        self.put_file("home.png", C)
        self.fill()
        self.put_file("home.png", C)
        self.bl("record")
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assert_no_key(json.dumps(res), "gate output")
        env_text = slurp(os.path.join(self.f.out, "checks.env"))
        self.assert_no_key(env_text, "checks.sh environment")
        self.assertIn("FACTORY_BASELINES=", env_text)
        for dirpath, _, names in os.walk(self.f.jd):
            for n in names:
                if n.endswith((".md", ".log", ".json")):
                    self.assert_no_key(slurp(os.path.join(dirpath, n)), n)

    def test_fx_never_puts_them_in_its_own_environment(self):
        self.bl("sync")
        self.bl("record")
        self.bl("push")
        for key in self.KEYS:
            self.assertNotIn(key, os.environ.values())

    def test_agents_are_not_told(self):
        self.gate()
        for agent in ("builder", "security", "code", "ui", "approver"):
            self.assert_no_key(self.fx("ctx", "T-1", "--agent", agent)[1]["prompt"], agent)

    def test_the_projects_own_aws_variables_pass_and_are_not_used(self):
        own = {"AWS_ACCESS_KEY_ID": "PROJECTSOWNKEY", "AWS_SECRET_ACCESS_KEY": "projects-own-secret",
               "AWS_REGION": "ap-southeast-1", "AWS_PROFILE": "projects-profile"}
        with mock.patch.dict(os.environ, own):
            code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        env_text = slurp(os.path.join(self.f.out, "checks.env"))
        for name, value in own.items():
            self.assertIn("%s=%s" % (name, value), env_text)
        self.assertEqual({c["id"] for c in self.s3.log}, {READ_ID}, "fx signs with its own read key")

    def test_a_failing_signature_does_not_leak_what_s3_echoes(self):
        self.s3.echo_key = True
        write(self.key_path("read"),
              "AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=wrong-secret-value\n" % READ_ID)
        code, res = self.bl("sync")
        self.assertEqual(code, 3)
        self.assert_no_key(json.dumps(res), "sync output")
        self.assertIn("SignatureDoesNotMatch", res["error"])
        code, res = self.gate()
        self.assert_no_key(self.checks_md(), "checks.md")
        self.assert_no_key(json.dumps(res), "gate output")

    def test_outgoing_text_redacts_them(self):
        ctx = self.f.ctx()
        for key in self.KEYS:
            text, hits = fx.scrub_text(ctx, "the key is %s here" % key)
            self.assertNotIn(key, text)
            self.assertEqual(hits, ["local-env-value"])

    def test_the_read_key_is_used_to_read_and_the_write_key_only_to_write(self):
        self.bl("sync")
        self.s3.log.clear()
        self.put_file("home.png", C)
        self.bl("record")
        code, res = self.bl("push")
        self.assertEqual(code, 0, res)
        self.assertEqual({c["id"] for c in self.s3.calls("PUT")}, {WRITE_ID})
        self.assertEqual({c["id"] for c in self.s3.calls("HEAD")}, {READ_ID})
        self.assertEqual(self.s3.calls("GET"), [])

    def test_a_read_key_cannot_push(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record")
        write(self.key_path("write"),
              "AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\n" % (READ_ID, READ_SECRET))
        code, res = self.bl("push")
        self.assertEqual(code, 3)
        self.assertIn("HTTP 403", res["error"])
        self.assertNotIn(self.s3.key_of(C), self.s3.objects)

    def test_no_write_key_says_where_to_put_it(self):
        self.bl("sync")
        os.remove(self.key_path("write"))
        code, res = self.bl("push")
        self.assertEqual(code, 3)
        self.assertIn("no write key", res["error"])


class AwsProfileTest(BaselinesCase):
    extra_store = {"read_profile": "fx-read"}

    def setUp(self):
        super().setUp()
        os.remove(self.key_path("read"))
        self.creds = os.path.join(self.tmp, "aws-credentials")
        p = mock.patch.dict(os.environ, {"AWS_SHARED_CREDENTIALS_FILE": self.creds})
        p.start()
        self.addCleanup(p.stop)

    def test_static_profile(self):
        write(self.creds, "[fx-read]\naws_access_key_id=%s\naws_secret_access_key=%s\n" % (READ_ID, READ_SECRET))
        code, res = self.bl("sync")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.on_disk(), A)
        self.assertEqual(self.fx("install", "status")[1]["tools"][-1]["need"], "optional")

    def test_profile_without_static_keys_goes_through_the_aws_cli(self):
        write(self.creds, "")
        aws = os.path.join(self.f.bin, "aws")
        write(aws, '#!/bin/sh\necho \'{"Version":1,"AccessKeyId":"%s","SecretAccessKey":"%s"}\'\n' % (READ_ID, READ_SECRET))
        os.chmod(aws, 0o755)
        code, res = self.bl("sync")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.on_disk(), A)
        tool = next(t for t in self.fx("install", "status")[1]["tools"] if t["name"] == "aws")
        self.assertEqual(tool["need"], "required")

    def test_profile_without_the_cli_says_so(self):
        write(self.creds, "")
        code, res = self.bl("sync")
        self.assertEqual(code, 3)
        self.assertIn("without the AWS CLI", json.dumps(res))


# --------------------------------------------------------------------------
# the list, record and push

class RecordPushTest(BaselinesCase):
    def test_the_list_is_sorted_and_stable(self):
        self.put_file("z.png", A)
        self.put_file("a/b.png", B)
        self.put_file("a.png", C)
        self.bl("record", "--allow-removed")
        first = slurp(self.list_path)
        self.assertEqual(re.findall(r'"path": "([^"]+)"', first), ["a.png", "a/b.png", "z.png"])
        for line in first.splitlines():
            if '"path"' in line:
                self.assertEqual(list(json.loads(line.strip().rstrip(","))), ["path", "sha256", "size"])
        self.bl("record")
        self.assertEqual(slurp(self.list_path), first)

    def test_record_uploads_nothing(self):
        self.fill()
        self.put_file("home.png", C)
        self.s3.log.clear()
        code, res = self.bl("record")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["counts"], {"changed": 1, "added": 0, "removed": 0})
        self.assertEqual(self.s3.log, [])
        self.assertNotIn(self.s3.key_of(C), self.s3.objects)

    def test_record_does_not_wipe_the_list_on_an_empty_machine(self):
        before = slurp(self.list_path)
        code, res = self.bl("record")
        self.assertEqual(code, 3)
        self.assertIn("--allow-removed", res["error"])
        self.assertEqual(slurp(self.list_path), before)

    def test_removal_is_deliberate_and_shows(self):
        self.fill()
        os.remove(os.path.join(self.folder, "home.png"))
        code, res = self.bl("record", "--allow-removed")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["counts"]["removed"], 1)
        self.assertNotIn("home.png", slurp(self.list_path))

    def test_record_refuses_symlinks(self):
        self.fill()
        os.symlink(os.path.join(self.tmp, "gitconfig"), os.path.join(self.folder, "stray.png"))
        code, res = self.bl("record")
        self.assertEqual(code, 3)
        self.assertIn("symlink", res["error"])

    def test_junk_files_are_not_baselines(self):
        self.fill()
        self.put_file(".DS_Store", b"junk")
        self.bl("record")
        self.assertNotIn("DS_Store", slurp(self.list_path))
        self.assertEqual(self.bl("status")[1]["unrecorded"], [])

    def test_push_uploads_only_what_is_missing_and_confirms_everything(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.put_file("copy.png", C)
        self.bl("record")
        self.s3.log.clear()
        code, res = self.bl("push")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["uploaded"], 1)
        self.assertEqual(res["confirmed"], 2, "distinct hashes")
        self.assertEqual(res["images"], 3)
        self.assertEqual(len(self.s3.calls("PUT")), 1)
        self.assertEqual(self.s3.objects[self.s3.key_of(C)], C)
        code, res = self.bl("push")
        self.assertEqual(res["uploaded"], 0)

    def test_push_never_overwrites(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record")
        self.s3.objects[self.s3.key_of(C)] = b"x" * len(C)  # another machine won the race, whatever it uploaded
        self.s3.log.clear()
        code, res = self.bl("push")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.s3.calls("PUT"), [])
        self.assertEqual(self.s3.objects[self.s3.key_of(C)], b"x" * len(C))
        cfg = fx.baselines_config(self.f.ctx())
        store = fx.baselines_store(self.f.ctx(), cfg, "write")
        store.put(sha(C), C)
        self.assertEqual(self.s3.objects[self.s3.key_of(C)], b"x" * len(C), "If-None-Match keeps it even without the HEAD")

    def test_push_refuses_an_object_of_the_wrong_size(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record")
        self.s3.objects[self.s3.key_of(C)] = b"short"
        code, res = self.bl("push")
        self.assertEqual(code, 3)
        self.assertIn("not overwritten", res["error"])

    def test_push_refuses_a_local_file_that_does_not_match(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record")
        self.put_file("home.png", b"edited after record")
        code, res = self.bl("push", "--commit")
        self.assertEqual(code, 3)
        self.assertIn("does not match the list", res["error"])
        self.assertIn("must not be committed", res["error"])
        self.assertEqual(sh(["git", "log", "--oneline", "-1", "--", MANIFEST], self.f.factory).count("\n"), 1)
        self.assertIn(MANIFEST, sh(["git", "status", "--porcelain"], self.f.factory))

    def test_push_commit_commits_the_list_alone(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record", "--job", "T-1")
        write(os.path.join(self.f.factory, "note.txt"), "x\n")
        sh(["git", "add", "note.txt"], self.f.factory)
        code, res = self.bl("push", "--job", "T-1", "--commit")
        self.assertEqual(code, 0, res)
        self.assertEqual(sh(["git", "show", "--name-only", "--format=", "HEAD"], self.f.factory).split(), [MANIFEST])
        self.assertIn("A  note.txt", sh(["git", "status", "--porcelain"], self.f.factory), "still staged, not committed")
        self.assertIn("T-1", sh(["git", "log", "-1", "--format=%s"], self.f.factory))
        self.assertEqual(self.bl("push", "--commit")[1]["commit"], None, "nothing left to commit")

    def test_a_failed_push_commits_nothing(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record")
        self.s3.fail_status = 500
        head = sh(["git", "rev-parse", "HEAD"], self.f.factory)
        code, res = self.bl("push", "--commit")
        self.assertEqual(code, 3)
        self.assertEqual(sh(["git", "rev-parse", "HEAD"], self.f.factory), head)

    def test_a_request_that_fails_once_is_retried(self):
        hits = []
        real = self.s3.server.RequestHandlerClass.handle_any

        def flaky(handler):
            hits.append(1)
            self.s3.fail_status = 503 if len(hits) == 1 else None
            real(handler)
        with mock.patch.object(self.s3.server.RequestHandlerClass, "do_GET", flaky):
            code, res = self.bl("sync")
        self.assertEqual(code, 0, res)


# --------------------------------------------------------------------------
# a UI job that changes images: everything shows

class JobChangesImagesTest(BaselinesCase):
    def change_images(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.put_file("added.png", A + b"-added")
        os.remove(os.path.join(self.folder, "sub", "menu.png"))
        code, res = self.bl("record", "--job", "T-1", "--allow-removed")
        self.assertEqual(code, 0, res)

    def test_the_gate_uses_the_local_images_of_the_list_being_edited(self):
        self.change_images()
        self.s3.log.clear()
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(self.on_disk(), C)
        self.assertEqual(self.s3.calls("PUT"), [], "nothing goes up before push")
        self.assertEqual(res["baselines"]["unrecorded"], [])
        self.assertEqual(res["baselines"]["pending_push"], 2)

    def test_checks_md_and_the_reviewers_see_the_change(self):
        self.change_images()
        code, res = self.gate()
        md = self.checks_md()
        self.assertIn("### Baselines changed", md)
        self.assertIn("1 changed, 1 added, 1 removed", md)
        self.assertIn("`home.png` | changed", md)
        self.assertIn("`added.png` | added", md)
        self.assertIn("`sub/menu.png` | removed", md)
        self.assertIn("round-1/baselines/before/", md)
        self.assertEqual(res["baselines"]["counts"], {"changed": 1, "added": 1, "removed": 1})
        self.assertTrue(any("baselines changed" in n for n in res["notes"]), res["notes"])
        rdir = os.path.join(self.f.jd, "round-1")
        self.assertIn("`home.png`", slurp(os.path.join(rdir, "baselines.md")))
        with open(os.path.join(rdir, "baselines", "before", "home.png"), "rb") as f:
            self.assertEqual(f.read(), A)
        with open(os.path.join(rdir, "baselines", "after", "home.png"), "rb") as f:
            self.assertEqual(f.read(), C)
        self.assertFalse(os.path.exists(os.path.join(rdir, "baselines", "before", "added.png")))
        self.assertFalse(os.path.exists(os.path.join(rdir, "baselines", "after", "sub", "menu.png")))
        for agent in ("code", "ux", "ui", "security", "approver"):
            prompt = self.fx("ctx", "T-1", "--agent", agent)[1]["prompt"]
            self.assertIn("Baselines changed by this job (1 changed, 1 added, 1 removed)", prompt, agent)
            self.assertIn("round-1/baselines.md", prompt, agent)

    def test_the_builder_is_told_the_rules_and_never_a_key(self):
        prompt = self.fx("ctx", "T-1", "--agent", "builder")[1]["prompt"]
        self.assertIn(os.path.join(self.f.factory, DIR), prompt)
        self.assertIn("Never edit or delete an image", prompt)
        self.assertIn("baselines record --job T-1", prompt)
        self.assertNotIn(READ_SECRET, prompt)

    def test_a_change_without_job_or_committed_directly_still_shows(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record")
        self.bl("push")
        sh(["git", "add", MANIFEST], self.f.factory)
        sh(["git", "commit", "-q", "-m", "builder commits the list by hand"], self.f.factory)
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(res["baselines"]["counts"]["changed"], 1)
        self.assertIn("`home.png` | changed", self.checks_md())

    def test_hand_edited_list_shows_and_is_restored_to_it(self):
        self.bl("sync")
        self.s3.put(C)
        data = entries({"home.png": C, "sub/menu.png": B})
        write(self.list_path, fx.baselines_text(data))
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(res["baselines"]["counts"]["changed"], 1)
        self.assertEqual(self.on_disk(), C)

    def test_pull_request_stops_until_the_list_is_uploaded_and_committed(self):
        self.change_images()
        self.f.commit({"src/a.txt": "a\n"})
        self.gate()
        self.f.save_job(stage="pr")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 3)
        self.assertIn("baselines push --job T-1 --commit", res["error"])
        self.assertEqual(sh(["git", "ls-remote", "--heads", self.f.origin, self.f.branch], self.tmp), "")
        code, res = self.bl("push", "--job", "T-1", "--commit")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["uploaded"], 2)
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 0, res)
        self.assertIn(self.f.branch, sh(["git", "ls-remote", "--heads", self.f.origin], self.tmp))
        self.assertTrue(any("not on its remote" in n for n in res["notes"]), res["notes"])
        body = slurp(os.path.join(self.f.jd, "pr-body.md"))
        self.assertIn("## Baselines changed", body)
        self.assertIn("1 changed, 1 added, 1 removed", body)
        self.assertIn("`home.png` | changed", body)
        self.assertIn("baselines-pr/before/", body)
        self.assertIn("fx baselines diff", body)
        with open(os.path.join(self.f.jd, "baselines-pr", "before", "home.png"), "rb") as f:
            self.assertEqual(f.read(), A)

    def test_fx_pr_is_still_the_only_thing_that_pushes_the_project(self):
        self.change_images()
        before = sh(["git", "ls-remote", self.f.origin], self.tmp)
        self.bl("push", "--job", "T-1", "--commit")
        self.assertEqual(sh(["git", "ls-remote", self.f.origin], self.tmp), before)
        self.assertEqual(sh(["git", "rev-parse", "HEAD"], self.remote), sh(["git", "rev-parse", "HEAD~1"], self.f.factory),
                         "nor does anything push the factory repository")

    def test_changes_stay_in_the_report_after_the_factory_repository_is_pushed(self):
        self.change_images()
        self.bl("push", "--job", "T-1", "--commit")
        sh(["git", "push", "-q", "origin", "main"], self.f.factory)
        self.f.commit({"src/a.txt": "a\n"})
        self.f.save_job(stage="pr")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 0, res)
        self.assertIn("1 changed, 1 added, 1 removed", slurp(os.path.join(self.f.jd, "pr-body.md")))

    def test_an_image_put_back_is_no_change(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record", "--job", "T-1")
        self.put_file("home.png", A)
        self.bl("record", "--job", "T-1")
        code, res = self.gate()
        self.assertEqual(res["baselines"]["counts"], {"changed": 0, "added": 0, "removed": 0})
        self.assertNotIn("### Baselines changed", self.checks_md())

    def test_diff_between_commits(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record")
        self.bl("push", "--commit")
        out_dir = os.path.join(self.tmp, "cmp")
        shutil.rmtree(self.folder)
        code, res = self.bl("diff", "--from", "HEAD~1", "--to", "HEAD", "--out", out_dir)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["counts"]["changed"], 1)
        with open(os.path.join(out_dir, "before", "home.png"), "rb") as f:
            self.assertEqual(f.read(), A)
        with open(os.path.join(out_dir, "after", "home.png"), "rb") as f:
            self.assertEqual(f.read(), C)

    def test_a_failing_final_check_says_why(self):
        self.bl("sync")
        self.f.commit({"src/a.txt": "a\n"})
        self.f.save_job(stage="pr", checks_passed_at="not-this-commit")
        self.s3.objects.clear()
        cfg = fx.baselines_config(self.f.ctx())
        shutil.rmtree(os.path.dirname(os.path.dirname(os.path.dirname(fx.baselines_cache_path(cfg, sha(A))))))
        shutil.rmtree(self.folder)
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 3)
        self.assertIn("not on S3", res["error"])


# --------------------------------------------------------------------------
# migrating a repository that commits its images

class MigrateTest(BaselinesCase):
    images = {}

    def prepare(self):
        sh(["git", "init", "-q", "-b", "main"], self.f.factory)
        write(os.path.join(self.f.factory, ".gitignore"), "jobs/\nlocal/\n")
        self.put_file("home.png", A)
        self.put_file("sub/menu.png", B)
        sh(["git", "add", "-A"], self.f.factory)
        sh(["git", "commit", "-q", "-m", "images in git"], self.f.factory)
        self.remote = os.path.join(self.tmp, "factory-origin.git")
        sh(["git", "clone", "-q", "--bare", self.f.factory, self.remote], self.tmp)
        sh(["git", "remote", "add", "origin", self.remote], self.f.factory)
        sh(["git", "fetch", "-q", "origin"], self.f.factory)
        sh(["git", "branch", "-q", "--set-upstream-to=origin/main", "main"], self.f.factory)
        self.history = sh(["git", "log", "--format=%H"], self.f.factory)

    def tracked(self):
        return sh(["git", "ls-files", "--", DIR], self.f.factory).split()

    def test_a_tracked_image_with_an_accent_in_its_name_is_tracked(self):
        name = unicodedata.normalize("NFC", "caf\u00e9.png")
        self.put_file(name, C)
        sh(["git", "add", "-A"], self.f.factory)
        sh(["git", "commit", "-q", "-m", "one more"], self.f.factory)
        code, res = self.bl("migrate")
        self.assertEqual(code, 0, res)
        self.assertIn(name, fx.baselines_read(fx.baselines_config(self.f.ctx()), self.f.factory))

    def test_a_file_git_does_not_track_is_not_taken_for_an_image(self):
        self.put_file("stray-local.png", b"never committed")
        code, res = self.bl("migrate")
        self.assertEqual(code, 3, res)
        self.assertIn("stray-local.png", res["error"])
        self.assertFalse(os.path.exists(self.list_path))
        self.assertEqual(self.s3.objects, {})

    def test_an_ignored_file_is_not_taken_for_one_either(self):
        with open(os.path.join(self.f.factory, ".gitignore"), "a") as f:
            f.write("*.tmp.png\n")
        self.put_file("shot.tmp.png", b"ignored")
        code, res = self.bl("migrate")
        self.assertEqual(code, 3, res)
        self.assertIn("shot.tmp.png", res["error"])

    def test_images_in_git_with_no_list_ever_are_only_not_moved_yet(self):
        cfg = fx.baselines_config(self.f.ctx())
        self.assertEqual(fx.baselines_list_state(cfg, self.f.factory, self.f.job()), "not-migrated")
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())

    def test_without_permission_nothing_is_committed(self):
        code, res = self.bl("migrate")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["uploaded"], 2)
        self.assertEqual(self.s3.objects[self.s3.key_of(A)], A)
        self.assertEqual(sorted(self.tracked()), [DIR + "/home.png", DIR + "/sub/menu.png"])
        self.assertEqual(sh(["git", "log", "--format=%H"], self.f.factory), self.history)
        self.assertIn(DIR + "/", slurp(os.path.join(self.f.factory, ".gitignore")))
        self.assertIn("migrate --commit", res["next"])
        self.assertEqual(fx.baselines_parse(slurp(self.list_path), "x"), entries({"home.png": A, "sub/menu.png": B}))

    def test_with_permission_a_normal_commit_takes_them_out(self):
        code, res = self.bl("migrate", "--commit")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.tracked(), [])
        self.assertEqual(self.on_disk(), A, "the working copies stay")
        log = sh(["git", "log", "--format=%H"], self.f.factory).split()
        self.assertEqual(log[1:], self.history.split(), "one new commit on top; history is not rewritten")
        self.assertEqual(sh(["git", "show", "HEAD~1:%s/home.png" % DIR], self.f.factory), A.decode(), "old blobs remain")
        names = sh(["git", "show", "--name-status", "--format=", "HEAD"], self.f.factory)
        self.assertIn("A\t" + MANIFEST, names)
        self.assertIn("D\t%s/home.png" % DIR, names)
        self.assertIn("M\t.gitignore", names)
        self.assertEqual(sh(["git", "status", "--porcelain", "--untracked-files=no"], self.f.factory), "")
        self.assertEqual(sh(["git", "check-ignore", DIR + "/home.png"], self.f.factory).strip(), DIR + "/home.png")
        self.assertEqual(sh(["git", "ls-remote", self.remote, "refs/heads/main"], self.tmp).split()[0], self.history.split()[0],
                         "nothing is pushed")

    def test_a_teammate_gets_the_images_after_the_pull(self):
        self.bl("migrate", "--commit")
        sh(["git", "push", "-q", "origin", "main"], self.f.factory)
        shutil.rmtree(self.folder)
        code, res = self.bl("sync")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.on_disk(), A)

    def test_it_refuses_images_with_uncommitted_changes(self):
        self.put_file("home.png", b"edited")
        code, res = self.bl("migrate")
        self.assertEqual(code, 3)
        self.assertIn("uncommitted changes", res["error"])
        self.assertEqual(self.s3.objects, {})

    def test_a_commit_of_its_own(self):
        write(os.path.join(self.f.factory, "other.txt"), "x\n")
        sh(["git", "add", "other.txt"], self.f.factory)
        code, res = self.bl("migrate", "--commit")
        self.assertEqual(code, 3)
        self.assertIn("other.txt", res["error"])
        self.assertEqual(len(self.tracked()), 2)
        self.assertEqual(sh(["git", "log", "--format=%H"], self.f.factory), self.history)

    def test_it_uploads_before_anything_is_committed(self):
        self.s3.fail_status = 500
        code, res = self.bl("migrate", "--commit")
        self.assertEqual(code, 3)
        self.assertEqual(len(self.tracked()), 2)
        self.assertEqual(sh(["git", "log", "--format=%H"], self.f.factory), self.history)

    def test_running_it_again_is_safe(self):
        self.bl("migrate", "--commit")
        head = sh(["git", "rev-parse", "HEAD"], self.f.factory)
        code, res = self.bl("migrate")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["uploaded"], 0)
        self.assertEqual(sh(["git", "rev-parse", "HEAD"], self.f.factory), head)

    def test_before_the_migration_the_gate_uses_the_images_in_git(self):
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertIn("still in git", self.checks_md())
        self.assertEqual(self.on_disk(), A)


# --------------------------------------------------------------------------
# the list in the project's own repository

PROJECT_IMAGES = {"home.png": A, "sub/menu.png": B}


class ProjectRepoCase(BaselinesCase):
    repo = "project"
    images = PROJECT_IMAGES
    base_files = {"README.md": "project\n", ".gitignore": DIR + "/\n",
                  MANIFEST: "%s"}  # filled in below, when fx is at hand

    def prepare(self):
        for data in self.images.values():
            self.s3.put(data)
        sh(["git", "init", "-q", "-b", "main"], self.f.factory)
        sh(["git", "add", "-A"], self.f.factory)
        sh(["git", "commit", "-q", "-m", "factory"], self.f.factory)
        self.folder = os.path.join(self.f.wt, DIR)
        self.list_path = os.path.join(self.f.wt, MANIFEST)

    def bl(self, *argv):
        return self.fx("baselines", *(list(argv) + ["--job", "T-1"]))


ProjectRepoCase.base_files = {"README.md": "project\n", ".gitignore": DIR + "/\n",
                              MANIFEST: fx.baselines_text(entries(PROJECT_IMAGES))}


class ProjectRepoTest(ProjectRepoCase):
    def test_a_new_job_clone_gets_its_images(self):
        issue = os.path.join(self.tmp, "issue.md")
        write(issue, "Do it\n")
        code, res = self.fx("new", "--local", "a job", "--issue-file", issue)
        self.assertEqual(code, 0, res)
        with open(os.path.join(res["worktree"], DIR, "home.png"), "rb") as f:
            self.assertEqual(f.read(), A)
        self.assertEqual(sh(["git", "status", "--porcelain"], res["worktree"]), "", "ignored: not untracked work")

    def test_the_gate_syncs_the_clone(self):
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(self.on_disk(), A)
        env_text = slurp(os.path.join(self.f.out, "checks.env"))
        self.assertIn("FACTORY_BASELINES=%s" % self.folder, env_text)

    def test_commands_need_a_job_or_a_clone(self):
        code, res = self.fx("baselines", "status")
        self.assertEqual(code, 3)
        self.assertIn("--job", res["error"])
        code, res = self.fx("baselines", "status", "--clone", "/etc")
        self.assertEqual(code, 3)
        code, res = self.fx("baselines", "status", "--clone", self.f.wt)
        self.assertEqual(code, 0, res)

    def test_the_whole_flow_of_a_job_that_changes_images(self):
        self.bl("sync")
        self.put_file("home.png", C)
        code, res = self.bl("record")
        self.assertEqual(code, 0, res)
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())  # the list is not committed yet
        self.assertEqual(res["baselines"]["counts"]["changed"], 1)
        self.assertIn("`home.png` | changed", self.checks_md())
        self.assertNotIn(self.s3.key_of(C), self.s3.objects)
        self.f.save_job(stage="pr")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 3, "fx pr must not take the list into a WIP commit before its images are up")
        self.assertEqual(sh(["git", "ls-remote", "--heads", self.f.origin, self.f.branch], self.tmp), "")
        code, res = self.bl("push", "--commit")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.s3.objects[self.s3.key_of(C)], C)
        self.assertEqual(sh(["git", "ls-remote", "--heads", self.f.origin, self.f.branch], self.tmp), "", "not pushed yet")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 0, res)
        pushed = sh(["git", "show", "%s:%s" % (self.f.branch, MANIFEST)], self.f.origin)
        self.assertIn(sha(C), pushed)
        self.assertIn("## Baselines changed", slurp(os.path.join(self.f.jd, "pr-body.md")))

    def test_committing_the_list_before_uploading_is_caught(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record")
        self.f.commit({MANIFEST: slurp(self.list_path)}, "list too early")
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("not on S3", self.checks_md())

    def test_an_uncommitted_list_is_the_only_exception_to_all_work_committed(self):
        self.bl("sync")
        self.f.commit({"app.py": "x = 2\n"})
        write(os.path.join(self.f.wt, "README.md"), "changed\n")
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL")
        self.assertIn("README.md", self.checks_md())

    def test_a_job_branch_is_never_pulled(self):
        self.bl("sync")
        before = sh(["git", "rev-parse", "HEAD"], self.f.wt)
        seed = os.path.join(self.tmp, "seed")
        write(os.path.join(seed, "new.txt"), "x\n")
        sh(["git", "add", "-A"], seed)
        sh(["git", "commit", "-q", "-m", "moved on"], seed)
        sh(["git", "push", "-q", self.f.origin, "HEAD:main"], seed)
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(sh(["git", "rev-parse", "HEAD"], self.f.wt), before)

    def test_doctor_reads_the_list_from_the_base_branch(self):
        code, res = self.fx("doctor")
        self.assertTrue(self.check(res, "baselines list")["ok"], res["checks"])
        self.assertTrue(self.check(res, "baselines images on S3")["ok"], res["checks"])
        del self.s3.objects[self.s3.key_of(A)]
        code, res = self.fx("doctor")
        self.assertFalse(self.check(res, "baselines images on S3")["ok"])

    def test_selftest_uses_its_own_clone(self):
        code, res = self.fx("selftest", "--no-app")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["steps"]["baselines"]["fetched"], 2)
        self.assertIn("./home.png png-a", slurp(os.path.join(self.f.out, "seen.txt")))


class DiffOptionsTest(BaselinesCase):
    def try_ref(self, *argv):
        out = os.path.join(self.tmp, "written")
        os.makedirs(out + ":qa")
        code, res = self.bl("diff", *(a.replace("%s", out) for a in argv))
        self.assertEqual(code, 3, res)
        self.assertIn("git would read it as an option", res["error"])
        self.assertEqual(os.listdir(out + ":qa"), [], "git wrote a file it was told to by the ref")

    def test_from_cannot_be_an_option(self):
        self.try_ref("--from=--output=%s", "--to=HEAD")

    def test_to_cannot_be_an_option(self):
        self.try_ref("--from=HEAD", "--to=--output=%s")


class UnicodeNamesTest(BaselinesCase):
    """A name written by a macOS tool: on Linux, the NFC name on the list does not open it."""
    NFC, NFD = unicodedata.normalize("NFC", "\u00e9.png"), unicodedata.normalize("NFD", "\u00e9.png")

    def setUp(self):
        super().setUp()
        self.bl("sync")
        self.put_file(self.NFD, C)

    def test_record(self):
        code, res = self.bl("record")
        self.assertEqual(code, 0, res)
        self.assertEqual(fx.baselines_read(fx.baselines_config(self.f.ctx()), self.f.factory)[self.NFC]["sha256"], sha(C))

    def test_status(self):
        code, res = self.bl("status")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["unrecorded"], [self.NFC])

    def test_sync_takes_the_file_away_when_it_is_not_on_the_list(self):
        code, res = self.bl("sync")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["removed"], [self.NFC])
        self.assertFalse([n for n in os.listdir(self.folder) if n.endswith("\u0301.png") or n == self.NFC])

    def test_a_listed_image_stored_in_the_other_form_is_not_fetched_again(self):
        self.s3.put(C)
        write(self.list_path, fx.baselines_text(entries(dict(self.images, **{self.NFC: C}))))
        code, res = self.bl("status")
        self.assertEqual((code, res["missing"], res["wrong"]), (0, [], []), res)
        self.s3.log.clear()
        code, res = self.bl("sync")
        self.assertEqual((code, res["fetched"]), (0, 0), res)

    def test_a_file_and_its_folder_in_different_forms(self):
        name = unicodedata.normalize("NFC", "c\u00e1/") + self.NFD
        self.put_file(name, C)
        self.s3.put(C)
        listed = unicodedata.normalize("NFC", name)
        write(self.list_path, fx.baselines_text(entries(dict(self.images, **{listed: C, self.NFC: C}))))
        code, res = self.bl("status")
        self.assertEqual((code, res["missing"], res["wrong"]), (0, [], []), res)

    def test_both_forms_side_by_side_are_refused(self):
        self.put_file(self.NFC, A)
        if len([n for n in os.listdir(self.folder) if unicodedata.normalize("NFC", n) == self.NFC]) < 2:
            self.skipTest("this filesystem does not tell the forms apart")
        code, res = self.bl("record")
        self.assertEqual(code, 3, res)
        self.assertIn("Unicode form", res["error"])


class KeysOutOfReachTest(BaselinesCase):
    def old_place(self, mode="write"):
        path = os.path.join(self.f.factory, "local", "s3-%s.env" % mode)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.replace(self.key_path(mode), path)
        return path

    def test_following_the_folder_to_the_factory_finds_no_key(self):
        write(os.path.join(self.f.factory, "checks.sh"), CHECKS.replace("exit 0\n", (
            'top=$(git -C "$FACTORY_BASELINES/.." rev-parse --show-toplevel)\n'
            'find "$top" "$top-work" -name "s3-*.env" > "$FX_TEST_OUT/found.txt"\n'
            'grep -rl --exclude=checks.sh "%s" "$top" "$top-work" >> "$FX_TEST_OUT/found.txt" || true\nexit 0\n' % WRITE_SECRET)))
        sh(["git", "add", "-A"], self.f.factory)
        sh(["git", "commit", "-q", "-m", "checks"], self.f.factory)
        sh(["git", "push", "-q", "origin", "main"], self.f.factory)
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(slurp(os.path.join(self.f.out, "found.txt")), "")

    def test_the_keys_are_used_from_their_new_place(self):
        code, res = self.bl("sync")
        self.assertEqual(code, 0, res)
        self.assertEqual({c["id"] for c in self.s3.log}, {READ_ID})
        self.assertEqual(os.path.dirname(self.key_path("read")), self.bl("status")[1]["keys_folder"])

    def test_keys_in_the_old_place_still_work_and_doctor_says_how_to_move_them(self):
        old = self.old_place("read")
        code, res = self.bl("sync")
        self.assertEqual(code, 0, res)
        code, res = self.fx("doctor")
        row = self.check(res, "storage keys outside the factory repository (advice)")
        self.assertIn(old, row["detail"])
        self.assertIn("mkdir -p -m 700", row["detail"])

    def test_no_such_row_once_they_have_moved(self):
        code, res = self.fx("doctor")
        self.assertFalse([c for c in res["checks"] if c["check"].startswith("storage keys outside")])

    def test_doctor_asks_for_private_files_in_the_new_place_too(self):
        os.chmod(self.key_path("read"), 0o644)
        code, res = self.fx("doctor")
        self.assertFalse(self.check(res, "storage read key file private")["ok"])

    def test_init_keeps_agents_from_reading_them(self):
        code, res = self.fx("init", "--project", self.f.origin)
        self.assertEqual(code, 0, res)
        deny = load_json(os.path.join(self.f.factory, ".claude", "settings.local.json"))["permissions"]["deny"]
        self.assertIn("Read(//%s/**)" % fx.keys_root().lstrip("/"), deny)
        self.assertIn("Read(//%s/local/s3-*.env)" % self.f.factory.lstrip("/"), deny)

    def test_a_value_in_either_place_is_redacted(self):
        self.old_place("write")
        self.assertIn(WRITE_SECRET, fx.baselines_key_values(self.f.ctx()))
        self.assertIn(READ_SECRET, fx.baselines_key_values(self.f.ctx()))


class CommittedListUncheckedTest(BaselinesCase):
    """The list names an image S3 lacks. Not being able to check that is not a pass."""

    def commit_unknown(self):
        self.bl("sync")
        self.put_file("c.png", C)
        write(self.list_path, fx.baselines_text(entries(dict(self.images, **{"c.png": C}))))
        sh(["git", "add", MANIFEST], self.f.factory)
        sh(["git", "commit", "-q", "-m", "list names an image S3 lacks"], self.f.factory)

    def assert_stopped(self):
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("could not check that S3", self.checks_md())
        self.f.save_job(stage="pr", gate="PASS")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 3, res)
        self.assertIn("could not check that S3", res["error"])
        self.assertEqual(sh(["git", "ls-remote", "--heads", self.f.origin, self.f.branch], self.tmp), "")

    def test_s3_answers_503(self):
        self.commit_unknown()
        self.s3.fail_status = 503
        self.assert_stopped()

    def test_no_read_key(self):
        self.commit_unknown()
        os.remove(self.key_path("read"))
        self.assert_stopped()


class ListCannotBeRemovedTest(BaselinesCase):
    """With the list gone every check used to step aside: the builder's own screenshot became the reference."""

    def remove_the_list(self):
        self.bl("sync")
        self.put_file("home.png", b"the builder's own screenshot")
        os.remove(self.list_path)
        sh(["git", "add", "-f", DIR], self.f.factory)

    def assert_stopped(self):
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("is gone", self.checks_md())
        self.assertFalse(self.ran_checks())
        self.f.save_job(stage="pr", gate="PASS")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 3, res)
        self.assertIn("is gone", res["error"])
        self.assertEqual(sh(["git", "ls-remote", "--heads", self.f.origin, self.f.branch], self.tmp), "")

    def test_deleting_it_and_staging_the_images(self):
        self.remove_the_list()
        self.assert_stopped()

    def test_a_copy_kept_in_the_folder_does_not_bring_it_back(self):
        self.bl("sync")
        sh(["git", "rm", "-q", "--cached", MANIFEST], self.f.factory)
        with open(os.path.join(self.f.factory, ".gitignore"), "a") as f:
            f.write(MANIFEST + "\n")
        sh(["git", "add", ".gitignore"], self.f.factory)
        sh(["git", "commit", "-q", "-m", "stop tracking the list"], self.f.factory)
        self.assertTrue(os.path.exists(self.list_path))
        self.assert_stopped()

    def test_committing_that_too(self):
        self.remove_the_list()
        sh(["git", "rm", "-q", "--cached", "--ignore-unmatch", MANIFEST], self.f.factory)
        sh(["git", "commit", "-q", "-m", "images back in git"], self.f.factory)
        self.assert_stopped()


def without_the_list(root, rev, image):
    """A commit like rev with no list and an image of the builder's, for git replace."""
    env = dict(os.environ, GIT_INDEX_FILE=os.path.join(root, ".git", "other-index"))
    subprocess.run(["git", "read-tree", rev], cwd=root, env=env, check=True)
    subprocess.run(["git", "rm", "-q", "--cached", "--ignore-unmatch", MANIFEST], cwd=root, env=env, check=True)
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=root, input=b"mine", stdout=subprocess.PIPE,
                          check=True).stdout.decode().strip()
    subprocess.run(["git", "update-index", "--add", "--cacheinfo", "100644,%s,%s/%s" % (blob, DIR, image)], cwd=root,
                   env=env, check=True)
    tree = subprocess.run(["git", "write-tree"], cwd=root, env=env, stdout=subprocess.PIPE, check=True).stdout.decode().strip()
    return sh(["git", "commit-tree", tree, "-m", "same message"], root).strip()


class ReplacedHistoryCase:
    """Replacing the commit the check looks at used to make the list look as if it never existed."""

    def assert_stopped(self, because):
        code, res = self.gate()
        self.assertEqual(code, 3, res)
        self.assertIn(because, res["error"])
        self.assertFalse(self.ran_checks())
        self.f.save_job(stage="pr", gate="PASS")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 3, res)
        self.assertIn(because, res["error"])
        self.assertEqual(sh(["git", "ls-remote", "--heads", self.f.origin, self.f.branch], self.tmp), "")


class ListReplacedInTheFactoryTest(ReplacedHistoryCase, BaselinesCase):
    def test_a_copy_of_head_without_the_list(self):
        self.bl("sync")
        head = sh(["git", "rev-parse", "HEAD"], self.f.factory).strip()
        sh(["git", "replace", head, without_the_list(self.f.factory, head, "home.png")], self.f.factory)
        os.remove(self.list_path)
        self.put_file("home.png", b"the builder's own screenshot")
        self.assert_stopped("refs/replace/")

    def test_fx_itself_looks_past_it(self):
        self.bl("sync")
        head = sh(["git", "rev-parse", "HEAD"], self.f.factory).strip()
        sh(["git", "replace", head, without_the_list(self.f.factory, head, "home.png")], self.f.factory)
        os.remove(self.list_path)
        cfg = fx.baselines_config(self.f.ctx())
        with mock.patch.dict(os.environ, GIT_NO_REPLACE_OBJECTS="1"):
            self.assertEqual(fx.baselines_list_state(cfg, self.f.factory), "lost")


class ListReplacedInTheProjectTest(ReplacedHistoryCase, ProjectRepoCase):
    def test_a_copy_of_the_merge_base_without_the_list(self):
        self.bl("sync")
        base = sh(["git", "merge-base", "origin/main", "HEAD"], self.f.wt).strip()
        sh(["git", "replace", base, without_the_list(self.f.wt, base, "home.png")], self.f.wt)
        os.remove(self.list_path)
        self.put_file("home.png", b"the builder's own screenshot")
        sh(["git", "add", "-A"], self.f.wt)
        sh(["git", "add", "-f", DIR], self.f.wt)
        sh(["git", "commit", "-q", "-m", "work"], self.f.wt)
        self.assert_stopped("refs/replace/")

    def test_fx_itself_looks_past_it(self):
        self.bl("sync")
        base = sh(["git", "merge-base", "origin/main", "HEAD"], self.f.wt).strip()
        sh(["git", "replace", base, without_the_list(self.f.wt, base, "home.png")], self.f.wt)
        os.remove(self.list_path)
        sh(["git", "add", "-A"], self.f.wt)
        sh(["git", "commit", "-q", "-m", "work"], self.f.wt)
        cfg = fx.baselines_config(self.f.ctx())
        with mock.patch.dict(os.environ, GIT_NO_REPLACE_OBJECTS="1"):
            self.assertEqual(fx.baselines_list_state(cfg, self.f.wt, self.f.job()), "lost")


class BranchFromBeforeTheListTest(BaselinesCase):
    """The team added the list after the commit this branch starts from: no git tricks needed."""
    repo = "project"
    images = {"home.png": A}

    def prepare(self):
        self.s3.put(A)
        sh(["git", "init", "-q", "-b", "main"], self.f.factory)
        sh(["git", "add", "-A"], self.f.factory)
        sh(["git", "commit", "-q", "-m", "factory"], self.f.factory)
        self.folder = os.path.join(self.f.wt, DIR)
        self.list_path = os.path.join(self.f.wt, MANIFEST)
        team = os.path.join(self.tmp, "team")
        sh(["git", "clone", "-q", self.f.origin, team], self.tmp)
        write(os.path.join(team, MANIFEST), fx.baselines_text(entries({"home.png": A})))
        write(os.path.join(team, ".gitignore"), DIR + "/\n")
        sh(["git", "add", "-A"], team)
        sh(["git", "commit", "-q", "-m", "the team adds the list"], team)
        sh(["git", "push", "-q", "origin", "main"], team)
        sh(["git", "fetch", "-q", "origin"], self.f.wt)

    def commit_own_images(self):
        self.put_file("home.png", b"the builder's own screenshot")
        sh(["git", "add", "-f", DIR], self.f.wt)
        sh(["git", "commit", "-q", "-m", "work"], self.f.wt)

    def test_it_counts_as_lost(self):
        self.commit_own_images()
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("is gone", self.checks_md())
        self.assertFalse(self.ran_checks())
        self.f.save_job(stage="pr", gate="PASS")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 3, res)
        self.assertIn("merge that branch", res["error"])

    def test_moving_the_ref_back_does_not_help(self):
        self.commit_own_images()
        old = sh(["git", "merge-base", "origin/main", "HEAD"], self.f.wt).strip()
        sh(["git", "update-ref", "refs/remotes/origin/main", old], self.f.wt)
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("is gone", self.checks_md())

    def test_merging_the_base_is_enough(self):
        self.commit_own_images()
        sh(["git", "rm", "-rq", "--cached", DIR], self.f.wt)
        sh(["git", "commit", "-q", "-m", "not those"], self.f.wt)
        sh(["git", "merge", "-q", "--no-edit", "origin/main"], self.f.wt)
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())


class ListHiddenFromGitTest(ProjectRepoCase):
    def commit_a_new_list_and_hide_it(self, how):
        old = slurp(self.list_path)
        self.s3.put(C)
        write(self.list_path, fx.baselines_text(entries({"home.png": C})))
        sh(["git", "add", MANIFEST], self.f.wt)
        sh(["git", "commit", "-q", "-m", "a list the checks must not use"], self.f.wt)
        sh(["git", "update-index", how, MANIFEST], self.f.wt)
        write(self.list_path, old)

    def assert_stopped(self):
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("git is told not to look", self.checks_md())
        self.f.save_job(stage="pr", gate="PASS")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 3, res)
        self.assertIn("git is told not to look", res["error"])

    def test_skip_worktree(self):
        self.commit_a_new_list_and_hide_it("--skip-worktree")
        self.assert_stopped()

    def test_assume_unchanged(self):
        self.commit_a_new_list_and_hide_it("--assume-unchanged")
        self.assert_stopped()


class ProjectListCopyKeptTest(ProjectRepoCase):
    def test_an_untracked_ignored_copy_does_not_count(self):
        sh(["git", "rm", "-q", "--cached", MANIFEST], self.f.wt)
        with open(os.path.join(self.f.wt, ".gitignore"), "a") as f:
            f.write(MANIFEST + "\n")
        sh(["git", "add", ".gitignore"], self.f.wt)
        sh(["git", "commit", "-q", "-m", "stop tracking the list"], self.f.wt)
        self.assertTrue(os.path.exists(self.list_path))
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("is gone", self.checks_md())
        self.f.save_job(stage="pr", gate="PASS")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 3, res)
        self.assertEqual(sh(["git", "ls-remote", "--heads", self.f.origin, self.f.branch], self.tmp), "")


class ProjectListCannotBeRemovedTest(ProjectRepoCase):
    def test_a_commit_that_removes_the_list(self):
        p = os.path.join(self.folder, "home.png")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"the builder's own screenshot")
        sh(["git", "rm", "-q", MANIFEST], self.f.wt)
        sh(["git", "add", "-f", DIR], self.f.wt)
        sh(["git", "commit", "-q", "-m", "images back in git"], self.f.wt)
        code, res = self.gate()
        self.assertEqual(res["gate"], "FAIL", self.checks_md())
        self.assertIn("is gone", self.checks_md())
        self.f.save_job(stage="pr", gate="PASS")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 3, res)
        self.assertEqual(sh(["git", "ls-remote", "--heads", self.f.origin, self.f.branch], self.tmp), "")


class ProjectMigrateTest(ProjectRepoCase):
    base_files = {"README.md": "project\n", DIR + "/home.png": A.decode(), DIR + "/sub/menu.png": B.decode()}
    images = {}

    def test_the_migration_is_a_commit_on_the_job_branch_and_fx_pr_pushes_it(self):
        code, res = self.bl("migrate", "--commit")
        self.assertEqual(code, 0, res)
        self.assertEqual(sh(["git", "ls-files", "--", DIR], self.f.wt), "")
        self.assertIn(MANIFEST, sh(["git", "show", "--name-only", "--format=", "HEAD"], self.f.wt))
        self.assertEqual(sh(["git", "ls-remote", "--heads", self.f.origin, self.f.branch], self.tmp), "", "not pushed")
        self.assertEqual(self.s3.objects[self.s3.key_of(A)], A)
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.f.save_job(stage="pr")
        code, res = self.fx("pr", "T-1", "--no-secret-scan")
        self.assertEqual(code, 0, res)
        self.assertEqual(sh(["git", "ls-tree", "-r", "--name-only", self.f.branch, DIR], self.f.origin), "")

    def test_a_branch_before_the_migration_uses_the_images_in_git(self):
        code, res = self.gate()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertIn("still in git", self.checks_md())
        self.assertEqual(self.s3.log, [])


class ProjectCloneTest(ProjectRepoCase):
    def make_clone(self, name):
        path = os.path.join(self.tmp, "proj-factory-work", name)
        sh(["git", "clone", "-q", self.f.wt, path], self.tmp)
        return path

    def test_a_package_records_pushes_and_its_images_reach_the_job(self):
        self.bl("sync")
        clone = self.make_clone("T-1-wp1")
        code, res = self.fx("baselines", "sync", "--clone", clone)
        self.assertEqual(code, 0, res)
        with open(os.path.join(clone, DIR, "home.png"), "wb") as f:
            f.write(C)
        code, res = self.fx("baselines", "record", "--clone", clone)
        self.assertEqual(code, 0, res)
        code, res = self.fx("baselines", "push", "--clone", clone, "--commit")
        self.assertEqual(code, 0, res)
        sh(["git", "pull", "-q", clone, "HEAD"], self.f.wt)
        fx.baselines_take_images(self.f.ctx(), clone, self.f.wt)
        self.assertEqual(self.on_disk(), C)

    def test_builder_prompt_in_a_clone_says_to_push_before_committing(self):
        lines = fx.baselines_prompt_lines(self.f.ctx(), self.f.job(), "builder", None, {"clone": self.f.wt + "-wp1"})
        self.assertIn("--clone %s-wp1" % self.f.wt, lines[0])
        self.assertIn("baselines push", lines[0])
        lines = fx.baselines_prompt_lines(self.f.ctx(), self.f.job(), "builder", None, None)
        self.assertIn("--job T-1", lines[0])
        self.assertIn("Leave the list uncommitted", lines[0])

    def test_pick_brings_the_winner_images_along(self):
        self.bl("sync")
        clone = self.make_clone("T-1-c1")
        sh(["git", "checkout", "-q", "-b", self.f.branch + "-c1"], clone)
        self.fx("baselines", "sync", "--clone", clone)
        with open(os.path.join(clone, DIR, "home.png"), "wb") as f:
            f.write(C)
        self.fx("baselines", "record", "--clone", clone)
        self.fx("baselines", "push", "--clone", clone, "--commit")
        self.f.save_job(best_of=2, candidates=[{"k": 1, "worktree": clone, "branch": self.f.branch + "-c1", "port": 5103,
                                                 "setup": True}])
        code, res = self.fx("pick", "T-1", "--k", "1")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.on_disk(), C)


# --------------------------------------------------------------------------
# settings, doctor, install, init, signature

class SettingsTest(BaselinesCase):
    def bad(self, **change):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["baselines"].update(change.get("top", {}))
        cfg["storage"].update(change.get("store", {}))
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        code, res = self.bl("status")
        self.assertEqual(code, 3, change)
        return res["error"]

    def test_bad_settings_are_refused(self):
        self.assertIn("baselines.dir", self.bad(top={"dir": "../out"}))
        self.assertIn("baselines.dir", self.bad(top={"dir": "/abs", "manifest": MANIFEST}))
        self.assertIn("baselines.manifest", self.bad(top={"dir": DIR, "manifest": "../x.json"}))
        self.assertIn("inside baselines.dir", self.bad(top={"manifest": DIR + "/list.json"}))
        self.assertIn("baselines.repo", self.bad(top={"manifest": MANIFEST, "repo": "elsewhere"}))

    def test_bad_store_settings_are_refused(self):
        self.assertIn("storage.type", self.bad(store={"type": "gcs"}))
        self.assertIn("bucket", self.bad(store={"type": "s3", "bucket": ""}))
        self.assertIn("endpoint", self.bad(store={"bucket": BUCKET, "endpoint": "ftp://x"}))
        self.assertIn("region", self.bad(store={"endpoint": "", "region": ""}))

    def test_aws_addressing(self):
        cfg = fx.baselines_config(self.f.ctx())
        cfg["store"].update(endpoint="", region="eu-west-1", bucket="plain", prefix="p")
        store = fx.S3Store(cfg, {"id": "x", "secret": "y"})
        url, host, uri = store.where("p/sha256/ab/abc")
        self.assertEqual(url, "https://plain.s3.eu-west-1.amazonaws.com/p/sha256/ab/abc")
        self.assertEqual(host, "plain.s3.eu-west-1.amazonaws.com")
        cfg["store"]["bucket"] = "with.dots"
        url, host, uri = fx.S3Store(cfg, {"id": "x", "secret": "y"}).where("k")
        self.assertEqual(url, "https://s3.eu-west-1.amazonaws.com/with.dots/k")

    def test_empty_prefix(self):
        cfg = fx.baselines_config(self.f.ctx())
        cfg["store"]["prefix"] = ""
        self.assertEqual(fx.S3Store(cfg, {}).object_key("ab" + "0" * 62), "baselines/sha256/ab/ab" + "0" * 62)

    def test_no_storage_block_asks_for_it(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        del cfg["storage"]
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        code, res = self.bl("status")
        self.assertEqual(code, 3)
        self.assertIn("ask the user which S3", res["error"])

    def test_the_old_place_of_the_settings_is_refused(self):
        self.assertIn("top-level storage", self.bad(top={"store": {"bucket": "x"}}))

    def test_a_bad_folder_is_refused(self):
        self.assertIn("baselines.folder", self.bad(top={"folder": "../other-project"}))


class FoldersTest(BaselinesCase):
    """One place per project, one folder per part of the factory."""

    def set_folder(self, name):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["baselines"]["folder"] = name
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))

    def test_baselines_stay_in_their_own_folder(self):
        self.bl("sync")
        self.put_file("home.png", C)
        self.bl("record")
        self.bl("push")
        self.assertTrue(all(k.startswith("team/proj/baselines/sha256/") for k in self.s3.objects), sorted(self.s3.objects))

    def test_another_folder_is_another_place_and_another_cache(self):
        before = fx.baselines_cache_path(fx.baselines_config(self.f.ctx()), sha(A))
        self.set_folder("screens/web")
        cfg = fx.baselines_config(self.f.ctx())
        self.assertEqual(fx.S3Store(cfg, {}).object_key("ab" + "0" * 62),
                         "team/proj/screens/web/sha256/ab/ab" + "0" * 62)
        self.assertNotEqual(fx.baselines_cache_path(cfg, sha(A)), before)
        code, res = self.bl("sync")
        self.assertEqual(code, 3, "nothing is in the new folder yet")

    def test_a_part_of_the_storage_does_not_read_another_part(self):
        self.s3.objects["team/proj/reports/sha256/%s/%s" % (sha(C)[:2], sha(C))] = C
        write(self.list_path, fx.baselines_text(entries({"home.png": C})))
        code, res = self.bl("sync")
        self.assertEqual(code, 3)
        self.assertIn("not on S3", res["error"])


class OwnStorageTest(BaselinesCase):
    def other_factory(self, storage):
        other = os.path.join(self.tmp, "other-factory")
        write(os.path.join(other, "factory.json"), json.dumps({"storage": storage}))
        write(os.path.join(self.tmp, "state", "factories.json"), json.dumps([self.f.factory, other]))
        return other

    def doctor_check(self):
        return self.check(self.fx("doctor")[1], "storage is this project's own")

    def test_doctor_fails_when_two_factories_share_a_place(self):
        other = self.other_factory({"bucket": BUCKET, "region": "x", "prefix": PREFIX + "/",
                                    "endpoint": "http://127.0.0.1:%d/" % self.s3.port})
        check = self.doctor_check()
        self.assertFalse(check["ok"])
        self.assertIn(other, check["detail"])

    def test_another_prefix_or_bucket_is_its_own(self):
        self.other_factory({"bucket": BUCKET, "region": "x", "prefix": "team/other",
                            "endpoint": "http://127.0.0.1:%d" % self.s3.port})
        self.assertTrue(self.doctor_check()["ok"])
        self.other_factory({"bucket": "another", "region": "x", "prefix": PREFIX,
                            "endpoint": "http://127.0.0.1:%d" % self.s3.port})
        self.assertTrue(self.doctor_check()["ok"])

    def test_this_factory_alone_is_its_own(self):
        write(os.path.join(self.tmp, "state", "factories.json"), json.dumps([self.f.factory]))
        self.assertTrue(self.doctor_check()["ok"])


class SignatureTest(unittest.TestCase):
    def test_aws_published_example(self):
        """The GET Object example of the AWS Signature Version 4 documentation."""
        headers = {"Host": "examplebucket.s3.amazonaws.com", "Range": "bytes=0-9",
                   "x-amz-content-sha256": fx.EMPTY_SHA256, "x-amz-date": "20130524T000000Z"}
        signed, scope, sig = fx.s3_signature("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1", "20130524T000000Z",
                                             "GET", "/test.txt", "", headers, fx.EMPTY_SHA256)
        self.assertEqual(signed, "host;range;x-amz-content-sha256;x-amz-date")
        self.assertEqual(scope, "20130524/us-east-1/s3/aws4_request")
        self.assertEqual(sig, "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


class DoctorTest(BaselinesCase):
    def test_all_good(self):
        code, res = self.fx("doctor")
        for name in ("baselines settings", "storage read key", "storage write key", "storage read key file private", "storage is this project's own",
                     "baselines folder git-ignored", "baselines images out of git", "baselines list",
                     "baselines store readable", "baselines images on S3"):
            self.assertTrue(self.check(res, name)["ok"], (name, res["checks"]))

    def test_no_read_key(self):
        os.remove(self.key_path("read"))
        code, res = self.fx("doctor")
        self.assertFalse(self.check(res, "storage read key")["ok"])

    def test_no_write_key_is_only_advice(self):
        os.remove(self.key_path("write"))
        code, res = self.fx("doctor")
        self.assertTrue(self.check(res, "storage write key (advice: only uploads need it)")["ok"])

    def test_no_access(self):
        write(self.key_path("read"),
              "AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=wrong\n" % READ_ID)
        os.chmod(self.key_path("read"), 0o600)
        code, res = self.fx("doctor")
        check = self.check(res, "baselines store readable")
        self.assertFalse(check["ok"])
        self.assertIn("HTTP 403", check["detail"])
        self.assertNotIn("wrong", check["detail"])

    def test_store_down(self):
        self.s3.stop()
        code, res = self.fx("doctor")
        self.assertFalse(self.check(res, "baselines store readable")["ok"])

    def test_loose_key_file(self):
        os.chmod(self.key_path("read"), 0o644)
        code, res = self.fx("doctor")
        self.assertFalse(self.check(res, "storage read key file private")["ok"])

    def test_key_file_that_git_would_take(self):
        old = os.path.join(self.f.factory, "local", "s3-read.env")
        os.makedirs(os.path.dirname(old))
        os.replace(self.key_path("read"), old)
        write(os.path.join(self.f.factory, ".gitignore"), "jobs/\n" + DIR + "/\n")
        code, res = self.fx("doctor")
        self.assertFalse(self.check(res, "storage read key file private")["ok"])

    def test_folder_not_ignored(self):
        write(os.path.join(self.f.factory, ".gitignore"), "jobs/\nlocal/\n")
        code, res = self.fx("doctor")
        self.assertFalse(self.check(res, "baselines folder git-ignored")["ok"])

    def test_images_still_tracked(self):
        self.fill()
        write(os.path.join(self.f.factory, ".gitignore"), "jobs/\nlocal/\n")
        sh(["git", "add", "-A"], self.f.factory)
        sh(["git", "commit", "-q", "-m", "images in git"], self.f.factory)
        code, res = self.fx("doctor")
        check = self.check(res, "baselines images out of git")
        self.assertFalse(check["ok"])
        self.assertIn("2 still tracked", check["detail"])

    def test_bad_settings(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["storage"]["bucket"] = ""
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        code, res = self.fx("doctor")
        self.assertFalse(self.check(res, "baselines settings")["ok"])

    def test_behind_is_advice(self):
        self.teammate_changes({"home.png": C})
        code, res = self.fx("doctor")
        self.assertIn("1 behind", self.check(res, "baselines repository up to date (advice)")["detail"])


class InstallAndInitTest(BaselinesCase):
    def test_install_declares_the_aws_cli_only_with_a_baselines_block(self):
        code, res = self.fx("install", "status")
        tool = next(t for t in res["tools"] if t["name"] == "aws")
        self.assertEqual(tool["need"], "optional", "with key files fx needs no CLI")

    def test_init_keeps_the_folder_out_of_git(self):
        write(os.path.join(self.f.factory, ".gitignore"), "jobs/\n")
        code, res = self.fx("init", "--project", self.f.origin)
        self.assertEqual(code, 0, res)
        have = slurp(os.path.join(self.f.factory, ".gitignore")).splitlines()
        self.assertIn(DIR + "/", have)
        self.assertIn("local/", have)

    def test_template_has_docs_but_no_active_block(self):
        template = load_json(os.path.join(os.path.dirname(FX_PATH), "templates", "config.json"))
        self.assertNotIn("baselines", template)
        self.assertNotIn("storage", template)
        self.assertIn("_doc_baselines", template)
        self.assertIn("_doc_storage", template)


if __name__ == "__main__":
    unittest.main()
