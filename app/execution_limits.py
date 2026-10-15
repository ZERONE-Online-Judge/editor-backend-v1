"""Shared execution policy values used by validation, runners and the UI API."""
MAX_TESTS_PER_SET = 1000
MAX_STRESS_ITERATIONS = 1000
MAX_GENERATOR_ARGUMENTS = 64
GENERATOR_CPU_MS = 10000
GENERATOR_MEMORY_MB = 512
GENERATOR_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_ACTIVE_JOBS_PER_USER = 5
COUNTEREXAMPLE_TARGETS = [1, 5, 10]
MAX_STRESS_PATTERNS = 20
MAX_STRESS_COUNTEREXAMPLES = 200
MAX_STRESS_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_MINIMIZE_ATTEMPTS = 200
MAX_MINIMIZE_SECONDS = 60
MAX_MINIMIZE_INPUT_BYTES = 1024 * 1024


def public_limits(settings):
    return {"tests_per_set": MAX_TESTS_PER_SET, "stress_iterations": MAX_STRESS_ITERATIONS,
            "stress_seconds": min(settings.stress_max_seconds, settings.job_timeout_seconds),
            "counterexample_targets": COUNTEREXAMPLE_TARGETS,
            "stress_patterns": MAX_STRESS_PATTERNS, "stress_counterexamples": MAX_STRESS_COUNTEREXAMPLES,
            "stress_evidence_bytes": MAX_STRESS_EVIDENCE_BYTES,
            "minimize_attempts": MAX_MINIMIZE_ATTEMPTS, "minimize_seconds": MAX_MINIMIZE_SECONDS,
            "minimize_input_bytes": MAX_MINIMIZE_INPUT_BYTES,
            "generator_arguments": MAX_GENERATOR_ARGUMENTS, "generator_cpu_ms": GENERATOR_CPU_MS,
            "generator_memory_mb": GENERATOR_MEMORY_MB, "generator_output_bytes": GENERATOR_OUTPUT_BYTES,
            "job_timeout_seconds": settings.job_timeout_seconds, "worker_concurrency": settings.worker_concurrency,
            "judge_concurrency": settings.judge_concurrency, "active_jobs_per_user": MAX_ACTIVE_JOBS_PER_USER}
