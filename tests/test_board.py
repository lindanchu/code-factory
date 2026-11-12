"""The board: one page of every job on this machine (standard library only). Run from the repository root:
    python3 -I -m unittest discover -s tests

fx ctx starts an agent's run, its result file or fx usage ends it, and fx's own long steps (setup, gate, push) show
while they run. The page carries the jobs as data, which a job's text must never get out of.
"""

import os
import unittest

from test_security import FxTestCase, fx, slurp, write


class BoardTest(FxTestCase):
    def card(self):
        data = fx.board_data([self.f.factory])
        cards = [v for v in data["active"] + data["done"] if v["id"] == "T-1"]
        self.assertEqual(len(cards), 1, data)
        return cards[0]

    def test_the_agent_at_work_shows_and_its_usage_ends_it(self):
        code, res = self.fx("ctx", "T-1", "--agent", "builder")
        self.assertEqual(code, 0, res)
        self.assertEqual([(r["name"], r["model"]) for r in self.card()["running"]], [("builder", res["model"])])
        code, res = self.fx("usage", "T-1", "--agent", "builder", "--tokens", "150", "--ms", "1000")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["tokens"], 150)
        v = self.card()
        self.assertEqual((v["running"], v["tokens"]), ([], 150))
        run = self.f.job()["runs"][-1]
        self.assertEqual((run["agent"], run["status"], run["tokens"], run["ms"]), ("builder", "done", 150, 1000))

    def test_usage_needs_a_run_fx_ctx_started(self):
        code, res = self.fx("usage", "T-1", "--agent", "code", "--tokens", "10")
        self.assertEqual(code, 1, res)
        self.assertIn("no code run recorded for T-1", res["error"])

    def test_the_agent_ends_with_its_file_and_the_gate_shows_as_a_step(self):
        self.fx("ctx", "T-1", "--agent", "builder")
        self.assertEqual(len(self.card()["running"]), 1)
        write(os.path.join(self.f.jd, "build.md"), "STATUS: READY\n")
        self.f.commit({"app.py": "x = 1\n"})
        code, res = self.gate()
        self.assertEqual(code, 0, res)
        job = self.f.job()
        self.assertEqual(job["runs"][-1]["status"], "done", "its file was written after it started")
        self.assertEqual([s["step"] for s in job["steps"]], ["gate"])
        self.assertTrue(all(s["ended"] for s in job["steps"]))
        self.assertEqual(job["steps"][-1]["result"], job["gate"])
        self.assertEqual(self.card()["running"], [])

    def test_a_hostile_title_cannot_get_out_of_the_data_on_the_page(self):
        bad = "</script><script>alert(1)</script>"
        self.f.save_job(summary="Reset password " + bad)
        code, res = self.fx("board")
        self.assertEqual(code, 0, res)
        page = slurp(fx.board_file())
        self.assertNotIn(bad, page)
        self.assertIn("Reset password <\\/script><script>alert(1)<\\/script>", page)
        self.assertNotIn("__DATA__", page)

    def test_the_page_opens_on_the_flow_map_and_keeps_the_office(self):
        self.assertIn("let S = {layout: 'flow',", fx.BOARD_HTML)
        for word in ("flowTitle:", "lFlow:", "lOffice:", "flowLegend:", "flowJobs:", "colCount:", "pkgWait:", "mayStop:"):
            self.assertEqual(fx.BOARD_HTML.count(word), 2, word)  # once in English, once in Vietnamese
        for part in ('data-layout="', "function flowMap(", "function rooms(", "function jobRow("):
            self.assertIn(part, fx.BOARD_HTML)
        self.assertEqual(fx.BOARD_HTML.count("__DATA__"), 1)


if __name__ == "__main__":
    unittest.main()
