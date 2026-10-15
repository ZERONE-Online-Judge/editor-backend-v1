"""Publish confirmed testcase outcomes without waiting for other solutions."""
import copy
import math
import threading
import time

from .domain import expanded_tests
from .expectations import expected_tag


class VerificationProgress:
    def __init__(self, pipeline, selected):
        self.pipeline = pipeline
        self.lock = getattr(pipeline, "result_lock", threading.RLock())
        self.last_publish = 0
        self.total = {}
        sets = list(pipeline.doc["testsets"]) if pipeline.job.kind == "package" else [pipeline.job.options["testset"]]
        indices = pipeline.job.options.get("test_indices", []) if pipeline.job.kind != "package" else []
        self.planned = {name: [test for test in expanded_tests(pipeline.doc["testsets"][name]) if not indices or test["index"] in indices] for name in sets}
        rows = []
        for name in selected:
            source = pipeline.doc["solutions"][name]
            total = sum(expected_tag(source, set_name, test.get("group", ""), pipeline.doc["testsets"])[0] != "NR" for set_name, tests in self.planned.items() for test in tests)
            self.total[name] = total
            rows.append({"name": name, "tag": source["tag"], "matched": False, "skipped": source["tag"] == "NR",
                         "cases": [], "judge_runs": [], "state": "queued", "completed_tests": 0, "total_tests": total})
        pipeline.result["solutions"] = rows
        self.rows = {row["name"]: row for row in rows}
        pipeline.result["verification_progress"] = {"phase": "preparing", "current": 0, "total": sum(self.total.values()), "percent": 0}
        self.publish(True)

    def publish(self, force=False):
        with self.lock:
            progress = self.pipeline.result["verification_progress"]
            current = sum(len({(case["testset"], case["index"]) for case in row["cases"] if case["index"] > 0}) for row in self.rows.values())
            progress.update(current=current, completed_solutions=sum(row["state"] == "completed" for row in self.rows.values()), total_solutions=len(self.rows))
            progress["percent"] = min(100, math.floor(100 * current / progress["total"])) if progress["total"] else (100 if progress["phase"] == "complete" else 0)
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
            target["cases"] = sorted([case for case in target["cases"] if case["testset"] != set_name] + cases, key=lambda case: (case["testset"], case["index"]))
            target["completed_tests"] = sum(case["index"] > 0 for case in target["cases"])
            if record:
                target["judge_runs"] = [value for value in target["judge_runs"] if value["testset"] != set_name] + [record]
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
