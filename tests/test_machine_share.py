"""Sharing one machine between jobs, the rest of it: a gate takes its room one phase at a time, every step is given a
number of CPUs (FACTORY_CPUS for the project's commands, --cpus for ZAP), and a job that waits for a person rests its
stack until the answers come.

Standard library only. Run from the repository root:
    python3 -I -m unittest discover -s tests

No Docker anywhere: the machine is the JSON file the other tests use (FACTORY_FAKE_MACHINE), and where Docker has to
answer (which containers there are, stopping and starting them), the test says what it answers.
"""

import contextlib
import json
import os
import shutil
import tempfile
import time
import types
import unittest
from unittest import mock

from test_security import fx, slurp, write
from test_speed import COUNTING, LEASE_KIND, ChecksCase


def reading(cpus=8, docker_cpus=8, docker_mb=10000.0, used_mb=0.0, by_lease=None):
    return {"at": time.time(), "host": {"total_mb": 100000.0, "avail_mb": 100000.0, "pressure": 0, "pressure_source": "-"},
            "docker": {"total_mb": docker_mb, "used_mb": used_mb, "cpus": docker_cpus}, "cpus": cpus, "load1": 0.0,
            "by_lease": by_lease or {}}


# --------------------------------------------------------------------------
# 1. a gate takes its room one phase at a time

class GatePhasesTest(ChecksCase):
    script = LEASE_KIND

    def kinds(self):
        path = os.path.join(self.f.out, "kinds")
        return [k for k in slurp(path).split("\n") if k] if os.path.exists(path) else []

    def phases(self, *argv):
        """The places the gate asked for, in order: (kind, keeps a place in the line, waits as long as it takes)."""
        seen, real = [], fx.step_place

        def spy(ctx, kind, *a, **kw):
            seen.append((kind, kw.get("seq") is not None, bool(kw.get("wait_all"))))
            return real(ctx, kind, *a, **kw)

        with mock.patch.object(fx, "step_place", side_effect=spy):
            code, res = self.fx("gate", "T-1", *argv)
        return code, res, seen

    def test_the_checks_and_the_app_with_its_scans_are_two_places(self):
        code, res, seen = self.phases()
        self.assertEqual(res["gate"], "PASS", self.checks_md())
        self.assertEqual(seen, [(fx.GATE_CHECKS, False, False), (fx.GATE_APP, True, True)])
        self.assertEqual(self.kinds(), ["%s False" % fx.GATE_CHECKS])  # the checks ran in the first phase's own place
        with open(os.path.join(os.environ["FACTORY_STATE_DIR"], "run", "state.json")) as f:
            self.assertEqual(json.load(f)["leases"], {})  # both given back
        steps = [s for s in self.f.job()["steps"] if s["step"] == "gate"]
        self.assertEqual([(s["result"], bool(s["ended"])) for s in steps], [("PASS", True)])  # one gate on the board

    def test_a_gate_whose_checks_passed_has_the_second_phase_only(self):
        self.checks()
        code, res, seen = self.phases()
        self.assertEqual((res["gate"], res["checks"]["reused"]), ("PASS", True), res)
        self.assertEqual([k for k, _, _ in seen if k != "checks"], [fx.GATE_APP])

    def test_a_round_that_stops_before_its_checks_takes_no_second_place(self):
        write(os.path.join(self.f.wt, "left-over.txt"), "x\n")  # untracked: the round fails before anything runs
        code, res, seen = self.phases()
        self.assertEqual(res["gate"], "FAIL", res)
        self.assertEqual(seen, [(fx.GATE_CHECKS, False, False)])

    def test_the_second_phase_waits_whatever_max_wait_says(self):
        code, res, seen = self.phases()
        self.assertTrue(seen[-1][2])
        self.assertEqual(code, 0, res)


class GateLineTest(unittest.TestCase):
    """The second phase of a gate keeps the gate's place in the line: a step that asked after it does not go first."""

    def test_the_earlier_place_goes_first_when_only_one_fits(self):
        cfg = json.loads(json.dumps(fx.MACHINE_DEFAULTS))
        t = time.time()
        running = {"id": "r", "kind": "checks", "need": {"docker_mb": 5000.0, "host_mb": 0.0, "cpu": 0.0}}
        need = {"docker_mb": 3500.0, "host_mb": 0.0, "cpu": 0.0}
        phase2 = {"id": "g2", "kind": fx.GATE_APP, "seq": 1, "enqueued": t, "seen": t, "need": need}
        other = {"id": "o", "kind": "checks", "seq": 2, "enqueued": t - 100, "seen": t, "need": need}
        st = {"leases": {"r": running}, "queue": [other, phase2]}
        self.assertTrue(fx.place_decide(cfg, st, "g2", reading())[0])
        self.assertFalse(fx.place_decide(cfg, st, "o", reading())[0])


# --------------------------------------------------------------------------
# 2. how many CPUs a step may use

class CpusTest(unittest.TestCase):
    def cpus(self, leases, r, here=8):
        with mock.patch.object(fx, "cpus_here", return_value=here):
            return fx.place_cpus(json.loads(json.dumps(fx.MACHINE_DEFAULTS)), {"leases": leases, "queue": []}, "me", r)

    def me(self):
        return {"id": "me", "kind": fx.GATE_APP, "need": {"cpu": 2.0}}

    def test_alone_a_step_gets_every_cpu(self):
        self.assertEqual(self.cpus({"me": self.me()}, None), 8)
        self.assertEqual(self.cpus({"me": self.me()}, reading()), 8)

    def test_beside_another_step_it_gets_what_is_left(self):
        gate = {"id": "g", "kind": fx.GATE_CHECKS, "need": {"cpu": 5.0}}
        self.assertEqual(self.cpus({"me": self.me(), "g": gate}, reading()), 3)

    def test_what_another_step_uses_counts_when_it_is_more_than_expected(self):
        gate = {"id": "g", "kind": fx.GATE_CHECKS, "need": {"cpu": 1.0}}
        self.assertEqual(self.cpus({"me": self.me(), "g": gate}, reading(by_lease={"g": {"cpu": 6.0}})), 2)

    def test_never_less_than_one(self):
        gate = {"id": "g", "kind": fx.GATE_CHECKS, "need": {"cpu": 20.0}}
        self.assertEqual(self.cpus({"me": self.me(), "g": gate}, reading()), 1)

    def test_never_more_than_docker_has(self):
        self.assertEqual(self.cpus({"me": self.me()}, reading(docker_cpus=4)), 4)

    def test_outside_a_place_nothing_is_said(self):
        with mock.patch.object(fx, "_PLACES", []):
            self.assertIsNone(fx.place_cpus_now())

    def test_zap_runs_with_the_steps_cpus_and_never_more_than_docker_has(self):
        place = types.SimpleNamespace(lease={"id": "p", "cpus": 3})
        for docker_says, want in (("4\n", 3), ("2\n", 2), (None, None)):
            with mock.patch.object(fx, "_PLACES", [place]), mock.patch.object(fx, "_DOCKER_NCPU", []), \
                    mock.patch.object(fx, "try_out", return_value=docker_says):
                self.assertEqual(fx.zap_cpus(), want, docker_says)
        with mock.patch.object(fx, "_PLACES", []), mock.patch.object(fx, "_DOCKER_NCPU", [8]):
            self.assertIsNone(fx.zap_cpus())  # outside a place ZAP runs as before


class CpusEnvTest(ChecksCase):
    script = COUNTING

    def test_the_checks_script_is_told_and_a_pass_stands_whatever_it_was_told(self):
        code, res = self.checks()
        self.assertEqual((code, res["checks"]), (0, "PASS"), res)
        env = dict(l.split("=", 1) for l in slurp(os.path.join(self.f.out, "checks.env")).splitlines() if "=" in l)
        self.assertTrue(env.get("FACTORY_CPUS", "").isdigit() and int(env["FACTORY_CPUS"]) >= 1, env.get("FACTORY_CPUS"))
        self.assertIn("FACTORY_CPUS", fx.ENV_NOT_READ)
        code, res = self.gate()
        self.assertEqual((res["gate"], res["checks"]["reused"]), ("PASS", True), res)


# --------------------------------------------------------------------------
# 3. a job that waits for a person rests its stack

class RestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="fx-rest-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.work = os.path.join(self.tmp, "work")
        self.clone = os.path.join(self.work, "J-1")
        self.other = os.path.join(self.work, "J-2")
        self.jd = os.path.join(self.tmp, "factory", "jobs", "J-1")
        for d in (self.clone, self.other, self.jd):
            os.makedirs(d)
        self.ctx = types.SimpleNamespace(work=self.work, job_dir=lambda i: self.jd)
        self.calls = []
        self.ledger = {"leases": {}, "queue": [], "ports": {}}

    def container(self, name, mounts, place=""):
        return {"name": name, "project": "factory_j_1", "workdir": mounts[0], "place": place, "oneoff": False,
                "mounts": mounts, "mem_mb": 400.0, "cpu": 0.1}

    def try_out(self, argv, **kw):
        self.calls.append(argv[:3])
        if argv[:2] == ["docker", "inspect"]:
            return "/db\t{\"/var/lib/mysql\":\"\"}\t\n/app\tnull\tbind \n"
        return ""

    def rest(self, job):
        docker = {"total_mb": 16000.0, "cpus": 8, "system": "test", "used_mb": 1600.0, "containers": [
            self.container("app", [self.clone]), self.container("db", [self.clone]),
            self.container("shared", [self.clone, self.other]), self.container("fx-zap-J-1-abc", [self.jd], place="x-1")]}
        env = {k: v for k, v in os.environ.items() if k != "FACTORY_FAKE_MACHINE"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(fx, "meter_docker", return_value=docker), \
                mock.patch.object(fx, "try_out", side_effect=self.try_out), \
                mock.patch.object(fx, "ledger_lock", contextlib.nullcontext), \
                mock.patch.object(fx, "ledger_load", return_value=self.ledger), \
                mock.patch.object(fx, "ledger_reap", return_value=[]):
            return fx.stack_rest(self.ctx, job)

    def job(self, **kw):
        return dict({"id": "J-1", "stage": "needs-info", "worktree": self.clone}, **kw)

    def test_only_the_jobs_own_containers_stop_and_none_that_would_lose_data(self):
        res = self.rest(self.job())
        self.assertEqual((res["containers"], res["kept_running"], res["memory_mb"]), (["app"], ["db"], 400), res)
        self.assertEqual([c for c in self.calls if c[:2] == ["docker", "stop"]], [["docker", "stop", "-t"]])
        self.assertNotIn(["docker", "compose", "-p"], self.calls)  # stopped, never taken down: the volumes stay

    def test_nothing_stops_while_a_step_of_the_job_holds_room(self):
        self.ledger["leases"]["l"] = {"id": "l", "kind": fx.GATE_APP, "job": "J-1", "need": {"cpu": 1.0}}
        self.assertIsNone(self.rest(self.job()))
        self.assertEqual(self.calls, [])

    def test_a_lane_is_no_step(self):
        self.ledger["leases"]["l"] = {"id": "l", "kind": "lane", "job": "J-1", "need": {}}
        self.assertEqual(self.rest(self.job())["containers"], ["app"])

    def test_nothing_stops_while_a_package_is_being_built(self):
        job = self.job(split={"packages": {"wp1": {"status": "building", "clone": self.other}}})
        self.assertIsNone(self.rest(job))

    def test_nothing_stops_unless_the_job_waits_for_answers(self):
        self.assertIsNone(self.rest(self.job(stage="build")))

    def test_the_answers_start_them_again(self):
        job = self.job(rested={"containers": ["app"], "memory_mb": 400})
        env = {k: v for k, v in os.environ.items() if k != "FACTORY_FAKE_MACHINE"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(fx, "try_out", side_effect=self.try_out):
            self.assertEqual(fx.stack_wake(job), ["app"])
        self.assertEqual(self.calls, [["docker", "start", "app"]])
        self.assertNotIn("rested", job)


if __name__ == "__main__":
    unittest.main(verbosity=2)
