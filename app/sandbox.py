import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import stat
from dataclasses import dataclass
from pathlib import Path
from .config import get_settings
from .db import uid
from .schema import safe_name
from .storage import read_blob

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "testlib"


def safe_output(path, limit):
    if not path.exists():
        return b""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SandboxError("산출물은 일반 파일이어야 합니다.")
        return os.read(fd, limit)
    finally:
        os.close(fd)


class SandboxError(RuntimeError):
    pass


@dataclass
class Program:
    name: str
    language: str
    artifacts: Path
    command: list[str]


class Sandbox:
    def __init__(self, check_cancelled=lambda: None):
        self.settings = get_settings()
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="editor-", dir=self.settings.work_dir)).resolve()
        self.check_cancelled = check_cancelled
        self.programs = {}

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def new_folder(self):
        path = self.root / uid()
        path.mkdir(mode=0o777)
        path.chmod(0o777)
        return path

    def execute(self, folder, command, stdin=b"", cpu_ms=1000, memory_mb=256, output_bytes=1048576, wall_seconds=None, interactive=None, output_file="stdout", image=None, compile_language=None, export_files=None):
        self.check_cancelled()
        config = {"command": command, "cpu_ms": cpu_ms, "memory_mb": memory_mb, "output_bytes": output_bytes,
                  "wall_seconds": wall_seconds or cpu_ms / 1000 * 5 + 3, "output_file": output_file, "compile_language": compile_language, "export_files": export_files or []}
        if interactive:
            config.update(interactive)
        (folder / ".input").write_bytes(stdin)
        config_file = folder / ".editor-job.json"
        config_file.write_text(json.dumps(config))
        config_file.chmod(0o600)
        name = "editor-run-" + uid()
        files = {}
        total = 0
        for path in folder.rglob("*"):
            if path.is_symlink():
                raise SandboxError("심볼릭 링크는 실행 자료로 사용할 수 없습니다.")
            if path.is_file() and path.name != ".editor-job.json":
                total += path.stat().st_size
                if total > 64 * 1024 * 1024:
                    raise SandboxError("한 실행의 입력·소스·리소스 합계는 64MiB 이하여야 합니다.")
                files[str(path.relative_to(folder))] = {"base64": base64.b64encode(path.read_bytes()).decode("ascii"), "executable": bool(path.stat().st_mode & 0o111)}
        payload = json.dumps({"config": config, "files": files}).encode()
        argv = ["docker", "run", "-i", "--name", name, "--network=none", "--read-only", "--cap-drop=ALL",
                "--cap-add=SETUID", "--cap-add=SETGID", "--cap-add=CHOWN", "--cap-add=KILL", "--cap-add=DAC_OVERRIDE", "--security-opt=no-new-privileges",
                "--pids-limit=128", "--cpus=1", f"--memory={memory_mb + 256}m", f"--memory-swap={memory_mb + 256}m",
                "--ulimit", "nofile=256:256", "--ulimit", "core=0:0", "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
                "--volume", "/work", "--workdir=/work", image or self.settings.runner_image]
        try:
            completed = subprocess.run(argv, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=config["wall_seconds"] + 15, check=False)
            try:
                result = json.loads(completed.stdout)
            except (ValueError, UnicodeError):
                state = subprocess.run(["docker", "inspect", name, "--format", "{{.State.OOMKilled}}"], capture_output=True, text=True, timeout=10)
                if state.stdout.strip() == "true":
                    result = {"status": "memory-limit", "cpu_ms": None, "wall_ms": None, "memory_kb": None, "exit_code": 137, "stderr": "컨테이너 OOM으로 자원 계측을 완료하지 못했습니다.", "metrics_available": False}
                else:
                    raise SandboxError("격리 실행 환경 오류: " + completed.stderr[:4096].decode("utf-8", "replace"))
            result["output"] = base64.b64decode(result.pop("output_base64", ""), validate=True)
            artifacts = result.pop("artifacts", {})
            target_root = folder / "run"
            target_root.mkdir(exist_ok=True)
            for relative, encoded in artifacts.items():
                parts = Path(relative).parts
                if not parts or Path(relative).is_absolute() or any(part in {"..", "."} for part in parts):
                    raise SandboxError("Invalid artifact path")
                for part in parts:
                    safe_name(part)
                target = target_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(encoded, validate=True))
                target.chmod(0o755 if relative == "program" else 0o644)
            self.check_cancelled()
            return result
        except subprocess.TimeoutExpired:
            raise SandboxError("격리 감독 프로세스 응답 시간이 초과되었습니다.")
        finally:
            subprocess.run(["docker", "rm", "-fv", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)

    def compile(self, source, resources, is_solution=False):
        cache_key = (source["name"], source["sha256"], is_solution)
        if cache_key in self.programs:
            return self.programs[cache_key]
        folder = self.new_folder()
        name, language = safe_name(source["name"]), source["language"]
        (folder / name).write_bytes(read_blob(source["sha256"]))
        shutil.copy(VENDOR / "testlib.h", folder / "testlib.h")
        for resource in resources:
            if (resource["kind"] in {"resource", "aux"} or (is_solution and resource.get("compile_with_solution"))) and resource["name"] != name:
                (folder / safe_name(resource["name"])).write_bytes(read_blob(resource["sha256"]))
        if language in {"cpp17", "cpp20", "c17"}:
            std = {"cpp17": "c++17", "cpp20": "c++20", "c17": "c17"}[language]
            compiler = "gcc" if language == "c17" else "g++"
            units = [r["name"] for r in resources if is_solution and r.get("compile_with_solution") and r["name"] != name and r["language"] in ({"c17"} if language == "c17" else {"cpp17", "cpp20"})]
            argv = [compiler, f"-std={std}", "-O2", "-pipe", name, *units, "-o", "program"]
            cmd = ["./program"]
        elif language == "java17":
            text = (folder / name).read_text()
            match = re.search(r"public\s+class\s+([A-Za-z_][A-Za-z0-9_]*)", text)
            classname = match.group(1) if match else Path(name).stem
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", classname):
                raise SandboxError("Java 클래스명을 확인하세요.")
            actual_name = classname + ".java"
            if actual_name != name:
                (folder / actual_name).write_bytes(read_blob(source["sha256"]))
            units = [r["name"] for r in resources if is_solution and r.get("compile_with_solution") and r["language"] == "java17" and r["name"] != actual_name]
            argv = ["javac", "-J-XX:ActiveProcessorCount=1", "-d", ".", actual_name, *units]
            package = re.search(r"^\s*package\s+([A-Za-z_][A-Za-z0-9_.]*)\s*;", text, re.M)
            entry = source.get("entry_class") or (package.group(1) + "." if package else "") + classname
            cmd = ["java", "-XX:ActiveProcessorCount=1", "-XX:+UseSerialGC", "-Xss1m", entry]
        elif language == "python3":
            argv = ["python3", "-m", "py_compile", name]
            cmd = ["python3", name]
        else:
            raise SandboxError("실행 가능한 언어를 선택하세요.")
        result = self.execute(folder, argv, cpu_ms=30000, memory_mb=2048, output_bytes=64 * 1024 * 1024, wall_seconds=60, compile_language=language)
        if result["status"] != "ok" or result["exit_code"] != 0:
            raise SandboxError(f"{name} 컴파일 실패\n{result['stderr']}\n{result['output'].decode('utf-8', 'replace')[:32768]}")
        for filename in [".output", ".stderr", ".editor-job.json", ".input"]:
            (folder / filename).unlink(missing_ok=True)
        artifacts = folder / "run"
        for file in folder.iterdir():
            if file.name != "run" and not file.name.startswith(".") and file.is_file():
                shutil.copy2(file, artifacts / file.name)
        program = Program(name, language, artifacts, cmd)
        self.programs[cache_key] = program
        return program

    def copy_program(self, program, folder):
        if any(file.is_symlink() for file in program.artifacts.rglob("*")):
            raise SandboxError("실행 산출물에 심볼릭 링크가 있습니다.")
        for file in program.artifacts.iterdir():
            if file.is_symlink():
                raise SandboxError("실행 산출물에 심볼릭 링크가 있습니다.")
            if file.is_dir():
                shutil.copytree(file, folder / file.name, symlinks=False)
            elif file.is_file():
                shutil.copy2(file, folder / file.name)

    def run(self, program, stdin=b"", args=(), cpu_ms=1000, memory_mb=256, input_file="stdin", output_file="stdout", files=None, output_bytes=1048576):
        folder = self.new_folder()
        self.copy_program(program, folder)
        for name, content in (files or {}).items():
            (folder / safe_name(name)).write_bytes(content)
        if input_file != "stdin":
            (folder / safe_name(input_file)).write_bytes(stdin)
        command = list(program.command)
        if program.language == "java17":
            command.insert(-1, f"-Xmx{max(4, memory_mb - 64)}m")
        result = self.execute(folder, [*command, *args], stdin, cpu_ms, memory_mb, output_bytes, output_file=output_file)
        return result

    def interactive(self, program, actor, stdin, answer=b"", cpu_ms=1000, memory_mb=256):
        folder = self.new_folder()
        solution_dir, interactor_dir = folder / "solution", folder / "interactor"
        solution_dir.mkdir()
        interactor_dir.mkdir()
        self.copy_program(program, solution_dir)
        self.copy_program(actor, interactor_dir)
        (interactor_dir / "input").write_bytes(stdin)
        (interactor_dir / "answer").write_bytes(answer)
        command = list(program.command)
        if program.language == "java17":
            command.insert(-1, f"-Xmx{max(4, memory_mb - 64)}m")
        result = self.execute(folder, command, cpu_ms=cpu_ms, memory_mb=memory_mb,
            interactive={"interactive": True, "interactor_command": [*actor.command, "input", "transcript", "answer"]})
        return result
