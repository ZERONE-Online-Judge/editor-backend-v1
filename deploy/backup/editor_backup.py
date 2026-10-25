#!/usr/bin/env python3
"""Host-only Editor backups. Never imports the app or executes problem code."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

TAG = "zoj-editor-complete-v1"
PENDING = "zoj-editor-pending-v1"
TABLES = ("users", "problems", "working_copies", "revisions", "jobs", "blobs")
DEFAULT_STATE = Path("/var/lib/zoj-editor-backup")
DEFAULT_CONFIG = Path("/etc/zoj-editor-backup/config.json")
MANIFEST_SQL = "SELECT json_build_object('counts', json_build_object(" + ",".join(
    f"'{table}', (SELECT count(*) FROM {table})" for table in TABLES
) + "), 'blobs', (SELECT coalesce(json_agg(row_to_json(b)), '[]'::json) FROM (SELECT DISTINCT sha256, size FROM blobs ORDER BY sha256) b))::text;"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def locked(path, timeout=1200):
    # Existing deployment lock ownership must be preserved for its runner.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another backup or deployment is still running")
                time.sleep(1)
        yield
    finally:
        os.close(fd)


def run(command, *, env=None, input=None, stdin=None, stdout=None, timeout=1200):
    result = subprocess.run(command, env=env, input=input, stdin=stdin, stdout=stdout or subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        # Commands have no passwords in argv. Do not copy data or private envs to status.
        sys.stderr.write(result.stderr.decode(errors="replace")[-2000:] + "\n")
        raise RuntimeError(f"{Path(command[0]).name} failed (exit {result.returncode}); see the private service journal")
    return result.stdout


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_blobs(data, manifest, *, hashes=False):
    seen = set()
    for blob in manifest["blobs"]:
        sha, size = blob["sha256"], blob["size"]
        if not re.fullmatch(r"[a-f0-9]{64}", sha) or not isinstance(size, int) or size < 0:
            raise RuntimeError("Invalid blob reference in database manifest")
        if sha in seen:
            raise RuntimeError("Duplicate blob reference in database manifest")
        seen.add(sha)
        path = Path(data) / "blobs" / sha[:2] / sha
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
            raise RuntimeError("A database-referenced problem file is missing or has the wrong size")
        if hashes and file_hash(path) != sha:
            raise RuntimeError("A restored problem file does not match its SHA-256")
    return len(seen)


def snapshot_latest(snapshots):
    complete = [s for s in snapshots if TAG in s.get("tags", [])]
    if not complete:
        raise RuntimeError("No complete Editor backup exists")
    return max(complete, key=lambda s: (s["time"], s["id"]))


def health(status):
    if any(status.get(action + "_error") for action in ("backup", "maintain", "drill")):
        return "failed"
    stamp = status.get("last_backup_at")
    if not stamp:
        return "not_configured"
    if (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(stamp)).total_seconds() > 2700:
        return "stale"
    verified = status.get("last_restore_verified_at")
    if not verified or (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(verified)).total_seconds() > 172800:
        return "unverified"
    if status.get("disk_used_percent", 0) >= 85:
        return "low_space"
    return "ok"


class Backup:
    def __init__(self, config):
        self.config = config
        self.root = Path(config.get("storage_root", "/srv/zoj-editor"))
        self.mount = Path(config["mount"])
        self.state = Path(config.get("state_dir", str(DEFAULT_STATE)))
        self.stage = self.state / "staging"
        self.repo = self.mount / "restic"
        self.env = {**os.environ, "RESTIC_REPOSITORY": str(self.repo),
                    "RESTIC_PASSWORD_FILE": config["password_file"],
                    "RESTIC_CACHE_DIR": str(self.state / "cache")}
        self.status_path = self.state / "status.json"

    def status(self, **fields):
        previous = json.loads(self.status_path.read_text()) if self.status_path.exists() else {}
        result = {**previous, **fields, "updated_at": now()}
        result["health"] = health(result)
        atomic_json(self.status_path, result, 0o644)

    def guard(self):
        if not self.mount.is_absolute() or self.mount.is_symlink() or self.mount == Path("/"):
            raise RuntimeError("Unsafe backup mount path")
        result = json.loads(run(["findmnt", "--json", "--mountpoint", str(self.mount), "--output", "TARGET,UUID"]))
        filesystems = result.get("filesystems", [])
        if len(filesystems) != 1 or filesystems[0].get("uuid") != self.config["filesystem_uuid"]:
            raise RuntimeError("Backup disk is absent or its UUID changed; refusing to write to the OS disk")
        if self.mount.stat().st_dev == self.root.stat().st_dev:
            raise RuntimeError("Backup and source must be on different filesystems")
        for path in (self.repo, Path(self.env["RESTIC_PASSWORD_FILE"]), self.state):
            if path.is_symlink():
                raise RuntimeError("Backup control paths must not be symlinks")
        if not (self.repo / "config").is_file():
            raise RuntimeError("Restic repository is not initialized")

    def restic(self, *args, timeout=3600):
        return run([self.config.get("restic", "restic"), *args], env=self.env, timeout=timeout)

    def snapshots(self):
        return json.loads(self.restic("snapshots", "--tag", TAG, "--json"))

    def db_container(self, service="db"):
        rows = run(["docker", "ps", "--filter", "label=com.docker.compose.project=zoj-editor",
                    "--filter", f"label=com.docker.compose.service={service}", "--format", "{{.ID}}"]).decode().split()
        if len(rows) != 1:
            raise RuntimeError(f"Expected exactly one running Editor {service} container")
        return rows[0]

    def image(self, container):
        return run(["docker", "inspect", "--format", "{{.Image}}", container]).decode().strip()

    def export_database(self):
        self.stage.mkdir(mode=0o700, parents=True, exist_ok=True)
        database = self.db_container()
        command = ["docker", "exec", "-i", database, "psql", "-XqAt", "-v", "ON_ERROR_STOP=1", "-U", "editor", "-d", "editor"]
        # The dump and reference manifest use ONE exported repeatable-read snapshot.
        with tempfile.TemporaryFile() as errors:
            session = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
            temporary = self.stage / "editor.dump.pending"
            try:
                session.stdin.write("SET idle_in_transaction_session_timeout='20min';\nBEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSELECT pg_export_snapshot();\n")
                session.stdin.flush()
                snapshot = session.stdout.readline().strip()
                if not re.fullmatch(r"[A-Fa-f0-9-]+", snapshot):
                    raise RuntimeError("Could not export a consistent database snapshot")
                with temporary.open("wb") as target:
                    run(["docker", "exec", database, "pg_dump", "-U", "editor", "-d", "editor", "-Fc", f"--snapshot={snapshot}"], stdout=target, timeout=900)
                    target.flush()
                    os.fsync(target.fileno())
                output, _ = session.communicate(MANIFEST_SQL + "\nCOMMIT;\n\\q\n", timeout=120)
                if session.returncode:
                    raise RuntimeError("Database reference snapshot could not be read")
                manifest = json.loads(output)
                if set(manifest["counts"]) != set(TABLES):
                    raise RuntimeError("Database snapshot is missing required Editor tables")
                validate_blobs(self.root / "data", manifest)
                # Read the archive catalog before replacing the last staging dump.
                with temporary.open("rb") as archive:
                    run(["docker", "exec", "-i", database, "pg_restore", "--list"], stdin=archive, timeout=120)
                os.replace(temporary, self.stage / "editor.dump")
                atomic_json(self.stage / "database-manifest.json", manifest)
                return {"postgres_image": self.image(database), "api_image": self.image(self.db_container("api")),
                        "counts": manifest["counts"], "referenced_files": len(manifest["blobs"]),
                        "dump_sha256": file_hash(self.stage / "editor.dump")}
            finally:
                if session.poll() is None:
                    session.kill()
                session.wait()
                temporary.unlink(missing_ok=True)

    def backup(self, pin=False):
        self.guard()
        usage = shutil.disk_usage(self.mount)
        if usage.free < max(2 * 1024**3, usage.total // 20):
            raise RuntimeError("Backup disk has less than 5% or 2 GiB free; run maintenance and add capacity")
        # Serialize only the production snapshot with deployments, not restores/pruning.
        deployment_lock = self.root / "config" / "deploy.lock"
        if not deployment_lock.is_file():
            raise RuntimeError("Editor deployment lock does not exist")
        with locked(deployment_lock):
            metadata = self.export_database()
            metadata.update(format="zoj-editor-backup-v1", started_at=now(), run_id=uuid.uuid4().hex,
                            storage_root=str(self.root), staging_root=str(self.stage))
            atomic_json(self.stage / "backup-manifest.json", metadata)
            paths = [self.root / "data", self.root / "config", self.root / "certs", self.stage]
            paths += [Path(self.config.get("program_dir", "/usr/local/lib/zoj-editor-backup")),
                      Path(self.config.get("config_file", str(DEFAULT_CONFIG)))]
            if any(not path.exists() for path in paths):
                raise RuntimeError("A required backup source is missing")
            # Incomplete restic snapshots never receive the complete tag.
            result = self.restic("backup", "--json", "--host", "zoj-editor", "--tag", PENDING,
                                 "--exclude", str(self.root / "data" / "imports"),
                                 "--exclude", str(self.root / "data" / "work"), *map(str, paths))
            summaries = [json.loads(line) for line in result.decode().splitlines() if line.strip()]
            summary = next((row for row in summaries if row.get("message_type") == "summary"), None)
            if not summary or not re.fullmatch(r"[a-f0-9]{8,64}", summary.get("snapshot_id", "")):
                raise RuntimeError("Restic did not confirm a complete snapshot")
            tags = ["tag", "--remove", PENDING, "--add", TAG]
            if pin:
                tags += ["--add", "pinned"]
            self.restic(*tags, summary["snapshot_id"])
            latest = snapshot_latest(self.snapshots())
        usage = shutil.disk_usage(self.mount)
        self.status(backup_error=None, last_backup_at=now(), snapshot=latest["id"], counts=metadata["counts"],
                    referenced_files=metadata["referenced_files"], disk_used_percent=round(100 * (1 - usage.free / usage.total), 1),
                    )
        print(json.dumps({"snapshot": latest["short_id"], "counts": metadata["counts"]}))

    def maintain(self):
        self.guard()
        snapshot_latest(self.snapshots())
        self.restic("check", f"--read-data-subset={dt.date.today().weekday() + 1}/7")
        self.restic("forget", "--tag", TAG, "--group-by", "host,paths", "--keep-within", "1d",
                    "--keep-hourly", "72", "--keep-daily", "30", "--keep-weekly", "12", "--keep-monthly", "12", "--keep-tag", "pinned")
        self.restic("forget", "--tag", PENDING, "--keep-within", "7d")
        self.restic("prune")
        self.status(last_maintenance_at=now(), maintain_error=None)
        print("Editor backup retention and pruning completed")

    def drill(self):
        self.guard()
        latest = snapshot_latest(self.snapshots())
        directory = self.state / "restore-drills"
        directory.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="drill-", dir=directory) as scratch:
            target = Path(scratch)
            self.restic("restore", latest["id"], "--target", str(target), "--verify")
            stage = target / self.stage.relative_to("/")
            manifest = json.loads((stage / "backup-manifest.json").read_text())
            expected = json.loads((stage / "database-manifest.json").read_text())
            if manifest["format"] != "zoj-editor-backup-v1" or file_hash(stage / "editor.dump") != manifest["dump_sha256"]:
                raise RuntimeError("Restored database archive checksum failed")
            count = validate_blobs(target / self.root.relative_to("/") / "data", expected, hashes=True)
            name = "zoj-editor-restore-drill-" + uuid.uuid4().hex[:12]
            # No port or network; no live database/file paths are mounted.
            run(["docker", "run", "-d", "--rm", "--network", "none", "--memory", "1g", "--cpus", "1",
                 "--name", name, "--label", "zoj.editor.restore-drill=true", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                 manifest["postgres_image"]])
            try:
                for attempt in range(60):
                    ready = subprocess.run(["docker", "exec", name, "sh", "-c",
                                            'test "$(cat /proc/1/comm)" = postgres && pg_isready -U postgres'],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if ready.returncode == 0:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("Temporary restore database did not become ready")
                run(["docker", "exec", name, "createdb", "-U", "postgres", "editor_restore"])
                with (stage / "editor.dump").open("rb") as archive:
                    run(["docker", "exec", "-i", name, "pg_restore", "-U", "postgres", "-d", "editor_restore",
                         "--no-owner", "--no-privileges", "--exit-on-error"], stdin=archive)
                actual = json.loads(run(["docker", "exec", "-i", name, "psql", "-XqAt", "-v", "ON_ERROR_STOP=1",
                                         "-U", "postgres", "-d", "editor_restore"], input=MANIFEST_SQL.encode()))
                if actual != expected:
                    raise RuntimeError("Restored problem/working-copy/revision/file references differ from the backup snapshot")
            finally:
                run(["docker", "rm", "-f", name])
        self.status(last_restore_verified_at=now(), verified_snapshot=latest["id"], verified_files=count, drill_error=None)
        print(json.dumps({"verified_snapshot": latest["short_id"], "verified_files": count, "counts": expected["counts"]}))


def public_status(state=DEFAULT_STATE):
    path = state / "status.json"
    result = json.loads(path.read_text()) if path.exists() else {"health": "not_configured"}
    result["health"] = health(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("health") == "ok" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "maintain", "drill", "status"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--pin", action="store_true", help="Keep a manual snapshot until explicitly unpinned")
    args = parser.parse_args()
    if args.action == "status":
        return public_status()
    if os.geteuid() != 0:
        parser.error("Run backup operations with sudo; status is readable without sudo")
    backup = Backup(json.loads(args.config.read_text()))
    try:
        with locked("/run/lock/zoj-editor-backup.lock"):
            getattr(backup, args.action)(pin=args.pin) if args.action == "backup" else getattr(backup, args.action)()
    except Exception as error:
        backup.status(**{args.action + "_error": str(error)}, failed_action=args.action, failed_at=now())
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
