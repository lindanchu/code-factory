"""Tests for the text fx posts for people (standard library only). Run from the repository root:
    python3 -I -m unittest discover -s tests

What a person reads or acts on has two parts: the orchestrator's summary for people first (--summary-file), then what fx
writes, the technical part. That is the comment when intake turns an issue down, the comment on an issue that differs
from logic/, the questions fx ask-jira posts, and the pull request description and Jira comment of fx pr. The files the
agents write for people are read in their older and their newer format: questions.md with or without Easy: and
Technical: lines, decision.md with or without an easy and a technical part under ## For the human.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unicodedata
import unittest
from unittest import mock

from test_jira import KEY as ISSUE, TOKEN, JiraCase
from test_jobs_store import PushCase
from test_multirepo import KEY as FEATURE, MultiCase
from test_security import FX_PATH, TEMPLATE, FxTestCase, fx, load_json, sh, slurp, write

SITE = "https://example.atlassian.net"
SUMMARY = ("People who forgot their password cannot get back in yet. Nothing in the app was changed. Not checked on "
           "a phone.")
Q1 = "Should a locked account be able to reset its password?"
VI = "Người dùng đặt lại được mật khẩu."
MISSING = "a summary for people is missing"

QUESTIONS_NEW = """## Q1: Should a locked account be able to reset its password?
Why: it decides whether the reset page checks the lock.
Easy: someone whose account was locked after too many tries clicks "Forgot password" and gets an e-mail anyway.
Technical: src/auth/reset.ts:42 sends the link without reading users.locked_at; the spec says nothing of it.
Options:
- Allow it: the reset also unlocks the account; nobody has more to do.
- Block it: the page says to call support; support has more to do.
Recommended: Allow it

## Q2: Which e-mail text?
Why: every user reads it.
Easy: the e-mail says "Click here" and nothing of how long the link works.
Technical: templates/mail/reset.html has no expiry line; config/auth.php:12 sets 60 minutes.
Options:
- Say 60 minutes: users know when to hurry.
- Leave it: nothing changes.
Recommended: Say 60 minutes
"""

# The same questions as the newer format puts them for people: each Easy: line right under its question.
QUESTIONS_EASY_FIRST = """## Q1: Should a locked account be able to reset its password?
Easy: someone whose account was locked after too many tries clicks "Forgot password" and gets an e-mail anyway.
Why: it decides whether the reset page checks the lock.
Technical: src/auth/reset.ts:42 sends the link without reading users.locked_at; the spec says nothing of it.
Options:
- Allow it: the reset also unlocks the account; nobody has more to do.
- Block it: the page says to call support; support has more to do.
Recommended: Allow it

## Q2: Which e-mail text?
Easy: the e-mail says "Click here" and nothing of how long the link works.
Why: every user reads it.
Technical: templates/mail/reset.html has no expiry line; config/auth.php:12 sets 60 minutes.
Options:
- Say 60 minutes: users know when to hurry.
- Leave it: nothing changes.
Recommended: Say 60 minutes"""

QUESTIONS_OLD = """## Q1: Should a locked account be able to reset its password?
Why: it decides whether the reset page checks the lock.
Options:
- Allow it: the reset also unlocks the account.
- Block it: the page says to call support.
Recommended: Allow it
"""

ANSWERS = """## Q1: Should a locked account be able to reset its password?
Answer: Allow it

## Q2: Which e-mail text?
Answer: Say 60 minutes
"""

DECISION_NEW = """APPROVE

## Why
The reset works as the issue asks.

## Acceptance criteria
1. met: the link resets the password (a test was added)

## Spot check
Ran the reset on the job's app.

## For the human
### Easy version
Nothing needs a look before merging. The e-mail was not looked at on a phone.
### Technical version
Nothing blocking; templates/mail/reset.html was not rendered at 390 px (not measured).
"""

DECISION_OLD = """APPROVE

## Why
The reset works as the issue asks.

## For the human
Nothing to look at.
"""


def two_parts(summary, technical, vietnamese=False):
    head, tech = ("## Tóm tắt", "## Phần kỹ thuật") if vietnamese else ("## Summary", "## Technical part")
    return "%s\n\n%s\n\n%s\n\n%s" % (head, summary, tech, technical)


class PeopleCase(FxTestCase):
    """Job T-1 is about the Jira issue T-1. In mode mcp every comment is queued, where the test reads it as it goes."""
    cfg = {"jira": {"mode": "mcp"}, "copy_into_worktree": [".env"], "security": {"required": []}}

    def setUp(self):
        super().setUp()
        self.f.save_job(jira_base=SITE, issue_url="%s/browse/T-1" % SITE)

    def summary_file(self, text=SUMMARY, name="summary.md"):
        path = os.path.join(self.f.jd, name)
        write(path, text)
        return path

    def language(self, value):
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["language"] = value
        write(path, json.dumps(cfg, indent=1))

    def local_secret(self):
        write(os.path.join(self.f.factory, "local", ".env"), "DB_PASSWORD=Zx81-real-pw\n")
        return "Zx81-real-pw"

    def comments(self):
        return [i["markdown"] for i in self.f.job().get("jira_todo", []) if i["action"] == "comment"]

    def missing(self, res):
        return [n for n in res.get("notes") or [] if MISSING in n]

    def again(self, res):
        return [n for n in res.get("notes") or [] if "earlier run of this step" in n]

    def interrupted(self, where, *argv):
        """fx stops in `where`, as it would on Ctrl-C there."""
        with mock.patch.object(fx, where, side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                fx.main(["--repo", self.f.factory] + list(argv))
        os.chdir(self.cwd)


# --------------------------------------------------------------------------
# the two parts

class HeadingsTest(unittest.TestCase):
    def ctx(self, language):
        return types.SimpleNamespace(c=lambda key, default=None: language if key == "language" else default)

    def test_vietnamese_by_any_of_its_names(self):
        for name in ("vi", "VI", "vi-VN", "vi_VN", "Vietnamese", "vietnamese (default)", "Tiếng Việt",
                     "tiếng việt", "Tieng Viet"):
            self.assertTrue(fx.speaks_vietnamese(name), name)
        for name in ("", None, "English", "en", "en-US", "fr", "Deutsch"):
            self.assertFalse(fx.speaks_vietnamese(name), name)

    def test_the_summary_first_then_what_fx_writes(self):
        self.assertEqual(fx.two_parts(self.ctx("English"), "For people.\n", "**Software factory** did it."),
                         "## Summary\n\nFor people.\n\n## Technical part\n\n**Software factory** did it.")
        self.assertEqual(fx.two_parts(self.ctx("Tiếng Việt"), "Cho mọi người.", "x\n"),
                         "## Tóm tắt\n\nCho mọi người.\n\n## Phần kỹ thuật\n\nx\n")
        self.assertEqual(fx.two_parts(self.ctx(""), "For people.", "x"),
                         "## Summary\n\nFor people.\n\n## Technical part\n\nx")
        self.assertEqual(fx.two_parts(self.ctx("vi"), None, "only what fx writes"), "only what fx writes")

    def test_the_board_takes_the_same_names(self):
        tmp = tempfile.mkdtemp(prefix="fx-people-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for language, lang in (("Tiếng Việt", "vi"), ("vi", "vi"), ("English", "en")):
            write(os.path.join(tmp, "factory.json"), json.dumps({"language": language, "project": {"name": "p"}}))
            self.assertEqual(fx.board_data([tmp])["lang"], lang, language)

    def test_the_template_says_so(self):
        doc = load_json(TEMPLATE)["_doc_language"]
        for words in ("## Summary", "## Technical part", "## Tóm tắt", "## Phần kỹ thuật", "Tiếng Việt"):
            self.assertIn(words, doc)

    def test_the_three_commands_take_the_file(self):
        for command in ("result", "ask-jira", "pr"):
            p = subprocess.run([sys.executable, "-I", FX_PATH, command, "--help"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
            self.assertEqual(p.returncode, 0, p.stdout)
            self.assertIn("--summary-file", p.stdout)


# --------------------------------------------------------------------------
# the files the agents write for people, in both formats

class QuestionsFormatTest(unittest.TestCase):
    def test_the_questions_are_read_in_both(self):
        self.assertEqual(fx.parse_questions(QUESTIONS_NEW), ["Q1: " + Q1, "Q2: Which e-mail text?"])
        self.assertEqual(fx.parse_questions(QUESTIONS_OLD), ["Q1: " + Q1])

    def test_the_answers_are_read_as_before(self):
        self.assertEqual(fx.parse_answers(ANSWERS), [("Q1: " + Q1, "Allow it"),
                                                     ("Q2: Which e-mail text?", "Say 60 minutes")])

    def test_the_easy_line_comes_first_and_nothing_is_lost(self):
        self.assertEqual(fx.easy_first(QUESTIONS_NEW), QUESTIONS_EASY_FIRST)
        self.assertEqual(sorted(QUESTIONS_NEW.splitlines()), sorted(QUESTIONS_EASY_FIRST.splitlines()))

    def test_the_older_format_stays_as_written(self):
        self.assertEqual(fx.easy_first(QUESTIONS_OLD), QUESTIONS_OLD.rstrip("\n"))
        self.assertEqual(fx.easy_first(QUESTIONS_EASY_FIRST), QUESTIONS_EASY_FIRST)

    def test_what_is_around_the_questions_stays(self):
        text = ("# Questions\n\nPlease answer.\n\n## Q1: Which?\n\nWhy: w\neasy: e\n  that runs on\nTechnical: t\n"
                "Options:\n- A: a\nRecommended: A\n\n## Notes\nEasy: not a question\n")
        self.assertEqual(fx.easy_first(text), "# Questions\n\nPlease answer.\n\n## Q1: Which?\n\neasy: e\n"
                                              "  that runs on\nWhy: w\nTechnical: t\nOptions:\n- A: a\n"
                                              "Recommended: A\n\n## Notes\nEasy: not a question")


# --------------------------------------------------------------------------
# intake turns the issue down

class IntakeRejectTest(PeopleCase):
    REASON = "The issue asks for two things that cannot both be true."

    def setUp(self):
        super().setUp()
        self.f.save_job(stage="intake")
        write(os.path.join(self.f.jd, "intake.md"),
              "INTAKE: REJECT\nRISK: low\nSURFACES: -\n\n## Reason\n%s\n" % self.REASON)
        self.technical = ("**Software factory did not take T-1.**\n\n%s\n\nThe run stopped; nothing was changed in the "
                          "code." % self.REASON)

    def reject(self, *argv):
        code, res = self.fx("result", "T-1", "intake", *argv)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "reject")
        return res

    def test_the_summary_goes_first(self):
        res = self.reject("--summary-file", self.summary_file())
        self.assertEqual(self.comments(), [two_parts(SUMMARY, self.technical)])
        self.assertNotIn("notes", res)
        job = self.f.job()
        self.assertEqual(job["stage"], "rejected")
        self.assertNotIn("people_summary", job, "nothing of it is left once the comment went")

    def test_without_one_the_comment_still_goes_and_the_notes_say_so(self):
        res = self.reject()
        self.assertEqual(self.comments(), [self.technical])
        self.assertEqual(len(self.missing(res)), 1, res)
        self.assertIn("the comment on T-1 has only the technical part", self.missing(res)[0])
        self.assertIn("--summary-file", self.missing(res)[0])

    def test_in_vietnamese(self):
        self.language("Tiếng Việt")
        self.reject("--summary-file", self.summary_file(VI))
        self.assertEqual(self.comments(), [two_parts(VI, self.technical, True)])

    def test_a_missing_or_empty_file_is_refused_before_anything_happens(self):
        for path in (os.path.join(self.tmp, "nowhere.md"), self.f.jd, self.summary_file(" \n", "blank.md"),
                     self.summary_file("## Summary\n\n", "heading-only.md")):
            code, res = self.fx("result", "T-1", "intake", "--summary-file", path)
            self.assertEqual(code, 3, res)
            self.assertIn("--summary-file %s" % path, res["error"])
            job = self.f.job()
            self.assertEqual(job["stage"], "intake")
            self.assertFalse(job.get("jira_todo"))
            self.assertNotIn("people_summary", job)

    def test_a_heading_of_its_own_is_not_written_twice(self):
        self.reject("--summary-file", self.summary_file("## Summary\n\n%s\n" % SUMMARY))
        self.assertEqual(self.comments(), [two_parts(SUMMARY, self.technical)])

    def test_a_relative_path_is_read_from_where_fx_was_started(self):
        self.summary_file()
        os.chdir(self.f.jd)
        self.reject("--summary-file", "summary.md")
        self.assertEqual(self.comments(), [two_parts(SUMMARY, self.technical)])

    def test_a_local_task_posts_nothing_and_needs_no_summary(self):
        self.f.save_job(jira_base=None, issue_url=None)
        res = self.reject()
        self.assertEqual(self.comments(), [])
        self.assertNotIn("notes", res)

    def test_comments_turned_off_need_no_summary(self):
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["jira"]["comment"] = False
        write(path, json.dumps(cfg))
        res = self.reject("--summary-file", self.summary_file())
        self.assertEqual(self.comments(), [])
        self.assertNotIn("notes", res)
        self.assertNotIn("people_summary", self.f.job())

    def test_a_secret_in_the_summary_is_redacted(self):
        pw = self.local_secret()
        key = "ghp_" + "aB3dE5" * 6
        res = self.reject("--summary-file", self.summary_file("The test login is %s and %s." % (pw, key)))
        posted = self.comments()[0]
        self.assertTrue(posted.startswith("## Summary\n\nThe test login is %s and %s." % (fx.REDACTED, fx.REDACTED)))
        self.assertNotIn(pw, posted)
        self.assertNotIn(key, posted)
        self.assertEqual(res["jira"]["redacted"], ["github-token", "local-env-value"])
        self.assertNotIn(pw, slurp(os.path.join(self.f.jd, "job.json")))

    def test_it_comes_back_when_the_step_runs_again_after_an_interruption(self):
        self.interrupted("jira_comment", "result", "T-1", "intake", "--summary-file", self.summary_file())
        self.assertEqual(self.f.job()["stage"], "intake", "the run stopped before the move: the step can run again")
        os.remove(os.path.join(self.f.jd, "summary.md"))
        res = self.reject()
        self.assertEqual(self.comments(), [two_parts(SUMMARY, self.technical)])
        self.assertEqual(len(self.again(res)), 1, res)
        self.assertFalse(self.missing(res))
        self.assertNotIn("people_summary", self.f.job())


# --------------------------------------------------------------------------
# the issue differs from logic/

class LogicConflictsTest(PeopleCase):
    CONFLICTS = "- logic/auth/reset.md says a locked account cannot reset its password; the issue says it can."

    def setUp(self):
        super().setUp()
        self.f.save_job(stage="spec")
        write(os.path.join(self.f.jd, "spec.md"),
              "SPEC: READY\n\n## Goal\nReset works.\n\n## Acceptance criteria\n1. The link resets the password.\n")
        write(os.path.join(self.f.jd, "logic-conflicts.md"), self.CONFLICTS + "\n")
        self.technical = ("**Software factory: this issue differs from the documented logic** (factory repository, "
                          "`logic/`). The team is asked before any code is written.\n\n" + self.CONFLICTS)

    def test_the_summary_goes_first_and_the_start_stays_short(self):
        code, res = self.fx("result", "T-1", "spec", "--summary-file", self.summary_file())
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "build")
        conflicts, start = self.comments()
        self.assertEqual(conflicts, two_parts(SUMMARY, self.technical))
        self.assertTrue(start.startswith("**Software factory started T-1**"), start)
        self.assertNotIn("notes", res)
        self.assertNotIn("people_summary", self.f.job())

    def test_without_one_the_notes_say_so(self):
        code, res = self.fx("result", "T-1", "spec")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.comments()[0], self.technical)
        self.assertEqual(len(self.missing(res)), 1, res)

    def test_in_vietnamese(self):
        self.language("vi")
        # a heading of its own, even with its accents written apart, is not written twice
        text = unicodedata.normalize("NFD", "## Tóm tắt") + "\n\nLogic cũ khác với yêu cầu."
        code, res = self.fx("result", "T-1", "spec", "--summary-file", self.summary_file(text))
        self.assertEqual(code, 0, res)
        self.assertEqual(self.comments()[0], two_parts("Logic cũ khác với yêu cầu.", self.technical, True))

    def test_differences_already_posted_take_nothing(self):
        self.f.save_job(logic_conflicts_posted=self.CONFLICTS[:200])
        code, res = self.fx("result", "T-1", "spec", "--summary-file", self.summary_file())
        self.assertEqual(code, 0, res)
        self.assertEqual(len(self.comments()), 1, "only the start")
        self.assertNotIn("notes", res)
        self.assertNotIn("people_summary", self.f.job())


# --------------------------------------------------------------------------
# questions on Jira, the answers, and the questions of a split build

class AskOnJiraTest(PeopleCase):
    def setUp(self):
        super().setUp()
        self.f.save_job(stage="needs-info", resume_stage="spec")

    def ask(self, questions, *argv):
        write(os.path.join(self.f.jd, "questions.md"), questions)
        return self.fx("ask-jira", "T-1", *argv)

    def technical(self, questions):
        return ("**Software factory needs information before continuing T-1.**\n\n%s\n\nReply in a comment, then run "
                "`/code-factory:factory resume T-1`." % questions.strip())

    def test_the_summary_goes_first_and_each_easy_line_under_its_question(self):
        code, res = self.ask(QUESTIONS_NEW, "--summary-file", self.summary_file())
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "stop")
        self.assertEqual(self.comments(), [two_parts(SUMMARY, self.technical(QUESTIONS_EASY_FIRST))])
        self.assertNotIn("notes", res)
        self.assertNotIn("people_summary", self.f.job())

    def test_the_older_format_goes_as_written(self):
        code, res = self.ask(QUESTIONS_OLD)
        self.assertEqual(code, 0, res)
        self.assertEqual(self.comments(), [self.technical(QUESTIONS_OLD)])
        self.assertEqual(len(self.missing(res)), 1, res)

    def test_comments_turned_off_do_not_stop_the_questions(self):
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["jira"]["comment"] = False
        write(path, json.dumps(cfg))
        code, res = self.ask(QUESTIONS_NEW, "--summary-file", self.summary_file())
        self.assertEqual(code, 0, res)
        self.assertEqual(self.comments(), [two_parts(SUMMARY, self.technical(QUESTIONS_EASY_FIRST))])

    def test_a_missing_file_is_refused_and_nothing_goes(self):
        code, res = self.ask(QUESTIONS_NEW, "--summary-file", os.path.join(self.tmp, "nowhere.md"))
        self.assertEqual(code, 3, res)
        self.assertIn("--summary-file", res["error"])
        self.assertEqual(self.comments(), [])

    def test_it_comes_back_when_the_step_runs_again_after_an_interruption(self):
        write(os.path.join(self.f.jd, "questions.md"), QUESTIONS_NEW)
        self.interrupted("jira_comment", "ask-jira", "T-1", "--summary-file", self.summary_file())
        self.assertEqual(self.comments(), [])
        code, res = self.fx("ask-jira", "T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.comments(), [two_parts(SUMMARY, self.technical(QUESTIONS_EASY_FIRST))])
        self.assertEqual(len(self.again(res)), 1, res)
        self.assertNotIn("people_summary", self.f.job())

    def test_answered_in_the_session_it_does_not_come_back_later(self):
        write(os.path.join(self.f.jd, "questions.md"), QUESTIONS_NEW)
        self.interrupted("jira_comment", "ask-jira", "T-1", "--summary-file", self.summary_file())
        write(os.path.join(self.f.jd, "answers.md"), ANSWERS)
        code, res = self.fx("answers", "T-1", "--no-jira")
        self.assertEqual(code, 0, res)
        self.assertNotIn("people_summary", self.f.job())


class AnswersTest(PeopleCase):
    def setUp(self):
        super().setUp()
        self.f.save_job(stage="needs-info", resume_stage="spec")

    def answer(self, questions, answers):
        write(os.path.join(self.f.jd, "questions.md"), questions)
        write(os.path.join(self.f.jd, "answers.md"), answers)
        return self.fx("answers", "T-1")

    def test_newer_questions(self):
        code, res = self.answer(QUESTIONS_NEW, ANSWERS)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "spec")
        log = slurp(res["qa_log"])
        self.assertIn(QUESTIONS_NEW.strip(), log, "the agent reads both versions of each question again")
        self.assertIn(ANSWERS.strip(), log)
        self.assertEqual(self.comments(), ["**Software factory: clarifications for T-1**\n\n"
                                           "- Q1: %s → **Allow it**\n" % Q1 +
                                           "- Q2: Which e-mail text? → **Say 60 minutes**"])
        self.assertNotIn("notes", res, "a short comment takes no summary")

    def test_older_questions(self):
        code, res = self.answer(QUESTIONS_OLD, "## Q1: %s\nAnswer: Block it\n" % Q1)
        self.assertEqual(code, 0, res)
        self.assertIn(QUESTIONS_OLD.strip(), slurp(res["qa_log"]))
        self.assertEqual(self.comments(), ["**Software factory: clarifications for T-1**\n\n"
                                           "- Q1: %s → **Block it**" % Q1])

    def test_answers_short_of_the_questions_are_refused(self):
        code, res = self.answer(QUESTIONS_NEW, ANSWERS.split("\n\n")[0] + "\n")
        self.assertEqual(code, 1, res)
        self.assertIn("covers 1 of 2", res["error"])


class PackageQuestionsTest(PeopleCase):
    def test_a_package_builder_s_questions_keep_both_versions(self):
        self.f.save_job(stage="build", split={"mode": "split", "order": ["wp1"], "packages": {
            "wp1": {"title": "Reset API", "status": "question", "after": [], "owns": ["src/*"]}}})
        write(os.path.join(self.f.jd, "build-wp1.md"),
              "STATUS: NEEDS-INFO\n\n## Questions\n\n%s\n## Notes for reviewers\nNone.\n" % QUESTIONS_NEW)
        code, res = self.fx("wp", "next", "T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "ask")
        questions = slurp(os.path.join(self.f.jd, "questions.md"))
        self.assertEqual(fx.parse_questions(questions), ["Q1: [wp1] " + Q1, "Q2: [wp1] Which e-mail text?"])
        for line in QUESTIONS_NEW.splitlines()[1:]:
            if not line.startswith("## "):
                self.assertIn(line, questions)
        self.assertNotIn("Notes for reviewers", questions)
        code, res = self.fx("ask-jira", "T-1")
        self.assertEqual(code, 0, res)
        self.assertIn("## Q1: [wp1] %s\nEasy: someone whose account" % Q1, self.comments()[0])


# --------------------------------------------------------------------------
# the pull request

class PullRequestTest(PeopleCase):
    def setUp(self):
        super().setUp()
        self.f.commit({"src/reset.txt": "reset\n"})
        self.f.save_job(stage="pr", round=1, decision="APPROVE", gate="PASS", reviews={"code": "PASS"}, risk="low")
        write(os.path.join(self.f.jd, "decision.md"), DECISION_NEW)

    def pr(self, *argv):
        code, res = self.fx("pr", "T-1", "--target", "main", *argv)
        self.assertEqual(code, 0, res)
        return res

    def body(self):
        return slurp(os.path.join(self.f.jd, "pr-body.md"))

    def pr_comment(self):
        return [c for c in self.comments() if "work ready for review" in c][-1]

    def sent_back(self):
        """Reworked and approved again: a fresh clone of the pushed branch with one more commit, ready for a push."""
        sh(["git", "clone", "-q", "-b", self.f.branch, self.f.origin, self.f.wt], self.tmp)
        self.f.commit({"src/reset.txt": "reset, reworked\n"})
        self.f.save_job(stage="pr", decision="APPROVE", gate="PASS")

    def test_the_description_and_the_comment_open_with_the_summary(self):
        res = self.pr("--summary-file", self.summary_file())
        body = self.body()
        self.assertTrue(body.startswith(two_parts(SUMMARY, "Jira: [T-1](%s/browse/T-1) test job\n" % SITE)), body[:300])
        self.assertEqual(body, two_parts(SUMMARY, fx.pr_body(self.f.ctx(), self.f.job())))
        self.assertTrue(self.pr_comment().startswith(two_parts(SUMMARY, "**Software factory: work ready for review**")))
        self.assertFalse(self.missing(res))
        self.assertEqual(self.f.job()["people_summary"], {"pr": {"text": SUMMARY, "redacted": [], "posted": True}})

    def test_the_decision_is_quoted_with_both_of_its_parts(self):
        self.pr("--summary-file", self.summary_file())
        body = self.body()
        technical = body.split("## Technical part\n\n", 1)[1]
        self.assertIn("## Approver: APPROVE", technical)
        self.assertIn("## For the human\n### Easy version\nNothing needs a look before merging. The e-mail was not "
                      "looked at on a phone.\n### Technical version\nNothing blocking;", technical)

    def test_the_older_decision_is_quoted_as_written(self):
        write(os.path.join(self.f.jd, "decision.md"), DECISION_OLD)
        self.pr("--summary-file", self.summary_file())
        self.assertIn("## Approver: APPROVE\n\n## Why\nThe reset works as the issue asks.\n\n## For the human\n"
                      "Nothing to look at.\n", self.body())

    def test_without_one_both_go_and_the_notes_say_so(self):
        res = self.pr()
        self.assertTrue(self.body().startswith("Jira: [T-1]"))
        self.assertTrue(self.pr_comment().startswith("**Software factory: work ready for review**"))
        self.assertEqual(self.missing(res), ["%s: the pull request description and the comment on T-1 have only the "
                                             "technical part (fx takes the summary with --summary-file)" % MISSING])
        self.assertNotIn("people_summary", self.f.job())

    def test_a_local_task_names_only_the_description(self):
        self.f.save_job(jira_base=None, issue_url=None)
        res = self.pr()
        self.assertEqual(len(self.missing(res)), 1, res)
        self.assertIn("the pull request description has only the technical part", self.missing(res)[0])

    def test_in_vietnamese(self):
        self.language("Tiếng Việt")
        self.pr("--summary-file", self.summary_file(VI))
        self.assertTrue(self.body().startswith(two_parts(VI, "Jira: [T-1]", True)))
        self.assertTrue(self.pr_comment().startswith(two_parts(VI, "**Software factory: work ready", True)))

    def test_a_secret_in_the_summary_never_leaves(self):
        pw = self.local_secret()
        res = self.pr("--summary-file", self.summary_file("The test login is %s." % pw))
        for text in (self.body(), self.pr_comment(), slurp(os.path.join(self.f.jd, "job.json")), json.dumps(res)):
            self.assertNotIn(pw, text)
        self.assertIn("The test login is %s." % fx.REDACTED, self.body())
        self.assertTrue(any("redacted" in n and "local-env-value" in n for n in res["notes"]), res["notes"])
        self.assertEqual(res["jira"]["comment"]["redacted"], ["local-env-value"])

    def test_it_comes_back_when_fx_pr_runs_again_after_an_interruption(self):
        self.interrupted("app_stop", "pr", "T-1", "--target", "main", "--summary-file", self.summary_file())
        job = self.f.job()
        self.assertEqual(job["stage"], "pr")
        self.assertTrue(job.get("pushed_head"), "it stopped after the push")
        self.assertFalse(job["people_summary"]["pr"]["posted"])
        res = self.pr()
        self.assertTrue(self.body().startswith(two_parts(SUMMARY, "Jira: [T-1]")))
        self.assertTrue(self.pr_comment().startswith(two_parts(SUMMARY, "**Software factory")))
        self.assertEqual(len(self.again(res)), 1, res)
        self.assertFalse(self.missing(res))

    def test_a_later_push_needs_a_summary_of_its_own(self):
        self.pr("--summary-file", self.summary_file())
        self.sent_back()
        res = self.pr()
        self.assertTrue(self.body().startswith("Jira: [T-1]"), "the summary of the earlier push is not this one's")
        self.assertEqual(len(self.missing(res)), 1, res)
        self.assertFalse(self.again(res))
        self.assertNotIn("people_summary", self.f.job())

    def test_a_later_push_with_a_new_summary(self):
        self.pr("--summary-file", self.summary_file())
        self.sent_back()
        self.pr("--summary-file", self.summary_file("The reset e-mail now says how long the link works."))
        self.assertTrue(self.body().startswith("## Summary\n\nThe reset e-mail now says how long the link works.\n"))

    def test_a_secret_found_before_the_push_drops_the_summary(self):
        os.environ["STUB_GITLEAKS_REPORT"] = json.dumps([{"RuleID": "aws-access-token", "File": "src/reset.txt",
                                                          "StartLine": 1, "Commit": "abcdef1234"}])
        code, res = self.fx("pr", "T-1", "--target", "main", "--summary-file", self.summary_file())
        self.assertEqual(code, 0, res)
        self.assertEqual(res["next"], "build")
        self.assertNotIn("people_summary", self.f.job(), "it told of what was not pushed")

    def test_no_commits_the_comment_still_has_the_summary(self):
        sh(["git", "reset", "-q", "--hard", "origin/main"], self.f.wt)
        res = self.pr("--summary-file", self.summary_file())
        self.assertIsNone(res["pr_url"])
        comment = self.comments()[-1]
        self.assertTrue(comment.startswith(two_parts(SUMMARY, "**Software factory stopped on T-1 without code")))
        self.assertFalse(self.missing(res))


# --------------------------------------------------------------------------
# what Jira gets in mode token

class TokenModeTest(JiraCase):
    def setUp(self):
        super().setUp()
        self.mode("token")
        self.put_env()
        self.f.save_job(stage="needs-info", resume_stage="spec")
        write(os.path.join(self.f.jd, "questions.md"), QUESTIONS_NEW)

    def blocks(self, adf):
        return [(b["type"], "".join(t.get("text", "") for t in b.get("content", []) if t.get("type") == "text"))
                for b in adf["content"]]

    def test_jira_gets_the_two_parts(self):
        code, res = self.fx("ask-jira", "T-1", "--summary-file", self.note(SUMMARY))
        self.assertEqual(code, 0, res)
        blocks = self.blocks(self.jira.comments[-1])
        self.assertEqual(blocks[:5], [("heading", "Summary"), ("paragraph", SUMMARY), ("heading", "Technical part"),
                                      ("paragraph", "Software factory needs information before continuing %s." % ISSUE),
                                      ("heading", "Q1: Should a locked account be able to reset its password?")])
        self.assertTrue(blocks[5][1].startswith("Easy: someone whose account"), blocks[5])

    def test_it_comes_back_when_jira_did_not_take_it(self):
        with mock.patch.object(fx.Jira, "comment", side_effect=OSError("connection reset")):
            code, res = self.fx("ask-jira", "T-1", "--summary-file", self.note(SUMMARY))
        self.assertEqual(code, 4, res)
        self.assertEqual(self.jira.comments, [])
        code, res = self.fx("ask-jira", "T-1")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.blocks(self.jira.comments[-1])[:2], [("heading", "Summary"), ("paragraph", SUMMARY)])
        self.assertTrue(any("earlier run of this step" in n for n in res["notes"]), res)
        self.assertNotIn("people_summary", self.f.job())

    def test_the_token_never_reaches_jira_in_a_summary(self):
        code, res = self.fx("ask-jira", "T-1", "--summary-file", self.note("Log in with %s first." % TOKEN))
        self.assertEqual(code, 0, res)
        self.assertNotIn(TOKEN, json.dumps(self.jira.comments))
        self.assertIn("local-env-value", res["jira"]["redacted"])
        self.assertNotIn(TOKEN, json.dumps(res))
        self.assertNotIn(TOKEN, slurp(os.path.join(self.f.jd, "job.json")))


# --------------------------------------------------------------------------
# one issue across repositories

class FeatureTest(MultiCase):
    def setUp(self):
        super().setUp()
        self.scope()
        self.start()
        self.new("be")
        self.new("fe")

    def pr_with(self, name, text):
        path = os.path.join(self.tmp, "summary-%s.md" % name)
        write(path, text)
        self.to_pr(name)
        return self.ok("pr", FEATURE, "--target", "main", "--summary-file", path, repo=self.factory[name])

    def test_each_description_keeps_its_summary_when_the_others_link_comes(self):
        be = "The server sends the reset link."
        self.pr_with("be", be)
        self.pr_with("fe", VI)
        edits = [c for c in self.gh_calls() if c["argv"][:2] == ["pr", "edit"] and "body" in c]
        be_edit = [c for c in edits if c["argv"][2].endswith("/be/pull/7")][-1]
        self.assertTrue(be_edit["body"].startswith(two_parts(be, "Jira: [%s]" % FEATURE)), be_edit["body"][:200])
        self.assertIn("https://github.com/acme/fe/pull/7", be_edit["body"])
        fe_body = slurp(os.path.join(self.factory["fe"], "jobs", FEATURE, "pr-body.md"))
        self.assertTrue(fe_body.startswith(two_parts(VI, "Jira: [%s]" % FEATURE, True)), fe_body[:200])

    def test_a_comment_on_the_shared_issue_keeps_its_heading(self):
        self.pr_with("be", "The server sends the reset link.")
        comment = [i["markdown"] for i in self.job("be")["jira_todo"] if i["action"] == "comment"][-1]
        self.assertTrue(comment.startswith("**[be]**\n\n## Summary\n\nThe server sends the reset link.\n\n"
                                           "## Technical part\n\n**Software factory: work ready for review**"), comment)


# --------------------------------------------------------------------------
# the jobs store

class SummaryInTheJobsStoreTest(PushCase):
    cfg = {"copy_into_worktree": [".env"], "security": {"required": []}}

    def test_the_job_goes_up_and_a_summary_file_with_a_secret_stays(self):
        write(os.path.join(self.f.factory, "local", ".env"), "DB_PASSWORD=Zx81-real-pw\n")
        path = os.path.join(self.f.jd, "summary-pr.md")
        write(path, "The test login is Zx81-real-pw.\n")
        self.f.save_job(stage="pr", round=1, decision="APPROVE", gate="PASS", target_branch="main", risk="low",
                        reviewers=[])
        self.f.commit({"app.py": "x = 1\n"})
        code, res = self.fx("pr", "T-1", "--no-secret-scan", "--summary-file", path)
        self.assertEqual(code, 0, res)
        snap = self.snapshot()
        files = [f["path"] for f in snap["files"]]
        self.assertIn("job.json", files)
        self.assertIn("pr-body.md", files)
        self.assertIn({"path": "summary-pr.md", "reason": "secret: local-secret"}, snap["skipped"])
        self.assertFalse([k for k, d in self.s3.objects.items() if b"Zx81-real-pw" in d])
        self.assertEqual(self.f.job()["people_summary"]["pr"]["text"], "The test login is %s." % fx.REDACTED)


if __name__ == "__main__":
    unittest.main()
