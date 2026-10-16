"""Evaluate a bounded window of seeds; commit evidence in seed order."""
import hashlib
from concurrent.futures import Future, TimeoutError
from threading import Lock
from .execution_limits import MAX_STRESS_COUNTEREXAMPLES, MAX_STRESS_EVIDENCE_BYTES
from .parallel import ordered_work
from .sandbox import SandboxError
from .stress import generator_command


def collect(search):
    p, stats, options = search.p, search.stats, search.options
    seen, shared, lock = set(), {}, Lock()
    stats["iterations_scheduled"] = 0
    stats["parallelism"] = p.parallelism

    def packets():
        for iteration in range(1, options["iterations"] + 1):
            candidates = [name for name in search.candidates if not search.focus or not search.summaries[name]["counterexamples"]]
            if not candidates:
                return
            index = (iteration - 1) % len(search.patterns)
            pattern = search.patterns[index]
            seed = options.get("seed_start", 1) + (iteration - 1) * options.get("seed_step", 1)
            draws = []
            command = generator_command(pattern["command"], seed, index, draws)
            with p.result_lock:
                stats["iterations_scheduled"] += 1
                stats["last_scheduled_seed"] = seed
            yield {"iteration": iteration, "index": index, "pattern": pattern, "seed": seed,
                   "command": command, "draws": draws, "candidates": candidates}

    def evaluate(packet):
        result = {**packet, "comparisons": [], "valid": False}
        promise, owner = None, False
        try:
            search.check()
            content = p.generator(packet["command"])
            digest = hashlib.sha256(content).hexdigest()
            result.update(content=content, digest=digest)
            # Equal inputs share the same pending reference/comparisons. The
            # ordered consumer assigns provenance to the first seed, regardless
            # of which thread generated those bytes first.
            with lock:
                if digest in seen:
                    return result
                promise = shared.get(digest)
                if promise is None:
                    promise = shared[digest] = Future()
                    owner = True
            if not owner:
                while True:
                    search.check()
                    try:
                        return {**packet, **promise.result(timeout=.1), "digest": digest}
                    except TimeoutError:
                        if promise.done():
                            return {**packet, **promise.result(), "digest": digest}
            group = options.get("group", "")
            valid, validation = p.validator(content, options["testset"], group) if group else p.validator(content, options["testset"])
            if not valid:
                raise SandboxError("생성한 반례 입력이 유효하지 않습니다.\n" + validation["stderr"])
            result["valid"] = True
            verdict, correct = p.run_solution(search.reference, content)
            result.update(correct=correct, reference_verdict=verdict)
            if verdict != "AC":
                raise SandboxError(f"기준 풀이가 {verdict}입니다.")
            outputs = {digest: content, hashlib.sha256(correct["output"]).hexdigest(): correct["output"]}
            buffered = sum(len(value) for value in outputs.values())
            for candidate in packet["candidates"]:
                search.check()
                if search.focus and search.summaries[candidate]["counterexamples"]:
                    continue
                p.event(f"비교 풀이 실행 · {candidate} / 반복 {packet['iteration']}")
                verdict, actual, checked = search.compare(candidate, content, correct["output"])
                output = actual["output"]
                output_sha = hashlib.sha256(output).hexdigest()
                if output_sha not in outputs:
                    if buffered + len(output) > MAX_STRESS_EVIDENCE_BYTES:
                        from .stress_search import EvidenceLimitReached
                        raise EvidenceLimitReached()
                    outputs[output_sha] = output
                    buffered += len(output)
                output = outputs[output_sha]
                result["comparisons"].append((candidate, verdict,
                    {"output": output, "cpu_ms": actual.get("cpu_ms"), "wall_ms": actual.get("wall_ms"), "memory_kb": actual.get("memory_kb")},
                    {"stderr": checked["stderr"]}))
                if verdict == "JUDGE_ERROR":
                    raise SandboxError("checker 또는 격리 실행 환경의 오류입니다.\n" + checked["stderr"])
        except Exception as error:
            result["error"] = error
        finally:
            if owner:
                # Only byte/result data are shared; seed provenance is never
                # copied from another iteration.
                promise.set_result({k:v for k,v in result.items() if k not in packet})
        return result

    with ordered_work(evaluate, packets(), p.parallelism, search.check) as results:
        for value in results:
            iteration, index, seed, command = (value[k] for k in ("iteration", "index", "seed", "command"))
            pattern, draws = value["pattern"], value["draws"]
            pattern_stats = search.pattern_stats[index]
            with p.result_lock:
                stats.update(iterations_started=iteration, last_seed=seed, last_command=command,
                             last_pattern_index=index, last_range_values=draws)
                pattern_stats["attempts"] += 1
            p.event(f"스트레스 테스트 · {iteration} / {options['iterations']} · 시드 {seed} · {pattern['name']} · 비교 풀이 {len(value['candidates'])}개", seed=seed, pattern_index=index)
            digest = value.get("digest")
            with lock:
                duplicate = digest in seen if digest else False
                if digest:
                    seen.add(digest)
                    shared.pop(digest, None)
            if duplicate:
                with p.result_lock:
                    stats["duplicates_skipped"] += 1
                    pattern_stats["duplicates_skipped"] += 1
                    stats["iterations_completed"] = iteration
                continue
            example, compared = None, False
            with p.result_lock:
                if value["valid"]:
                    pattern_stats["valid_inputs"] += 1
                if "correct" in value:
                    correct = value["correct"]
                    search.results.case(search.reference, value["reference_verdict"], correct,
                        correct.get("stderr", ""), value["content"],
                        correct["output"] if value["reference_verdict"] == "AC" else None, value)
                for candidate, verdict, wrong, checked in value["comparisons"]:
                    search.results.case(candidate, verdict, wrong, checked["stderr"],
                                        value["content"], value["correct"]["output"], value)
                    if verdict == "JUDGE_ERROR":
                        continue
                    if search.focus and search.summaries[candidate]["counterexamples"]:
                        continue
                    if not compared:
                        pattern_stats["compared_inputs"] += 1
                        compared = True
                    example = search.record(candidate, verdict, wrong, checked, value["content"], value["correct"], example,
                        iteration, seed, command, index, pattern, draws, pattern_stats)
                if value.get("error"):
                    raise value["error"]
                stats["iterations_completed"] = iteration
            if search.focus and all(row["counterexamples"] for row in search.summaries.values()):
                stats["stop_reason"] = "all_candidates_covered"
                return
            if example and search.mode == "first":
                stats["stop_reason"] = "first_counterexample"
                return
            if search.mode != "all_candidates" and stats["counterexample_count"] >= options.get("counterexample_limit", 1):
                stats["stop_reason"] = "counterexample_target"
                return
            if stats["counterexample_count"] >= MAX_STRESS_COUNTEREXAMPLES:
                stats["stop_reason"] = "collection_limit"
                return
    stats["stop_reason"] = "all_candidates_covered" if search.focus and all(row["counterexamples"] for row in search.summaries.values()) else "iterations"
