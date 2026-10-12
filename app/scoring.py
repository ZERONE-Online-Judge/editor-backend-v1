def score_results(document, prepared, rows):
    """Report points only for complete testsets; preserve raw case verdicts."""
    if not document["info"]["points_enabled"]:
        return {"total_score": None, "groups": []}
    groups = []
    complete = True
    total = 0
    for set_name in {row["testset"] for row in rows}:
        tests = prepared[set_name]
        scoped = [row for row in rows if row["testset"] == set_name]
        config = document["testsets"][set_name]
        if len(scoped) != len(tests):
            complete = False
        if not config["groups_enabled"]:
            total += sum(row["score"] for row in scoped)
            continue
        states = {}
        def evaluate(name):
            if name in states:
                return states[name]
            rules = config["groups"][name]
            members = [row for row in scoped if row["group"] == name]
            expected = [test for test in tests if test["group"] == name]
            executed = bool(expected) and len(members) == len(expected)
            passed = executed and all(row["verdict"] == "AC" for row in members)
            dependencies = all(evaluate(dep)["passed"] for dep in rules["dependencies"])
            score = sum(row["score"] for row in members)
            if not executed or not dependencies or (rules["points_policy"] == "COMPLETE_GROUP" and not passed):
                score = 0
            state = {"name": name, "testset": set_name, "score": score,
                     "passed": passed and dependencies, "complete": executed,
                     "blocked": not dependencies, "policy": rules["points_policy"]}
            states[name] = state
            return state
        for name in config["groups"]:
            evaluate(name)
        groups.extend(states.values())
        total += sum(group["score"] for group in states.values())
        total += sum(row["score"] for row in scoped if not row["group"])
    return {"total_score": total if complete else None, "groups": groups,
            "score_scope_complete": complete}
