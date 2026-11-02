"""Owned temporary workspace lifecycle; no real benchmark or system Trash calls."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark
import system_io


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.target = dict(path=str(self.root), inode=self.root.stat().st_ino,
                           st_dev=self.root.stat().st_dev)

    def workspace(self, retain=False):
        run = "a" * 32
        path = self.root / (".diskbench-" + run)
        path.mkdir()
        system_io.mark_workspace(path, self.target, run, retain)
        (path / "payload.bin").write_bytes(b"fixture")
        return path

    def test_disk_alias_preserves_old_target_contract(self):
        for option in ("--disk", "--target"):
            self.assertEqual(benchmark.parser().parse_args([option, str(self.root)]).target, str(self.root))

    def test_marked_workspace_has_explanation_and_is_automatically_trashed(self):
        path = self.workspace()
        self.assertIn("清理磁盘时可以删除整个目录", (path / "README.txt").read_text())
        with patch.object(system_io, "trash_owned") as trash:
            self.assertEqual(system_io.clean_stale_workspaces(self.target),
                             [dict(path=str(path), status="trashed")])
            trash.assert_called_once_with(path, self.target)

    def test_foreign_or_changed_contents_are_never_removed(self):
        path = self.workspace()
        for change in ("foreign", "symlink", "hardlink", "marker", "root_identity"):
            with self.subTest(change=change):
                extra = path / "personal.txt"
                original = (path / system_io.WORKSPACE_MARKER).read_text()
                target = dict(self.target)
                if change == "foreign":
                    extra.write_text("personal")
                elif change == "symlink":
                    extra = path / "README.txt"
                    extra.unlink()
                    extra.symlink_to(self.root)
                elif change == "hardlink":
                    extra = self.root / "linked-data"
                    extra.hardlink_to(path / "payload.bin")
                elif change == "marker":
                    (path / system_io.WORKSPACE_MARKER).write_text("{}")
                else:
                    target["inode"] += 1
                with patch.object(system_io, "trash_owned") as trash:
                    system_io.clean_stale_workspaces(target)
                    trash.assert_not_called()
                (path / system_io.WORKSPACE_MARKER).write_text(original)
                if extra.is_symlink() or extra.exists():
                    extra.unlink()
                if not (path / "README.txt").exists():
                    (path / "README.txt").write_text(system_io.WORKSPACE_README)

    def test_retained_unmarked_and_symlink_directories_are_preserved(self):
        path = self.workspace(retain=True)
        unmarked = self.root / (".diskbench-" + "b" * 32)
        unmarked.mkdir()
        (unmarked / "payload.bin").write_bytes(b"unknown")
        (self.root / (".diskbench-" + "c" * 32)).symlink_to(path)
        with patch.object(system_io, "trash_owned") as trash:
            outcomes = system_io.clean_stale_workspaces(self.target)
            trash.assert_not_called()
        self.assertEqual(len(outcomes), 3)
        self.assertTrue(all(item["status"] == "retained" for item in outcomes))

    def test_corrupt_marker_or_trash_failure_is_recorded_without_deleting(self):
        path = self.workspace()
        with patch.object(system_io, "trash_owned", side_effect=OSError("fixture failure")):
            self.assertEqual(system_io.clean_stale_workspaces(self.target)[0]["status"], "cleanup_failed")
        (path / system_io.WORKSPACE_MARKER).write_text("not json")
        with patch.object(system_io, "trash_owned") as trash:
            self.assertEqual(system_io.clean_stale_workspaces(self.target)[0]["status"], "retained")
            trash.assert_not_called()
        with patch.object(system_io, "check_target"), patch.object(system_io, "capture") as command:
            with self.assertRaises(system_io.BenchError):
                system_io.trash_owned(path, self.target)
            command.assert_not_called()

    def test_final_cleanup_refuses_foreign_files_before_system_trash(self):
        path = self.workspace()
        (path / "personal.txt").write_text("preserve")
        with patch.object(system_io, "check_target"), patch.object(system_io, "capture") as command:
            with self.assertRaises(system_io.BenchError):
                system_io.trash_owned(path, self.target)
            command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
