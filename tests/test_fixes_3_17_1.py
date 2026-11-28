"""Tests for the fixes of 3.17.1 (standard library only). Run from the repository root:
    python3 -I -m unittest discover -s tests

A review's box leaves its stack's compose services out of the proxy, as a job's box does.
"""

import json
import os
import subprocess
import tempfile
import shutil
import unittest
from unittest import mock

from test_review_sandbox import SandboxCase
from test_security import fx, load_json, slurp, write

COMPOSE = "services:\n  app:\n    image: app\n  minio:\n    image: minio\n    hostname: objects\n"


class StackNamesTest(unittest.TestCase):
    def test_the_names_reach_the_commands_and_the_docker_client(self):
        root = tempfile.mkdtemp(prefix="fx-stack-")
        self.addCleanup(shutil.rmtree, root, True)
        write(os.path.join(root, "docker-compose.yml"), COMPOSE)
        scripts = []
        box = {"name": "fxbox-t", "online": True, "proxy_ip": "10.0.0.2"}
        with mock.patch.object(fx, "vm_run", lambda argv, **kw: scripts.append(argv[-1]) or
                               subprocess.CompletedProcess(argv, 0, b"")):
            fx.box_stack_names(box, [root])
        self.assertEqual(box["no_proxy"], "app,minio,objects")
        self.assertIn("minio", fx.box_no_proxy(box))
        conf = json.loads(scripts[0].split("printf %s ", 1)[1].split(" > ", 1)[0].strip("'"))
        self.assertIn("minio", conf["proxies"]["default"]["noProxy"])

    def test_an_offline_box_only_keeps_them(self):
        box = {"name": "fxbox-t", "online": False}
        with mock.patch.object(fx, "vm_run", side_effect=AssertionError("nothing to write")):
            fx.box_stack_names(box, [])
        self.assertEqual(box["no_proxy"], "")


class ReviewBoxNoProxyTest(SandboxCase):
    def setUp(self):
        super().setUp()
        path = os.path.join(self.f.factory, "factory.json")
        cfg = load_json(path)
        cfg["setup"] = "echo setup-ran"
        write(path, json.dumps(cfg))
        write(os.path.join(self.f.factory, "stack", "docker-compose.yml"), COMPOSE)

    def test_the_setup_and_the_stack_skip_the_proxy_for_their_services(self):
        self.up()
        setup = next(c for c in self.f.calls("box") if c["cmd"] == "echo setup-ran")
        self.assertIn("NO_PROXY", setup["env"])  # the stub keeps the names only: its value is the box's no_proxy below
        log = slurp(self.f.log)
        names = log.index("fx-box-noproxy")
        self.assertLess(names, log.index('"cmd": "echo setup-ran"'))
        self.assertIn("minio", self.job()["sandbox"]["box"]["no_proxy"])


if __name__ == "__main__":
    unittest.main()
