"""Persist executed stress cases, keeping bulk I/O out of the job JSON."""
import hashlib


class StressResults:
    def __init__(self, search):
        self.search = search
        self.p = search.p
        self.saved = set()
        self.rows = {
            name: {"name": name, "tag": self.p.doc["solutions"][name]["tag"],
                   "role": "reference" if name == search.reference else "candidate",
                   "cases": [], "max_cpu_ms": None, "max_memory_kb": None}
            for name in [search.reference, *search.candidates]
        }
        self.summary = {"executions": 0, "testcase_count": 0,
                        "max_cpu_ms": None, "max_memory_kb": None}
        self.p.result.update(stress_results_version=1, stress_results=list(self.rows.values()),
                             stress_result_summary=self.summary)

    def blobs(self, *contents):
        hashed = [(hashlib.sha256(value).hexdigest(), value) if value is not None else ("", None) for value in contents]
        pending = {sha: value for sha, value in hashed if value is not None}
        pending = {sha: value for sha, value in pending.items() if sha not in self.saved}
        # The same input, reference output and matching candidate output are
        # counted and stored only once, including counterexample references.
        self.search.reserve(sum(len(value) for value in pending.values()))
        for sha, value in pending.items():
            self.p.blob(value)
            self.saved.add(sha)
        return [sha for sha, value in hashed]

    def case(self, name, verdict, actual, comment, content, answer, packet):
        with self.p.result_lock:
            inp, ans, output = self.blobs(content, answer, actual["output"])
            row = {"testset": self.search.options["testset"], "index": packet["iteration"],
                   "iteration": packet["iteration"], "seed": packet["seed"], "command": packet["command"],
                   "pattern_name": packet["pattern"]["name"], "range_values": packet["draws"],
                   "verdict": verdict, "cpu_ms": actual.get("cpu_ms"), "wall_ms": actual.get("wall_ms"),
                   "memory_kb": actual.get("memory_kb"), "comment": comment[:2048],
                   "input_sha": inp, "answer_sha": ans, "actual_sha": output}
            solution = self.rows[name]
            solution["cases"].append(row)
            self.summary["executions"] += 1
            if name == self.search.reference:
                self.summary["testcase_count"] += 1
            for field, maximum in (("cpu_ms", "max_cpu_ms"), ("memory_kb", "max_memory_kb")):
                value = row[field]
                if value is not None:
                    for target in (solution, self.summary):
                        target[maximum] = value if target[maximum] is None else max(target[maximum], value)
            return row
