"""A factory repository that also holds reviews of merge requests (standard library only). Run from the repository root:
    python3 -I -m unittest discover -s tests

Reviews of merge requests are not part of code-factory, but its factory repository may hold what they leave: their
settings in factory.json, jobs/_reviews/, lessons tagged [review] and knowledge/repos/. code-factory leaves all of it as
it is, and none of it reaches a job's list, an agent or S3.
"""

import io
import json
import os
import shutil
import unittest
from unittest import mock

from test_baselines import PREFIX, sha
from test_jobs_store import PushCase
from test_security import fx, load_json, sh, slurp, write

REVIEW = "_reviews/PROJ-9-mr7"
REVIEW_BE = "_reviews/PROJ-9-be-mr3"
TICKET = "_reviews/_ticket-PROJ-9"
NOT_JOBS = "jobs/_reviews/ holds reviews of merge requests, which code-factory does not handle: they are not jobs"
NOT_PUSHED = ("jobs/_reviews/ holds reviews of merge requests, which code-factory does not handle: it never pushes or "
              "pulls them")
CTX_AGENTS = ["intake", "spec", "planner", "builder", "security", "code", "ux", "ui", "approver", "judge", "verifier"]

# Every factory.json key only reviews of merge requests use, with jobs_store.include_reviews among them (set below).
REVIEW_SETTINGS = {
    "review": {"repos": [{"name": "fe", "url": "git@gitlab.example.com:group/fe.git", "ui_paths": [], "setup": "pnpm i",
                          "app": {"up": "", "down": "", "health": "/"}, "checks": ["pnpm ds:check"]}],
               "checks": ["make lint"], "design_parent": "PROJ-1", "design_max_mb": 200},
    "models": {"review": {"checklist": "sonnet", "requirement": "opus", "code": "opus", "security": "opus",
                          "ux": "sonnet", "ui": "sonnet", "verifier": "opus"}},
    "jira": {"transitions": {"start": "", "review": "", "review_changes": "In Progress", "conflict": "Blocked"},
             "code_owner": {"MSG": "ABC-1", "API-01": "ABC-24"}, "subtask_type": "Sub-task"},
    "learning": {"sources": {"factory_reviews": True, "rework_notes": True, "pr_comments": True, "human_edits": True,
                             "review_missed": True, "review_rejections": True, "review_outcomes": True,
                             "review_followup": True}},
}

LESSONS = """# Lessons learned

Rules distilled from what reviewers caught in earlier factory jobs.

## 2026-01-02 (from T-1)

- [L1] [code] Every list endpoint is paginated <!-- 2026-01-02; from T-1 -->
- [L2] [review]   Do not ask for style changes the linter does not ask for    <!-- 2026-01-03; from _reviews/PROJ-9-mr7 -->
- [L3] [test] Every bug fix starts with a failing test <!-- 2026-01-02; from T-1 -->

## 2026-01-04 (from _reviews/PROJ-9-mr7)

- [L4] [review, code] Check that each migration runs both ways <!-- 2026-01-04; from _reviews/PROJ-9-mr7 -->
- [review] Name the acceptance criteria the change misses
- [L5] [all, review] Quote the line a finding is about <!-- 2026-01-04; from _reviews/PROJ-9-mr7 -->

## 2026-01-05 (from T-1)

- [L6] [spec, security] Every new endpoint says who may call it <!-- 2026-01-05; from T-1 -->
- Keep each commit to one change
"""
REVIEW_RULES = ["Do not ask for style changes", "Check that each migration runs both ways",
                "Name the acceptance criteria", "Quote the line a finding is about"]
OTHER_RULES = ["Every list endpoint is paginated", "Every bug fix starts with a failing test",
               "Every new endpoint says who may call it", "Keep each commit to one change"]


def review_lines(text):
    """The lines of the lessons tagged [review], whatever their other tags."""
    return [line for line in text.splitlines() if any(rule in line for rule in REVIEW_RULES)]


REVIEW_LINES = review_lines(LESSONS)

# What another repository's knowledge may say: a check against this project's code would fail on every line of it.
REPO_KNOWLEDGE = ("# The web app\n\nScreens live in `src/screens/Invoices.tsx:1-40`.\n\n"
                  "The test account logs in with password=hunter2hunter2.\n")


def review_settings_removed(cfg):
    """factory.json without the keys only reviews of merge requests use."""
    cfg = json.loads(json.dumps(cfg))
    del cfg["review"], cfg["models"]["review"], cfg["jira"]["code_owner"], cfg["jira"]["subtask_type"]
    del cfg["jira"]["transitions"]["review_changes"], cfg["jira"]["transitions"]["conflict"]
    del cfg["jobs_store"]["include_reviews"]
    for k in [k for k in cfg["learning"]["sources"] if k.startswith("review_")]:
        del cfg["learning"]["sources"][k]
    return cfg


class SharedCase(PushCase):
    """The jobs store's factory with T-1 and PROJ-9, and everything reviews of merge requests leave in it: their
    settings, two reviews of PROJ-9 and the ticket that groups them, knowledge of another repository and lessons tagged
    [review]."""

    def setUp(self):
        super().setUp()
        self.configure(jobs_store={"include_reviews": True})
        self.settings_path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(self.settings_path)
        cfg.update(json.loads(json.dumps(REVIEW_SETTINGS)))
        write(self.settings_path, json.dumps(cfg, indent=1))
        self.settings_text = slurp(self.settings_path)
        self.write_reviews()
        # a job of the same issue: a refused review must never be taken for it
        job = dict(self.f.job(), id="PROJ-9", issue_key="PROJ-9", summary="the invoices list", stage="handed-off",
                   branch="factory/PROJ-9-invoices", worktree=os.path.join(self.tmp, "proj-factory-work", "PROJ-9"),
                   created_at="2026-01-02T09:00:00+00:00", history=[])
        self.write_job("PROJ-9", {"job.json": json.dumps(job, indent=1)})
        kdir = os.path.join(self.f.factory, "knowledge")
        write(os.path.join(kdir, "README.md"), "# Knowledge\n\n- overview.md\n- style.md\n")
        write(os.path.join(kdir, "overview.md"), "# Overview\n\nAn app that keeps invoices.\n")
        write(os.path.join(kdir, "style.md"), "# Style\n\nSmall functions.\n")
        for name in ("overview.md", "architecture.md", "style.md"):
            write(os.path.join(kdir, "repos", "fe", name), REPO_KNOWLEDGE)
        self.lessons_path = os.path.join(self.f.factory, "lessons.md")
        write(self.lessons_path, LESSONS)
        self.reviews_before = self.tree(os.path.join("jobs", "_reviews"))
        self.repos_before = self.tree(os.path.join("knowledge", "repos"))

    def write_reviews(self):
        """As reviews of merge requests leave them: one folder per merge request, and one for the ticket."""
        base = "https://example.atlassian.net"
        for rid, repo, iid in ((REVIEW, "app", 7), (REVIEW_BE, "be", 3)):
            job = {"id": rid, "kind": "review", "issue_key": "PROJ-9", "jira_base": base,
                   "issue_url": base + "/browse/PROJ-9", "stage": "done", "round": 1,
                   "created_at": "2026-01-03T10:00:00+00:00", "updated_at": "2026-01-03T11:00:00+00:00",
                   "host": "gitlab.example.com", "project": "group/" + repo, "repo": repo,
                   "reviewers": ["checklist", "requirement", "code"],
                   "mr": {"iid": iid, "title": "Invoices list", "author": "someone",
                          "url": "https://gitlab.example.com/group/%s/-/merge_requests/%d" % (repo, iid)},
                   "ticket": {"key": "PROJ-9", "lead": REVIEW, "members": [REVIEW, REVIEW_BE]},
                   "runs": [{"agent": "checklist", "started": "2026-01-03T10:00:00+00:00",
                             "ended": "2026-01-03T10:05:00+00:00", "status": "done"}],
                   "history": [{"at": "2026-01-03T11:00:00+00:00", "stage": "done", "note": "posted"}]}
            self.write_job(rid, {"job.json": json.dumps(job, indent=2) + "\n",
                                 "round-1/review.md": "an open hole in %s\n" % repo,
                                 "round-1/diff.patch": "+a change under review\n", "round-1/run/app.log": "started\n"})
        ticket = {"key": "PROJ-9", "members": [REVIEW, REVIEW_BE], "lead": REVIEW, "stage": "done", "fit": "ok",
                  "draft": None, "round": 1, "updated_at": "2026-01-03T11:00:00+00:00"}
        self.write_job(TICKET, {"ticket.json": json.dumps(ticket, indent=1), "round-1/fit.md": "FIT: OK\n"})

    def tree(self, rel):
        """{path: bytes} of every file under rel in the factory repository."""
        root, found = os.path.join(self.f.factory, rel), {}
        for d, _, names in os.walk(root):
            for n in names:
                with open(os.path.join(d, n), "rb") as fh:
                    found[os.path.relpath(os.path.join(d, n), root)] = fh.read()
        return found

    def assert_left_alone(self):
        self.assertEqual(self.tree(os.path.join("jobs", "_reviews")), self.reviews_before)
        self.assertEqual(self.tree(os.path.join("knowledge", "repos")), self.repos_before)

    def set_config(self, dotted, value):
        cfg = load_json(self.settings_path)
        *parents, last = dotted.split(".")
        block = cfg
        for p in parents:
            block = block.setdefault(p, {})
        block[last] = value
        write(self.settings_path, json.dumps(cfg, indent=1))

    def lessons(self, action, stdin="", *argv):
        with mock.patch.object(fx.sys, "stdin", io.StringIO(stdin)):
            return self.fx("lessons", action, *argv)

    def assert_no_review_lesson(self, text, msg=None):
        self.assertEqual(review_lines(text), [], msg)
        self.assertNotIn("review]", text, msg)


# --------------------------------------------------------------------------
# S1. their settings in factory.json

class ReviewSettingsTest(SharedCase):
    def test_they_are_never_an_error(self):
        cfg = fx.jobs_store_config(self.f.ctx())
        self.assertNotIn("include_reviews", cfg)
        for argv in (("status",), ("jobs", "status"), ("ctx", "T-1", "--agent", "code"), ("lessons", "stats")):
            code, res = self.fx(*argv)
            self.assertEqual(code, 0, (argv, res))
        # models.review is no risk level: the job's models still come from the floor
        models, _ = fx.route(self.f.ctx(), dict(self.f.job(), risk="high"))
        self.assertEqual(set(models.values()), {"sonnet"})
        self.assertEqual(slurp(self.settings_path), self.settings_text)

    def test_doctor_fails_on_nothing_they_hold(self):
        code, res = self.fx("doctor")
        with_them = {c["check"]: c["ok"] for c in res["checks"]}
        self.assertNotIn("_reviews", json.dumps(res))
        self.assertEqual(slurp(self.settings_path), self.settings_text)
        write(self.settings_path, json.dumps(review_settings_removed(load_json(self.settings_path)), indent=1))
        code, res = self.fx("doctor")
        self.assertEqual(with_them, {c["check"]: c["ok"] for c in res["checks"]})

    def test_init_keeps_them_when_it_rewrites_the_file(self):
        self.set_config("base_branch", "develop")  # not on the remote: init sets the default branch and writes the file
        code, res = self.fx("init", "--project", self.f.origin)
        self.assertEqual(code, 0, res)
        cfg = load_json(self.settings_path)
        self.assertEqual(cfg["base_branch"], "main")
        for key in ("review", "jira", "learning"):
            self.assertEqual(cfg[key], REVIEW_SETTINGS[key])
        self.assertEqual(cfg["models"]["review"], REVIEW_SETTINGS["models"]["review"])
        self.assertIs(cfg["jobs_store"]["include_reviews"], True)

    def assert_review_settings_kept(self, cfg):
        self.assertEqual(cfg["review"], REVIEW_SETTINGS["review"])
        self.assertEqual(cfg["models"]["review"], REVIEW_SETTINGS["models"]["review"])
        for key in ("code_owner", "subtask_type"):
            self.assertEqual(cfg["jira"][key], REVIEW_SETTINGS["jira"][key])
        for key in ("review_changes", "conflict"):
            self.assertEqual(cfg["jira"]["transitions"][key], REVIEW_SETTINGS["jira"]["transitions"][key])
        self.assertIs(cfg["jobs_store"]["include_reviews"], True)
        for key in ("review_missed", "review_rejections", "review_outcomes", "review_followup"):
            self.assertIs(cfg["learning"]["sources"][key], True)

    def test_init_keeps_another_skill_s_permission(self):
        # Another plugin's skill may be named factory too, so its fx path ends the same way: its rule stays.
        version_dir = os.path.dirname(os.path.dirname(fx.SKILL_DIR))
        older = "Bash(python3 %s *)" % os.path.join(os.path.dirname(version_dir), "0.9.0", "skills", "factory", "fx")
        other = "Bash(python3 /home/someone/.claude/plugins/cache/tools/other-plugin/2.0.0/skills/factory/fx *)"
        personal = "Bash(python3 /home/someone/.claude/skills/factory/fx *)"
        current = "Bash(%s *)" % fx.FX_CMD
        path = os.path.join(self.f.factory, ".claude", "settings.local.json")
        write(path, json.dumps({"permissions": {"allow": [other, current, older, personal, "Bash(npm test *)"]}}))
        code, res = self.fx("init", "--project", self.f.origin)
        self.assertEqual(code, 0, res)
        allow = load_json(path)["permissions"]["allow"]
        for rule in (other, personal, "Bash(npm test *)", current):
            self.assertIn(rule, allow)
        self.assertEqual(allow.count(current), 1, "this fx's own rule is kept, once")
        self.assertNotIn(older, allow, "this plugin's older version")

    def test_only_this_plugin_s_other_versions_are_older_copies(self):
        cache = os.path.join(self.tmp, "cache")
        fx_at = lambda *parts: os.path.join(cache, *parts + ("skills", "factory", "fx"))
        rule = lambda path: "Bash(python3 %s *)" % path
        here = fx_at("market", "code-factory", "1.0.0")
        write(os.path.join(cache, "market", "code-factory", "1.0.0", ".claude-plugin", "plugin.json"), "{}")
        self.assertTrue(fx.older_fx_rule(rule(fx_at("market", "code-factory", "0.9.0")), here))
        for path in (here, fx_at("tools", "other-plugin", "2.0.0"), fx_at("market", "other-plugin", "1.0.0")):
            self.assertFalse(fx.older_fx_rule(rule(path), here), path)
        # install.sh: fx in one folder of its own, never another version's
        personal = os.path.join(self.tmp, "home", ".claude", "skills", "factory", "fx")
        other = os.path.join(self.tmp, "home", ".claude-work", "skills", "factory", "fx")
        self.assertFalse(fx.older_fx_rule(rule(other), personal))

    def old_layout(self):
        """A project folder as an older version left it, with the factory inside."""
        src = os.path.join(self.tmp, "old-project")
        old = os.path.join(src, "factory")
        write(os.path.join(old, "config.json"), json.dumps({
            "max_rounds": 5, "jira": {"transitions": {"start": "Doing"}}, "models": {"floor": "opus"},
            "learning": {"sources": {"pr_comments": False}}, "jobs_store": {"auto": False}}))
        write(os.path.join(old, "lessons.md"), "# Lessons learned\n\n- [L1] [code] An old rule\n")
        write(os.path.join(old, "knowledge", "README.md"), "# Knowledge\n\n- overview.md\n")
        write(os.path.join(old, "knowledge", "overview.md"), "# Overview\n\nThe old words.\n")
        return src

    def test_an_import_leaves_them_as_they_are(self):
        code, res = self.fx("init", "--project", self.f.origin, "--import", self.old_layout())
        self.assertEqual(code, 0, res)
        self.assertEqual(res["imported"], ["factory.json (from config.json)", "knowledge/"])
        cfg = load_json(self.settings_path)
        self.assertEqual((cfg["max_rounds"], cfg["jira"]["transitions"]["start"], cfg["models"]["floor"],
                          cfg["learning"]["sources"]["pr_comments"], cfg["jobs_store"]["auto"]), (5, "Doing", "opus",
                                                                                                False, False))
        self.assert_review_settings_kept(cfg)
        self.assertEqual(slurp(self.lessons_path), LESSONS)
        self.assertTrue([n for n in res["notes"] if n.startswith("lessons.md was not imported")], res["notes"])
        knowledge = os.path.join(self.f.factory, "knowledge")
        self.assertEqual(slurp(os.path.join(knowledge, "overview.md")), "# Overview\n\nThe old words.\n")
        self.assertFalse(os.path.exists(os.path.join(knowledge, "style.md")), "the old knowledge, as before")
        self.assert_left_alone()

    def test_an_import_takes_lessons_without_them_as_before(self):
        write(self.lessons_path, "# Lessons learned\n\n- [L1] [code] A rule of this repository\n")
        code, res = self.fx("init", "--project", self.f.origin, "--import", self.old_layout())
        self.assertEqual(code, 0, res)
        self.assertIn("lessons.md", res["imported"])
        self.assertIn("An old rule", slurp(self.lessons_path))


# --------------------------------------------------------------------------
# S2. jobs/_reviews/ holds no job

class ReviewsAreNotJobsTest(SharedCase):
    def test_status_and_the_board_list_only_the_jobs(self):
        code, res = self.fx("status")
        self.assertEqual(code, 0, res)
        self.assertEqual([r["job"] for r in res["jobs"]], ["PROJ-9", "T-1"])
        self.assertNotIn("_reviews", json.dumps(res))
        data = fx.board_data([self.f.factory])
        self.assertEqual(sorted(v["id"] for v in data["active"] + data["done"]), ["PROJ-9", "T-1"])
        self.assertEqual(data["factories"][0]["jobs"], 2)
        code, res = self.fx("board")
        self.assertEqual(code, 0, res)
        page = slurp(fx.board_file())
        for name in ("PROJ-9-mr7", "PROJ-9-be-mr3", "_ticket-PROJ-9"):
            self.assertNotIn(name, page)
        self.assert_left_alone()

    def test_naming_one_is_refused_and_never_taken_for_the_issue_s_job(self):
        job = slurp(os.path.join(self.f.factory, "jobs", "PROJ-9", "job.json"))
        for argv in (("show", REVIEW), ("cleanup", REVIEW, "--force"), ("cleanup", "jobs/" + TICKET, "--force"),
                     ("ctx", REVIEW, "--agent", "builder"), ("jira-done", REVIEW_BE, "--all"),
                     ("show", "_REVIEWS/PROJ-9-MR7"), ("show", " %s " % REVIEW),
                     # a review's folder named alone, as merge-request review names it, holds the issue key too
                     ("show", "PROJ-9-mr7"), ("show", "proj-9-be-mr3"), ("show", "_ticket-PROJ-9"),
                     ("cleanup", "PROJ-9-mr7"), ("ctx", "PROJ-9-be-mr3", "--agent", "code")):
            code, res = self.fx(*argv)
            self.assertEqual((code, res["error"]), (3, NOT_JOBS), argv)
        self.assertEqual(slurp(os.path.join(self.f.factory, "jobs", "PROJ-9", "job.json")), job)
        self.assert_left_alone()

    def test_doctor_names_only_the_jobs_clones(self):
        work = self.f.ctx().work
        for name in ("_review-PROJ-9-mr7", "_sandbox-PROJ-9-mr7", "_review-code-fe"):
            os.makedirs(os.path.join(work, name))
        code, res = self.fx("doctor")
        row = next(c for c in res["checks"] if c["check"] == "project clones on this machine")
        self.assertEqual(row["detail"], "T-1")
        self.assertNotIn("mr7", json.dumps(res))

    def test_cleanup_of_the_issue_s_job_leaves_them(self):
        code, res = self.fx("cleanup", "PROJ-9")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["job"], "PROJ-9")
        self.assert_left_alone()

    def test_jobs_status_does_not_count_them(self):
        code, res = self.fx("jobs", "status")
        self.assertEqual(code, 0, res)
        self.assertEqual(sorted(r["job"] for r in res["jobs"]), ["PROJ-9", "T-1"])
        self.assertEqual(sorted(res["unsaved"]), ["PROJ-9", "T-1"])

    def test_push_all_leaves_them_here_whatever_include_reviews_says(self):
        code, res = self.push("--all")
        self.assertEqual(code, 0, res)
        self.assertEqual(sorted(r["job"] for r in res["jobs"]), ["PROJ-9", "T-1"])
        self.assertFalse([k for k in self.s3.objects if "_reviews" in k or "_ticket" in k])
        uploaded = {k.rsplit("/", 1)[-1] for k in self.content_keys()}
        for rel, data in self.reviews_before.items():
            self.assertNotIn(sha(data), uploaded, rel)
        self.assert_left_alone()

    def test_naming_one_in_a_push_or_a_pull_is_refused(self):
        for ref in (REVIEW, TICKET, REVIEW_BE + "/", "jobs/" + REVIEW, "_reviews", "_Reviews/PROJ-9-mr7",
                    " %s" % REVIEW, "PROJ-9-mr7", "_ticket-PROJ-9", "PROJ-9-BE-MR3"):
            for action in ("push", "pull"):
                code, res = self.fx("jobs", action, ref)
                self.assertEqual((code, res["error"]), (3, NOT_PUSHED), (action, ref))
        self.assertEqual(self.s3.objects, {})
        self.assertEqual(self.s3.log, [])
        self.assert_left_alone()

    def test_pull_all_and_list_pass_over_a_review_on_s3(self):
        # A review as it would be on S3: its files by hash and a snapshot that fx could restore.
        files = {"job.json": self.reviews_before["PROJ-9-mr7/job.json"],
                 "round-1/review.md": self.reviews_before["PROJ-9-mr7/round-1/review.md"]}
        for data in files.values():
            self.s3.objects["%s/jobs/sha256/%s/%s" % (PREFIX, sha(data)[:2], sha(data))] = data
        snap = {"version": fx.JOBS_SNAPSHOT_VERSION, "job": REVIEW, "created_at": "2026-01-03T10:00:00+00:00",
                "saved_at": "2026-01-03T11:00:00+00:00", "fx_version": "1.0.0", "stage": "done", "summary": None,
                "pr_url": None, "factory_root": self.f.factory, "work_root": self.f.ctx().work, "skipped": [],
                "files": [{"path": rel, "sha256": sha(data), "size": len(data)} for rel, data in sorted(files.items())]}
        self.assertEqual(sorted(fx.jobs_check_snapshot(snap, REVIEW)), sorted(files))
        self.s3.objects["%s/jobs/snapshots/%s/20260103T110000Z-0123abcd.json" % (PREFIX, REVIEW)] = \
            json.dumps(snap).encode()
        code, res = self.push("T-1")
        self.assertEqual(code, 0, res)
        code, res = self.fx("jobs", "list")
        self.assertEqual(code, 0, res)
        self.assertEqual([r["job"] for r in res["jobs"]], ["T-1"])
        shutil.rmtree(os.path.join(self.f.factory, "jobs"))
        code, res = self.fx("jobs", "pull", "--all")
        self.assertEqual(code, 0, res)
        self.assertEqual([r["job"] for r in res["jobs"]], ["T-1"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.f.factory, "jobs"))), ["T-1"])


# --------------------------------------------------------------------------
# S3. lessons tagged [review]

class ReviewLessonsTest(SharedCase):
    def test_no_agent_gets_them(self):
        for agent in CTX_AGENTS:
            code, res = self.fx("ctx", "T-1", "--agent", agent)
            self.assertEqual(code, 0, (agent, res))
            self.assertNotIn(os.path.join(self.f.factory, "knowledge", "repos"), res["prompt"], agent)
            given = [line.split(": ", 1)[1] for line in res["prompt"].splitlines() if line.startswith("Lessons")]
            if agent == "intake":
                self.assertEqual(given, [])
                continue
            text = slurp(given[0])
            self.assert_no_review_lesson(text, agent)
            self.assertIn("Keep each commit to one change", text, agent)  # untagged: every agent
        self.assertIn("Every list endpoint is paginated", slurp(os.path.join(self.f.jd, "lessons-builder.md")))
        self.assertEqual(slurp(self.lessons_path), LESSONS)

    def test_whatever_the_reader_the_filter_leaves_them_out(self):
        readers = {a for topics in fx.LESSON_TOPICS.values() for a in topics} | {"intake", "librarian", "planner", None}
        for agent in readers:
            text, dropped = fx.lessons_kept(LESSONS, agent)
            self.assert_no_review_lesson(text, agent)
            self.assertGreaterEqual(dropped, len(REVIEW_LINES))
        text, dropped = fx.lessons_kept(LESSONS)  # every topic: what the librarian and fx lessons collect see
        self.assertEqual(dropped, len(REVIEW_LINES))
        for rule in OTHER_RULES:
            self.assertIn(rule, text)

    def test_what_is_written_under_one_goes_with_it(self):
        under = "  - always ask for screenshots first\n    then once more\n\n  and after a blank line too\n"
        text = LESSONS.replace(REVIEW_LINES[0] + "\n", REVIEW_LINES[0] + "\n" + under)
        kept_under = LESSONS.replace("from T-1 -->\n- [L2]", "from T-1 -->\n  - cursor-based\n- [L2]")
        for agent in (None, "builder"):
            text_, dropped = fx.lessons_kept(text, agent)
            for words in ("screenshots", "then once more", "after a blank line"):
                self.assertNotIn(words, text_, agent)
            self.assertIn("Every list endpoint is paginated", text_, agent)
            self.assertIn("Every bug fix starts with a failing test", text_, agent)
            self.assertIn("  - cursor-based", fx.lessons_kept(kept_under, agent)[0], "under a lesson that stays")
        self.assertNotIn("cursor-based", fx.lessons_kept(kept_under, "ux")[0], "nor under one left out by topic")
        # after the last one left out, the blank line before the next heading stays
        self.assertIn("\n\n## 2026-01-05 (from T-1)\n", fx.lessons_kept(text)[0])

    def test_collecting_keeps_their_place_in_what_is_pending(self):
        path = os.path.join(self.f.factory, "jobs", "_learn", "pending.json")
        theirs = {REVIEW: {"factory": True, "comments": ["n1"]}, REVIEW_BE: {"factory": False}}
        write(path, json.dumps(dict(theirs, **{"T-1": {"factory": True}})))
        code, res = self.lessons("collect")
        self.assertEqual(code, 0, res)
        pending = load_json(path)
        for rid, cur in theirs.items():
            self.assertEqual(pending[rid], cur)
        self.assertNotIn("T-1", pending, "a job's cursors are this collect's")

    def test_the_librarian_does_not_get_them(self):
        base = sh(["git", "rev-parse", "main"], self.f.origin).strip()
        write(os.path.join(self.f.factory, "knowledge", ".ref"), json.dumps({"commit": base}))
        clone = os.path.join(self.tmp, "dev")
        sh(["git", "clone", "-q", self.f.origin, clone], self.tmp)
        write(os.path.join(clone, "package.json"), '{"name": "app"}\n')
        sh(["git", "add", "package.json"], clone)
        sh(["git", "commit", "-q", "-m", "a dependency"], clone)
        sh(["git", "push", "-q", "origin", "HEAD:main"], clone)
        code, res = self.fx("knowledge", "ctx")
        self.assertEqual(code, 0, res)
        given = [line.split(": ", 1)[1] for line in res["prompt"].splitlines() if line.startswith("Lessons")]
        self.assertEqual(given, [os.path.join(self.f.factory, "jobs", "_learn", "lessons-librarian.md")])
        self.assertFalse(given[0].startswith(res["stage"] + os.sep))  # not a staged knowledge file
        text = slurp(given[0])
        self.assert_no_review_lesson(text)
        for rule in OTHER_RULES:  # every topic, for context
            self.assertIn(rule, text)
        self.assertNotIn(os.path.join(self.f.factory, "knowledge", "repos"), res["prompt"])
        self.assertEqual(slurp(self.lessons_path), LESSONS)

    def test_lessons_collect_does_not_show_them(self):
        code, res = self.lessons("collect")
        self.assertEqual(code, 0, res)
        self.assert_no_review_lesson(res["existing"])
        for rule in OTHER_RULES:
            self.assertIn(rule, res["existing"])
        self.assertEqual([lesson["id"] for lesson in res["stats"]["lessons"]], ["L1", "L3", "L6", None])
        self.assertEqual(slurp(self.lessons_path), LESSONS)

    def test_saving_and_updating_leave_them_as_written(self):
        code, res = self.lessons("update", json.dumps({"add": ["[code] Name every query"],
                                                       "replace": {"L1": "[code] Every list endpoint is paginated and "
                                                                           "sorted"},
                                                       "delete": ["L3"]}), "--jobs", "T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["changed"], {"added": ["L8"], "replaced": ["L1"], "deleted": ["L3"]})
        self.assertEqual(review_lines(slurp(self.lessons_path)), REVIEW_LINES)
        text = slurp(self.lessons_path)
        self.assertIn("- [L7] Keep each commit to one change", text)  # after the highest id, a [review] one's included
        self.assertNotIn("Every bug fix starts with a failing test", text)
        code, res = self.lessons("save", "- [ui] Buttons say what they do\n- [data] Every migration has a down step\n",
                                 "--jobs", "T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["changed"]["added"], ["L9", "L10"])
        self.assertEqual(review_lines(slurp(self.lessons_path)), REVIEW_LINES)
        code, res = self.lessons("update", "{}")
        self.assertEqual(code, 0, res)
        self.assertEqual(review_lines(slurp(self.lessons_path)), REVIEW_LINES)
        code, res = self.lessons("graduate", json.dumps({"L6": "style.md"}))
        self.assertEqual(code, 0, res)
        self.assertEqual(review_lines(slurp(self.lessons_path)), REVIEW_LINES)
        self.assertIn("Every new endpoint says who may call it", slurp(os.path.join(self.f.factory, "knowledge",
                                                                                      "style.md")))

    def test_their_lines_cannot_be_changed_by_id(self):
        knowledge = self.tree("knowledge")
        kept, moved = "it leaves them as written", "they never move into the knowledge"
        for action, stdin, then in (("update", {"replace": {"L2": "[code] Something else"}}, kept),
                                    ("update", {"delete": ["l4"]}, kept),
                                    ("update", {"replace": {"L1": "[code] Fine"}, "delete": ["L5"]}, kept),
                                    ("graduate", {"L1": "style.md", "L4": "style.md"}, moved)):
            code, res = self.lessons(action, json.dumps(stdin))
            self.assertEqual(code, 1, (action, stdin, res))
            self.assertIn("lessons are for reviews of merge requests, which code-factory does not handle: " + then,
                          res["error"])
        self.assertEqual(slurp(self.lessons_path), LESSONS)
        self.assertEqual(self.tree("knowledge"), knowledge)

    def test_learning_never_writes_a_new_one(self):
        for action, stdin in (("update", json.dumps({"add": ["[review] Ask for screenshots"]})),
                              ("update", json.dumps({"add": ["[code] Fine", "[code, review] Ask for screenshots"]})),
                              ("update", json.dumps({"replace": {"L1": "[review] Ask for screenshots"}})),
                              ("save", "- [ui] Fine\n- [review] Ask for screenshots\n"),
                              ("save", REVIEW_LINES[0] + "\n")):
            code, res = self.lessons(action, stdin)
            self.assertEqual(code, 1, (action, stdin, res))
            self.assertIn("which code-factory does not handle: it never writes one", res["error"])
        self.assertEqual(slurp(self.lessons_path), LESSONS)

    def test_graduation_never_proposes_them(self):
        self.set_config("learning.graduate_after_jobs", 1)
        self.f.save_job(stage="handed-off", created_at="2026-02-01T10:00:00+00:00")
        code, res = self.lessons("stats")
        self.assertEqual(code, 0, res)
        self.assertEqual(sorted(g["id"] for g in res["graduate"]), ["L1", "L3", "L6"])
        self.assertEqual([lesson["id"] for lesson in res["lessons"]], ["L1", "L3", "L6", None])

    def test_stats_and_doctor_do_not_count_them(self):
        self.set_config("learning.max_lessons", 4)  # with them the file holds 8
        code, res = self.lessons("stats")
        self.assertEqual(code, 0, res)
        self.assertEqual((res["count"], res["over_limit"], res["untagged"]), (4, False, []))
        code, res = self.fx("doctor")
        row = next(c for c in res["checks"] if c["check"] == "lessons.md size (advice)")
        self.assertEqual(row["detail"], "3 lessons (limit 4)")

    def test_a_review_is_never_marked_as_learned_from(self):
        for action, stdin in (("update", "{}"), ("save", "- [code] Fine\n")):
            for jobs in ("T-1," + REVIEW, "T-1, " + REVIEW, "T-1,PROJ-9-mr7"):
                code, res = self.lessons(action, stdin, "--jobs", jobs)
                self.assertEqual((code, res["error"]), (3, NOT_JOBS), jobs)
        self.assertEqual(slurp(self.lessons_path), LESSONS)
        self.assertNotIn("learn", self.f.job())
        self.assert_left_alone()


# --------------------------------------------------------------------------
# S4. knowledge/repos/

class RepositoriesKnowledgeTest(SharedCase):
    def test_no_agent_gets_it(self):
        ctx = self.f.ctx()
        roles = {r for v in fx.KNOWLEDGE_ROLES.values() if v != "*" for r in v.split()} | set(CTX_AGENTS)
        for agent in roles:
            files = fx.knowledge_files(ctx, agent)
            self.assertTrue(files, agent)
            self.assertFalse([f for f in files if os.sep + "repos" + os.sep in f], agent)
        code, res = self.fx("knowledge", "review")
        self.assertEqual(code, 0, res)
        self.assertNotIn(os.path.join(self.f.factory, "knowledge", "repos"), res["prompt"])
        self.assert_left_alone()

    def test_the_knowledge_check_leaves_it_out(self):
        os.environ["STUB_GITLEAKS_FIND"] = "hunter2hunter2"
        code, res = self.fx("knowledge", "check")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["problems"], [])
        self.assertTrue(res["clean"], res)
        # the same lines in this project's knowledge fail the check: it is the folder that is left out
        write(os.path.join(self.f.factory, "knowledge", "fe.md"), REPO_KNOWLEDGE)
        code, res = self.fx("knowledge", "check")
        self.assertEqual(code, 0, res)
        self.assertFalse(res["clean"])
        self.assertTrue(res["problems"])
        self.assertFalse([p for p in res["problems"] if not p.startswith("fe.md:")], res["problems"])
        self.assert_left_alone()


if __name__ == "__main__":
    unittest.main()
