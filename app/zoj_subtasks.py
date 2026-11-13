"""Translate fixed Editor points without changing group scoring semantics."""
import hashlib
import json
from decimal import Decimal, InvalidOperation


def subtask_id(kind, *identity):
    encoded = json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()
    return kind + "-" + hashlib.sha256(encoded).hexdigest()[:32]


def export_subtasks(document, entries):
    """entries are (source testset, prepared test, output display_order)."""
    if not document["info"]["points_enabled"]:
        return []
    if document["info"].get("checker_percent"):
        raise ValueError("Checker 백분율 점수는 ZOJ에서 지원하지 않습니다. 고정 배점의 테스트·그룹 점수를 사용하세요.")
    if not entries:
        raise ValueError("부분 점수 테스트 세트에는 배점과 입력·정답이 있어야 합니다.")
    tests = {}
    points = {}
    for set_name, test, order in entries:
        identity = (set_name, test["index"])
        if identity in tests:
            raise ValueError("부분 점수 테스트 번호가 중복되었습니다.")
        tests[identity] = (test, order)
        try:
            value = Decimal(str(test.get("points", 0)))
        except (ValueError, InvalidOperation):
            raise ValueError("테스트 배점은 유한한 0 이상의 숫자여야 합니다.") from None
        if not value.is_finite() or value < 0:
            raise ValueError("테스트 배점은 유한한 0 이상의 숫자여야 합니다.")
        points[identity] = value
    if sum(points.values()) != 100:
        raise ValueError("ZOJ 부분 점수는 내보내는 테스트 세트의 총 배점이 정확히 100점이어야 합니다.")

    result, covered = [], set()
    closures = {}

    def members(set_name, group):
        return {identity for identity, (test, _) in tests.items()
                if identity[0] == set_name and test.get("group", "") == group}

    def closure(set_name, group, trail=()):
        key = (set_name, group)
        if key in closures:
            return closures[key]
        config = document["testsets"][set_name]
        if group in trail or group not in config["groups"]:
            raise ValueError("테스트 그룹의 선행 그룹과 순환 의존성을 확인하세요.")
        selected = members(set_name, group)
        if not selected:
            raise ValueError(f"{set_name} / {group}: 선행·배점 그룹에는 테스트가 있어야 합니다.")
        for dependency in config["groups"][group]["dependencies"]:
            selected |= closure(set_name, dependency, (*trail, group))
        closures[key] = selected
        return selected

    def add(identifier, name, score, selected):
        if score <= 0:
            return
        if score != score.to_integral_value() or not 1 <= score <= 100:
            raise ValueError("ZOJ 서브태스크 배점은 1~100의 정수여야 합니다. 그룹 합계 또는 테스트별 배점을 조정하세요.")
        covered.update(selected)
        result.append({"subtask_id": identifier, "name": name[:120], "points": int(score),
                       "testcase_orders": sorted(tests[identity][1] for identity in selected)})

    for set_name in dict.fromkeys(identity[0] for identity in tests):
        config = document["testsets"][set_name]
        if config["groups_enabled"]:
            for group, rule in config["groups"].items():
                own = members(set_name, group)
                if rule["points_policy"] == "COMPLETE_GROUP":
                    weight = sum((points[identity] for identity in own), Decimal(0))
                    if weight:
                        add(subtask_id("g", set_name, group), f"{set_name} / {group}", weight,
                            closure(set_name, group))
                else:
                    dependencies = set()
                    for dependency in rule["dependencies"]:
                        dependencies |= closure(set_name, dependency)
                    for identity in sorted(own):
                        add(subtask_id("t", *identity), f"{set_name} / {group} #{identity[1]}",
                            points[identity], {identity} | dependencies)
        for identity, (test, _) in tests.items():
            if identity[0] == set_name and not test.get("group"):
                add(subtask_id("t", *identity), f"{set_name} #{identity[1]}", points[identity], {identity})
    if len(result) > 100:
        raise ValueError("ZOJ 서브태스크는 최대 100개입니다. 테스트별 점수를 그룹 전체 통과 방식으로 묶으세요.")
    if covered != set(tests):
        raise ValueError("배점 없는 테스트가 점수 그룹에 연결되지 않았습니다. 0점 테스트를 관련 배점 그룹에 넣거나 선행 그룹으로 연결하세요. 임의로 점수 조건을 바꾸지는 않습니다.")
    return result
