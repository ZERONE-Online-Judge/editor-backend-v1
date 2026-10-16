"""Keep testset identities consistent through preparation and judging."""


def selected_testsets(document, options, kind):
    if options.get("testsets") is not None:
        requested = set(options["testsets"])
        return [name for name in document["testsets"] if name in requested]
    if kind == "package" or options.get("all_testsets", False):
        return list(document["testsets"])
    return [options.get("testset", "tests")]
