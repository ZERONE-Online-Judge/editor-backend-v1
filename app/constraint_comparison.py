"""Bounded, explainable comparison of declared integer ranges."""
import hashlib
import re
from collections import Counter, defaultdict

from .constraint_bounds import parse_statement, parse_validator
from .storage import blob_path

MAX_ROWS = 200
MAX_EVIDENCE = 8
MAX_EXPRESSION = 512
REASONS = {
    'analysis_truncated': '분석 한도를 넘는 내용에 추가 범위나 충돌이 있을 수 있어 비교를 확정하지 않았습니다.',
    'conditional_statement': '조건·부분 문제·예시로 제시된 범위이므로 전체 입력 제한으로 단정하지 않았습니다.',
    'conditional_validator': '조건에 따라 실행되는 입력 검사이므로 공통 범위로 단정하지 않았습니다.',
    'symbolic_statement_bound': '지문 범위에 다른 변수 또는 지원하지 않는 수식이 있어 정수 끝값을 확정하지 못했습니다.',
    'symbolic_validator_bound': 'Validator 범위에 실행 중 값 또는 안전하게 해석할 수 없는 상수가 있습니다.',
    'unsupported_comparison': '지원하는 단일 상한·하한 연쇄 부등식이 아닙니다.',
    'multiple_comparisons': '여러 비교식이 연결되어 변수별 끝값을 확정하지 못했습니다.',
    'ambiguous_list_prefix': '목록 기호와 음수 부호를 구분할 수 없어 범위를 확정하지 않았습니다.',
    'invalid_range': '하한이 상한보다 커서 유효한 정수 범위로 비교하지 않았습니다.',
    'unsupported_call': '지원하는 testlib 정수 입력 호출 형식이 아닙니다.',
    'missing_name': '입력 호출의 변수명이 없고 단순 대입문에서도 이름을 확인할 수 없습니다.',
}
LIMITATIONS = [
    '지문 legend·input·notes의 명시적 정수 연쇄 범위만 정적으로 비교합니다. 출력·풀이·예시 코드는 비교하지 않습니다.',
    'C++ testlib inf.readInt/readInteger/readLong 및 readInts/readLongs의 명시적 끝값을 해석합니다. 실행이나 AI 추론을 수행하지 않습니다.',
    '다른 변수에 의존하는 식, 조건부 제한, 충돌하는 번역·선언, 불확실한 이름은 불일치로 단정하지 않습니다.',
    '대소문자는 양쪽 이름이 유일할 때만 대응하며, 첨자 이름과 기본 이름은 배열 입력 호출이 확인될 때만 대응합니다.',
]


def indexed_base(name):
    match = re.fullmatch(r'([A-Za-z][A-Za-z0-9]*)_(?:[ijk]|\d+)', name)
    return match[1].casefold() if match else None


def compare_constraints(doc):
    statements, notices = parse_statement(doc)
    validator = doc['files'].get(doc.get('validator'))
    info = {'name': validator['name'], 'language': validator['language']} if validator else None
    source, calls = '', []
    available = False
    if not validator:
        notices.append({'code': 'validator_missing', 'message': '선택한 Validator 소스가 없어 지문 범위와 비교하지 못했습니다.'})
    elif validator['language'] not in {'cpp17', 'cpp20'}:
        notices.append({'code': 'validator_language_unsupported', 'message': f"{validator['language']} Validator는 정적 정수 범위 비교를 지원하지 않습니다."})
    else:
        try:
            with blob_path(validator['sha256']).open('rb') as stream:
                raw = stream.read(262145)
            if len(raw) > 262144:
                notices.append({'code': 'validator_too_large', 'message': 'Validator가 256KiB를 초과하여 정적 범위 분석을 생략했습니다.'})
            else:
                source, calls, extra = parse_validator(raw.decode('utf-8'))
                notices.extend(extra)
                available = True
        except (OSError, UnicodeError):
            notices.append({'code': 'validator_unreadable', 'message': 'Validator 소스를 읽거나 UTF-8로 해석할 수 없어 범위를 비교하지 못했습니다.'})
    if not statements:
        notices.append({'code': 'no_statement_ranges', 'message': '지원하는 형태의 명시적 지문 정수 범위를 찾지 못했습니다.'})
    if available and not calls:
        notices.append({'code': 'no_validator_ranges', 'message': '지원하는 testlib 정수 입력 호출을 찾지 못했습니다.'})
    left, right = defaultdict(list), defaultdict(list)
    for entry in statements: left[entry.name].append(entry)
    for i, entry in enumerate(calls): right[entry.name or f'이름 없는 입력 {i + 1}'].append(entry)
    left_case, right_case = defaultdict(list), defaultdict(list)
    for name in left: left_case[name.casefold()].append(name)
    for name in right: right_case[name.casefold()].append(name)
    indexed_names = Counter(indexed_base(name) for name in left if indexed_base(name))
    rows, used = [], set()
    trimmed = False

    def evidence(entries):
        nonlocal trimmed
        trimmed |= len(entries) > MAX_EVIDENCE or any(len(entry.expression) > MAX_EXPRESSION for entry in entries)
        return [{'name': entry.name, 'low': str(entry.low) if entry.low is not None else None,
                 'high': str(entry.high) if entry.high is not None else None,
                 'expression': entry.expression[:MAX_EXPRESSION], 'location': entry.location}
                for entry in entries[:MAX_EVIDENCE]]

    def append(name, declared, checked, matching=None, ambiguity=None):
        bad = [entry.reason_code for entry in declared + checked if entry.reason_code]
        declared_ranges = {(entry.low, entry.high) for entry in declared if not entry.reason_code}
        checked_ranges = {(entry.low, entry.high) for entry in checked if not entry.reason_code}
        if ambiguity:
            status, code, reason = 'ambiguous', 'ambiguous_name', '대소문자·첨자 이름에 여러 대응 후보가 있어 변수 이름을 확정하지 않았습니다.'
        elif bad:
            status, code = 'unresolved', bad[0]
            reason = REASONS.get(code, '안전하게 해석할 수 없는 범위가 있어 비교를 보류했습니다.')
        elif len(declared_ranges) > 1 or len(checked_ranges) > 1:
            status, code = 'ambiguous', 'conflicting_ranges'
            reason = '동일 변수의 지문·번역 또는 Validator에 서로 다른 범위가 있어 어느 범위도 정답으로 단정하지 않았습니다.'
        elif not checked:
            status, code = 'statement_only', 'validator_name_missing' if available else 'validator_unavailable'
            reason = '지문 범위에 대응하는 이름의 Validator 입력 검사를 찾지 못했습니다.' if available else 'Validator를 분석할 수 없어 이 지문 범위를 비교하지 못했습니다.'
        elif not declared:
            status, code, reason = 'validator_only', 'statement_name_missing', 'Validator 입력 검사에 대응하는 이름의 명시적 지문 범위를 찾지 못했습니다.'
        else:
            equal = declared_ranges == checked_ranges
            status, code = ('match', 'same_integer_range') if equal else ('difference', 'different_integer_range')
            reason = '명시된 정수 하한·상한이 일치합니다.' if equal else '명시된 정수 하한·상한이 서로 다릅니다.'
            if matching == 'case': reason += ' 양쪽에서 유일한 대소문자 차이 이름을 대응했습니다.'
            elif matching == 'indexed': reason += ' 배열 입력 호출을 근거로 첨자 변수와 배열 이름을 대응했습니다.'
        if any(entry.naming == 'assignment' for entry in checked):
            reason += ' Validator의 단순 대입문에서 변수 이름을 확인했습니다.'
        identifier = hashlib.sha256((name + '\0' + status + '\0' + '\0'.join(e.location + ':' + e.expression[:MAX_EXPRESSION] for e in declared + checked)).encode()).hexdigest()[:20]
        rows.append({'id': identifier, 'name': name, 'status': status, 'reason_code': code, 'reason': reason,
                     'matching': matching, 'statement': evidence(declared), 'validator': evidence(checked)})

    for name, entries in left.items():
        if name in right:
            append(name, entries, right[name], 'exact')
            used.add(name)
            continue
        candidates = right_case.get(name.casefold(), [])
        if candidates:
            ambiguous = len(candidates) != 1 or len(left_case[name.casefold()]) != 1 or candidates[0] in used
            append(name, entries, [entry for candidate in candidates for entry in right[candidate]], 'case' if not ambiguous else None, ambiguous)
            used.update(candidates)
            continue
        base = indexed_base(name)
        candidates = [candidate for candidate in right_case.get(base, []) if any(entry.vector for entry in right[candidate])] if base else []
        if candidates:
            ambiguous = len(candidates) != 1 or indexed_names[base] != 1 or bool(left_case.get(base)) or candidates[0] in used
            append(name, entries, [entry for candidate in candidates for entry in right[candidate]], 'indexed' if not ambiguous else None, ambiguous)
            used.update(candidates)
        else:
            append(name, entries, [])
    for name, entries in right.items():
        if name not in used: append(name, [], entries)
    priority = {'difference': 0, 'ambiguous': 1, 'unresolved': 1, 'statement_only': 2, 'validator_only': 2, 'match': 3}
    rows.sort(key=lambda row: priority[row['status']])
    counts = Counter(row['status'] for row in rows)
    if len(rows) > MAX_ROWS:
        notices.append({'code': 'rows_truncated', 'message': f'총 {len(rows)}개 변수 중 앞의 {MAX_ROWS}개만 표시합니다. 집계에는 전체 분석 결과를 포함합니다.'})
    if trimmed:
        notices.append({'code': 'evidence_truncated', 'message': '변수별 근거는 각 출처 8개, 수식은 512자까지 표시합니다. 판정은 자르기 전 전체 수식으로 수행했습니다.'})
    unique_notices = list({notice['code']: notice for notice in notices}.values())
    comparison = {'version': 1, 'validator': info,
                  'counts': {'compared': counts['match'] + counts['difference'], 'matched': counts['match'],
                             'different': counts['difference'], 'unresolved': counts['unresolved'] + counts['ambiguous'],
                             'statement_only': counts['statement_only'], 'validator_only': counts['validator_only']},
                  'rows': rows[:MAX_ROWS], 'notices': unique_notices[:20], 'limitations': LIMITATIONS}
    return comparison, source, [entry for entry in calls if not entry.reason_code], {entry.name for entry in calls if entry.reason_code}, rows
