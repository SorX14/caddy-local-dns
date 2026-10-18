"""Keeping /etc/resolver files in step with the running containers (Docker faked, real temp dir)."""

import os
import tempfile
import unittest
from unittest import mock

import sync_resolvers as sr


class Reconcile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        patcher = mock.patch.object(sr, "RESOLVER_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.syncer = sr.Syncer()

    def path(self, name):
        return os.path.join(self.dir, name)

    def files(self):
        return sorted(os.listdir(self.dir))

    def read(self, name):
        with open(self.path(name)) as file:
            return file.read()

    def put(self, name, text):
        with open(self.path(name), "w") as file:
            file.write(text)

    def reconcile(self, labels):
        with mock.patch.object(sr, "running_labels", return_value=labels):
            self.syncer.reconcile()

    def test_adds_and_removes(self):
        self.reconcile([{"caddy": "a.example"}, {"caddy": "b.example"}])
        self.assertEqual(self.files(), ["a.example", "b.example"])
        self.assertEqual(self.read("a.example"), sr.CONTENT)
        self.reconcile([{"caddy": "b.example"}])
        self.assertEqual(self.files(), ["b.example"])

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        old = f"{sr.MARKER}\nnameserver 127.0.0.1\nport 1\n" + "x" * 500
        self.put("a.example", old)
        seen = {}
        real_replace = os.replace

        def spy(source, destination):
            seen["destination"] = self.read("a.example")  # still the old file...
            with open(source) as file:
                seen["source"] = file.read()  # ...while the new one is complete
            real_replace(source, destination)

        with mock.patch.object(sr.os, "replace", side_effect=spy):
            self.reconcile([{"caddy": "a.example"}])
        self.assertEqual(seen, {"destination": old, "source": sr.CONTENT})
        self.assertEqual(self.files(), ["a.example"])
        self.assertEqual(self.read("a.example"), sr.CONTENT)

    def test_docker_failure_keeps_files(self):
        self.reconcile([{"caddy": "keep.example"}])
        for error in (OSError("gone"), RuntimeError("500"), ValueError("bad json")):
            with mock.patch.object(sr, "running_labels", side_effect=error):
                self.syncer.reconcile()
            self.assertEqual(self.files(), ["keep.example"])

    def test_empty_snapshot_removes_files(self):
        self.reconcile([{"caddy": "gone.example"}])
        self.reconcile([])
        self.assertEqual(self.files(), [])

    def test_unmanaged_file_untouched(self):
        self.put("mine.dev", "nameserver 10.0.0.1\n")
        self.reconcile([{"caddy": "mine.dev"}])
        self.reconcile([])
        self.assertEqual(self.read("mine.dev"), "nameserver 10.0.0.1\n")

    def test_symlink_to_managed_file_is_not_followed_or_removed(self):
        target = os.path.join(tempfile.gettempdir(), f"sr-target-{os.getpid()}")
        original = f"{sr.MARKER}\nnameserver 127.0.0.1\nport 1\n"
        with open(target, "w") as file:
            file.write(original)
        self.addCleanup(os.unlink, target)
        os.symlink(target, self.path("linked.example"))

        self.reconcile([{"caddy": "linked.example"}])
        self.reconcile([])

        self.assertTrue(os.path.islink(self.path("linked.example")))
        with open(target) as file:
            self.assertEqual(file.read(), original)

    def test_directory_and_fifo_do_not_crash_or_get_removed(self):
        os.mkdir(self.path("dir.example"))
        os.mkfifo(self.path("fifo.example"))
        self.reconcile([{"caddy": "dir.example fifo.example other.example"}])
        self.reconcile([])
        self.assertEqual(self.files(), ["dir.example", "fifo.example"])

    def test_directory_among_stale_managed_files(self):
        self.reconcile([{"caddy": "stale.example"}])
        os.mkdir(self.path("dir.example"))
        self.reconcile([])
        self.assertEqual(self.files(), ["dir.example"])

    def test_differently_cased_managed_file_for_wanted_name_is_kept(self):
        self.put("A.example", sr.CONTENT)
        sr.remove_managed({"a.example"})
        self.assertEqual(self.files(), ["A.example"])

    def test_stale_port_is_rewritten(self):
        self.put("a.example", f"{sr.MARKER}\nnameserver 127.0.0.1\nport 1\n")
        self.reconcile([{"caddy": "a.example"}])
        self.assertEqual(self.read("a.example"), sr.CONTENT)

    def test_unwritable_entry_does_not_stop_others(self):
        real_open = os.open

        def flaky(path, *args, **kwargs):
            if "bad.example" in path:
                raise OSError(36, "File name too long")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(sr.os, "open", side_effect=flaky):
            self.reconcile([{"caddy": "bad.example good.example"}])
        self.assertEqual(self.files(), ["good.example"])

    def test_remove_managed_only_removes_managed(self):
        self.reconcile([{"caddy": "a.example"}])
        self.put("mine.dev", "nameserver 10.0.0.1\n")
        sr.remove_managed(set())
        self.assertEqual(self.files(), ["mine.dev"])
