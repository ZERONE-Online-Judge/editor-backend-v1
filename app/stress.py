"""Reproducible stress-test request metadata and generator commands."""

DEFAULT_VERDICTS = ["WA", "TLE", "MLE", "RE", "PE", "OLE", "IDLENESS_LIMIT", "PARTIAL"]


def generator_command(template, seed):
    return template.replace("{seed}", str(seed)) if "{seed}" in template else template + " " + str(seed)


def stress_configuration(document, options):
    reference = options.get("reference") or next(
        (name for name, source in document["solutions"].items() if source["tag"] == "MA"), ""
    )
    candidates = list(dict.fromkeys(options.get("candidates") or ([options["candidate"]] if options.get("candidate") else [])))
    return {
        "generator": options["generator"],
        "seed_start": 1,
        "seed_step": 1,
        "seed_mode": "placeholder" if "{seed}" in options["generator"] else "append",
        "reference": reference,
        "candidates": [{"name": name, "tag": document["solutions"][name]["tag"]} for name in candidates],
        "testset": options["testset"],
        "iterations": options["iterations"],
        "duration_seconds": options.get("duration_seconds"),
        "stop_on_first": options.get("stop_on_first", True),
        "counterexample_limit": options.get("counterexample_limit", 1),
        "counterexample_verdicts": list(options.get("counterexample_verdicts") or DEFAULT_VERDICTS),
        "time_limit_ms": document["info"]["time_limit_ms"],
        "memory_limit_mb": document["info"]["memory_limit_mb"],
        "validator": document["validator"],
        "extra_validators": list(document["extra_validators"]),
        "checker": document["checker"],
        "interactor": document["interactor"] if document["info"]["interactive"] else "",
    }
