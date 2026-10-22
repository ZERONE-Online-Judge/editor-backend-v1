"""ZOJ runtime policy v1. Memory is the entire sandbox budget in MiB."""
import hashlib
import json
import subprocess

POLICY = "zoj-runtime-v1"
JAVA = "/usr/local/lib/zoj-jdk/bin/java"
JAVAC = "/usr/local/lib/zoj-jdk/bin/javac"
PYTHON = "/usr/local/bin/python3.11"


def runtime_fingerprint():
    tools = [[PYTHON, "--version"], ["/usr/local/bin/python3.13", "--version"],
             ["/usr/local/bin/pypy3", "--version"], [JAVA, "-version"],
             ["/usr/bin/gcc", "--version"], ["/usr/bin/g++", "--version"]]
    versions = []
    for command in tools:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
        versions.append([command[0], result.stdout.strip(), result.stderr.strip()])
    return hashlib.sha256(json.dumps([POLICY, jvm_options(256), versions], sort_keys=True).encode()).hexdigest()


def jvm_heap_mb(memory_mb):
    reserve = min((memory_mb + 1) // 2, max(64, (memory_mb + 3) // 4))
    return max(1, memory_mb - reserve)


def jvm_options(memory_mb):
    heap = jvm_heap_mb(memory_mb)
    return ["-Dfile.encoding=UTF-8", "-XX:ActiveProcessorCount=1",
            "-XX:+UseSerialGC", "-Xss8m", f"-Xms{min(32, heap)}m", f"-Xmx{heap}m"]


def with_runtime_limits(command, memory_mb):
    """Replace resource flags, including flags retained in cached programs."""
    if not command or command[0].rsplit("/", 1)[-1] != "java":
        return list(command)
    old_flags = ("-Xmx", "-Xms", "-Xss", "-XX:ActiveProcessorCount=",
                 "-XX:ReservedCodeCacheSize=", "-XX:CompressedClassSpaceSize=",
                 "-XX:MaxMetaspaceSize=", "-Dfile.encoding=")
    args = [arg for arg in command[1:] if not arg.startswith(old_flags)
            and arg not in {"-XX:+UseSerialGC", "-XX:+UseParallelGC", "-XX:+UseG1GC"}]
    return [JAVA, *jvm_options(memory_mb), *args]
