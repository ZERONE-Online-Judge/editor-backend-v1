# ZOJ runtime policy v1

ZOJ uses one resource policy across ordinary judging and Editor preparation.
ICPC toolchain/runtime flags are no longer the execution policy.

- New CPython (`python311`) and PyPy (`pypy3`) both implement Python 3.11.17.
  CPython is 3.11.17; PyPy is stable 8.0.1. Python 3.12 support in this PyPy
  release is beta and PyPy does not provide Python 3.13.
- Historical `python313` submissions retain their explicit CPython 3.13.16
  runtime ID. Problem admission and manual allowances migrate to `python311`;
  historical submission source, language IDs and verdicts are not rewritten.
- Java and Kotlin use the same digest-pinned Temurin 21.0.12.1+1 build.
  Java still compiles with `--release 8`; Kotlin remains 1.9.24.
- Both JVM languages use UTF-8, SerialGC, ActiveProcessorCount=1 and Xss8m.
  Xmx is the sandbox budget minus min(ceil(budget/2), max(64,ceil(budget/4))).
  Xms is min(32, Xmx), in MiB. JVM code, metadata, stacks and JIT are included
  in total memory; Xmx is not an additional memory allowance.
- CPython and PyPy both check syntax before testing; syntax errors are CE.
  Each testcase gets a fresh process. Startup, JIT and GC count toward CPU.
  Compilation, checker work and queue waiting are excluded from solution CPU.
- All managed compiler, checker, solution and Editor execution calls share a
  CPU lease pool. One guest CPU stays outside the sandbox pool when possible.
  Isolate per-box cgroup cpusets and Docker cpusets enforce one CPU per call.
  Calls wait for a free CPU before measured execution. Leases also serialize
  box reuse and are released on failure. This does not isolate host VM CPUs.

Automatic allowances remain unchanged pending repeated performance tests:
CPython time 3*T+2000ms, memory 2*M+32MiB; PyPy time 3*T+2000ms, memory
2*M+128MiB; Java/Kotlin time 2*T+1000ms, memory 2*M+16MiB; C/C++ use T,M.
Explicit testcase and per-language fields keep their existing precedence.
Displayed CPU time is not divided by these factors.

Base images are digest-pinned and runtime downloads are SHA-256 verified.
Actual compiler/interpreter versions and runtime options contribute to the
registration revision, so rebuilding toolchains invalidates runtime caches.
New jobs require judge-zoj-runtime-v1 / editor-zoj-runtime-v1 capabilities.
Legacy ICPC capability tokens, image labels and a JDK path symlink exist only
for queued legacy payload compatibility; they do not select JVM policies.

Deployment: pass unit/API/UI checks, actual six-language verdict and resource
checks, CPU-mask escape and recursion checks. Pause routing and drain each
node before replacement, preserve credentials/slots/VM resource limits, verify
registration and real jobs, then restore its previous routing flags. Retain
previous images and private Compose snapshots for rollback.
