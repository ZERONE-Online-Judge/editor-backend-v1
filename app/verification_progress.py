"""Publish confirmed testcase outcomes without waiting for other solutions."""
import copy
import math
import threading
import time

from .domain import expanded_tests
from .expectations import expected_tag
from .invocation_scope import selected_testsets


class VerificationProgress:
    def __init__(self, pipeline, selected):
        self.pipeline = pipeline
        self.lock = getattr(pipeline, "result_lock", threading.RLock())
        self.last_publish = 0
        self.total = {}
        sets = selected_testsets(pipeline.doc, pipeline.job.options, pipeline.job.kind)
        indices = pipeline.job.options.get("test_indices", []) if pipeline.job.kind != "package" else []
        self.planned = {name: [test for test in expanded_tests(pipeline.doc["testsets"][name]) if not indices or test["index"] in indices] for name in sets}
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
        pipeline.result["verification_progress"] = {"phase": "preparing", "current": 0, "total": sum(self.total.values()), "percent": 0,
            "testsets": {name: {"current": 0, "total": total, "percent": 0} for name, total in set_totals.items()}}
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
            progress.update(current=current, completed_solutions=sum(row["state"] == "completed" for row in self.rows.values()), total_solutions=len(self.rows))
            progress["percent"] = min(100, math.floor(100 * current / progress["total"])) if progress["total"] else (100 if progress["phase"] == "complete" else 0)
            for name, scoped in progress["testsets"].items():
                count = counts[name]
                scoped.update(current=count, percent=min(100, math.floor(100 * count / scoped["total"])) if scoped["total"] else 0)
            self.pipeline.result["progress"] = progress["percent"]
            if force or time.monotonic() - self.last_publish >= 1:
                self.pipeline.publish(copy.deepcopy(self.pipeline.result))
                self.last_publish = time.monotonic()

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
            target["cases"].sort(key=lambda case: (case["testset"], case["index"]))
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
            target["cases"] = sorted(merged.values(), key=lambda case: (case["testset"], case["index"]))
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
