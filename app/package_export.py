"""Archive committed files without compiling, judging or changing answers."""
import copy

from .domain import expanded_tests, fail, ordered_tests
from .invocation_scope import selected_testsets


def stored_testsets(document, options):
    complete_required = options.get("export_format") in {"zoj", "linux"}
    result, missing = {}, []
    for name in selected_testsets(document, options, "package"):
        config = document["testsets"][name]
        saved = {(t["index"], t.get("command", "")): t for t in config["tests"].values()}
        definitions = expanded_tests(config) if complete_required else ordered_tests(config)
        result[name] = []
        for definition in definitions:
            test = copy.deepcopy(definition)
            original = saved.get((test["index"], test.get("command", "")), {})
            for field in ("input_sha", "answer_sha"):
                test[field] = test.get(field) or original.get(field)
            if not test["input_sha"] or not test["answer_sha"]:
                missing.append(f"{name} #{test['index']}")
            else:
                result[name].append(test)
    if complete_required and (missing or not any(result.values())):
        fail("package_tests_not_prepared",
             "저장된 입력·정답이 필요합니다. 테스트에서 입력·정답을 생성·반영하고 커밋한 뒤 내보내세요. "
             + (", ".join(missing[:20]) if missing else "선택한 세트에 테스트가 없습니다."),
             409, tests=missing[:100])
    return result


def export_package(job, publish, check_cancelled):
    from .packages import build_package
    check_cancelled()
    testsets = stored_testsets(job.document, job.options)
    result = {"summary": "저장된 자료로 패키지를 생성했습니다. 검증은 실행하지 않았습니다.",
              "progress": 100, "verified": False, "verification_skipped": True,
              "runtime": {"backend": "archive", "execution": "none"},
              "testsets": testsets, "processed_testsets": list(testsets),
              "solutions": [], "unit_tests": [], "events": []}
    result["package"] = build_package(job, testsets, result)
    check_cancelled()
    publish(result)
    return result
