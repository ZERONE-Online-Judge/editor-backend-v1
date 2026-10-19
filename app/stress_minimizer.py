"""Bounded input reduction. Only validator-approved, re-executed failures survive."""
import math
import re
import time

from .execution_limits import MAX_MINIMIZE_INPUT_BYTES
from .sandbox import SandboxError


class ReductionBudgetReached(RuntimeError):
    pass


INTEGER = re.compile(r"(?<!\S)[+-]?\d+(?!\S)")


def array_layout(content):
    """Recognize N followed by exactly N integer values in three plain layouts."""
    try:
        text = content.decode("utf-8")
        tokens = text.split()
        if not tokens or any(not re.fullmatch(r"[+-]?\d{1,30}", token) for token in tokens):
            return None
        count = int(tokens[0])
        if count < 0 or count != len(tokens) - 1:
            return None
        lines = text.splitlines()
        if len(lines) == 1:
            layout = "inline"
        elif lines[0].strip() == tokens[0] and len(lines) == 2:
            layout = "row"
        elif len(lines) == count + 1 and all(len(line.split()) == 1 for line in lines):
            layout = "column"
        else:
            return None
        return layout, [int(token) for token in tokens[1:]]
    except (ValueError, UnicodeDecodeError):
        return None


def format_array(layout, values):
    if layout == "inline":
        return (" ".join(map(str, [len(values), *values])) + "\n").encode()
    if layout == "column":
        return ("\n".join(map(str, [len(values), *values])) + "\n").encode()
    return (str(len(values)) + "\n" + " ".join(map(str, values)) + "\n").encode()


def simplify_values(value):
    # Keep reductions monotonic in absolute value; no exhaustive minimum claim.
    values = [0, 1 if value > 0 else -1, (abs(value) // 2) * (1 if value >= 0 else -1)]
    return list(dict.fromkeys(candidate for candidate in values if abs(candidate) < abs(value)))


class CounterexampleMinimizer:
    def __init__(self, pipeline, example, content, reference, evidence_budget):
        self.p, self.example, self.best, self.reference = pipeline, example, content, reference
        self.original = content
        self.best_answer = None
        self.best_findings = None
        self.budget = evidence_budget
        self.options = pipeline.job.options
        self.metadata = {"status": "running", "attempts": 0, "accepted_steps": 0,
                         "original_bytes": len(content), "final_bytes": len(content), "elapsed_seconds": 0}
        example["minimization"] = self.metadata

    def check(self):
        self.outer_check()
        if time.monotonic() >= self.deadline:
            raise ReductionBudgetReached()

    def verdict(self, name, content, answer):
        verdict, actual = self.p.run_solution(name, content, answer)
        checked = {"stderr": actual["stderr"]}
        if verdict == "AC" and not actual.get("judged_by_zoj"):
            verdict, checked = self.p.checker(content, actual["output"], answer)
        if verdict == "JUDGE_ERROR":
            raise SandboxError("축소 후보를 검사하는 checker 또는 실행 환경에 오류가 발생했습니다.\n" + checked["stderr"])
        return verdict, actual, checked

    def try_input(self, content):
        self.check()
        if self.metadata["attempts"] >= self.options.get("minimize_attempts", 40):
            raise ReductionBudgetReached()
        self.metadata["attempts"] += 1
        group = self.options.get("group", "")
        valid, _ = self.p.validator(content, self.options["testset"], group) if group else self.p.validator(content, self.options["testset"])
        if not valid:
            return False
        verdict, reference = self.p.run_solution(self.reference, content)
        if verdict != "AC":
            return False
        findings = []
        for original in self.example["findings"]:
            self.check()
            verdict, actual, checked = self.verdict(original["solution"], content, reference["output"])
            if verdict != original["verdict"]:
                return False
            if verdict in {"TLE", "MLE"}:
                repeated, actual, checked = self.verdict(original["solution"], content, reference["output"])
                if repeated != verdict:
                    return False
            findings.append(({**original, "comment": checked["stderr"], "cpu_ms": actual.get("cpu_ms"),
                              "memory_kb": actual.get("memory_kb"), "output_truncated":actual.get("output_truncated",False), "output_total_bytes":actual.get("output_total_bytes")}, actual["output"]))
        required = len(content) + len(reference["output"]) + sum(len(output) for _, output in findings)
        if required > self.budget:
            self.metadata["status"] = "evidence_limit"
            raise ReductionBudgetReached()
        self.best, self.best_answer, self.best_findings = content, reference["output"], findings
        self.metadata.update(accepted_steps=self.metadata["accepted_steps"] + 1, final_bytes=len(content))
        self.p.event(f"반례 축소 · {len(self.original):,} → {len(content):,}바이트 · 검증 {self.metadata['attempts']}회")
        return True

    def reduce(self):
        started = time.monotonic()
        self.deadline = started + self.options.get("minimize_seconds", 10)
        self.outer_check = self.p.sandbox.check_cancelled
        self.p.sandbox.check_cancelled = self.check
        try:
            if len(self.original) > MAX_MINIMIZE_INPUT_BYTES:
                self.metadata["status"] = "input_limit"
                return
            try:
                self.original.decode("utf-8")
            except UnicodeDecodeError:
                self.metadata["status"] = "unsupported"
                return
            parsed = array_layout(self.best)
            self.metadata["strategy"] = "integer_array" if parsed else "integer_values"
            if parsed:
                layout, values = parsed
                granularity = 1
                while values:
                    chunk = math.ceil(len(values) / granularity)
                    changed = False
                    for offset in range(0, len(values), chunk):
                        candidate = values[:offset] + values[offset + chunk:]
                        if self.try_input(format_array(layout, candidate)):
                            values = candidate
                            granularity = max(1, granularity - 1)
                            changed = True
                            break
                    if changed:
                        continue
                    if granularity >= len(values):
                        break
                    granularity = min(len(values), granularity * 2)
            # Keep original whitespace for non-array inputs and value-only steps.
            while True:
                text = self.best.decode("utf-8")
                matches = list(INTEGER.finditer(text))
                if parsed and matches:
                    matches = matches[1:]
                changed = False
                for match in matches:
                    if len(match.group()) > 30:
                        continue
                    for value in simplify_values(int(match.group())):
                        candidate = (text[:match.start()] + str(value) + text[match.end():]).encode()
                        if self.try_input(candidate):
                            changed = True
                            break
                    if changed:
                        break
                if not changed:
                    break
            self.metadata["status"] = "finished"
        except ReductionBudgetReached:
            if self.metadata["status"] == "running":
                self.metadata["status"] = "budget"
        finally:
            self.p.sandbox.check_cancelled = self.outer_check
            self.metadata["elapsed_seconds"] = round(time.monotonic() - started, 2)
            if self.metadata["status"] == "running":
                self.metadata["status"] = "interrupted"
            if self.best_findings is not None:
                self.example["minimized"] = {"input_sha": self.p.blob(self.best), "answer_sha": self.p.blob(self.best_answer),
                                             "input_bytes": len(self.best), "findings": [{**finding, "actual_sha": self.p.blob(output)} for finding, output in self.best_findings]}
                self.metadata["evidence_bytes"] = len(self.best) + len(self.best_answer) + sum(len(output) for _, output in self.best_findings)
