# PyPy and Kotlin execution profile

The added toolchains follow the language versions listed in the ICPC World
Finals programming environment (2026 page, checked 2026-10-17):
https://icpc.global/worldfinals/programming-environment

- `pypy3`: PyPy 7.3.15, Python 3.9.18, source extension `.py`.
- `kotlin`: Kotlin/JVM 1.9.24, Temurin OpenJDK 21.0.4+7, `.kt`.
- Existing CPython 3.13 and Java 8-compatible submissions are unchanged.

Official PyPy binaries and Kotlin compiler archives are SHA-256 checked at
image build time. Architectures: Linux x86_64 and aarch64. The production
judge uses x86_64; local Apple Silicon checks use the aarch64 build.

PyPy syntax is checked with `pypy3 -m py_compile`. Kotlin is compiled with
`kotlinc SOURCE -include-runtime -d program.jar`. The self-contained jar
preserves top-level/package main entrypoints without renaming user classes.
It is run with the pinned Java 21 executable and the ICPC flags:
`-Dfile.encoding=UTF-8 -XX:+UseSerialGC -Xss64m -Xms{limit}m -Xmx{limit}m`.
We additionally set `-XX:ActiveProcessorCount=1` for the single-CPU sandbox.
The jar packaging differs from ICPC's class-directory + kotlin launcher;
the compiler version, JVM, standard library and runtime flags are fixed.

These new languages use the problem's base CPU time and total memory limit
without a language multiplier, as in the referenced ICPC environment. ZOJ's
explicit per-language problem overrides still take precedence. JVM/PyPy/JIT
memory counts toward the total sandbox limit; heap size is not extra memory.
Editor isolated execution uses raw problem limits. Existing CPython/Java
language corrections in the ZOJ contest judge are retained.

Only updated agents advertising `judge-icpc-languages-v1` claim new-language
judging jobs. Editor tools require `editor-icpc-languages-v1`, advertised only
when the pinned runtime image has the expected capability label. This keeps
mixed-version rollouts from turning new-language jobs into system errors.

Before enabling the public UI, build the agent and Editor runtime images,
drain one node at a time, retain its current CPU/memory/slot settings, and run
the judge-agent `python -m app.security_smoke` inside the replacement agent's isolate setup.
Recreate the normal agent after this local-only smoke check. Never run the
smoke script as a replacement for the agent service's normal command.
