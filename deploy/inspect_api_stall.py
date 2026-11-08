"""Read-only incident snapshot; never print locals, SQL, credentials or raw logs."""
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import urllib.request
import zipfile

WHEEL = "https://files.pythonhosted.org/packages/68/fb/bc7f639aed026bca6e7beb1e33f6951e16b7d315594e7635a4f7d21d63f4/py_spy-0.4.1-py2.py3-none-manylinux_2_5_x86_64.manylinux1_x86_64.whl"
SHA256 = "6a80ec05eb8a6883863a367c6a4d4f2d57de68466f7956b6367d4edd5c61bb29"
DATABASE_PROBE = r'''
import json
try:
    import psycopg
    from app.config import get_settings
    url = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url, connect_timeout=3, options="-c default_transaction_read_only=on -c statement_timeout=3000") as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT state, wait_event_type, wait_event, count(*), round(max(extract(epoch FROM (clock_timestamp()-state_change)))) FROM pg_stat_activity WHERE datname=current_database() GROUP BY state, wait_event_type, wait_event ORDER BY count(*) DESC")
            states=[{"state":a,"wait_type":b,"wait":c,"count":d,"max_state_seconds":float(e) if e is not None else None} for a,b,c,d,e in cursor.fetchall()]
            cursor.execute("SELECT mode, granted, count(*) FROM pg_locks WHERE database=(SELECT oid FROM pg_database WHERE datname=current_database()) GROUP BY mode,granted ORDER BY count(*) DESC")
            locks=[{"mode":a,"granted":b,"count":c} for a,b,c in cursor.fetchall()]
    print(json.dumps({"database_states":states,"database_locks":locks}))
except BaseException as error:
    print(json.dumps({"database_probe_error_class":type(error).__name__}))
'''


def command(*args, timeout=25, input=None):
    return subprocess.run(args, input=input, capture_output=True, timeout=timeout)


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    result = command("docker", "ps", "-q", "--filter", "label=com.docker.compose.project=zoj-editor", "--filter", "label=com.docker.compose.service=api")
    containers = result.stdout.decode().split()
    if result.returncode or len(containers) != 1:
        emit({"probe_error": "expected_one_running_editor_api", "count": len(containers)})
        return
    api = containers[0]
    result = command("docker", "inspect", "--format", "{{.State.Status}} {{.RestartCount}} {{.State.StartedAt}} {{.Config.Image}}", api)
    metadata = result.stdout.decode().strip().split()
    if result.returncode or len(metadata) != 4 or not metadata[3].startswith("zoj-editor-api:"):
        emit({"probe_error": "unexpected_api_metadata"})
        return
    emit({"api_state": metadata[0], "restarts": metadata[1], "started_at": metadata[2], "image": metadata[3]})
    result = command("docker", "exec", "-i", api, "python", "-", input=DATABASE_PROBE.encode(), timeout=15)
    if result.returncode == 0:
        emit(json.loads(result.stdout))
    else:
        emit({"database_probe_exit": result.returncode})
    logs = command("docker", "logs", "--since", "1h", "--tail", "10000", api)
    text = (logs.stdout + logs.stderr).decode(errors="replace")
    signatures = ["QueuePool limit", "TimeoutError", "OperationalError", "TooManyConnections", "Traceback", "Task was destroyed", "ERROR:"]
    emit({"recent_error_signature_counts": {name: text.count(name) for name in signatures}})
    del text, logs
    with tempfile.TemporaryDirectory(prefix="editor-stack-probe-") as temporary:
        target = Path(temporary) / "py-spy"
        with urllib.request.urlopen(WHEEL, timeout=30) as response:
            wheel = response.read(20 * 1024 * 1024)
        if hashlib.sha256(wheel).hexdigest() != SHA256:
            emit({"probe_error": "profiler_checksum_mismatch"})
            return
        with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
            target.write_bytes(archive.read("py_spy-0.4.1.data/scripts/py-spy"))
        target.chmod(0o755)
        result = command("docker", "run", "--rm", "--network", "none", "--pid", "container:" + api,
                         "--cap-add", "SYS_PTRACE", "--security-opt", "no-new-privileges", "--read-only", "--user", "0:0",
                         "-v", temporary + ":/probe:ro", metadata[3], "/probe/py-spy", "dump", "--pid", "1", "--json", "--nonblocking", timeout=35)
        if result.returncode:
            emit({"stack_probe_exit": result.returncode})
            return
        data = json.loads(result.stdout)
        threads = data if isinstance(data, list) else data["threads"]
        stacks = Counter()
        for thread in threads:
            frames = thread.get("frames", [])
            stack = tuple((frame.get("name", ""), Path(frame.get("filename") or "").name, frame.get("line")) for frame in frames)
            stacks[stack] += 1
        emit({"thread_count": len(threads), "stacks": [{"count": count, "frames": [{"function": name, "file": filename, "line": line} for name, filename, line in stack]} for stack, count in stacks.most_common()]})


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:
        emit({"probe_error_class": type(error).__name__})
        raise SystemExit(1)
