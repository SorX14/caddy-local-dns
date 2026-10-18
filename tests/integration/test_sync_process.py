"""The real sync script as a subprocess against a fake Docker socket."""

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import sync_resolvers as sr
from tests import DNS_DIR
from tests.support import FakeDocker


class RunningScript(unittest.TestCase):
    """The real script as a subprocess against a fake Docker socket."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.join(tmp.name, "resolver")
        os.mkdir(self.dir)
        self.docker = FakeDocker(os.path.join(tmp.name, "docker.sock"))
        threading.Thread(target=self.docker.serve_forever, daemon=True).start()
        self.addCleanup(self.docker.server_close)
        self.addCleanup(self.docker.shutdown)
        self.process = None
        self.tmp = tmp.name

    def start(self, interval="1"):
        env = dict(
            os.environ,
            DOCKER_SOCKET=self.docker.server_address,
            RESOLVER_DIR=self.dir,
            SYNC_INTERVAL=interval,
            PYTHONDONTWRITEBYTECODE="1",
        )
        self.process = subprocess.Popen(
            [sys.executable, "-I", os.path.join(DNS_DIR, "sync_resolvers.py")],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.process.wait)
        self.addCleanup(self.process.kill)

    def wait_for(self, condition, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.05)
        return False

    def files(self):
        return sorted(os.listdir(self.dir))

    def test_sync_resync_and_sigterm_cleanup(self):
        self.docker.labels = [{"caddy": "a.example"}]
        self.start()
        self.assertTrue(self.wait_for(lambda: self.files() == ["a.example"]))

        self.docker.labels = [{"caddy": "b.example"}]  # no event; only the timer can notice
        self.assertTrue(self.wait_for(lambda: self.files() == ["b.example"]), self.files())

        self.process.send_signal(signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=5), 0)
        self.assertEqual(self.files(), [])

    def test_stalled_docker_does_not_block_shutdown(self):
        self.docker.labels = [{"caddy": "a.example"}]
        self.start(interval="1")
        self.assertTrue(self.wait_for(lambda: self.files() == ["a.example"]))

        self.docker.stall = True
        time.sleep(1.5)  # let a reconcile get stuck on the trickle
        self.process.send_signal(signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=8), 0)
        self.assertEqual(self.files(), [])

    def test_snapshot_gives_up_on_a_trickling_response(self):
        self.docker.stall = True
        outcome = []

        def snapshot():
            try:
                sr.running_labels()
            except RuntimeError as error:
                outcome.append(error)

        with mock.patch.object(sr, "DOCKER_SOCKET", self.docker.server_address), \
                mock.patch.object(sr, "SNAPSHOT_TIMEOUT", 1):
            thread = threading.Thread(target=snapshot, daemon=True)
            thread.start()
            thread.join(4)
        self.docker.stall = False  # lets a stuck call finish so it can't linger
        self.assertFalse(thread.is_alive(), "snapshot never gave up")
        self.assertEqual(len(outcome), 1)

    def test_docker_gone_keeps_files_until_sigterm(self):
        self.docker.labels = [{"caddy": "a.example"}]
        self.start()
        self.assertTrue(self.wait_for(lambda: self.files() == ["a.example"]))
        self.docker.stall = False
        self.docker.shutdown()
        self.docker.server_close()
        os.unlink(self.docker.server_address)
        time.sleep(2.5)
        self.assertEqual(self.files(), ["a.example"])


if __name__ == "__main__":
    unittest.main()
