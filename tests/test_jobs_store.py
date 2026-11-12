"""Tests for the jobs on S3 (standard library only). Run from the repository root:
    python3 -I -m unittest discover -s tests

The fake S3 server and the factory of test_baselines.py, without its baselines block unless a test adds one. A push, a
pull and everything between them is real, and only the server is not.
"""

import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import unittest
import zipfile

from test_baselines import BUCKET, PREFIX, READ_ID, READ_SECRET, WRITE_ID, WRITE_SECRET, FakeS3, sha
from test_security import FxTestCase, fx, load_json, sh, slurp, write


def store_cfg(s3, prefix=PREFIX, folder="jobs"):
    return {"folder": folder,
            "store": {"bucket": BUCKET, "region": "eu-west-1", "endpoint": "http://127.0.0.1:%d" % s3.port, "prefix": prefix,
                      "read_profile": "", "write_profile": "", "timeout_sec": 5}}


READ = {"id": READ_ID, "secret": READ_SECRET, "token": ""}
WRITE = {"id": WRITE_ID, "secret": WRITE_SECRET, "token": ""}


# --------------------------------------------------------------------------
# J1. the store writes by name and lists

class NamedObjectsTest(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.addCleanup(self.s3.stop)
        self.reader = fx.S3Store(store_cfg(self.s3), READ)
        self.writer = fx.S3Store(store_cfg(self.s3), WRITE)

    def test_a_name_goes_in_the_part_s_folder_and_comes_back(self):
        self.assertTrue(self.writer.put_named("snapshots/T-1/a.json", b"one"))
        self.assertEqual(self.s3.objects, {PREFIX + "/jobs/snapshots/T-1/a.json": b"one"})
        r = self.reader.open_named("snapshots/T-1/a.json")
        self.assertEqual(r.read(), b"one")
        r.close()

    def test_a_name_is_never_overwritten(self):
        self.writer.put_named("snapshots/T-1/a.json", b"one")
        self.assertFalse(self.writer.put_named("snapshots/T-1/a.json", b"two"))
        self.assertEqual(self.s3.objects[PREFIX + "/jobs/snapshots/T-1/a.json"], b"one")

    def test_a_name_that_is_not_there(self):
        self.assertIsNone(self.reader.open_named("snapshots/none.json"))

    def test_the_read_key_cannot_write_by_name(self):
        with self.assertRaises(fx.StoreError) as e:
            self.reader.put_named("snapshots/x.json", b"one")
        self.assertEqual(e.exception.kind, "denied")

    def test_the_hash_objects_are_where_they_were(self):
        self.writer.put(sha(b"x"), b"x")
        self.assertEqual(self.reader.head(sha(b"x")), 1)
        self.assertIn("%s/jobs/sha256/%s/%s" % (PREFIX, sha(b"x")[:2], sha(b"x")), self.s3.objects)


class ListingTest(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.addCleanup(self.s3.stop)
        self.reader = fx.S3Store(store_cfg(self.s3), READ)

    def fill(self, names, prefix=PREFIX):
        for n in names:
            self.s3.objects["%s/jobs/%s" % (prefix, n)] = b"data"

    def test_it_lists_a_prefix_with_a_signed_query(self):
        self.fill(["snapshots/T-1/a.json", "snapshots/T-2/b.json", "sha256/aa/x"])
        self.assertEqual(self.reader.list("snapshots/T-1/"), [{"name": "snapshots/T-1/a.json", "size": 4}])
        self.assertEqual(self.reader.list("snapshots/"), [{"name": "snapshots/T-1/a.json", "size": 4},
                                                          {"name": "snapshots/T-2/b.json", "size": 4}])
        self.assertEqual({c["id"] for c in self.s3.calls("LIST")}, {READ_ID})

    def test_it_reads_every_page(self):
        names = ["snapshots/T-%02d/a.json" % i for i in range(23)]
        self.fill(names)
        self.s3.page_size = 5
        self.assertEqual([x["name"] for x in self.reader.list("snapshots/")], names)
        self.assertEqual(len(self.s3.calls("LIST")), 5)

    def test_a_limit_stops_after_one_page(self):
        self.fill(["snapshots/T-%02d/a.json" % i for i in range(9)])
        self.assertEqual(len(self.reader.list("snapshots/", limit=2)), 2)
        self.assertEqual(len(self.s3.calls("LIST")), 1)

    def test_a_name_with_characters_xml_escapes(self):
        self.fill(["snapshots/a&b/ü <x>.json"])
        self.assertEqual(self.reader.list("snapshots/")[0]["name"], "snapshots/a&b/ü <x>.json")

    def test_no_prefix_in_the_storage(self):
        self.s3.objects["jobs/snapshots/a.json"] = b"data"
        self.s3.objects["other/jobs/snapshots/b.json"] = b"data"
        store = fx.S3Store(store_cfg(self.s3, prefix=""), READ)
        self.assertEqual([x["name"] for x in store.list("snapshots/")], ["snapshots/a.json"])

    def test_a_dead_end_is_an_error_and_not_a_short_list(self):
        self.fill(["snapshots/a.json"])
        body = (b'<ListBucketResult><IsTruncated>true</IsTruncated><Contents><Key>%s/jobs/snapshots/a.json</Key>'
                b'<Size>1</Size></Contents></ListBucketResult>' % PREFIX.encode())

        class Reply:
            code = 200

            def read(self):
                return body

            def close(self):
                pass
        self.reader.send = lambda *a, **k: Reply()
        with self.assertRaises(fx.StoreError):
            self.reader.list("snapshots/")

    def test_a_bucket_that_is_not_there(self):
        self.reader.send = lambda *a, **k: None
        with self.assertRaises(fx.StoreError):
            self.reader.list("snapshots/")

    def test_the_server_fails(self):
        self.s3.fail_status = 503
        with self.assertRaises(fx.StoreError) as e:
            self.reader.list("snapshots/")
        self.assertEqual(e.exception.kind, "server")


class StorageWithoutBaselinesTest(FxTestCase):
    def set_storage(self, **kw):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["storage"] = kw
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))

    def test_the_block_stands_alone(self):
        self.set_storage(bucket="b", region="eu-west-1", prefix="p/")
        problems = []
        st = fx.storage_config(self.f.ctx(), problems)
        self.assertEqual(problems, [])
        self.assertEqual((st["bucket"], st["prefix"]), ("b", "p"))
        self.assertIsNone(fx.baselines_config(self.f.ctx()))

    def test_what_is_wrong_is_named(self):
        self.set_storage(type="gcs", endpoint="not a url", prefix="../x")
        problems = []
        fx.storage_config(self.f.ctx(), problems)
        text = " ".join(problems)
        for word in ("storage.type", "storage.bucket", "storage.endpoint", "storage.prefix"):
            self.assertIn(word, text)


# --------------------------------------------------------------------------
# J2. settings and keys

JOBS_ID, JOBS_SECRET = "JOBSKEYIDFORTEST0001", "jobs-secret-value-for-tests-0001"


class JobsStoreCase(FxTestCase):
    """A factory repository with a storage and a jobs_store block, a read and a write key, and no baselines."""
    settings = {}
    read_key, write_key, jobs_key = True, True, False

    def setUp(self):
        super().setUp()
        self.s3 = FakeS3()
        self.addCleanup(self.s3.stop)
        self.s3.keys[JOBS_ID] = (JOBS_SECRET, "jobs-write")
        self.configure(jobs_store=dict(self.settings))
        if self.read_key:
            self.put_key("read", READ_ID, READ_SECRET)
        if self.write_key:
            self.put_key("write", WRITE_ID, WRITE_SECRET)
        if self.jobs_key:
            self.put_key("jobs-write", JOBS_ID, JOBS_SECRET)

    def configure(self, **blocks):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["storage"] = {"type": "s3", "bucket": BUCKET, "region": "eu-west-1",
                          "endpoint": "http://127.0.0.1:%d" % self.s3.port, "prefix": PREFIX}
        cfg.update(blocks)
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg, indent=1))

    def key_path(self, mode):
        return os.path.join(fx.baselines_key_dir(self.f.ctx()), "s3-%s.env" % mode)

    def put_key(self, mode, key_id, secret):
        path = self.key_path(mode)
        write(path, "AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\n" % (key_id, secret))
        os.chmod(path, 0o600)


class NoJobsStoreTest(FxTestCase):
    def test_the_commands_say_it_is_not_in_use(self):
        for action in ("push", "status", "list", "pull"):
            code, res = self.fx("jobs", action)
            self.assertEqual(code, 3)
            self.assertIn("no jobs_store block", res["error"])

    def test_nothing_changes_without_the_block(self):
        self.assertIsNone(fx.jobs_store_config(self.f.ctx()))
        code, res = self.fx("doctor")
        self.assertFalse([c for c in res["checks"] if "jobs store" in c["check"]])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "state", "jobs-store")))

    def test_where_names_the_keys_folder_without_any_storage(self):
        code, res = self.fx("where")
        self.assertEqual(code, 0, res)
        ctx = self.f.ctx()
        self.assertIsNone(ctx.c("storage"))
        self.assertEqual(res["keys_folder"], fx.baselines_key_dir(ctx))
        self.assertEqual(os.path.dirname(fx.jira_env_path(ctx)), res["keys_folder"])
        self.assertTrue(res["keys_folder"].startswith(fx.keys_root() + os.sep))


class StorageWithoutJobsStoreTest(FxTestCase):
    """With S3, jobs always go there: a storage block without jobs_store is S3 set up halfway."""

    def setUp(self):
        super().setUp()
        self.s3 = FakeS3()
        self.addCleanup(self.s3.stop)
        self.settings(storage=True, jobs_store=False)

    def settings(self, storage, jobs_store):
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg.pop("storage", None)
        cfg.pop("jobs_store", None)
        if storage:
            cfg["storage"] = {"type": "s3", "bucket": BUCKET, "region": "eu-west-1",
                              "endpoint": "http://127.0.0.1:%d" % self.s3.port, "prefix": PREFIX}
        if jobs_store:
            cfg["jobs_store"] = {}
        write(path, json.dumps(cfg, indent=1))

    def doctor(self):
        code, res = self.fx("doctor")
        return res, [c for c in res["checks"] if c["check"] == "jobs store settings"]

    def test_doctor_fails_and_says_what_to_do(self):
        res, rows = self.doctor()
        self.assertEqual(len(rows), 1, res["checks"])
        self.assertFalse(rows[0]["ok"])
        self.assertFalse(res["ok"])
        self.assertIn("S3 is set up but jobs are not kept there", rows[0]["detail"])
        self.assertIn("add jobs_store with /factory init, or remove storage", rows[0]["detail"])

    def test_the_jobs_commands_say_so_too(self):
        for action in ("push", "status"):
            code, res = self.fx("jobs", action)
            self.assertEqual(code, 3)
            self.assertIn("no jobs_store block", res["error"])
            self.assertIn("S3 is set up but jobs are not kept there", res["error"])

    def test_both_blocks_pass(self):
        self.settings(storage=True, jobs_store=True)
        res, rows = self.doctor()
        self.assertTrue(rows[0]["ok"], rows)
        self.assertNotIn("S3 is set up", json.dumps(res))

    def test_neither_block_says_nothing_of_it(self):
        self.settings(storage=False, jobs_store=False)
        res, rows = self.doctor()
        self.assertEqual(rows, [])
        self.assertNotIn("S3 is set up", json.dumps(res))
        code, res = self.fx("jobs", "push")
        self.assertEqual(res["error"], "this factory has no jobs_store block in factory.json, so its jobs stay on this "
                                       "machine")

    def test_where_names_the_folder_the_s3_keys_are_read_from(self):
        self.assertEqual(self.fx("where")[1]["keys_folder"], fx.baselines_key_dir(self.f.ctx()))
        self.assertEqual(os.path.dirname(fx.baselines_key_file(self.f.ctx(), "read")), fx.baselines_key_dir(self.f.ctx()))


class JobsSettingsTest(JobsStoreCase):
    def problems(self, **block):
        self.configure(jobs_store=block)
        with self.assertRaises(fx.FxError) as e:
            fx.jobs_store_config(self.f.ctx())
        self.assertEqual(e.exception.code, 3)
        return str(e.exception)

    def test_the_defaults(self):
        self.configure(jobs_store={"auto": True})
        cfg = fx.jobs_store_config(self.f.ctx())
        self.assertEqual((cfg["folder"], cfg["auto"], cfg["include_audits"], cfg["max_bytes"]),
                         ("jobs", True, False, 50 * 1024 * 1024))
        self.assertIn("**/node_modules/", cfg["exclude"])

    def test_it_works_without_a_baselines_block(self):
        self.configure(jobs_store={"auto": False})
        self.assertIsNone(fx.baselines_config(self.f.ctx()))
        self.assertEqual(fx.jobs_store_config(self.f.ctx())["store"]["bucket"], BUCKET)

    def test_an_unsafe_folder(self):
        self.assertIn("jobs_store.folder", self.problems(folder="../other"))

    def test_the_baselines_folder_is_not_its_own(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["baselines"] = {"dir": "qa/b", "manifest": "qa/b.lock.json"}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.assertIn("baselines' folder", self.problems(folder="baselines"))

    def test_a_string_is_not_a_yes(self):
        self.assertIn("jobs_store.include_audits", self.problems(include_audits="false"))
        self.assertIn("jobs_store.auto", self.problems(auto="no"))

    def test_exclude_and_size(self):
        self.assertIn("jobs_store.exclude", self.problems(exclude="node_modules"))
        self.assertIn("jobs_store.max_file_mb", self.problems(max_file_mb=0))
        self.assertIn("jobs_store.max_file_mb", self.problems(max_file_mb=True))

    def test_the_block_must_be_an_object(self):
        self.configure(jobs_store="yes")
        with self.assertRaises(fx.FxError):
            fx.jobs_store_config(self.f.ctx())

    def test_the_sandbox_stays_out_whatever_exclude_says(self):
        self.configure(jobs_store={"exclude": []})
        self.assertEqual(fx.jobs_store_config(self.f.ctx())["exclude"], ["_audits/*/sandbox/"])

    def test_a_storage_problem_is_named(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["storage"]["bucket"] = ""
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        with self.assertRaises(fx.FxError) as e:
            fx.jobs_store_config(self.f.ctx())
        self.assertIn("storage.bucket", str(e.exception))


class JobsKeysTest(JobsStoreCase):
    def stores(self, **kw):
        ctx = self.f.ctx()
        return fx.jobs_stores(ctx, fx.jobs_store_config(ctx), **kw)

    def test_the_shared_write_key_without_one_of_its_own(self):
        reader, writer = self.stores()
        self.assertEqual((reader.creds["id"], writer.creds["id"]), (READ_ID, WRITE_ID))

    def test_its_own_key_comes_first(self):
        self.put_key("jobs-write", JOBS_ID, JOBS_SECRET)
        reader, writer = self.stores()
        self.assertEqual((reader.creds["id"], writer.creds["id"]), (READ_ID, JOBS_ID))

    def test_its_own_profile_comes_first(self):
        creds = os.path.join(self.tmp, "aws-credentials")
        write(creds, "[jobs]\naws_access_key_id = %s\naws_secret_access_key = %s\n" % (JOBS_ID, JOBS_SECRET))
        os.environ["AWS_SHARED_CREDENTIALS_FILE"] = creds
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["storage"]["jobs_write_profile"] = "jobs"
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.assertEqual(self.stores()[1].creds["id"], JOBS_ID)

    def test_the_key_is_redacted_like_the_others(self):
        self.put_key("jobs-write", JOBS_ID, JOBS_SECRET)
        self.assertIn(JOBS_SECRET, fx.local_secret_values(self.f.ctx()))

    def test_it_reaches_no_environment(self):
        self.put_key("jobs-write", JOBS_ID, JOBS_SECRET)
        self.stores()
        self.assertNotIn(JOBS_SECRET, " ".join(os.environ.values()))

    def test_no_key_at_all_says_where_to_put_it(self):
        os.remove(self.key_path("write"))
        with self.assertRaises(fx.StoreError) as e:
            self.stores()
        self.assertEqual(e.exception.kind, "credentials")
        self.assertIn("s3-write.env", str(e.exception))

    def test_reading_needs_no_write_key(self):
        os.remove(self.key_path("write"))
        reader, writer = self.stores(write=False)
        self.assertEqual(reader.creds["id"], READ_ID)
        self.assertIsNone(writer)


# --------------------------------------------------------------------------
# J3. fx jobs push

class PushCase(JobsStoreCase):
    def setUp(self):
        super().setUp()
        self.write_job("T-1", {"issue.md": "the issue\n", "round-1/review-code.md": "VERDICT PASS\n"})

    def write_job(self, name, files):
        for rel, text in files.items():
            write(os.path.join(self.f.factory, "jobs", name, rel), text)

    def push(self, *argv):
        return self.fx("jobs", "push", *argv)

    def snapshots(self, name="T-1"):
        start = "%s/jobs/snapshots/%s/" % (PREFIX, name)
        return sorted(k for k in self.s3.objects if k.startswith(start))

    def snapshot(self, name="T-1", index=-1):
        return json.loads(self.s3.objects[self.snapshots(name)[index]])

    def content_keys(self):
        return [k for k in self.s3.objects if "/jobs/sha256/" in k]


class PushTest(PushCase):
    def test_files_and_a_snapshot_go_up(self):
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["jobs"][0]["state"], "pushed")
        snap = self.snapshot()
        self.assertEqual(snap["version"], 1)
        self.assertEqual(snap["job"], "T-1")
        self.assertEqual(snap["stage"], "build")
        self.assertEqual(snap["summary"], "test job")
        self.assertEqual(snap["factory_root"], self.f.factory)
        self.assertEqual(snap["fx_version"], fx.VERSION)
        paths = {f["path"]: f for f in snap["files"]}
        self.assertEqual(set(paths), {"issue.md", "job.json", "round-1/review-code.md"})
        h = sha(b"the issue\n")
        self.assertEqual(paths["issue.md"], {"path": "issue.md", "sha256": h, "size": 10})
        self.assertEqual(self.s3.objects["%s/jobs/sha256/%s/%s" % (PREFIX, h[:2], h)], b"the issue\n")

    def test_the_snapshot_is_written_last(self):
        self.push("T-1")
        puts = [c["key"] for c in self.s3.calls("PUT")]
        self.assertIn("/snapshots/", puts[-1])
        self.assertFalse([k for k in puts[:-1] if "/snapshots/" in k])

    def test_a_second_push_uploads_nothing_and_makes_no_snapshot(self):
        self.push("T-1")
        self.s3.log.clear()
        code, res = self.push("T-1")
        self.assertEqual(code, 0)
        self.assertEqual(res["jobs"][0]["state"], "unchanged")
        self.assertEqual(self.s3.log, [])
        self.assertEqual(len(self.snapshots()), 1)

    def test_without_this_machine_s_note_it_compares_with_the_newest_snapshot(self):
        self.push("T-1")
        shutil.rmtree(os.path.join(self.tmp, "state", "jobs-store"))
        self.s3.log.clear()
        code, res = self.push("T-1")
        self.assertEqual(res["jobs"][0]["state"], "unchanged")
        self.assertFalse(self.s3.calls("PUT"))
        self.assertEqual(len(self.snapshots()), 1)

    def test_a_change_uploads_only_what_is_new(self):
        self.push("T-1")
        self.s3.log.clear()
        self.write_job("T-1", {"spec.md": "the spec\n"})
        code, res = self.push("T-1")
        self.assertEqual(res["jobs"][0]["uploaded"], 1)
        self.assertEqual(len(self.s3.calls("PUT")), 2)  # the file and the snapshot
        self.assertEqual(len(self.snapshots()), 2)
        self.assertLess(self.snapshots()[0], self.snapshots()[1])  # even within one second

    def test_two_jobs_share_a_file_once(self):
        self.write_job("T-2", {"job.json": json.dumps({"id": "T-2", "stage": "intake"}), "issue.md": "the issue\n"})
        code, res = self.push("--all")
        self.assertEqual(code, 0, res)
        self.assertEqual(sum(1 for k in self.s3.objects if k.endswith(sha(b"the issue\n"))), 1)
        self.assertEqual(len(self.snapshots("T-2")), 1)

    def test_no_names_means_every_job(self):
        self.write_job("T-2", {"job.json": json.dumps({"id": "T-2", "stage": "intake"})})
        code, res = self.push()
        self.assertEqual({r["job"] for r in res["jobs"]}, {"T-1", "T-2"})

    def test_names_and_all_together(self):
        self.assertEqual(self.push("T-1", "--all")[0], 3)

    def test_what_stays_out_of_the_selection(self):
        for name in ("_selftest", "_baselines", ".incoming"):
            self.write_job(name, {"x.md": "x\n"})
        self.write_job("_learn", {"notes.md": "a lesson\n"})
        code, res = self.push("--all")
        self.assertEqual({r["job"] for r in res["jobs"]}, {"T-1", "_learn"})
        self.assertEqual(self.snapshot("_learn")["stage"], None)

    def test_a_dry_run_scans_and_uploads_nothing(self):
        code, res = self.push("T-1", "--dry-run")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["jobs"][0]["state"], "would-push")
        self.assertEqual(res["jobs"][0]["to_upload"], 3)
        self.assertEqual(self.s3.objects, {})
        self.assertFalse(self.s3.calls("PUT"))
        self.assertTrue(self.f.calls("gitleaks"))

    def test_a_dry_run_needs_no_write_key(self):
        os.remove(self.key_path("write"))
        self.assertEqual(self.push("T-1", "--dry-run")[0], 0)

    def test_s3_holding_other_bytes_for_a_hash_is_not_overwritten(self):
        h = sha(b"the issue\n")
        self.s3.objects["%s/jobs/sha256/%s/%s" % (PREFIX, h[:2], h)] = b"short"
        code, res = self.push("T-1")
        self.assertEqual(code, 3)
        self.assertEqual(self.snapshots(), [])
        self.assertEqual(self.s3.objects["%s/jobs/sha256/%s/%s" % (PREFIX, h[:2], h)], b"short")


class PushLeavesOutTest(PushCase):
    def reasons(self, name="T-1"):
        return {x["path"]: x["reason"] for x in self.snapshot(name)["skipped"]}

    def test_no_file_of_a_sandbox_goes_up(self):
        self.configure(jobs_store={"include_audits": True})
        self.write_job("_audits/run1", {"findings.json": "[]\n", "sandbox/app.py": "code\n",
                                        "sandbox/deep/er/lib.py": "more code\n"})
        code, res = self.push("--all")
        self.assertEqual(code, 0, res)
        snap = self.snapshot("_audits/run1")
        self.assertEqual([f["path"] for f in snap["files"]], ["findings.json"])
        self.assertEqual(self.reasons("_audits/run1"), {"sandbox/": "excluded"})
        self.assertFalse([k for k in self.s3.objects if k.endswith(sha(b"code\n"))])

    def test_a_sandbox_stays_out_even_with_an_empty_exclude(self):
        self.configure(jobs_store={"include_audits": True, "exclude": []})
        self.write_job("_audits/run1", {"findings.json": "[]\n", "sandbox/app.py": "code\n"})
        self.push("--all")
        self.assertEqual([f["path"] for f in self.snapshot("_audits/run1")["files"]], ["findings.json"])

    def test_excluded_folders_at_any_depth_are_listed_once(self):
        self.write_job("T-1", {"ui/node_modules/a/b.js": "x\n", "ui/node_modules/c.js": "y\n", "vendor/z.php": "z\n"})
        self.push("T-1")
        self.assertEqual(self.reasons(), {"ui/node_modules/": "excluded", "vendor/": "excluded"})

    def test_a_pattern_for_files(self):
        self.configure(jobs_store={"exclude": ["**/*.log"]})
        self.write_job("T-1", {"app.log": "x\n", "sub/b.log": "y\n", "keep.txt": "z\n"})
        self.push("T-1")
        self.assertEqual(self.reasons(), {"app.log": "excluded", "sub/b.log": "excluded"})

    def test_a_symlink_is_listed_and_not_followed(self):
        outside = os.path.join(self.tmp, "outside.txt")
        write(outside, "private\n")
        os.symlink(outside, os.path.join(self.f.jd, "link.txt"))
        os.symlink(self.tmp, os.path.join(self.f.jd, "linkdir"))
        self.push("T-1")
        self.assertEqual(self.reasons(), {"link.txt": "symlink", "linkdir": "symlink"})
        self.assertFalse([k for k in self.s3.objects if k.endswith(sha(b"private\n"))])

    def test_a_large_file_is_listed(self):
        self.configure(jobs_store={"max_file_mb": 0.001})
        self.write_job("T-1", {"big.bin": "x" * 2000})
        self.push("T-1")
        self.assertEqual(self.reasons(), {"big.bin": "larger than 0.001 MB"})

    def test_a_name_the_restore_would_refuse_is_listed_and_does_not_break_the_job(self):
        self.write_job("T-1", {"copy/.git/config": "x\n"})
        self.push("T-1")
        self.assertEqual(self.reasons(), {"copy/.git/config": "a name fx would not restore"})

    def test_a_temporary_file_of_fx_is_nothing(self):
        self.write_job("T-1", {".tmp-abc": "x\n"})
        self.push("T-1")
        self.assertEqual(self.reasons(), {})
        self.assertNotIn(".tmp-abc", [f["path"] for f in self.snapshot()["files"]])

    def test_audits_are_off_by_default(self):
        self.write_job("_audits/run1", {"summary.md": "the hole is here\n"})
        code, res = self.push("--all")
        self.assertEqual({r["job"] for r in res["jobs"]}, {"T-1"})
        self.assertFalse([k for k in self.s3.objects if "_audits" in k or k.endswith(sha(b"the hole is here\n"))])
        code, res = self.push("_audits/run1")
        self.assertEqual(code, 3)
        self.assertIn("include_audits", res["error"])


class PushSecretsTest(PushCase):
    SECRET = "s3cr3t-value-in-a-review"

    def setUp(self):
        super().setUp()
        self.write_job("T-1", {"leak.md": "the key is %s\n" % self.SECRET})
        os.environ["STUB_GITLEAKS_FIND"] = self.SECRET

    def test_the_file_stays_and_the_rest_goes(self):
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        snap = self.snapshot()
        self.assertNotIn("leak.md", [f["path"] for f in snap["files"]])
        self.assertIn({"path": "leak.md", "reason": "secret: stub-rule"}, snap["skipped"])
        self.assertIn("issue.md", [f["path"] for f in snap["files"]])
        for key, data in self.s3.objects.items():
            self.assertNotIn(self.SECRET.encode(), data, key)

    def test_the_value_is_nowhere_in_the_output(self):
        buf_code, res = self.push("T-1")
        self.assertNotIn(self.SECRET, json.dumps(res))
        self.assertIn("leak.md", json.dumps(res))
        self.assertIn("stub-rule", json.dumps(res))

    def test_gitleaks_reads_the_factory_s_rules_and_no_job_file_tunes_it(self):
        self.write_job("T-1", {".gitleaks.toml": "[allowlist]\npaths = ['.*']\n", ".gitleaksignore": "x\n"})
        self.push("T-1")
        call = self.f.calls("gitleaks")[-1]
        argv = call["argv"]
        self.assertEqual(argv[0], "dir")
        for flag in ("--ignore-gitleaks-allow", "--redact", "--gitleaks-ignore-path"):
            self.assertIn(flag, argv)
        self.assertNotIn("--config", argv)
        # What it scans holds the job's files one level down: gitleaks looks for its settings at the top only.
        self.assertEqual(argv[1], "scan")
        self.assertIn({"path": "leak.md", "reason": "secret: stub-rule"}, self.snapshot()["skipped"])

    def test_the_factory_s_own_config_is_used(self):
        write(os.path.join(self.f.factory, "gl.toml"), "# rules\n")
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"] = {"gitleaks_config": "gl.toml"}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.push("T-1")
        self.assertIn("--config", self.f.calls("gitleaks")[-1]["argv"])

    def test_a_local_secret_value_keeps_a_file_back_too(self):
        del os.environ["STUB_GITLEAKS_FIND"]
        self.put_key("jobs-write", JOBS_ID, JOBS_SECRET)
        self.write_job("T-1", {"note.md": "key %s\n" % JOBS_SECRET})
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        self.assertIn({"path": "note.md", "reason": "secret: local-secret"}, self.snapshot()["skipped"])
        self.assertFalse([k for k, d in self.s3.objects.items() if JOBS_SECRET.encode() in d])
        self.assertNotIn(JOBS_SECRET, json.dumps(res))

    def test_a_file_skipped_as_a_secret_does_not_read_as_a_change(self):
        self.push("T-1")
        self.s3.log.clear()
        self.assertEqual(self.push("T-1")[1]["jobs"][0]["state"], "unchanged")
        self.assertEqual(self.s3.log, [])


class PushScannerGoneTest(PushCase):
    def test_no_gitleaks_means_no_push(self):
        from test_security import not_on_this_machine
        with not_on_this_machine("gitleaks"):
            code, res = self.push("T-1")
        self.assertEqual(code, 3)
        self.assertIn("gitleaks", res["error"])
        self.assertEqual(self.s3.objects, {})

    def test_turned_off_in_the_gate_it_is_still_needed_here(self):
        from test_security import not_on_this_machine
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"] = {"scanners": {"gitleaks": False}}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        with not_on_this_machine("gitleaks"):
            self.assertEqual(self.push("T-1")[0], 3)
        self.assertEqual(self.s3.objects, {})

    def test_gitleaks_failing_means_no_push(self):
        os.environ["STUB_GITLEAKS"] = "fail"
        code, res = self.push("T-1")
        self.assertEqual(code, 3)
        self.assertIn("gitleaks failed", res["error"])
        self.assertEqual(self.s3.objects, {})

    def test_a_report_about_another_file_means_no_push(self):
        os.environ["STUB_GITLEAKS_REPORT"] = json.dumps([{"File": "scan/job/not-given.md", "RuleID": "x"}])
        code, res = self.push("T-1")
        self.assertEqual(code, 3)
        self.assertEqual(self.s3.objects, {})

    def test_an_unreadable_report_means_no_push(self):
        os.environ["STUB_GITLEAKS_REPORT"] = "not json"
        self.assertEqual(self.push("T-1")[0], 3)
        self.assertEqual(self.s3.objects, {})

    def test_the_scan_copy_is_gone_afterwards(self):
        import glob
        import tempfile
        self.push("T-1")
        self.assertEqual(glob.glob(os.path.join(tempfile.gettempdir(), "fx-jobs-scan-*")), [])


class JobJsonGoesTest(PushCase):
    KEY = "api-key-rotation.md"
    HASH = "3f2a9c1b7d4e6f8a0b1c2d3e4f5a6b7c8d9e0f1a"

    def test_logic_base_is_not_a_finding(self):
        self.f.save_job(logic_base={self.KEY: self.HASH})
        os.environ["STUB_GITLEAKS_FIND"] = self.KEY
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        # The scan left logic_base out, and what went up is the job as it is.
        self.assertIn("job.json", [f["path"] for f in self.snapshot()["files"]])
        with open(os.path.join(self.f.jd, "job.json"), "rb") as f:
            data = f.read()
        self.assertIn(self.KEY.encode(), data)
        self.assertIn(data, self.s3.objects.values())

    def test_a_real_secret_in_job_json_stops_the_job(self):
        self.f.save_job(summary="the key is hunter2-real-secret")
        os.environ["STUB_GITLEAKS_FIND"] = "hunter2-real-secret"
        code, res = self.push("T-1")
        self.assertEqual(code, 3)
        self.assertIn("job.json", res["error"])
        self.assertNotIn("hunter2", json.dumps(res))
        self.assertEqual(self.s3.objects, {})

    def test_a_secret_hidden_in_logic_base_is_not_waved_through(self):
        self.f.save_job(logic_base={self.KEY: "hunter2-real-secret"})
        os.environ["STUB_GITLEAKS_FIND"] = "hunter2-real-secret"
        self.assertEqual(self.push("T-1")[0], 3)
        self.assertEqual(self.s3.objects, {})

    def test_one_bad_job_does_not_stop_the_others(self):
        self.write_job("T-2", {"job.json": json.dumps({"id": "T-2", "summary": "hunter2-real-secret", "stage": "build"})})
        os.environ["STUB_GITLEAKS_FIND"] = "hunter2-real-secret"
        code, res = self.push("--all")
        self.assertEqual(code, 3)
        self.assertEqual([f["job"] for f in res["failed"]], ["T-2"])
        self.assertEqual([r["job"] for r in res["pushed"]], ["T-1"])
        self.assertTrue(self.snapshots("T-1"))
        self.assertFalse(self.snapshots("T-2"))

    def test_a_job_json_that_is_not_json(self):
        self.write_job("T-2", {"job.json": "{nope"})
        code, res = self.push("T-2")
        self.assertEqual(code, 3)
        self.assertIn("job.json", res["error"])


class PushBreaksOffTest(PushCase):
    def test_no_snapshot_and_the_next_run_goes_on(self):
        self.s3.fail_puts_after = 1
        code, res = self.push("T-1")
        self.assertEqual(code, 3)
        self.assertEqual(self.snapshots(), [])
        self.s3.fail_puts_after = None
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(len(self.snapshots()), 1)
        self.assertLessEqual(res["jobs"][0]["uploaded"], 2)
        self.assertEqual(len(self.content_keys()), 3)

    def test_a_snapshot_that_failed_leaves_no_note_on_this_machine(self):
        self.s3.fail_status = 503
        self.assertEqual(self.push("T-1")[0], 3)
        self.assertFalse(fx.jobs_read_record(fx.jobs_store_config(self.f.ctx()), "T-1"))

    def test_a_dead_store_is_asked_once(self):
        self.write_job("T-2", {"job.json": json.dumps({"id": "T-2", "stage": "build"})})
        self.s3.fail_status = 503
        code, res = self.push("--all")
        self.assertEqual(code, 3)
        self.assertEqual(len(res["failed"]), 2)
        self.assertTrue(res["failed"][1]["error"].startswith("not tried"))

    def test_the_error_holds_no_key(self):
        self.s3.echo_key = True
        self.s3.fail_status = 403
        code, res = self.push("T-1")
        self.assertEqual(code, 3)
        self.assertNotIn(READ_ID, json.dumps(res))
        self.assertNotIn(WRITE_ID, json.dumps(res))


class OwnKeyPushTest(PushCase):
    jobs_key = True

    def test_alone_it_cannot_read_and_s3_says_so(self):
        os.remove(self.key_path("read"))
        code, res = self.push("T-1")
        self.assertEqual(code, 3)
        self.assertIn("may not", res["error"])
        self.assertEqual(self.snapshots(), [])

    def test_uploads_use_its_own_key_and_reads_the_read_key(self):
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual({c["id"] for c in self.s3.calls("PUT")}, {JOBS_ID})
        self.assertEqual({c["id"] for c in self.s3.log if c["method"] != "PUT"}, {READ_ID})


# --------------------------------------------------------------------------
# J4. fx jobs list and status

class JobsStatusTest(PushCase):
    def status(self):
        code, res = self.fx("jobs", "status")
        self.assertEqual(code, 0, res)
        return res

    def state(self, name="T-1"):
        return next(r for r in self.status()["jobs"] if r["job"] == name)

    def test_a_job_never_pushed(self):
        res = self.status()
        self.assertEqual(res["unsaved"], ["T-1"])
        self.assertEqual(res["jobs"][0]["state"], "not-saved")

    def test_after_a_push_it_is_saved(self):
        self.push("T-1")
        row = self.state()
        self.assertEqual(row["state"], "saved")
        self.assertRegex(row["snapshot"], r"^\d{8}T\d{6}Z-[0-9a-f]{8}\.json$")
        self.assertEqual(self.status()["unsaved"], [])

    def test_a_changed_a_new_and_a_removed_file(self):
        self.write_job("T-1", {"old.md": "x\n"})
        self.push("T-1")
        self.write_job("T-1", {"issue.md": "changed\n", "new.md": "y\n"})
        os.remove(os.path.join(self.f.jd, "old.md"))
        row = self.state()
        self.assertEqual(row["state"], "changed")
        self.assertEqual(row["changed"], ["issue.md", "new.md", "old.md"])

    def test_the_same_size_and_another_content_is_seen_when_the_time_differs(self):
        self.push("T-1")
        path = os.path.join(self.f.jd, "issue.md")
        write(path, "THE ISSUE\n")
        os.utime(path, ns=(1, 1))
        self.assertEqual(self.state()["state"], "changed")

    def test_a_touched_file_with_the_same_content_is_saved(self):
        self.push("T-1")
        os.utime(os.path.join(self.f.jd, "issue.md"), ns=(1, 1))
        self.assertEqual(self.state()["state"], "saved")

    def test_it_runs_without_network_or_keys(self):
        self.push("T-1")
        self.s3.stop()
        self.s3.log.clear()
        for mode in ("read", "write"):
            os.remove(self.key_path(mode))
        self.assertEqual(self.state()["state"], "saved")
        self.assertEqual(self.s3.log, [])

    def test_a_failed_push_leaves_it_unsaved(self):
        self.s3.fail_status = 503
        self.push("T-1")
        self.assertEqual(self.state()["state"], "not-saved")

    def test_audits_count_only_when_they_are_on(self):
        self.write_job("_audits/run1", {"findings.json": "[]\n"})
        self.assertEqual([r["job"] for r in self.status()["jobs"]], ["T-1"])
        self.configure(jobs_store={"include_audits": True})
        self.assertIn("_audits/run1", [r["job"] for r in self.status()["jobs"]])

    def test_a_note_that_cannot_be_read_is_no_note(self):
        self.push("T-1")
        cfg = fx.jobs_store_config(self.f.ctx())
        write(fx.jobs_record_path(cfg, "T-1"), "not json")
        self.assertEqual(self.state()["state"], "not-saved")


class JobsListTest(PushCase):
    def rows(self):
        code, res = self.fx("jobs", "list")
        self.assertEqual(code, 0, res)
        return {r["job"]: r for r in res["jobs"]}

    def test_each_job_with_its_snapshots(self):
        self.f.save_job(stage="handed-off", pr_url="https://example.test/pr/1")
        self.push("T-1")
        self.write_job("T-1", {"more.md": "more\n"})
        self.push("T-1")
        row = self.rows()["T-1"]
        self.assertEqual(len(row["snapshots"]), 2)
        self.assertTrue(row["latest"].startswith(row["snapshots"][-1]))
        self.assertEqual((row["stage"], row["summary"], row["pr"]), ("handed-off", "test job", "https://example.test/pr/1"))
        self.assertEqual(row["files"], 4)
        self.assertGreater(row["bytes"], 10)

    def test_it_downloads_no_file_s_content(self):
        self.push("T-1")
        self.s3.log.clear()
        self.rows()
        self.assertEqual({"/jobs/snapshots/" in "/" + c["key"] for c in self.s3.calls("GET")}, {True})
        self.assertFalse(self.s3.calls("PUT"))
        self.assertFalse(self.s3.calls("HEAD"))

    def test_it_reads_with_the_read_key(self):
        self.push("T-1")
        self.s3.log.clear()
        os.remove(self.key_path("write"))
        self.rows()
        self.assertEqual({c["id"] for c in self.s3.log}, {READ_ID})

    def test_every_page(self):
        self.s3.page_size = 2
        for i in range(2, 7):
            self.write_job("T-%d" % i, {"job.json": json.dumps({"id": "T-%d" % i, "stage": "build"})})
        self.push("--all")
        self.assertEqual(len(self.rows()), 6)

    def test_an_audit_and_a_name_that_is_not_a_snapshot(self):
        self.configure(jobs_store={"include_audits": True})
        self.write_job("_audits/run1", {"findings.json": "[]\n"})
        self.push("--all")
        self.s3.objects[PREFIX + "/jobs/snapshots/T-1/notes.txt"] = b"x"
        self.s3.objects[PREFIX + "/jobs/snapshots/../../evil/20260101T000000Z-abcdef01.json"] = b"{}"
        rows = self.rows()
        self.assertEqual(set(rows), {"T-1", "_audits/run1"})

    def test_a_snapshot_that_cannot_be_read_is_named_and_the_rest_listed(self):
        self.write_job("T-2", {"job.json": json.dumps({"id": "T-2", "stage": "build"})})
        self.push("--all")
        self.s3.objects[self.snapshots("T-2")[-1]] = b"not json"
        rows = self.rows()
        self.assertIn("error", rows["T-2"])
        self.assertNotIn("error", rows["T-1"])

    def test_no_read_key_says_so(self):
        os.remove(self.key_path("read"))
        code, res = self.fx("jobs", "list")
        self.assertEqual(code, 3)
        self.assertIn("s3-read.env", res["error"])

    def test_nothing_on_s3(self):
        self.assertEqual(self.rows(), {})


# --------------------------------------------------------------------------
# J5. fx jobs pull

class PullCase(PushCase):
    def setUp(self):
        super().setUp()
        self.f.save_job(stage="handed-off", pr_url="https://example.test/pr/1", created_at="2026-01-01T10:00:00+00:00",
                        pushed_head="abc")
        self.fx("jobs", "push", "T-1")
        self.before = self.hashes(self.f.jd)

    def hashes(self, folder):
        found = {}
        for dirpath, _, names in os.walk(folder):
            for n in names:
                with open(os.path.join(dirpath, n), "rb") as f:
                    found[os.path.relpath(os.path.join(dirpath, n), folder)] = hashlib.sha256(f.read()).hexdigest()
        return found

    def empty_jobs(self):
        shutil.rmtree(os.path.join(self.f.factory, "jobs"))

    def pull(self, *argv):
        return self.fx("jobs", "pull", *argv)

    def tmp_left(self):
        jobs = os.path.join(self.f.factory, "jobs")
        return [d for d in os.listdir(jobs) if d.startswith(".restoring")] if os.path.isdir(jobs) else []

    def rewrite_snapshot(self, change, name="T-1"):
        key = self.snapshots(name)[-1]
        snap = json.loads(self.s3.objects[key])
        change(snap)
        self.s3.objects[key] = json.dumps(snap).encode()


class PullTest(PullCase):
    def test_an_empty_folder_gets_every_file_back(self):
        self.empty_jobs()
        code, res = self.pull("T-1")
        self.assertEqual(code, 0, res)
        after = self.hashes(self.f.jd)
        self.assertEqual(set(after), set(self.before))
        for rel in after:
            if rel != "job.json":
                self.assertEqual(after[rel], self.before[rel], rel)
        self.assertEqual(self.tmp_left(), [])

    def test_the_read_key_is_enough(self):
        self.empty_jobs()
        os.remove(self.key_path("write"))
        self.s3.log.clear()
        code, res = self.pull("T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual({c["id"] for c in self.s3.log}, {READ_ID})
        self.assertFalse(self.s3.calls("PUT"))

    def test_private_folders_and_files(self):
        self.write_job("T-1", {"round-2/deep/review.md": "x\n"})
        self.fx("jobs", "push", "T-1")
        self.empty_jobs()
        self.pull("T-1")
        for dirpath, dirnames, names in os.walk(self.f.jd):
            self.assertEqual(stat.S_IMODE(os.stat(dirpath).st_mode), 0o700, dirpath)
            for n in names:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(dirpath, n)).st_mode), 0o600, n)

    def test_a_job_already_here_is_refused(self):
        code, res = self.pull("T-1")
        self.assertEqual(code, 3)
        self.assertIn("--force", res["error"])
        self.assertEqual(self.hashes(self.f.jd), self.before)

    def test_force_moves_the_old_one_aside_and_deletes_nothing(self):
        self.write_job("T-1", {"only-here.md": "not pushed\n"})
        code, res = self.pull("T-1", "--force")
        self.assertEqual(code, 0, res)
        replaced = res["jobs"][0]["replaced"]
        self.assertEqual(os.path.dirname(replaced), os.path.join(self.f.factory, "jobs", "_replaced"))
        self.assertEqual(slurp(os.path.join(replaced, "only-here.md")), "not pushed\n")
        self.assertFalse(os.path.exists(os.path.join(self.f.jd, "only-here.md")))
        self.assertNotIn("_replaced", self.f.ctx().list_ids())

    def test_what_was_left_out_is_reported(self):
        os.environ["STUB_GITLEAKS_FIND"] = "secret-text"
        self.write_job("T-1", {"leak.md": "secret-text\n"})
        self.fx("jobs", "push", "T-1")
        self.empty_jobs()
        code, res = self.pull("T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["jobs"][0]["not_restored"], [{"path": "leak.md", "reason": "secret: stub-rule"}])
        self.assertIn("not_restored", res["next"])
        self.assertFalse(os.path.exists(os.path.join(self.f.jd, "leak.md")))

    def test_at_gets_the_old_one(self):
        first = self.snapshots()[0]
        stamp = os.path.basename(first).split("-")[0]
        self.write_job("T-1", {"later.md": "later\n"})
        self.fx("jobs", "push", "T-1")
        self.assertEqual(len(self.snapshots()), 2)
        self.empty_jobs()
        code, res = self.pull("T-1", "--at", stamp)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["jobs"][0]["snapshot"], os.path.basename(first))
        self.assertFalse(os.path.exists(os.path.join(self.f.jd, "later.md")))

    def test_without_at_the_newest(self):
        self.write_job("T-1", {"later.md": "later\n"})
        self.fx("jobs", "push", "T-1")
        self.empty_jobs()
        self.pull("T-1")
        self.assertTrue(os.path.exists(os.path.join(self.f.jd, "later.md")))

    def test_at_before_everything(self):
        self.empty_jobs()
        code, res = self.pull("T-1", "--at", "2001-01-01T00:00:00Z")
        self.assertEqual(code, 3)
        self.assertIn("no snapshot", res["error"])

    def test_at_with_words(self):
        self.empty_jobs()
        code, res = self.pull("T-1", "--at", "yesterday")
        self.assertEqual(code, 3)
        self.assertIn("--at", res["error"])

    def test_at_can_be_cut_short(self):
        stamp = os.path.basename(self.snapshots()[0]).split("-")[0]
        self.empty_jobs()
        self.assertEqual(self.pull("T-1", "--at", stamp[:8])[0], 0)

    def test_a_job_not_on_s3(self):
        code, res = self.pull("T-9")
        self.assertEqual(code, 3)
        self.assertIn("no snapshot", res["error"])

    def test_nothing_named(self):
        self.assertEqual(self.pull()[0], 3)
        self.assertEqual(self.pull("T-1", "--all")[0], 3)

    def test_all_goes_round_what_is_here(self):
        self.write_job("T-2", {"job.json": json.dumps({"id": "T-2", "stage": "handed-off", "branch": "b"})})
        self.fx("jobs", "push", "T-2")
        shutil.rmtree(os.path.join(self.f.factory, "jobs", "T-2"))
        code, res = self.pull("--all")
        self.assertEqual(code, 0, res)
        self.assertEqual([r["job"] for r in res["jobs"]], ["T-2"])
        self.assertEqual(res["kept"], ["T-1"])

    def test_all_leaves_audits_when_they_are_off(self):
        self.configure(jobs_store={"include_audits": True})
        self.write_job("_audits/run1", {"findings.json": "[]\n"})
        self.fx("jobs", "push", "--all")
        self.empty_jobs()
        self.configure(jobs_store={})
        code, res = self.pull("--all")
        self.assertEqual([r["job"] for r in res["jobs"]], ["T-1"])
        self.assertFalse(os.path.exists(os.path.join(self.f.factory, "jobs", "_audits")))

    def test_a_pulled_job_reads_as_saved(self):
        self.empty_jobs()
        shutil.rmtree(os.path.join(self.tmp, "state", "jobs-store"))
        self.pull("T-1")
        code, res = self.fx("jobs", "status")
        self.assertEqual(res["jobs"][0]["state"], "saved")

    def test_a_folder_being_restored_is_no_job(self):
        self.write_job(".restoring-T-7", {"job.json": "{}"})
        self.assertNotIn(".restoring-T-7", self.f.ctx().list_ids())

    def test_a_leftover_of_a_killed_pull_does_not_get_in_the_way(self):
        self.empty_jobs()
        self.write_job(".restoring-T-1", {"stale.md": "x\n"})
        code, res = self.pull("T-1")
        self.assertEqual(code, 0, res)
        self.assertFalse(os.path.exists(os.path.join(self.f.jd, "stale.md")))


class PullRefusesTest(PullCase):
    def setUp(self):
        super().setUp()
        self.empty_jobs()

    def refused(self, word, *argv):
        code, res = self.pull(*(argv or ("T-1",)))
        self.assertEqual(code, 3, res)
        self.assertIn(word, res["error"])
        self.assertFalse(os.path.exists(self.f.jd))
        self.assertEqual(self.tmp_left(), [])

    def test_an_edited_snapshot_with_dots(self):
        def change(snap):
            snap["files"][0]["path"] = "../../evil.md"
        self.rewrite_snapshot(change)
        self.refused("unsafe")
        self.assertFalse(os.path.exists(os.path.join(self.f.factory, "evil.md")))

    def test_an_absolute_path(self):
        def change(snap):
            snap["files"][0]["path"] = "/tmp/evil.md"
        self.rewrite_snapshot(change)
        self.refused("unsafe")

    def test_a_version_it_does_not_know(self):
        def change(snap):
            snap["version"] = 2
        self.rewrite_snapshot(change)
        self.refused("version")

    def test_a_snapshot_of_another_job(self):
        def change(snap):
            snap["job"] = "T-2"
        self.rewrite_snapshot(change)
        self.refused("says it is of")

    def test_no_job_json(self):
        def change(snap):
            snap["files"] = [f for f in snap["files"] if f["path"] != "job.json"]
        self.rewrite_snapshot(change)
        self.refused("no job.json")

    def test_not_json(self):
        self.s3.objects[self.snapshots()[-1]] = b"nope"
        self.refused("not one fx wrote")

    def test_a_file_with_another_hash_refuses_the_whole_job(self):
        h = sha(b"the issue\n")
        self.s3.objects["%s/jobs/sha256/%s/%s" % (PREFIX, h[:2], h)] = b"THE ISSUE\n"
        self.refused("does not match")

    def test_a_file_of_another_size(self):
        h = sha(b"the issue\n")
        self.s3.objects["%s/jobs/sha256/%s/%s" % (PREFIX, h[:2], h)] = b"the issue and more\n"
        self.refused("does not match")

    def test_a_file_gone_from_s3(self):
        h = sha(b"the issue\n")
        del self.s3.objects["%s/jobs/sha256/%s/%s" % (PREFIX, h[:2], h)]
        self.refused("not on S3")

    def test_a_job_json_of_another_job(self):
        data = json.dumps({"id": "../../elsewhere", "stage": "handed-off"}).encode()
        h = sha(data)
        self.s3.objects["%s/jobs/sha256/%s/%s" % (PREFIX, h[:2], h)] = data

        def change(snap):
            for f in snap["files"]:
                if f["path"] == "job.json":
                    f.update(sha256=h, size=len(data))
        self.rewrite_snapshot(change)
        self.refused("not this job's")

    def test_the_old_job_stays_when_the_new_one_fails(self):
        self.write_job("T-1", {"job.json": json.dumps({"id": "T-1", "stage": "build"}), "mine.md": "x\n"})
        h = sha(b"the issue\n")
        self.s3.objects["%s/jobs/sha256/%s/%s" % (PREFIX, h[:2], h)] = b"THE ISSUE\n"
        code, res = self.pull("T-1", "--force")
        self.assertEqual(code, 3)
        self.assertTrue(os.path.exists(os.path.join(self.f.jd, "mine.md")))
        self.assertEqual(self.tmp_left(), [])
        self.assertFalse(os.path.exists(os.path.join(self.f.factory, "jobs", "_replaced")))

    def test_one_bad_job_does_not_stop_the_others(self):
        self.s3.objects[self.snapshots()[-1]] = b"nope"
        data = json.dumps({"id": "T-2", "stage": "handed-off"}).encode()
        self.write_job("T-2", {"job.json": data.decode()})
        self.fx("jobs", "push", "T-2")
        shutil.rmtree(os.path.join(self.f.factory, "jobs", "T-2"))
        code, res = self.pull("--all")
        self.assertEqual(code, 3)
        self.assertEqual([f["job"] for f in res["failed"]], ["T-1"])
        self.assertEqual([r["job"] for r in res["pulled"]], ["T-2"])

    def test_a_store_that_fails(self):
        self.s3.fail_status = 503
        code, res = self.pull("T-1")
        self.assertEqual(code, 3)
        self.assertFalse(os.path.exists(self.f.jd))

    def test_no_read_key(self):
        os.remove(self.key_path("read"))
        code, res = self.pull("T-1")
        self.assertEqual(code, 3)
        self.assertIn("s3-read.env", res["error"])


# --------------------------------------------------------------------------
# J6. a restored job works on the new machine

class OtherMachineCase(PullCase):
    """Pushed from one factory folder and pulled into another: the same project, another place on disk."""
    stage = "handed-off"

    def setUp(self):
        super().setUp()
        self.old_factory, self.old_work = self.f.factory, self.f.ctx().work
        self.old_job = dict(self.f.job())

    def move_to_a_new_machine(self):
        new = os.path.join(self.tmp, "new-home", "proj-factory")
        os.makedirs(new)
        shutil.copy(os.path.join(self.old_factory, "factory.json"), new)
        shutil.copy(os.path.join(self.old_factory, "checks.sh"), new)
        shutil.rmtree(os.path.join(self.old_factory, "jobs"))
        self.f.factory = new
        self.f.jd = os.path.join(new, "jobs", "T-1")
        return new

    def push_as(self, **changes):
        self.f.save_job(**changes)
        code, res = self.fx("jobs", "push", "T-1")
        self.assertEqual(code, 0, res)

    def restored(self):
        return load_json(os.path.join(self.f.jd, "job.json"))


class FinishedJobMovesTest(OtherMachineCase):
    def setUp(self):
        super().setUp()
        self.push_as(worktree=os.path.join(self.old_work, "T-1"), slot=3, port=5130, setup_done=True, upstream=[os.path.join(self.f.jd, "upstream", "1-a.md")],
                     runs=[{"agent": "builder", "result": [os.path.join(self.f.jd, "build.md")], "ended": "x"}],
                     note_text="see %s/jobs for it" % self.old_factory, other_path=self.old_factory + "-other/x")
        self.new = self.move_to_a_new_machine()
        self.code, self.res = self.pull("T-1")
        self.assertEqual(self.code, 0, self.res)

    def test_paths_move_and_the_rest_stays(self):
        job = self.restored()
        ctx = self.f.ctx()
        self.assertEqual(job["worktree"], os.path.join(ctx.work, "T-1"))
        self.assertEqual(job["upstream"], [os.path.join(self.new, "jobs", "T-1", "upstream", "1-a.md")])
        self.assertEqual(job["runs"][0]["result"], [os.path.join(self.new, "jobs", "T-1", "build.md")])
        self.assertEqual(job["note_text"], "see %s/jobs for it" % self.old_factory)  # only what starts with the path
        self.assertEqual(job["other_path"], self.old_factory + "-other/x")  # another folder with a similar name
        self.assertNotIn(self.old_work, job["worktree"])

    def test_what_only_the_old_machine_knew_goes(self):
        job = self.restored()
        self.assertFalse(job["setup_done"])
        self.assertEqual(job["slot"], 1)
        self.assertEqual(job["port"], 5110)
        self.assertEqual(self.res["jobs"][0]["port"], 5110)

    def test_it_says_so_in_its_history(self):
        self.assertIn("restored from S3", self.restored()["history"][-1]["note"])

    def test_show_status_and_board(self):
        code, res = self.fx("show", "T-1")
        self.assertEqual((code, res["stage"], res["pr_url"]), (0, "handed-off", "https://example.test/pr/1"))
        code, res = self.fx("status")
        self.assertEqual(res["jobs"][0]["stage"], "handed-off")
        self.assertEqual(res["jobs"][0]["pr"], "https://example.test/pr/1")
        data = fx.board_data([self.new])
        self.assertEqual([v["id"] for v in data["done"]], ["T-1"])

    def test_it_is_not_asking_for_a_human(self):
        self.assertFalse(self.restored()["needs_human"])
        self.assertNotIn("needs_human", self.res["jobs"][0])
        self.assertIn("rework", self.res["jobs"][0]["next"])

    def test_rework_clones_the_branch_from_the_remote(self):
        sh(["git", "push", "-q", "origin", self.f.branch], self.f.wt)
        code, res = self.fx("rework", "T-1", "again please")
        self.assertEqual(code, 0, res)
        job = self.restored()
        self.assertEqual(job["stage"], "build")
        self.assertEqual(job["worktree"], os.path.join(self.f.ctx().work, "T-1"))
        self.assertTrue(os.path.isdir(os.path.join(job["worktree"], ".git")))
        self.assertTrue(res["setup_needed"])

    def test_rework_when_the_branch_is_gone_says_so(self):
        code, res = self.fx("rework", "T-1", "again please")
        self.assertEqual(code, 3)
        self.assertIn("not on the remote", res["error"])


class UnfinishedJobMovesTest(OtherMachineCase):
    def go(self, **changes):
        self.push_as(stage="build", pushed_head=None, pr_url=None, **changes)
        self.new = self.move_to_a_new_machine()
        code, res = self.pull("T-1")
        self.assertEqual(code, 0, res)
        return res["jobs"][0]

    def test_a_branch_on_the_remote_is_cloned_from_it(self):
        sh(["git", "commit", "-q", "--allow-empty", "-m", "pushed work"], self.f.wt)
        sh(["git", "push", "-q", "origin", self.f.branch], self.f.wt)
        row = self.go()
        job = self.restored()
        self.assertEqual(job["stage"], "build")
        self.assertTrue(job["needs_human"])
        self.assertTrue(row["needs_human"])
        self.assertIn("not in the snapshot", row["next"])
        self.assertIn("fx setup", row["next"])
        wt = os.path.join(self.f.ctx().work, "T-1")
        self.assertEqual(job["worktree"], wt)
        self.assertEqual(sh(["git", "log", "-1", "--format=%s"], wt).strip(), "pushed work")
        self.assertEqual(sh(["git", "branch", "--show-current"], wt).strip(), self.f.branch)

    def test_no_branch_on_the_remote_suggests_again(self):
        row = self.go()
        job = self.restored()
        self.assertEqual(job["stage"], "build")
        self.assertTrue(job["needs_human"])
        self.assertIn("not in the snapshot", job["handoff_reason"])
        self.assertIn("--again", row["next"])
        self.assertFalse(os.path.exists(job["worktree"]))

    def test_the_old_folders_are_never_touched(self):
        marker = os.path.join(self.old_work, "T-1", "marker.txt")
        write(marker, "still here\n")
        self.go()
        job = self.restored()
        self.assertFalse(job["worktree"].startswith(self.old_work))
        self.fx("gate", "T-1")
        self.assertEqual(slurp(marker), "still here\n")
        self.assertEqual(self.f.calls("semgrep"), [])  # no scanner ran on the old clone or anywhere

    def test_a_gate_on_it_stops_without_a_clone(self):
        self.go()
        code, res = self.fx("gate", "T-1")
        self.assertNotEqual(code, 0)

    def test_an_unreachable_remote_does_not_lose_the_restore(self):
        self.push_as(stage="build", pushed_head=None, pr_url=None)
        self.new = self.move_to_a_new_machine()
        cfg = load_json(os.path.join(self.new, "factory.json"))
        cfg["project"]["url"] = os.path.join(self.tmp, "no-such-remote.git")
        write(os.path.join(self.new, "factory.json"), json.dumps(cfg))
        self.put_key("read", READ_ID, READ_SECRET)  # the keys folder goes by the project's URL
        code, res = self.pull("T-1")
        self.assertEqual(code, 0, res)
        self.assertTrue(os.path.exists(os.path.join(self.f.jd, "job.json")))
        self.assertIn("try again later", res["jobs"][0]["next"])

    def test_a_clone_already_there_is_left_as_it_is(self):
        self.push_as(stage="build", pushed_head=None, pr_url=None)
        self.new = self.move_to_a_new_machine()
        wt = os.path.join(self.f.ctx().work, "T-1")
        write(os.path.join(wt, "work-in-progress.txt"), "not pushed\n")
        code, res = self.pull("T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(slurp(os.path.join(wt, "work-in-progress.txt")), "not pushed\n")
        self.assertIn("left as it is", res["jobs"][0]["next"])

    def test_another_active_job_keeps_its_slot(self):
        self.push_as(stage="build", pushed_head=None, pr_url=None, slot=1, port=5110)
        self.new = self.move_to_a_new_machine()
        write(os.path.join(self.new, "jobs", "T-7", "job.json"), json.dumps({"id": "T-7", "stage": "build", "slot": 1}))
        code, res = self.pull("T-1")
        self.assertEqual(self.restored()["slot"], 2)


class WhatTheFileMaySayTest(OtherMachineCase):
    """job.json comes from S3: it must not point fx's deletes or its signals at anything of this machine."""

    def victim(self):
        path = os.path.join(self.tmp, "precious")
        write(os.path.join(path, "file.txt"), "keep me\n")
        return path

    def test_a_worktree_elsewhere_is_replaced(self):
        victim = self.victim()
        self.push_as(worktree=victim)
        self.new = self.move_to_a_new_machine()
        self.pull("T-1")
        self.assertEqual(self.restored()["worktree"], os.path.join(self.f.ctx().work, "T-1"))

    def test_a_package_clone_elsewhere_is_replaced_and_rework_deletes_nothing_of_it(self):
        victim = self.victim()
        self.push_as(split={"mode": "split", "packages": {"wp1": {"clone": victim, "port": 1}}})
        sh(["git", "push", "-q", "origin", self.f.branch], self.f.wt)
        self.new = self.move_to_a_new_machine()
        self.pull("T-1")
        self.assertEqual(self.restored()["split"]["packages"]["wp1"]["clone"],
                         os.path.join(self.f.ctx().work, "T-1") + "--wp1")
        self.fx("rework", "T-1", "again")
        self.assertEqual(slurp(os.path.join(victim, "file.txt")), "keep me\n")

    def test_a_package_name_with_dots_refuses_the_job(self):
        self.push_as(split={"mode": "split", "packages": {"../../x": {"clone": "/x"}}})
        self.new = self.move_to_a_new_machine()
        code, res = self.pull("T-1")
        self.assertEqual(code, 3)
        self.assertFalse(os.path.exists(self.f.jd))

    def test_a_candidate_elsewhere_is_replaced(self):
        victim = self.victim()
        self.push_as(candidates=[{"k": 1, "worktree": victim, "branch": "b", "port": 1}])
        self.new = self.move_to_a_new_machine()
        self.pull("T-1")
        self.assertEqual(self.restored()["candidates"][0]["worktree"], os.path.join(self.f.ctx().work, "T-1") + "-c1")

    def test_an_upstream_file_elsewhere_is_dropped(self):
        self.push_as(upstream=[os.path.join(self.f.jd, "upstream", "1-a.md"), "/etc/passwd", 7])
        self.new = self.move_to_a_new_machine()
        self.pull("T-1")
        self.assertEqual(self.restored()["upstream"], [os.path.join(self.new, "jobs", "T-1", "upstream", "1-a.md")])

    def test_the_old_machine_s_process_is_forgotten(self):
        self.write_job("T-1", {"app.json": json.dumps({"pid": os.getpid()})})
        self.push_as(app_dir=os.path.join(self.tmp, "elsewhere"))
        self.new = self.move_to_a_new_machine()
        code, res = self.pull("T-1")
        self.assertEqual(code, 0, res)
        self.assertFalse(os.path.exists(os.path.join(self.f.jd, "app.json")))
        self.assertNotIn("app_dir", self.restored())
        self.assertEqual(self.fx("jobs", "status")[1]["jobs"][0]["state"], "saved")


# --------------------------------------------------------------------------
# J7. pushed by itself, and said when it was not

class AfterPrCase(PushCase):
    def pr(self):
        self.f.save_job(stage="pr", round=1, reviews={"security": "PASS", "code": "PASS"}, decision="APPROVE", gate="PASS",
                        target_branch="main", risk="low", reviewers=[])
        self.f.commit({"app.py": "x = 1\n"})
        return self.fx("pr", "T-1", "--no-secret-scan")


class AfterPrTest(AfterPrCase):
    def test_a_finished_job_goes_up_as_it_ended(self):
        code, res = self.pr()
        self.assertEqual(code, 0, res)
        self.assertEqual(res["jobs_store"], {"pushed": ["T-1"], "failed": []})
        self.assertEqual(self.snapshot()["stage"], "handed-off")
        code, st = self.fx("jobs", "status")
        self.assertEqual(st["jobs"][0]["state"], "saved")  # nothing wrote the job again after its push

    def test_503_in_fx_pr_is_a_warning_and_the_pr_is_done(self):
        self.s3.fail_status = 503
        code, res = self.pr()
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "done")
        self.assertEqual(self.f.job()["stage"], "handed-off")
        self.assertEqual(len(res["warnings"]), 1)
        self.assertIn("jobs store", res["warnings"][0])
        self.assertIn(res["warnings"][0], res["notes"])
        code, st = self.fx("jobs", "status")
        self.assertEqual(st["unsaved"], ["T-1"])
        self.s3.fail_status = None
        self.assertEqual(self.push("T-1")[0], 0)
        self.assertEqual(self.fx("jobs", "status")[1]["unsaved"], [])

    def test_no_gitleaks_is_a_warning_too(self):
        from test_security import not_on_this_machine
        with not_on_this_machine("gitleaks"):
            code, res = self.pr()
        self.assertEqual(code, 0, res)
        self.assertIn("gitleaks", res["warnings"][0])
        self.assertEqual(self.s3.objects, {})

    def test_no_key_is_a_warning_too(self):
        os.remove(self.key_path("write"))
        code, res = self.pr()
        self.assertEqual(code, 0, res)
        self.assertIn("s3-write.env", res["warnings"][0])

    def test_broken_settings_are_a_warning_too(self):
        self.configure(jobs_store={"auto": True, "max_file_mb": -1})
        code, res = self.pr()
        self.assertEqual(code, 0, res)
        self.assertIn("max_file_mb", res["warnings"][0])

    def test_auto_off_pushes_nothing(self):
        self.configure(jobs_store={"auto": False})
        code, res = self.pr()
        self.assertEqual(code, 0, res)
        self.assertNotIn("jobs_store", res)
        self.assertEqual(self.s3.log, [])

    def test_learning_goes_with_it(self):
        self.write_job("_learn", {"cursor.json": "{}\n"})
        code, res = self.pr()
        self.assertEqual(res["jobs_store"]["pushed"], ["T-1", "_learn"])

    def test_no_block_nothing_changes(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        del cfg["jobs_store"]
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        code, res = self.pr()
        self.assertEqual(code, 0, res)
        self.assertNotIn("jobs_store", res)
        self.assertNotIn("warnings", res)


class NotAtEveryStepTest(PushCase):
    def test_the_gate_pushes_nothing(self):
        self.f.commit({"app.py": "x = 1\n"})
        self.fx("gate", "T-1")
        self.assertEqual(self.s3.objects, {})
        self.assertFalse(self.s3.calls("PUT"))


class AfterAuditImportTest(PushCase):
    def run_import(self):
        path = os.path.join(self.tmp, "findings.json")
        write(path, json.dumps({"findings": [{"id": "F1", "status": "confirmed", "severity": "high", "title": "a hole"}]}))
        return self.fx("audit-import", path)

    def test_off_by_default_nothing_goes(self):
        code, res = self.run_import()
        self.assertEqual(code, 0, res)
        self.assertNotIn("jobs_store", res)
        self.assertEqual(self.s3.objects, {})

    def test_on_it_goes_up(self):
        self.configure(jobs_store={"include_audits": True})
        self.write_job("_audits/skill-run", {"findings.json": "[]\n", "coverage-ledger.json": "{}\n", "sandbox/x.py": "x\n"})
        code, res = self.run_import()
        self.assertEqual(code, 0, res)
        self.assertEqual(len(res["jobs_store"]["pushed"]), 2)
        self.assertIn("_audits/skill-run", res["jobs_store"]["pushed"])
        self.assertTrue(self.snapshots("_audits/skill-run"))
        self.assertEqual(self.snapshots(), [])  # the jobs wait for their own fx pr

    def test_a_failure_is_a_warning(self):
        self.configure(jobs_store={"include_audits": True})
        self.s3.fail_status = 503
        code, res = self.run_import()
        self.assertEqual(code, 0, res)
        self.assertIn("jobs store", res["warnings"][0])


class ShownWhenNotSavedTest(PushCase):
    def test_fx_status(self):
        rows = self.fx("status")[1]
        self.assertEqual(rows["unsaved"], ["T-1"])
        self.assertFalse(rows["jobs"][0]["saved"])
        self.push("T-1")
        rows = self.fx("status")[1]
        self.assertEqual(rows["unsaved"], [])
        self.assertTrue(rows["jobs"][0]["saved"])

    def test_fx_status_without_the_block_is_as_before(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        del cfg["jobs_store"]
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        res = self.fx("status")[1]
        self.assertEqual(list(res), ["jobs"])
        self.assertNotIn("saved", res["jobs"][0])

    def test_fx_status_with_broken_settings_still_shows_the_jobs(self):
        self.configure(jobs_store={"max_file_mb": -1})
        code, res = self.fx("status")
        self.assertEqual(code, 0)
        self.assertEqual(res["jobs"][0]["job"], "T-1")
        self.assertIn("max_file_mb", res["jobs_store_error"])

    def test_the_board(self):
        def view():
            data = fx.board_data([self.f.factory])
            return (data["active"] + data["done"])[0]
        self.assertTrue(view()["unsaved"])
        self.push("T-1")
        self.assertFalse(view()["unsaved"])
        self.assertIn("v.unsaved", fx.BOARD_HTML)

    def test_the_board_without_the_block(self):
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        del cfg["jobs_store"]
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        data = fx.board_data([self.f.factory])
        self.assertNotIn("unsaved", (data["active"] + data["done"])[0])


class JobsDoctorTest(PushCase):
    def rows(self):
        code, res = self.fx("doctor")
        return {c["check"]: c for c in res["checks"]}

    def test_all_well(self):
        self.push("T-1")
        rows = self.rows()
        for name in ("jobs store settings", "jobs store is this project's own", "jobs store read key",
                     "jobs store write key", "jobs store read key file private", "jobs store scans with gitleaks",
                     "jobs store readable"):
            self.assertTrue(rows[name]["ok"], name)
        self.assertNotIn("jobs saved on S3 (advice)", rows)
        self.assertIn("jobs store has its own write key (advice)", rows)

    def test_a_job_not_saved_is_only_advice(self):
        row = self.rows()["jobs saved on S3 (advice)"]
        self.assertTrue(row["ok"])
        self.assertIn("T-1", row["detail"])

    def test_broken_settings_fail(self):
        self.configure(jobs_store={"max_file_mb": -1})
        self.assertFalse(self.rows()["jobs store settings"]["ok"])

    def test_no_read_key_fails(self):
        os.remove(self.key_path("read"))
        rows = self.rows()
        self.assertFalse(rows["jobs store read key"]["ok"])
        self.assertFalse(rows["jobs store readable"]["ok"])

    def test_no_write_key_with_auto_fails_and_without_it_is_advice(self):
        os.remove(self.key_path("write"))
        self.assertFalse(self.rows()["jobs store write key"]["ok"])
        self.configure(jobs_store={"auto": False})
        self.assertTrue(self.rows()["jobs store write key (advice: only pushes need it)"]["ok"])

    def test_no_read_permission_fails(self):
        self.s3.fail_status = 403
        self.assertFalse(self.rows()["jobs store readable"]["ok"])

    def test_a_loose_key_file_fails(self):
        os.chmod(self.key_path("read"), 0o644)
        self.assertFalse(self.rows()["jobs store read key file private"]["ok"])

    def test_its_own_key_quiets_the_advice(self):
        self.put_key("jobs-write", JOBS_ID, JOBS_SECRET)
        rows = self.rows()
        self.assertNotIn("jobs store has its own write key (advice)", rows)
        self.assertTrue(rows["jobs store jobs-write key"]["ok"])

    def test_another_factory_at_the_same_place_fails(self):
        other = os.path.join(self.tmp, "other-factory")
        write(os.path.join(other, "factory.json"), slurp(os.path.join(self.f.factory, "factory.json")))
        write(os.path.join(self.tmp, "state", "factories.json"), json.dumps([other, self.f.factory]))
        self.assertFalse(self.rows()["jobs store is this project's own"]["ok"])

    def test_no_gitleaks_fails(self):
        from test_security import not_on_this_machine
        with not_on_this_machine("gitleaks"):
            self.assertFalse(self.rows()["jobs store scans with gitleaks"]["ok"])


# --------------------------------------------------------------------------
# J8. LOCAL numbers across machines

class LocalNumbersTest(JobsStoreCase):
    def seed(self, *ids):
        for i in ids:
            self.s3.objects["%s/jobs/snapshots/%s/20260101T000000Z-abcdef01.json" % (PREFIX, i)] = b"{}"

    def new(self):
        issue = os.path.join(self.tmp, "issue.md")
        write(issue, "Do it\n")
        return self.fx("new", "--local", "a job", "--issue-file", issue)

    def test_an_empty_machine_counts_what_is_on_s3(self):
        self.seed(*("LOCAL-%d" % i for i in range(1, 8)))
        code, res = self.new()
        self.assertEqual(code, 0, res)
        self.assertEqual(res["job"], "LOCAL-8")
        self.assertNotIn("warnings", res)

    def test_a_retry_on_s3_counts(self):
        self.seed("LOCAL-1", "LOCAL-2-r2")
        self.assertEqual(self.new()[1]["job"], "LOCAL-3")

    def test_jobs_here_still_count(self):
        write(os.path.join(self.f.factory, "jobs", "LOCAL-1", "job.json"), json.dumps({"id": "LOCAL-1", "stage": "build"}))
        self.seed("LOCAL-2")
        self.assertEqual(self.new()[1]["job"], "LOCAL-3")

    def test_s3_out_of_reach_warns_and_goes_on(self):
        self.seed("LOCAL-1")
        self.s3.fail_status = 503
        code, res = self.new()
        self.assertEqual(code, 0, res)
        self.assertEqual(res["job"], "LOCAL-1")
        self.assertIn("jobs store", res["warnings"][0])

    def test_no_read_key_warns_and_goes_on(self):
        os.remove(self.key_path("read"))
        code, res = self.new()
        self.assertEqual(code, 0, res)
        self.assertIn("s3-read.env", res["warnings"][0])

    def test_a_jira_job_does_not_ask_s3(self):
        self.s3.log.clear()
        write(os.path.join(self.tmp, "issue.md"), "# ABC-1: a thing\n\nDo it\n")
        self.fx("new", "ABC-1", "--issue-file", os.path.join(self.tmp, "issue.md"))
        self.assertFalse(self.s3.calls("LIST"))


class SameNumberOtherJobTest(PushCase):
    def test_another_created_at_is_refused_and_says_it_is_another_job(self):
        self.f.save_job(created_at="2026-01-01T10:00:00+00:00")
        self.push("T-1")
        before = dict(self.s3.objects)
        self.f.save_job(created_at="2026-02-02T10:00:00+00:00", summary="something else")
        code, res = self.push("T-1")
        self.assertEqual(code, 3)
        self.assertIn("another job", res["error"])
        self.assertIn("2026-01-01", res["error"])
        self.assertEqual(self.s3.objects, before)
        self.assertEqual(self.fx("jobs", "status")[1]["jobs"][0]["state"], "changed")

    def test_the_same_job_goes_on(self):
        self.f.save_job(created_at="2026-01-01T10:00:00+00:00")
        self.push("T-1")
        self.f.save_job(summary="renamed")
        self.assertEqual(self.push("T-1")[0], 0)
        self.assertEqual(len(self.snapshots()), 2)

    def test_it_stops_only_that_job(self):
        self.f.save_job(created_at="2026-01-01T10:00:00+00:00")
        self.push("T-1")
        self.f.save_job(created_at="2026-02-02T10:00:00+00:00")
        self.write_job("T-2", {"job.json": json.dumps({"id": "T-2", "stage": "build"})})
        code, res = self.push("--all")
        self.assertEqual(code, 3)
        self.assertEqual([f["job"] for f in res["failed"]], ["T-1"])
        self.assertEqual([r["job"] for r in res["pushed"]], ["T-2"])

    def test_a_snapshot_that_cannot_be_read_does_not_block_the_job_for_ever(self):
        self.push("T-1")
        self.s3.objects[self.snapshots()[-1]] = b"nope"
        self.write_job("T-1", {"new.md": "x\n"})
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        self.assertIn("cannot be read", res["jobs"][0]["note"])
        self.assertEqual(len(self.snapshots()), 2)


# --------------------------------------------------------------------------
# the whole way, on a factory with two finished jobs and an audit

class WholeWayTest(JobsStoreCase):
    def test_a_new_machine_gets_it_all_back(self):
        self.configure(jobs_store={"include_audits": True})
        fac = self.f.factory
        write(os.path.join(fac, ".gitignore"), "jobs/\nlocal/\nbaseline.json\n.claude/settings.local.json\n")
        self.f.save_job(stage="handed-off", pr_url="https://example.test/pr/1", pushed_head="abc",
                        created_at="2026-01-01T10:00:00+00:00")
        write(os.path.join(self.f.jd, "spec.md"), "the spec\n")
        write(os.path.join(fac, "jobs", "T-2", "job.json"), json.dumps({
            "id": "T-2", "issue_key": "T-2", "stage": "handed-off", "summary": "another", "pr_url": "https://example.test/pr/2",
            "branch": "factory/T-2-x", "worktree": os.path.join(self.f.ctx().work, "T-2"), "base_branch": "main",
            "base_ref": "origin/main", "round": 1, "round_start": 0, "max_rounds": 3, "slot": 2, "port": 5120,
            "setup_done": True, "created_at": "2026-01-02T10:00:00+00:00", "history": []}))
        audit = os.path.join(fac, "jobs", "_audits", "run1")
        for rel, text in (("findings.json", "[1]\n"), ("coverage-ledger.json", "{\"a\": 1}\n"), ("summary.md", "a hole\n"),
                          ("sandbox/copy-of-the-code.py", "x = 1\n")):
            write(os.path.join(audit, rel), text)
        sh(["git", "push", "-q", "origin", self.f.branch], self.f.wt)

        code, res = self.fx("jobs", "push", "--all")
        self.assertEqual(code, 0, res)
        code, old_status = self.fx("status")
        with open(os.path.join(audit, "findings.json"), "rb") as f:
            findings = f.read()
        with open(os.path.join(audit, "coverage-ledger.json"), "rb") as f:
            ledger = f.read()

        # A clone of the factory repository somewhere else, with no jobs/.
        sh(["git", "init", "-q", "-b", "main"], fac)
        sh(["git", "add", "-A"], fac)
        sh(["git", "commit", "-q", "-m", "factory"], fac)
        new = os.path.join(self.tmp, "other-machine", "proj-factory")
        os.makedirs(os.path.dirname(new))
        sh(["git", "clone", "-q", fac, new], self.tmp)
        self.assertFalse(os.path.exists(os.path.join(new, "jobs")))
        self.f.factory = new

        code, res = self.fx("jobs", "list")
        self.assertEqual(code, 0, res)
        self.assertEqual({r["job"] for r in res["jobs"]}, {"T-1", "T-2", "_audits/run1"})

        code, res = self.fx("jobs", "pull", "--all")
        self.assertEqual(code, 0, res)
        self.assertEqual({r["job"] for r in res["jobs"]}, {"T-1", "T-2", "_audits/run1"})

        code, status = self.fx("status")
        see = lambda st: {r["job"]: (r["stage"], r["pr"]) for r in st["jobs"]}
        self.assertEqual(see(status), see(old_status))

        code, res = self.fx("rework", "T-1", "one more thing")
        self.assertEqual(code, 0, res)
        job = load_json(os.path.join(new, "jobs", "T-1", "job.json"))
        self.assertEqual(job["stage"], "build")
        self.assertTrue(job["worktree"].startswith(os.path.join(self.tmp, "other-machine")))
        self.assertTrue(os.path.isdir(os.path.join(job["worktree"], ".git")))

        here = os.path.join(new, "jobs", "_audits", "run1")
        with open(os.path.join(here, "findings.json"), "rb") as f:
            self.assertEqual(f.read(), findings)
        with open(os.path.join(here, "coverage-ledger.json"), "rb") as f:
            self.assertEqual(f.read(), ledger)
        self.assertFalse(os.path.exists(os.path.join(here, "sandbox")))


class ANameThatIsNotUtf8Test(PushCase):
    """Linux allows it, and APFS refuses it, so the test does not run there."""

    def test_it_goes_up_and_comes_back(self):
        name = b"round-1/r\xe9sum\xe9.md"
        path = os.path.join(os.fsencode(self.f.jd), name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "wb") as f:
                f.write(b"latin-1 name\n")
        except OSError:
            self.skipTest("this file system refuses such a name")
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        shutil.rmtree(self.f.jd)
        code, res = self.fx("jobs", "pull", "T-1")
        self.assertEqual(code, 0, res)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"latin-1 name\n")
        self.assertEqual(self.fx("jobs", "status")[1]["jobs"][0]["state"], "saved")


# --------------------------------------------------------------------------
# K1. what stays on this machine is said, every time

class HeldBackTest(PushCase):
    SECRET = "s3cr3t-value-kept-off-s3"

    def setUp(self):
        super().setUp()
        self.write_job("T-1", {"leak.md": "the key is %s\n" % self.SECRET})
        os.environ["STUB_GITLEAKS_FIND"] = self.SECRET
        self.want = [{"path": "leak.md", "reason": "secret: stub-rule"}]

    def test_the_push_says_so_and_does_not_say_all_is_on_s3(self):
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["jobs"][0]["state"], "pushed")
        self.assertEqual(res["jobs"][0]["held_back"], self.want)
        self.assertIn("T-1/leak.md (secret: stub-rule)", res["next"])
        self.assertNotIn("every job's files", res["next"])

    def test_a_dry_run_says_so_too(self):
        code, res = self.push("T-1", "--dry-run")
        self.assertEqual(res["jobs"][0]["held_back"], self.want)
        self.assertIn("nothing was uploaded", res["next"])
        self.assertIn("T-1/leak.md", res["next"])

    def test_the_second_push_says_it_again(self):
        self.push("T-1")
        self.s3.log.clear()
        code, res = self.push("T-1")
        self.assertEqual(res["jobs"][0]["state"], "unchanged")
        self.assertEqual(res["jobs"][0]["held_back"], self.want)
        self.assertIn("T-1/leak.md", res["next"])
        self.assertEqual(self.s3.log, [])

    def test_status_is_saved_and_says_so(self):
        self.push("T-1")
        code, res = self.fx("jobs", "status")
        row = res["jobs"][0]
        self.assertEqual(row["state"], "saved")
        self.assertEqual(row["held_back"], self.want)
        self.assertEqual(row["skipped"], 1)
        self.assertEqual(res["unsaved"], [])
        self.assertIn("T-1/leak.md", res["next"])
        self.assertNotIn("every file is on S3", res["next"])

    def test_fx_status_the_board_and_doctor(self):
        self.push("T-1")
        res = self.fx("status")[1]
        self.assertEqual(res["jobs"][0]["held_back"], self.want)
        self.assertTrue(res["jobs"][0]["saved"])
        data = fx.board_data([self.f.factory])
        view = (data["active"] + data["done"])[0]
        self.assertEqual(view["held_back"], 1)
        self.assertFalse(view["unsaved"])
        self.assertIn("v.held_back", fx.BOARD_HTML)
        code, res = self.fx("doctor")
        row = next(c for c in res["checks"] if c["check"] == "jobs store files held back (advice)")
        self.assertTrue(row["ok"])
        self.assertIn("T-1/leak.md", row["detail"])

    def test_the_value_is_nowhere(self):
        seen = [self.push("T-1"), self.push("T-1"), self.fx("jobs", "status"), self.fx("status"), self.fx("doctor")]
        for code, res in seen:
            self.assertNotIn(self.SECRET, json.dumps(res))
        for key, data in self.s3.objects.items():
            self.assertNotIn(self.SECRET.encode(), data, key)
        cfg = fx.jobs_store_config(self.f.ctx())
        self.assertNotIn(self.SECRET, slurp(fx.jobs_record_path(cfg, "T-1")))

    def test_each_reason_but_excluded_counts(self):
        del os.environ["STUB_GITLEAKS_FIND"]
        os.remove(os.path.join(self.f.jd, "leak.md"))
        self.configure(jobs_store={"max_file_mb": 0.001})
        self.write_job("T-1", {"big.bin": "x" * 2000, "vendor/a.php": "x\n", "copy/.git/config": "x\n"})
        os.symlink(self.tmp, os.path.join(self.f.jd, "link"))
        code, res = self.push("T-1")
        self.assertEqual(sorted(x["path"] for x in res["jobs"][0]["held_back"]), ["big.bin", "copy/.git/config", "link"])
        self.assertIn("vendor/", [x["path"] for x in res["jobs"][0]["skipped"]])

    def test_a_job_changed_after_its_push_still_says_what_stayed(self):
        self.push("T-1")
        self.write_job("T-1", {"more.md": "x\n"})
        row = self.fx("jobs", "status")[1]["jobs"][0]
        self.assertEqual(row["state"], "changed")
        self.assertEqual(row["held_back"], self.want)


class ChosenExclusionsAreNotWarningsTest(PushCase):
    def test_a_sandbox_is_not_held_back(self):
        self.configure(jobs_store={"include_audits": True})
        self.write_job("_audits/run1", {"findings.json": "[]\n", "sandbox/app.py": "code\n"})
        code, res = self.push("--all")
        self.assertEqual(code, 0, res)
        row = next(r for r in res["jobs"] if r["job"] == "_audits/run1")
        self.assertEqual(row["skipped"], [{"path": "sandbox/", "reason": "excluded"}])
        self.assertEqual(row["held_back"], [])
        self.assertEqual(res["next"], "every job's files and snapshot are on S3")
        code, st = self.fx("jobs", "status")
        self.assertEqual(st["next"], "every job here is as its last push left it, and every file is on S3")
        code, doc = self.fx("doctor")
        self.assertFalse([c for c in doc["checks"] if "held back" in c["check"]])


class HeldBackAfterPrTest(AfterPrCase):
    def test_it_warns_and_exits_0(self):
        self.write_job("T-1", {"leak.md": "hunter2-after-pr\n"})
        os.environ["STUB_GITLEAKS_FIND"] = "hunter2-after-pr"
        code, res = self.pr()
        self.assertEqual(code, 0, res)
        self.assertEqual(res["jobs_store"]["held_back"], [{"path": "leak.md", "reason": "secret: stub-rule", "job": "T-1"}])
        self.assertEqual(len(res["warnings"]), 1)
        self.assertIn("T-1/leak.md", res["warnings"][0])
        self.assertIn(res["warnings"][0], res["notes"])
        self.assertNotIn("hunter2", json.dumps(res))

    def test_a_chosen_exclusion_does_not_warn(self):
        self.write_job("T-1", {"vendor/a.php": "x\n"})
        code, res = self.pr()
        self.assertEqual(code, 0, res)
        self.assertNotIn("held_back", res["jobs_store"])
        self.assertNotIn("warnings", res)


class NoteOfAnOlderFxTest(PushCase):
    def old_note(self):
        cfg = fx.jobs_store_config(self.f.ctx())
        path = fx.jobs_record_path(cfg, "T-1")
        note = load_json(path)
        del note["skipped"]
        write(path, json.dumps(note))

    def setUp(self):
        super().setUp()
        self.write_job("T-1", {"leak.md": "hunter2-old-note\n"})
        os.environ["STUB_GITLEAKS_FIND"] = "hunter2-old-note"
        self.push("T-1")
        self.old_note()
        self.count = len(self.snapshots())

    def test_status_does_not_know_and_says_what_to_run(self):
        code, res = self.fx("jobs", "status")
        self.assertEqual(res["jobs"][0]["state"], "saved")
        self.assertIsNone(res["jobs"][0]["held_back"])
        self.assertIn("fx jobs push", res["next"])

    def test_the_next_push_uploads_nothing_and_finds_out(self):
        self.s3.log.clear()
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["jobs"][0]["state"], "unchanged")
        self.assertEqual(res["jobs"][0]["held_back"], [{"path": "leak.md", "reason": "secret: stub-rule"}])
        self.assertFalse(self.s3.calls("PUT"))
        self.assertEqual(len(self.snapshots()), self.count)
        code, res = self.fx("jobs", "status")
        self.assertEqual(res["jobs"][0]["held_back"], [{"path": "leak.md", "reason": "secret: stub-rule"}])

    def test_after_that_it_costs_nothing(self):
        self.push("T-1")
        self.s3.log.clear()
        self.push("T-1")
        self.assertEqual(self.s3.log, [])

    def test_doctor_gives_advice(self):
        rows = {c["check"]: c for c in self.fx("doctor")[1]["checks"]}
        self.assertTrue(rows["jobs store notes of an older fx (advice)"]["ok"])


# --------------------------------------------------------------------------
# K2. the keys of AWS profiles are looked for too

class ProfileKeysCase(PushCase):
    read_key, write_key = False, False

    def setUp(self):
        super().setUp()
        creds = os.path.join(self.tmp, "aws-credentials")
        write(creds, "[team]\naws_access_key_id = %s\naws_secret_access_key = %s\n" % (READ_ID, READ_SECRET))
        os.environ["AWS_SHARED_CREDENTIALS_FILE"] = creds
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["storage"]["read_profile"] = "team"
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.put_key("write", WRITE_ID, WRITE_SECRET)


class ProfileKeysTest(ProfileKeysCase):
    def test_a_file_with_the_profile_s_secret_stays_here(self):
        self.write_job("T-1", {"values.txt": "%s\n" % READ_SECRET})
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["jobs"][0]["held_back"], [{"path": "values.txt", "reason": "secret: local-secret"}])
        self.assertNotIn(READ_SECRET, json.dumps(res))
        for key, data in self.s3.objects.items():
            self.assertNotIn(READ_SECRET.encode(), data, key)

    def test_each_of_the_three_settings(self):
        creds = os.environ["AWS_SHARED_CREDENTIALS_FILE"]
        for setting, value in (("write_profile", "writer-secret-value"), ("jobs_write_profile", "jobs-secret-value")):
            with open(creds, "a") as f:
                f.write("[%s]\naws_access_key_id = ID%s\naws_secret_access_key = %s\n" % (setting, setting, value))
            cfg = load_json(os.path.join(self.f.factory, "factory.json"))
            cfg["storage"][setting] = setting
            write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
            self.assertIn(value, fx.local_secret_values(self.f.ctx()))

    def test_a_short_value_is_not_looked_for(self):
        with open(os.environ["AWS_SHARED_CREDENTIALS_FILE"], "a") as f:
            f.write("[short]\naws_access_key_id = abc\naws_secret_access_key = defgh\n")
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["storage"]["read_profile"] = "short"
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.assertFalse({"abc", "defgh"} & fx.local_secret_values(self.f.ctx()))

    def test_a_profile_without_static_keys_does_not_call_the_aws_cli(self):
        marker = os.path.join(self.tmp, "aws-was-called")
        aws = os.path.join(self.f.bin, "aws")
        write(aws, "#!/bin/sh\ntouch %s\nexit 1\n" % marker)
        os.chmod(aws, 0o755)
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["storage"]["read_profile"] = "sso-only"
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        fx.local_secret_values(self.f.ctx())
        self.assertFalse(os.path.exists(marker))

    def test_a_credentials_file_that_cannot_be_read_breaks_nothing(self):
        with open(os.environ["AWS_SHARED_CREDENTIALS_FILE"], "wb") as f:
            f.write(b"\xff\xfe not text")
        self.assertIsNone(fx.baselines_static_profile("team"))
        write(os.environ["AWS_SHARED_CREDENTIALS_FILE"], "no section header\n")
        self.assertIsNone(fx.baselines_static_profile("team"))

    def test_outgoing_text_is_scrubbed_of_it(self):
        text, hits = fx.scrub_text(self.f.ctx(), "the value is %s here" % READ_SECRET)
        self.assertNotIn(READ_SECRET, text)
        self.assertIn("local-env-value", hits)

    def test_the_gate_s_own_check_blocks_a_commit_with_it(self):
        """The check that runs where gitleaks is not installed: with it, the gate leaves this to gitleaks' own rules."""
        from test_security import not_on_this_machine
        cfg = load_json(os.path.join(self.f.factory, "factory.json"))
        cfg["security"] = {"required": [], "scanners": {"zap": False, "headers": False}}
        write(os.path.join(self.f.factory, "factory.json"), json.dumps(cfg))
        self.f.commit({"app/config.php": "<?php\n$x = '%s';\n" % READ_SECRET})
        with not_on_this_machine("gitleaks"):
            code, res = self.fx("gate", "T-1")
        self.assertEqual(res["gate"], "FAIL", res)
        self.assertIn("local-env-value", self.checks_md())
        self.assertNotIn(READ_SECRET, json.dumps(res) + self.checks_md())


# --------------------------------------------------------------------------
# K3. inside archives

def a_zip(files, compression=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def a_tar_gz(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class ArchivesTest(PushCase):
    SECRET = b"archive-secret-value-0123456789"

    def setUp(self):
        super().setUp()
        os.environ["STUB_GITLEAKS_FIND"] = self.SECRET.decode()

    def put(self, rel, data):
        path = os.path.join(self.f.jd, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def held(self):
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        self.assertNotIn(self.SECRET.decode(), json.dumps(res))
        return {x["path"]: x["reason"] for x in res["jobs"][0]["held_back"]}

    def uploaded(self, data):
        return sha(data) in " ".join(self.s3.objects)

    def test_a_secret_in_a_zip(self):
        data = a_zip({"resources/network.log": b"Authorization: " + self.SECRET})
        self.assertNotIn(self.SECRET, data)
        self.put("trace.zip", data)
        self.assertEqual(self.held(), {"trace.zip": "secret: stub-rule"})
        self.assertFalse(self.uploaded(data))

    def test_a_secret_in_a_zip_inside_a_zip(self):
        data = a_zip({"inner.zip": a_zip({"deep.log": self.SECRET})})
        self.put("sub/nested.zip", data)
        self.assertEqual(self.held(), {"sub/nested.zip": "secret: stub-rule"})

    def test_a_secret_in_a_tar_gz(self):
        data = a_tar_gz({"x/net.log": self.SECRET})
        self.put("sub/t.tar.gz", data)
        self.assertEqual(self.held(), {"sub/t.tar.gz": "secret: stub-rule"})
        self.assertFalse(self.uploaded(data))

    def test_the_depth_is_passed_only_here(self):
        self.held()
        self.assertEqual(self.f.calls("gitleaks")[-1]["argv"][-2:], ["--max-archive-depth", "3"])
        self.assertNotIn("--max-archive-depth", fx.gitleaks_flags(self.f.ctx())[0])

    def test_deeper_than_the_depth_is_not_looked_at(self):
        data = self.SECRET
        for name in ("d.log", "c.zip", "b.zip", "a.zip"):
            data = a_zip({name: data})
        self.put("deep.zip", data)
        self.assertEqual(self.held(), {})

    def test_a_clean_zip_goes(self):
        data = a_zip({"resources/network.log": b"nothing here"})
        self.put("trace.zip", data)
        self.assertEqual(self.held(), {})
        self.assertTrue(self.uploaded(data))

    def test_a_damaged_zip_stays(self):
        data = b"PK\x03\x04 this is not a zip"
        self.put("trace.zip", data)
        self.assertEqual(self.held(), {"trace.zip": "an archive fx cannot read"})
        self.assertFalse(self.uploaded(data))

    def test_a_zip_with_a_damaged_member_stays(self):
        data = bytearray(a_zip({"a.log": b"x" * 500}, zipfile.ZIP_STORED))
        data[60] ^= 0xff  # inside a.log: its checksum no longer matches
        self.put("trace.zip", bytes(data))
        self.assertEqual(self.held(), {"trace.zip": "an archive fx cannot read"})

    def test_a_damaged_zip_inside_a_zip_stays(self):
        self.put("trace.zip", a_zip({"inner.zip": b"not a zip"}))
        self.assertEqual(self.held(), {"trace.zip": "an archive fx cannot read"})

    def test_a_zip_with_a_password_stays(self):
        data = bytearray(a_zip({"a.log": b"x"}, zipfile.ZIP_STORED))
        data[6] |= 1  # the local header
        at = data.find(b"PK\x01\x02")
        data[at + 8] |= 1  # the central directory: what says it has a password
        self.put("trace.zip", bytes(data))
        self.assertEqual(self.held(), {"trace.zip": "an archive fx cannot read"})

    def test_a_cut_tar_gz_stays(self):
        self.put("t.tar.gz", a_tar_gz({"x.log": os.urandom(4000)})[:-200])
        self.assertEqual(self.held(), {"t.tar.gz": "an archive fx cannot read"})

    def test_a_format_fx_has_no_module_for_stays(self):
        self.put("x.7z", b"7z\xbc\xaf\x27\x1c")
        self.assertEqual(self.held(), {"x.7z": "an archive fx cannot read"})

    def test_a_good_gz_goes_and_a_bad_one_stays(self):
        import gzip
        self.put("a.log.gz", gzip.compress(b"clean"))
        self.put("b.log.gz", b"not gzip")
        self.assertEqual(self.held(), {"b.log.gz": "an archive fx cannot read"})

    def test_too_large_unpacked_stays_and_gitleaks_never_sees_it(self):
        old = fx.JOBS_ARCHIVE_MAX_BYTES
        fx.JOBS_ARCHIVE_MAX_BYTES = 1000
        self.addCleanup(setattr, fx, "JOBS_ARCHIVE_MAX_BYTES", old)
        self.put("bomb.zip", a_zip({"a.log": self.SECRET + b"x" * 5000}))
        self.assertEqual(self.held(), {"bomb.zip": "an archive fx cannot read"})

    def test_a_plain_file_with_a_bang_in_its_name_goes(self):
        self.put("a!b.md", b"clean")
        self.assertEqual(self.held(), {})
        self.assertIn("a!b.md", [f["path"] for f in self.snapshot()["files"]])

    def test_a_plain_file_with_a_bang_and_a_secret_stays(self):
        self.put("a!b.md", self.SECRET)
        self.assertEqual(self.held(), {"a!b.md": "secret: stub-rule"})

    def test_an_archive_with_a_bang_in_its_name(self):
        self.put("a!b.zip", a_zip({"c.log": self.SECRET}))
        self.assertEqual(self.held(), {"a!b.zip": "secret: stub-rule"})

    def test_a_member_that_climbs_out_is_still_its_archive_s_finding(self):
        os.environ["STUB_GITLEAKS_FIND"] = ""
        self.put("trace.zip", a_zip({"a.log": b"x"}))
        os.environ["STUB_GITLEAKS_REPORT"] = json.dumps([{"File": "scan/job/trace.zip!../../../etc/x", "RuleID": "r"}])
        self.assertEqual(self.held(), {"trace.zip": "secret: r"})

    def test_a_report_on_nothing_it_was_given_still_stops_the_job(self):
        os.environ["STUB_GITLEAKS_FIND"] = ""
        for name in ("scan/job/other.zip!x.log", "scan/other/x.log", "../outside", ""):
            os.environ["STUB_GITLEAKS_REPORT"] = json.dumps([{"File": name, "RuleID": "r"}])
            code, res = self.push("T-1")
            self.assertEqual(code, 3, name)
            self.assertIn("did not give it", res["error"])
        self.assertEqual(self.s3.objects, {})

    def test_a_gitleaks_without_the_flag_stops_the_push(self):
        os.environ["STUB_GITLEAKS_OLD"] = "1"
        code, res = self.push("T-1")
        self.assertEqual(code, 3)
        self.assertIn("upgrade", res["error"])
        self.assertEqual(self.s3.objects, {})

    def test_a_local_value_inside_an_archive_stays_too(self):
        del os.environ["STUB_GITLEAKS_FIND"]
        self.put_key("jobs-write", JOBS_ID, JOBS_SECRET)
        self.put("trace.zip", a_zip({"net.log": JOBS_SECRET.encode()}))
        self.assertEqual(self.held(), {"trace.zip": "secret: local-secret"})


# --------------------------------------------------------------------------
# K4. what gitleaks' default rules let through

APP_KEY = "APP_KEY=base64:" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/AbCdE="
SANCTUM = '{"token":"12|' + "aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcd" + '"}'


class ExtraSecretsTest(PushCase):
    def held(self, text):
        self.write_job("T-1", {"note.md": text})
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        self.assertNotIn(text, json.dumps(res))
        return {x["path"]: x["reason"] for x in res["jobs"][0]["held_back"]}

    def test_a_laravel_app_key(self):
        self.assertEqual(self.held(APP_KEY + "\n"), {"note.md": "secret: laravel-app-key"})

    def test_a_quoted_app_key_with_a_colon(self):
        self.assertEqual(self.held('APP_KEY: "' + APP_KEY[8:] + '"\n'), {"note.md": "secret: laravel-app-key"})

    def test_a_sanctum_token(self):
        self.assertEqual(self.held(SANCTUM + "\n"), {"note.md": "secret: sanctum-token"})

    def test_it_does_not_reach_s3(self):
        self.held(APP_KEY + "\n")
        for key, data in self.s3.objects.items():
            self.assertNotIn(APP_KEY.encode(), data, key)

    def test_what_is_not_a_key(self):
        for text in ("APP_KEY=\n", "APP_KEY=null\n", "APP_KEY=${APP_KEY}\n", "APP_KEY=base64:short\n",
                     "| 1 | abc |\n| 2 | def |\n", "| 12 | aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcd |\n", "12|short\n"):
            self.assertEqual(self.held(text), {}, text)

    def test_in_job_json_the_job_does_not_go(self):
        self.f.save_job(summary=APP_KEY)
        code, res = self.push("T-1")
        self.assertEqual(code, 3)
        self.assertIn("job.json", res["error"])
        self.assertNotIn(APP_KEY, json.dumps(res))
        self.assertEqual(self.s3.objects, {})

    def test_inside_an_archive(self):
        path = os.path.join(self.f.jd, "trace.zip")
        with open(path, "wb") as f:
            f.write(a_zip({"net.log": SANCTUM.encode()}))
        code, res = self.push("T-1")
        self.assertEqual(res["jobs"][0]["held_back"], [{"path": "trace.zip", "reason": "secret: sanctum-token"}])

    def test_they_stay_out_of_the_gate(self):
        self.assertFalse([f for f in fx.gitleaks_flags(self.f.ctx())[0] if "laravel" in f])
        self.f.commit({"app/config.php": "<?php\n// %s\n" % APP_KEY})
        code, res = self.fx("gate", "T-1")
        self.assertNotIn("laravel-app-key", self.checks_md())


# --------------------------------------------------------------------------
# K5. a Jira job does not stay changed

class JiraDoneTest(AfterPrCase):
    def jira_pr(self):
        self.f.save_job(jira_base="https://example.atlassian.net", issue_key="T-1",
                        issue_url="https://example.atlassian.net/browse/T-1")
        code, res = self.pr()
        self.assertEqual(code, 0, res)
        self.assertTrue(res.get("jira_todo"), res)
        return res

    def status(self):
        return self.fx("jobs", "status")[1]["jobs"][0]["state"]

    def test_all_jira_work_done_and_the_job_is_saved(self):
        self.jira_pr()
        code, res = self.fx("jira-done", "T-1", "--all")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.status(), "saved")
        data = next(d for k, d in self.s3.objects.items() if k.endswith(sha(slurp(os.path.join(self.f.jd, "job.json")).encode())))
        self.assertEqual(json.loads(data)["jira_todo"], self.f.job()["jira_todo"])

    def test_with_jira_work_left_nothing_goes_yet(self):
        res = self.jira_pr()
        ids = [i["id"] for i in res["jira_todo"]]
        self.assertGreater(len(ids), 1)
        before = len(self.snapshots())
        code, res = self.fx("jira-done", "T-1", ids[0])
        self.assertEqual(code, 0, res)
        self.assertNotIn("jobs_store", res)
        self.assertEqual(len(self.snapshots()), before)
        self.assertEqual(self.status(), "changed")

    def test_s3_failing_is_a_warning(self):
        self.jira_pr()
        self.s3.fail_status = 503
        code, res = self.fx("jira-done", "T-1", "--all")
        self.assertEqual(code, 0, res)
        self.assertIn("jobs store", res["warnings"][0])
        self.assertEqual(self.status(), "changed")

    def test_an_unfinished_job_is_not_pushed(self):
        self.f.save_job(stage="build", jira_todo=[{"id": "j1", "kind": "comment", "done": None}])
        code, res = self.fx("jira-done", "T-1", "--all")
        self.assertEqual(code, 0, res)
        self.assertNotIn("jobs_store", res)
        self.assertEqual(self.s3.objects, {})


# --------------------------------------------------------------------------
# the whole way again, with files held back

class HeldBackWholeWayTest(PushCase):
    SECRET = "fake-secret-for-the-whole-way-0123456789"

    def test_it(self):
        self.configure(jobs_store={"include_audits": True})
        os.environ["STUB_GITLEAKS_FIND"] = self.SECRET
        fac = self.f.factory
        write(os.path.join(fac, ".gitignore"), "jobs/\nlocal/\nbaseline.json\n.claude/settings.local.json\n")
        self.f.save_job(stage="handed-off", pr_url="https://example.test/pr/1", created_at="2026-01-01T10:00:00+00:00")
        self.write_job("T-1", {"leak.md": "the key is %s\n" % self.SECRET})
        self.write_job("T-2", {"job.json": json.dumps({"id": "T-2", "stage": "handed-off", "pr_url": "https://example.test/pr/2",
                                                       "branch": "factory/T-2-x", "base_branch": "main", "max_rounds": 3, "round": 1,
                                                       "created_at": "2026-01-02T10:00:00+00:00", "history": []})})
        with open(os.path.join(fac, "jobs", "T-2", "trace.zip"), "wb") as f:
            f.write(a_zip({"resources/network.log": self.SECRET.encode()}))
        self.write_job("_audits/run1", {"findings.json": "[]\n", "sandbox/app.py": "code\n"})

        code, pushed = self.push("--all")
        self.assertEqual(code, 0, pushed)
        want = [{"job": "T-1", "path": "leak.md"}, {"job": "T-2", "path": "trace.zip"}]
        see = lambda held: [{"job": x["job"], "path": x["path"]} for x in held]
        self.assertEqual(see(fx.jobs_held_rows(pushed["jobs"])), want)
        self.assertIn("2 file(s) stay on this machine", pushed["next"])
        self.assertNotIn("every job's files", pushed["next"])

        code, status = self.fx("jobs", "status")
        self.assertEqual(see(fx.jobs_held_rows(status["jobs"])), want)
        self.assertEqual(status["unsaved"], [])
        code, plain = self.fx("status")
        self.assertEqual({r["job"]: [x["path"] for x in r.get("held_back", [])] for r in plain["jobs"]},
                         {"T-1": ["leak.md"], "T-2": ["trace.zip"]})
        code, doctor = self.fx("doctor")
        row = next(c for c in doctor["checks"] if c["check"] == "jobs store files held back (advice)")
        self.assertTrue(row["ok"])
        for name in ("T-1/leak.md", "T-2/trace.zip"):
            self.assertIn(name, row["detail"])
        for output in (pushed, status, plain, doctor):
            self.assertNotIn(self.SECRET, json.dumps(output))
        for key, data in self.s3.objects.items():
            self.assertNotIn(self.SECRET.encode(), data, key)
            self.assertNotEqual(key.rsplit("/", 1)[-1], sha(a_zip({"resources/network.log": self.SECRET.encode()})))

        self.s3.log.clear()
        count = len(self.s3.objects)
        self.assertEqual(self.push("--all")[0], 0)
        self.assertFalse(self.s3.calls("PUT"))
        self.assertEqual(len(self.s3.objects), count)

        sh(["git", "init", "-q", "-b", "main"], fac)
        sh(["git", "add", "-A"], fac)
        sh(["git", "commit", "-q", "-m", "factory"], fac)
        new = os.path.join(self.tmp, "other-machine", "proj-factory")
        os.makedirs(os.path.dirname(new))
        sh(["git", "clone", "-q", fac, new], self.tmp)
        self.f.factory = new
        code, pulled = self.fx("jobs", "pull", "--all")
        self.assertEqual(code, 0, pulled)
        self.assertEqual({r["job"]: [x["path"] for x in r["not_restored"]] for r in pulled["jobs"]},
                         {"T-1": ["leak.md"], "T-2": ["trace.zip"], "_audits/run1": []})

        self.s3.log.clear()
        code, again = self.push("--all")
        self.assertEqual(code, 0, again)
        self.assertEqual({r["state"] for r in again["jobs"]}, {"unchanged"})
        self.assertFalse(self.s3.calls("PUT"))
        self.assertEqual(len(self.s3.objects), count)
