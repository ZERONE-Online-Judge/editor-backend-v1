"""Shared-input stress discovery, focused coverage and bounded reduction."""
import copy
import hashlib
import time

from .execution_limits import MAX_STRESS_COUNTEREXAMPLES, MAX_STRESS_EVIDENCE_BYTES
from .sandbox import SandboxError
from .stress import DEFAULT_VERDICTS, generator_command, minimum_cover, patterns_for, stop_mode, stress_configuration
from .stress_minimizer import CounterexampleMinimizer


class StressDurationReached(RuntimeError):
    pass


class EvidenceLimitReached(RuntimeError):
    pass


class StressSearch:
    def __init__(self, pipeline):
        self.p = pipeline
        self.options = pipeline.job.options
        self.reference = self.options.get("reference") or pipeline.main()
        self.candidates = list(dict.fromkeys(self.options.get("candidates") or ([self.options["candidate"]] if self.options.get("candidate") else [])))
        if not self.candidates:
            raise SandboxError("비교할 풀이를 선택하세요.")
        self.patterns = patterns_for(self.options)
        self.mode = stop_mode(self.options)
        self.focus = self.options.get("focus_uncovered", False) or self.mode == "all_candidates"
        self.stats = {"iterations_started": 0, "iterations_completed": 0, "elapsed_seconds": 0, "duplicates_skipped": 0,
                      "counterexample_count": 0, "stop_reason": None, "last_seed": None, "last_command": None,
                      "last_pattern_index": None, "last_range_values": [], "phase": "search", "evidence_bytes": 0}
        self.summaries = {name: {"name": name, "tag": pipeline.doc["solutions"][name]["tag"], "tested": 0,
                                "verdicts": {}, "counterexamples": 0, "first_counterexample_id": None} for name in self.candidates}
        self.pattern_stats = [{"index": index, **pattern, "attempts": 0, "valid_inputs": 0, "duplicates_skipped": 0,
                               "compared_inputs": 0, "counterexample_inputs": 0, "findings": 0, "solution_findings": {}} for index, pattern in enumerate(self.patterns)]
        pipeline.result.update(counterexample=None, counterexamples=[], stress=self.stats, candidate_summaries=list(self.summaries.values()),
                               pattern_summaries=self.pattern_stats, stress_configuration=stress_configuration(pipeline.doc, self.options))

    def check(self):
        self.outer_check()
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise StressDurationReached()
        with self.p.result_lock:
            self.stats["elapsed_seconds"] = round(time.monotonic() - self.started, 2)
            if time.monotonic() - self.p.last_publish >= 1:
                self.p.publish(copy.deepcopy(self.p.result))
                self.p.last_publish = time.monotonic()

    def reserve(self, count):
        if self.stats["evidence_bytes"] + count > MAX_STRESS_EVIDENCE_BYTES:
            raise EvidenceLimitReached()
        self.stats["evidence_bytes"] += count

    def compare(self, candidate, content, answer):
        verdict, actual = self.p.run_solution(candidate, content, answer)
        checked = {"stderr": actual["stderr"]}
        if verdict == "AC":
            verdict, checked = self.p.checker(content, actual["output"], answer)
        if verdict == "JUDGE_ERROR":
            raise SandboxError("checker 또는 격리 실행 환경의 오류입니다.\n" + checked["stderr"])
        return verdict, actual, checked

    def record(self, candidate, verdict, wrong, checked, content, correct, example, iteration, seed, command, index, pattern, draws, pattern_stats):
        digest = hashlib.sha256(content).hexdigest()
        summary = self.summaries[candidate]
        summary["tested"] += 1
        summary["verdicts"][verdict] = summary["verdicts"].get(verdict, 0) + 1
        if verdict not in set(self.options.get("counterexample_verdicts") or DEFAULT_VERDICTS):
            return example
        self.reserve(len(wrong["output"]) + (len(content) + len(correct["output"]) if example is None else 0))
        if example is None:
            example = {"id": digest[:32], "iteration": iteration, "seed": seed, "command": command,
                       "pattern_index": index, "pattern_name": pattern["name"], "range_values": draws,
                       "input_bytes": len(content), "input_sha": self.p.blob(content), "answer_sha": self.p.blob(correct["output"]), "findings": []}
            self.p.result["counterexamples"].append(example)
            self.stats["counterexample_count"] += 1
            pattern_stats["counterexample_inputs"] += 1
        finding = {"solution": candidate, "tag": summary["tag"], "verdict": verdict,
                   "actual_sha": self.p.blob(wrong["output"]), "comment": checked["stderr"],
                   "cpu_ms": wrong.get("cpu_ms"), "memory_kb": wrong.get("memory_kb")}
        example["findings"].append(finding)
        summary["counterexamples"] += 1
        pattern_stats["findings"] += 1
        pattern_stats["solution_findings"][candidate] = pattern_stats["solution_findings"].get(candidate, 0) + 1
        if summary["first_counterexample_id"] is None:
            summary["first_counterexample_id"] = example["id"]
        if self.p.result["counterexample"] is None:
            self.p.result["counterexample"] = {**example, **finding}
        self.p.event(f"유효한 반례 발견 · {candidate} · {verdict}")
        self.p.publish(copy.deepcopy(self.p.result))
        return example

    def collect(self):
        if getattr(self.p, "parallelism", 1) > 1:
            from .stress_parallel import collect
            return collect(self)
        seen = set()
        for iteration in range(1, self.options["iterations"] + 1):
            self.check()
            candidates = [name for name in self.candidates if not self.focus or not self.summaries[name]["counterexamples"]]
            if not candidates:
                self.stats["stop_reason"] = "all_candidates_covered"
                return
            index = (iteration - 1) % len(self.patterns)
            pattern, pattern_stats = self.patterns[index], self.pattern_stats[index]
            seed = self.options.get("seed_start", 1) + (iteration - 1) * self.options.get("seed_step", 1)
            draws = []
            command = generator_command(pattern["command"], seed, index, draws)
            self.stats.update(iterations_started=iteration, last_seed=seed, last_command=command, last_pattern_index=index, last_range_values=draws)
            pattern_stats["attempts"] += 1
            self.p.event(f"스트레스 테스트 · {iteration} / {self.options['iterations']} · 시드 {seed} · {pattern['name']} · 비교 풀이 {len(candidates)}개", seed=seed, pattern_index=index)
            content = self.p.generator(command)
            digest = hashlib.sha256(content).hexdigest()
            if digest in seen:
                self.stats["duplicates_skipped"] += 1
                pattern_stats["duplicates_skipped"] += 1
                self.stats["iterations_completed"] = iteration
                continue
            group = self.options.get("group", "")
            valid, validation = self.p.validator(content, self.options["testset"], group) if group else self.p.validator(content, self.options["testset"])
            if not valid:
                raise SandboxError("생성한 반례 입력이 유효하지 않습니다.\n" + validation["stderr"])
            seen.add(digest)
            pattern_stats["valid_inputs"] += 1
            verdict, correct = self.p.run_solution(self.reference, content)
            if verdict != "AC":
                raise SandboxError(f"기준 풀이가 {verdict}입니다.")
            example = None
            compared = False
            for candidate in candidates:
                self.check()
                self.p.event(f"비교 풀이 실행 · {candidate} / 반복 {iteration}")
                verdict, wrong, checked = self.compare(candidate, content, correct["output"])
                if not compared:
                    pattern_stats["compared_inputs"] += 1
                    compared = True
                example = self.record(candidate, verdict, wrong, checked, content, correct, example, iteration, seed, command, index, pattern, draws, pattern_stats)
            self.stats["iterations_completed"] = iteration
            if self.focus and all(row["counterexamples"] for row in self.summaries.values()):
                self.stats["stop_reason"] = "all_candidates_covered"
                return
            if example and self.mode == "first":
                self.stats["stop_reason"] = "first_counterexample"
                return
            if self.mode != "all_candidates" and self.stats["counterexample_count"] >= self.options.get("counterexample_limit", 1):
                self.stats["stop_reason"] = "counterexample_target"
                return
            if self.stats["counterexample_count"] >= MAX_STRESS_COUNTEREXAMPLES:
                self.stats["stop_reason"] = "collection_limit"
                return
        self.stats["stop_reason"] = "iterations"

    def finish_analysis(self):
        examples = self.p.result["counterexamples"]
        self.p.result["coverage"] = minimum_cover(examples)
        self.p.result["uncovered_candidates"] = [name for name, row in self.summaries.items() if not row["counterexamples"]]
        categories = {}
        for example in examples:
            for finding in example["findings"]:
                key = finding["solution"], finding["verdict"]
                category = categories.setdefault(key, {"solution": finding["solution"], "tag": finding.get("tag", ""), "verdict": finding["verdict"], "example_ids": []})
                category["example_ids"].append(example["id"])
        self.p.result["counterexample_categories"] = list(categories.values())

    def run(self):
        self.started = time.monotonic()
        duration = self.options.get("duration_seconds")
        self.deadline = self.started + duration if duration else None
        self.outer_check = self.p.sandbox.check_cancelled
        self.p.sandbox.check_cancelled = self.check
        try:
            self.collect()
            self.stats["search_stop_reason"] = self.stats["stop_reason"]
            if self.options.get("minimize"):
                from .storage import read_blob
                self.stats["phase"] = "minimize"
                for example in self.p.result["counterexamples"]:
                    self.check()
                    self.p.event(f"반례 축소 시작 · 시드 {example['seed']}")
                    CounterexampleMinimizer(self.p, example, read_blob(example["input_sha"]), self.reference,
                                           MAX_STRESS_EVIDENCE_BYTES - self.stats["evidence_bytes"]).reduce()
                    self.stats["evidence_bytes"] += example.get("minimization", {}).get("evidence_bytes", 0)
        except StressDurationReached:
            self.stats["stop_reason"] = "duration"
        except EvidenceLimitReached:
            self.stats["stop_reason"] = "evidence_limit"
        finally:
            self.p.sandbox.check_cancelled = self.outer_check
            self.stats["elapsed_seconds"] = round(time.monotonic() - self.started, 2)
            self.stats["phase"] = "finished"
            self.p.result["iterations"] = self.stats["iterations_completed"]
            self.finish_analysis()
        reasons = {"duration": "실행 시간 한도", "iterations": "반복 횟수 한도", "first_counterexample": "첫 반례 발견",
                   "counterexample_target": "목표 반례 수", "all_candidates_covered": "모든 비교 풀이에서 반례 발견",
                   "evidence_limit": "수집 자료 한도", "collection_limit": "반례 보관 한도"}
        self.p.event(f"스트레스 테스트 완료 · {reasons.get(self.stats['stop_reason'], '실행 종료')} · {self.stats['iterations_completed']}회 · 유효한 반례 {self.stats['counterexample_count']}개")
