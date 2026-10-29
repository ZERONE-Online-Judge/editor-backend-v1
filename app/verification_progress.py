"""Publish confirmed preparation steps and testcase outcomes."""
import copy
import math
import threading
import time

from .domain import case_order, expanded_tests
from .expectations import expected_tag
from .invocation_scope import selected_testsets


def counter(total):
    return {"current": 0, "total": total, "percent": 0}


def update_counter(value, current):
    value.update(current=current, percent=min(100, math.floor(100 * current / value["total"])) if value["total"] else 0)


class VerificationProgress:
    def __init__(self, pipeline, selected):
        self.pipeline = pipeline
        self.lock = getattr(pipeline, "result_lock", threading.RLock())
        self.last_publish = 0
        self.total = {}
        sets = selected_testsets(pipeline.doc, pipeline.job.options, pipeline.job.kind)
        indices = pipeline.job.options.get("test_indices", []) if pipeline.job.kind != "package" else []
        self.planned = {name: [test for test in expanded_tests(pipeline.doc["testsets"][name]) if not indices or test["index"] in indices] for name in sets}
        self.preparation_plan = {}
        self.preparation_done = set()
        for set_name, tests in self.planned.items():
            for test in tests:
                stages = {"input", "validator", "answer", "checker", "ready"}
                if test.get("sample") and test.get("verify_statement_io", True) and (test.get("statement_input") is not None or test.get("statement_output") is not None):
                    stages.add("sample")
                self.preparation_plan[set_name, test["index"]] = stages
        rows = []
        set_totals = dict.fromkeys(sets, 0)
        for name in selected:
            source = pipeline.doc["solutions"][name]
            counts = {set_name: sum(expected_tag(source, set_name, test.get("group", ""), pipeline.doc["testsets"])[0] != "NR" for test in tests)
                      for set_name, tests in self.planned.items()}
            total = sum(counts.values())
            for set_name, count in counts.items():
                set_totals[set_name] += count
            self.total[name] = total
            rows.append({"name": name, "tag": source["tag"], "matched": False, "skipped": source["tag"] == "NR",
                         "cases": [], "judge_runs": [], "state": "queued", "completed_tests": 0, "total_tests": total})
        pipeline.result["solutions"] = rows
        self.rows = {row["name"]: row for row in rows}
        preparation_totals = {name: sum(len(stages) for (set_name, _), stages in self.preparation_plan.items() if set_name == name) for name in sets}
        preparation = counter(sum(preparation_totals.values()))
        preparation["stages"] = {stage: counter(sum(stage in stages for stages in self.preparation_plan.values()))
                                 for stage in ("input", "validator", "answer", "checker", "sample", "ready")}
        pipeline.result["verification_progress"] = {
            **counter(preparation["total"] + sum(self.total.values())), "phase": "preparing",
            "preparation": preparation, "judging": counter(sum(self.total.values())),
            "testsets": {name: {**counter(preparation_totals[name] + total),
                                "preparation": counter(preparation_totals[name]), "judging": counter(total)}
                         for name, total in set_totals.items()}}
        self.publish(True)

    def publish(self, force=False):
        with self.lock:
            progress = self.pipeline.result["verification_progress"]
            counts = dict.fromkeys(progress["testsets"], 0)
            current = 0
            for row in self.rows.values():
                confirmed = {(case["testset"], case["index"]) for case in row["cases"] if case["index"] > 0}
                current += len(confirmed)
                for name, _ in confirmed:
                    counts[name] += 1
            update_counter(progress["judging"], current)
            update_counter(progress, progress["preparation"]["current"] + current)
            if not progress["total"] and progress["phase"] == "complete":
                progress["percent"] = 100
            progress.update(completed_solutions=sum(row["state"] == "completed" for row in self.rows.values()), total_solutions=len(self.rows))
            for name, scoped in progress["testsets"].items():
                update_counter(scoped["judging"], counts[name])
                update_counter(scoped, scoped["preparation"]["current"] + counts[name])
            self.pipeline.result["progress"] = progress["percent"]
            if force or time.monotonic() - self.last_publish >= 1:
                self.pipeline.publish(copy.deepcopy(self.pipeline.result))
                self.last_publish = time.monotonic()

    def preparation_step(self, set_name, index, stage):
        """Count successful, selected work once, including stored/cache results."""
        with self.lock:
            key = (set_name, index, stage)
            if stage not in self.preparation_plan.get((set_name, index), ()) or key in self.preparation_done:
                return
            self.preparation_done.add(key)
            progress = self.pipeline.result["verification_progress"]
            preparation = progress["preparation"]
            for value in (preparation, preparation["stages"][stage], progress["testsets"][set_name]["preparation"]):
                update_counter(value, value["current"] + 1)
            stage_count = preparation["stages"][stage]
            self.publish(preparation["current"] == preparation["total"] or stage_count["current"] in {1, stage_count["total"]})

    def begin(self):
        with self.lock:
            self.pipeline.result["verification_progress"]["phase"] = "judging"
            self.publish(True)

    def started(self, name):
        with self.lock:
            if self.rows[name]["state"] != "completed":
                self.rows[name]["state"] = "running"
            self.publish()

    def case(self, name, row):
        with self.lock:
            target = self.rows[name]
            target["state"] = "running"
            target["cases"].append(row)
            target["cases"].sort(key=case_order(self.pipeline.doc))
            target["completed_tests"] = sum(case["index"] > 0 for case in target["cases"])
            complete = target["completed_tests"] == target["total_tests"]
            self.publish(target["completed_tests"] == 1 or complete)
            return complete

    def batch(self, name, set_name, cases, record=None):
        with self.lock:
            target = self.rows[name]
            target["state"] = "running"
            merged = {(case["testset"], case["index"]): case for case in target["cases"]}
            merged.update({(case["testset"], case["index"]): case for case in cases})
            target["cases"] = sorted(merged.values(), key=case_order(self.pipeline.doc))
            target["completed_tests"] = sum(case["index"] > 0 for case in target["cases"])
            if record:
                target["judge_runs"] = [value for value in target["judge_runs"] if value["id"] != record["id"]] + [record]
            self.publish(True)

    def finish_solution(self, result):
        with self.lock:
            target = self.rows[result["name"]]
            target.update(result, state="completed", completed_tests=sum(case["index"] > 0 for case in result["cases"]))
            self.publish(True)

    def finish(self):
        with self.lock:
            self.pipeline.result["verification_progress"]["phase"] = "complete"
            self.publish(True)
