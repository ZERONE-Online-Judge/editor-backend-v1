"""Keep testset identities consistent through preparation and judging."""


def selected_testsets(document, options, kind):
    if kind == "package" or options.get("all_testsets", False):
        return list(document["testsets"])
    return [options.get("testset", "tests")]
