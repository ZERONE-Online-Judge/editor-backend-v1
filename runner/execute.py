"""Trusted supervisor. Uploaded code runs as UID 1000, never as supervisor UID 0."""
import base64
import json
import shutil
import stat
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

payload = json.load(sys.stdin)
config = payload["config"]
root = Path("/work")
root.chmod(0o755)
for relative, entry in payload["files"].items():
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError("Unsafe input path")
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(entry["base64"], validate=True))
    target.chmod(0o755 if entry["executable"] else 0o644)
config_path = root / ".editor-job.json"
config_path.write_text(json.dumps(config))
config_path.chmod(0o600)
if not config.get("interactive"):
    run_dir = root / "run"
    run_dir.mkdir(exist_ok=True)
    for file in list(root.iterdir()):
        if not file.name.startswith(".") and file.name != "run":
            file.rename(run_dir / file.name)
    run_dir.chmod(0o700)
    os.chown(run_dir, 1000, 1000)


def command(uid, cpu_ms, output_bytes, evidence, args):
    return ["/opt/editor/launch", str(uid), str(cpu_ms), str(output_bytes), str(evidence), "--", *args]


def process_usage(pid):
    """Read all descendants in this process session, including detached forks."""
    total_cpu = 0
    total_rss = 0
    hz = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            status = (entry / "status").read_text()
            uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
            if int(uid_line.split()[1]) != 1000:
                continue
            total_cpu += (int(stat[11]) + int(stat[12])) * 1000 / hz
            total_rss += int(stat[21]) * page / 1024
        except (OSError, ValueError, StopIteration):
            pass
    return total_cpu, total_rss


def stop(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def supervise(proc, cpu_ms, memory_mb, wall_seconds, evidence, other=None):
    begin = time.monotonic()
    verdict = "ok"
    stopped_by_interactor = False
    peak_rss = 0
    measured_cpu = 0
    status = None
    usage = None
    while True:
        pid, status_now, usage_now = os.wait4(proc.pid, os.WNOHANG)
        if pid:
            status, usage = status_now, usage_now
            break
        cpu, rss = process_usage(proc.pid)
        peak_rss = max(peak_rss, rss)
        measured_cpu = max(measured_cpu, cpu)
        if cpu > cpu_ms:
            verdict = "time-limit"
        elif rss > memory_mb * 1024:
            verdict = "memory-limit"
        elif time.monotonic() - begin > wall_seconds:
            verdict = "wall-limit"
        if verdict != "ok":
            stop(proc)
            _, status, usage = os.wait4(proc.pid, 0)
            break
        if other is not None and other.poll() is not None and other.returncode != 0:
            stopped_by_interactor = True
            stop(proc)
            _, status, usage = os.wait4(proc.pid, 0)
            break
        time.sleep(0.01)
    proc.returncode = os.waitstatus_to_exitcode(status)
    if evidence.exists() and evidence.stat().st_size:
        recorded = json.loads(evidence.read_text())
        measured_cpu = max(measured_cpu, recorded["cpu_ms"])
        peak_rss = max(peak_rss, recorded["memory_kb"])
        proc.returncode = recorded["exit_code"]
    elif verdict == "ok" and not stopped_by_interactor:
        raise RuntimeError("Native resource evidence was not produced")
    if verdict == "ok" and (measured_cpu > cpu_ms or proc.returncode == -signal.SIGXCPU):
        verdict = "time-limit"
    if verdict == "ok" and peak_rss > memory_mb * 1024:
        verdict = "memory-limit"
    if verdict == "ok" and proc.returncode == -signal.SIGXFSZ:
        verdict = "output-limit"
    return {"status": verdict, "exit_code": proc.returncode, "cpu_ms": round(measured_cpu, 2), "wall_ms": round((time.monotonic() - begin) * 1000, 2), "memory_kb": int(peak_rss), "stopped_by_interactor": stopped_by_interactor}


memory_mb = config.get("memory_mb", 256)
cpu_ms = config.get("cpu_ms", 1000)
output_bytes = config.get("output_bytes", 1048576)
wall_seconds = config.get("wall_seconds", cpu_ms / 1000 * 5 + 3)
env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/tmp", "TMPDIR": "/tmp"}

if config.get("interactive"):
    solution_dir, interactor_dir = root / "solution", root / "interactor"
    for directory, uid in [(solution_dir, 1000), (interactor_dir, 1001)]:
        for file in directory.rglob("*"):
            os.chown(file, uid, uid)
        directory.chmod(0o700)
        os.chown(directory, uid, uid)
    interactor_log = (root / ".interactor-log").open("wb")
    candidate_log = (root / ".stderr").open("wb")
    read_a, write_a = os.pipe()
    read_b, write_b = os.pipe()
    actor_evidence, evidence = root / ".actor-metrics", root / ".solution-metrics"
    actor = subprocess.Popen(command(1001, max(10000, cpu_ms * 5), output_bytes, actor_evidence, config["interactor_command"]), stdin=read_a, stdout=write_b, stderr=interactor_log,
        cwd=interactor_dir, env=env, start_new_session=True)
    proc = subprocess.Popen(command(1000, cpu_ms, output_bytes, evidence, config["command"]), stdin=read_b, stdout=write_a, stderr=candidate_log,
        cwd=solution_dir, env=env, start_new_session=True)
    for fd in [read_a, write_a, read_b, write_b]:
        os.close(fd)
    result = supervise(proc, cpu_ms, memory_mb, wall_seconds, evidence, actor)
    try:
        actor.wait(timeout=2)
    except subprocess.TimeoutExpired:
        stop(actor)
        actor.wait()
    result["interactor_exit_code"] = json.loads(actor_evidence.read_text())["exit_code"] if actor_evidence.exists() and actor_evidence.stat().st_size else actor.returncode
    transcript = interactor_dir / "transcript"
    if transcript.exists() and transcript.is_file() and not transcript.is_symlink():
        (root / ".output").write_bytes(transcript.read_bytes()[:output_bytes])
    result["interactor_log"] = (root / ".interactor-log").read_bytes()[:32768].decode("utf-8", "replace")
else:
    input_path = root / ".input"
    output_path, error_path = root / ".output", root / ".stderr"
    with input_path.open("rb") as input_stream, output_path.open("wb") as output_stream, error_path.open("wb") as error_stream:
        evidence = root / ".solution-metrics"
        proc = subprocess.Popen(command(1000, cpu_ms, output_bytes, evidence, config["command"]), stdin=input_stream, stdout=output_stream, stderr=error_stream,
            env=env, cwd=run_dir, start_new_session=True)
        result = supervise(proc, cpu_ms, memory_mb, wall_seconds, evidence)
if not config.get("interactive") and config.get("output_file", "stdout") != "stdout":
    named = run_dir / config["output_file"]
    if named.is_file() and not named.is_symlink():
        (root / ".output").write_bytes(named.read_bytes()[:output_bytes])
    else:
        (root / ".output").write_bytes(b"")
result["stderr"] = (root / ".stderr").read_bytes()[:32768].decode("utf-8", "replace")
if result["status"] == "ok" and result["exit_code"] != 0 and any(token in result["stderr"] for token in ["MemoryError", "OutOfMemoryError", "std::bad_alloc"]):
    result["status"] = "memory-limit"
def encoded(path, limit):
    if not path.is_file() or path.is_symlink():
        return ""
    with path.open("rb") as stream:
        return base64.b64encode(stream.read(limit)).decode("ascii")

result["output_base64"] = encoded(root / ".output", output_bytes)
artifacts = {}
if config.get("compile_language") and result["status"] == "ok" and result["exit_code"] == 0:
    language = config["compile_language"]
    selected = [run_dir / "program"] if language in {"cpp17", "cpp20", "c17", "c99"} else list(run_dir.rglob("*.class")) if language in {"java17", "java8"} else []
    total = 0
    for file in selected:
        if file.is_symlink() or not file.is_file():
            raise RuntimeError("Invalid compiler artifact")
        total += file.stat().st_size
        if total > 32 * 1024 * 1024:
            raise RuntimeError("Compiler artifact size limit")
        artifacts[str(file.relative_to(run_dir))] = encoded(file, 32 * 1024 * 1024)
for name in config.get("export_files", []):
    file = run_dir / name
    if file.is_file() and not file.is_symlink():
        artifacts[name] = encoded(file, 16 * 1024 * 1024)
result["artifacts"] = artifacts
print(json.dumps(result))
