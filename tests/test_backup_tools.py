"""Host backup tests also run without application dependencies via unittest."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / "backup" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup = load("editor_backup")
disk = load("disk_guard")
RESTIC = os.environ.get("RESTIC_TEST_BINARY") or shutil.which("restic")


class DiskSafety(unittest.TestCase):
    def test_only_empty_dedicated_disk_is_allowed(self):
        record = {"type": "disk", "size": disk.EXPECTED_SIZE, "fstype": None, "mountpoints": [None]}
        disk.validate_inventory(record, [])
        for changes, signatures in [
            ({"size": 200 * 1024**3}, []), ({"type": "part"}, []),
            ({"fstype": "ext4"}, []), ({"mountpoints": ["/"]}, []),
            ({"children": [{"name": "/dev/sdb1"}]}, []),
            ({}, [{"type": "gpt"}]), ({}, [{"type": "LVM2_member"}]),
        ]:
            with self.subTest(changes=changes, signatures=signatures), self.assertRaises(ValueError):
                disk.validate_inventory({**record, **changes}, signatures)

    def test_failed_or_incomplete_snapshot_never_becomes_restore_target(self):
        rows = [{"id": "1", "time": "2026-10-24", "tags": [backup.TAG]},
                {"id": "2", "time": "2026-10-25", "tags": [backup.PENDING]}]
        self.assertEqual(backup.snapshot_latest(rows)["id"], "1")
        with self.assertRaises(RuntimeError):
            backup.snapshot_latest(rows[1:])

    def test_status_detects_stale_and_unverified_backups(self):
        self.assertEqual(backup.health({}), "not_configured")
        self.assertEqual(backup.health({"last_backup_at": backup.now()}), "unverified")
        status = {"last_backup_at": backup.now(), "last_restore_verified_at": backup.now()}
        self.assertEqual(backup.health(status), "ok")
        status["last_backup_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        self.assertEqual(backup.health(status), "stale")
        status["drill_error"] = "Restore failed"
        self.assertEqual(backup.health(status), "failed")


class BackupSetup:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        for folder in [source / "config", source / "certs", source / "data", self.root / "program"]:
            folder.mkdir(parents=True)
        (source / "config" / "deploy.lock").touch()
        (source / "config" / "editor.env").write_text("PRIVATE_TEST_SECRET=never-print-this\n")
        password = self.root / "password"
        password.write_text("test-only-repository-key\n")
        password.chmod(0o600)
        self.config_file = self.root / "config.json"
        self.config_file.write_text("{}")
        self.tool = backup.Backup({
            "mount": str(self.root / "backup"), "filesystem_uuid": "expected-uuid",
            "password_file": str(password), "storage_root": str(source),
            "state_dir": str(self.root / "state"), "program_dir": str(self.root / "program"),
            "config_file": str(self.config_file), "restic": RESTIC or "restic",
        })
        self.tool.mount.mkdir()
        self.tool.stage.mkdir(parents=True)
        content = b"working copy source and test data\n"
        import hashlib
        sha = hashlib.sha256(content).hexdigest()
        blob = source / "data" / "blobs" / sha[:2] / sha
        blob.parent.mkdir(parents=True)
        blob.write_bytes(content)
        self.blob = blob
        self.manifest = {"counts": dict.fromkeys(backup.TABLES, 1), "blobs": [{"sha256": sha, "size": len(content)}]}

    def export(self):
        (self.tool.stage / "editor.dump").write_bytes(b"custom-format-dump-test-fixture")
        backup.atomic_json(self.tool.stage / "database-manifest.json", self.manifest)
        return {"counts": self.manifest["counts"], "referenced_files": 1,
                "dump_sha256": backup.file_hash(self.tool.stage / "editor.dump"), "postgres_image": "sha256:test"}


class BackupFixture(BackupSetup, unittest.TestCase):
    def test_database_reference_checks_detect_missing_and_same_size_corrupt_files(self):
        self.assertEqual(backup.validate_blobs(self.tool.root / "data", self.manifest, hashes=True), 1)
        self.blob.write_bytes(b"x" * self.blob.stat().st_size)
        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            backup.validate_blobs(self.tool.root / "data", self.manifest, hashes=True)
        self.blob.unlink()
        with self.assertRaisesRegex(RuntimeError, "missing"):
            backup.validate_blobs(self.tool.root / "data", self.manifest)

    def test_invalid_or_duplicate_blob_paths_cannot_escape_storage(self):
        for blobs in [[{"sha256": "../secret", "size": 1}], self.manifest["blobs"] * 2]:
            with self.subTest(blobs=blobs), self.assertRaises(RuntimeError):
                backup.validate_blobs(self.tool.root / "data", {"blobs": blobs})

    def test_missing_mount_fails_before_restic_or_data_writes(self):
        with patch.object(backup, "run", return_value=b'{"filesystems": []}'), self.assertRaisesRegex(RuntimeError, "absent"):
            self.tool.guard()
        with patch.object(backup, "run", return_value=b'{"filesystems": [{"uuid":"wrong"}]}'), self.assertRaisesRegex(RuntimeError, "absent"):
            self.tool.guard()

    def test_restored_file_checks_refuse_symlinks(self):
        self.blob.unlink()
        self.blob.symlink_to(self.config_file)
        with self.assertRaises(RuntimeError):
            backup.validate_blobs(self.tool.root / "data", self.manifest)

    def test_later_successful_backup_does_not_hide_a_failed_restore_drill(self):
        self.tool.status(drill_error="A restore failed", last_backup_at=backup.now())
        self.tool.status(backup_error=None, last_backup_at=backup.now())
        self.assertEqual(json.loads(self.tool.status_path.read_text())["health"], "failed")
        self.tool.status(drill_error=None, last_restore_verified_at=backup.now())
        status = json.loads(self.tool.status_path.read_text())
        self.assertEqual(status["health"], "ok")
        self.assertNotIn("PRIVATE_TEST_SECRET", self.tool.status_path.read_text())
        self.assertEqual(self.tool.status_path.stat().st_mode & 0o777, 0o644)

    def test_dump_and_reference_manifest_share_one_exported_snapshot(self):
        session = Mock()
        session.stdin = io.StringIO()
        session.stdout.readline.return_value = "00000003-0000000A-1\n"
        session.communicate.return_value = (json.dumps(self.manifest), None)
        session.returncode = 0
        session.poll.return_value = 0
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if "pg_dump" in command:
                kwargs["stdout"].write(b"dump")
            return b"catalog"

        with patch.object(backup.subprocess, "Popen", return_value=session), patch.object(backup, "run", side_effect=run), \
             patch.object(self.tool, "db_container", return_value="db-container"), patch.object(self.tool, "image", return_value="sha256:test"):
            result = self.tool.export_database()
        self.assertTrue(any("--snapshot=00000003-0000000A-1" in command for command in calls))
        self.assertIn("REPEATABLE READ READ ONLY", session.stdin.getvalue())
        self.assertIn(backup.MANIFEST_SQL, session.communicate.call_args.args[0])
        self.assertEqual(result["counts"], self.manifest["counts"])

    def test_failed_dump_preserves_the_last_staging_dump(self):
        old = self.tool.stage / "editor.dump"
        old.write_bytes(b"old-good-dump")
        session = Mock()
        session.stdout.readline.return_value = "00000003-0000000A-1\n"
        session.poll.return_value = 0
        with patch.object(backup.subprocess, "Popen", return_value=session), patch.object(backup, "run", side_effect=RuntimeError("dump failed")), \
             patch.object(self.tool, "db_container", return_value="db-container"), self.assertRaises(RuntimeError):
            self.tool.export_database()
        self.assertEqual(old.read_bytes(), b"old-good-dump")
        self.assertFalse((self.tool.stage / "editor.dump.pending").exists())

    def test_retention_checks_the_repository_before_removing_history_and_keeps_pinned_snapshots(self):
        calls = []
        with patch.object(self.tool, "guard"), patch.object(self.tool, "snapshots", return_value=[{
            "id": "good", "time": backup.now(), "tags": [backup.TAG],
        }]), patch.object(self.tool, "restic", side_effect=lambda *args: calls.append(args)):
            self.tool.maintain()
        self.assertEqual(calls[0][0], "check")
        self.assertIn(("--keep-tag", "pinned"), list(zip(calls[1], calls[1][1:])))
        self.assertIn(backup.TAG, calls[1])
        self.assertIn(backup.PENDING, calls[2])
        self.assertEqual(calls[-1], ("prune",))

    def test_corrupt_repository_is_not_pruned(self):
        with patch.object(self.tool, "guard"), patch.object(self.tool, "snapshots", return_value=[{
            "id": "good", "time": backup.now(), "tags": [backup.TAG],
        }]), patch.object(self.tool, "restic", side_effect=RuntimeError("bad repository")) as restic, self.assertRaises(RuntimeError):
            self.tool.maintain()
        self.assertEqual(restic.call_count, 1)
        self.assertEqual(restic.call_args.args[0], "check")

    def test_another_backup_cannot_acquire_the_same_lock(self):
        lock = self.root / "lock"
        with backup.locked(lock), patch.object(backup.time, "monotonic", side_effect=[0, 2]), self.assertRaises(RuntimeError):
            with backup.locked(lock, timeout=1):
                self.fail("Concurrent lock was acquired")


@unittest.skipUnless(RESTIC, "Install restic or set RESTIC_TEST_BINARY for real repository tests")
class RealRestic(BackupSetup, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tool.restic("init")
        self.guard = patch.object(self.tool, "guard")
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.export_mock = patch.object(self.tool, "export_database", side_effect=self.export)
        self.export_mock.start()
        self.addCleanup(self.export_mock.stop)

    def test_encrypted_snapshot_restores_working_files_database_and_private_config(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.tool.backup(pin=True)
        latest = backup.snapshot_latest(self.tool.snapshots())
        self.assertIn("pinned", latest["tags"])
        self.assertNotIn(backup.PENDING, latest["tags"])
        self.blob.unlink()
        target = self.root / "recovered"
        self.tool.restic("restore", latest["id"], "--target", str(target), "--verify")
        restored_data = target / self.tool.root.relative_to("/") / "data"
        self.assertEqual(backup.validate_blobs(restored_data, self.manifest, hashes=True), 1)
        self.assertTrue((target / self.tool.stage.relative_to("/") / "editor.dump").is_file())
        self.assertEqual((target / self.tool.root.relative_to("/") / "config" / "editor.env").read_text(), "PRIVATE_TEST_SECRET=never-print-this\n")
        self.tool.restic("check", "--read-data")
        for path in self.tool.repo.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"never-print-this", path.read_bytes())

    def test_an_unconfirmed_new_snapshot_cannot_replace_the_last_complete_backup(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.tool.backup()
        original = backup.snapshot_latest(self.tool.snapshots())["id"]
        native = self.tool.restic

        def incomplete(*args, **kwargs):
            result = native(*args, **kwargs)
            if args[0] == "backup":
                raise RuntimeError("Backup command did not finish cleanly")
            return result

        with patch.object(self.tool, "restic", side_effect=incomplete), self.assertRaises(RuntimeError):
            self.tool.backup()
        self.assertEqual(backup.snapshot_latest(self.tool.snapshots())["id"], original)

    def test_real_restore_is_read_only_to_production_paths_and_cleans_temporary_database_on_failure(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.tool.backup()
        native_run = backup.run
        native_subprocess_run = subprocess.run
        calls = []

        def run(command, **kwargs):
            if command[0] != "docker":
                return native_run(command, **kwargs)
            calls.append(command)
            if "psql" in command:
                return json.dumps({**self.manifest, "counts": {}}).encode()
            return b"temporary-container"

        def ready(command, **kwargs):
            return subprocess.CompletedProcess(command, 0) if command[0] == "docker" else native_subprocess_run(command, **kwargs)

        with patch.object(backup, "run", side_effect=run), patch.object(backup.subprocess, "run", side_effect=ready), \
             self.assertRaisesRegex(RuntimeError, "differ"):
            self.tool.drill()
        create = next(command for command in calls if command[:2] == ["docker", "run"])
        self.assertIn("none", create)
        self.assertNotIn("-p", create)
        self.assertNotIn("-v", create)
        self.assertTrue(any(command[:3] == ["docker", "rm", "-f"] for command in calls))
        self.assertTrue(self.blob.exists())
        self.assertEqual(list((self.tool.state / "restore-drills").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
