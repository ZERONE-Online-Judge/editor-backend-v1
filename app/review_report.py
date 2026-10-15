"""Evidence-based review. No paid calls or execution during report generation."""
import copy
import re
from collections import OrderedDict, defaultdict
from threading import Lock
from sqlalchemy import or_, select
from .db import Job, JobApplication, now
from .domain import cautions, document_hash, expanded_tests
from .statements import effective, translation_status
from .storage import blob_path

_CACHE = OrderedDict()
_LOCK = Lock()
CATEGORIES = {'settings': '공통 설정', 'statements': '지문 · 번역', 'tests': '테스트 구조',
              'components': 'Checker · Validator', 'solutions': '풀이 검증', 'constraints': '제한 비교', 'packages': '패키지'}


def literal(value):
    value = value.strip().replace(' ', '').replace(',', '')
    if len(value) > 24: return None
    if re.fullmatch(r'-?\d+(?:[uUlL]{0,3})', value): return int(value.rstrip('uUlL'))
    power = re.fullmatch(r'10\^\{?(\d{1,2})\}?', value)
    if power and int(power[1]) <= 18: return 10 ** int(power[1])
    return None


def strip_comments(source):
    # Preserve quoted testlib variable labels and line numbers.
    return re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*[\s\S]*?\*/',
                  lambda m: m[0] if not m[0].startswith(('/',)) else re.sub(r'[^\n]', ' ', m[0]), source)


def constraint_checks(doc, tests, add):
    validator = doc['files'].get(doc.get('validator'))
    if not validator: return
    try:
        with blob_path(validator['sha256']).open('rb') as stream: raw = stream.read(262145)
        if len(raw) > 262144:
            add('CONSTRAINT_TOO_LARGE','info','constraints','Validator가 커서 제한 자동 비교를 생략했습니다.','validator','256KiB 이하 소스만 자동 비교합니다.')
            return
        source = strip_comments(raw.decode('utf-8'))
    except (OSError, UnicodeError): return
    # Only named readInt/readLong calls with literal endpoints are comparable.
    calls = list(re.finditer(r'\binf\.(?:readInt|readLong)\s*\(\s*([^,()]+),\s*([^,()]+),\s*"([A-Za-z][A-Za-z0-9_]*)"\s*\)', source))
    bounds = defaultdict(list)
    for call in calls:
        low, high = literal(call[1]), literal(call[2])
        if low is not None and high is not None: bounds[call[3]].append((low, high, call))
    statements = '\n'.join(effective(doc, lang).get(key, '') for lang in doc['statements'] for key in ('legend', 'input'))
    normalized = statements.replace('\\leq', '≤').replace('\\le', '≤').replace('<=', '≤')
    declared = defaultdict(set)
    for match in re.finditer(r'(?<![\w,])(-?\d[\d,]{0,23}(?:\s*\^\s*\{?\d{1,2}\}?)?)\s*≤\s*\$?([A-Za-z][A-Za-z0-9_]*)\$?\s*≤\s*(\d[\d,]{0,23}(?:\s*\^\s*\{?\d{1,2}\}?)?)(?![\w^,])', normalized):
        if re.match(r'\s*(?:[+*/^]|-(?!\s*$)|\\(?:times|cdot)|[A-Za-z])', normalized[match.end():]): continue
        low, high = literal(match[1]), literal(match[3])
        if low is not None and high is not None: declared[match[2]].add((low, high))
    compared = 0
    for name, entries in bounds.items():
        values = declared.get(name, set())
        if len(entries) != 1 or len(values) != 1: continue
        low, high, call = entries[0]; expected = next(iter(values)); compared += 1
        line = source.count('\n', 0, call.start()) + 1
        if (low, high) != expected:
            add('CONSTRAINT_DIFFERENCE', 'warning', 'constraints', f'{name}의 지문·Validator 제한이 다릅니다.', 'validator',
                f'지문 {expected[0]}…{expected[1]} / {validator["name"]}:{line} {low}…{high}. 동일 변수인지 검토하세요. 단순 리터럴 비교이며 의미 증명이 아닙니다.')
    add('CONSTRAINT_SCOPE', 'info', 'constraints', f'제한 {compared}개를 단순 리터럴로 비교했습니다.', 'validator',
        '지원: 이름이 일치하는 하한 ≤ 변수 ≤ 상한과 testlib readInt/readLong. 수식·조건부 제한·반복문·의미와 지문 문법 교정은 자동 검증하지 않습니다.')
    # A maximum-coverage claim requires a single unconditional first read directly
    # after registration. Otherwise the input prefix cannot identify the variable.
    first = re.search(r'\binf\.read\w+\s*\(', source)
    register = re.search(r'\bregisterValidation\s*\([^;]*\)\s*;', source)
    if not first or not register or not calls or first.start() != calls[0].start(): return
    call = calls[0]
    if not re.fullmatch(r'\s*(?:(?:const\s+)?(?:int|long\s+long|auto)\s+\w+\s*=\s*)?', source[register.end():call.start()]): return
    entries = bounds.get(call[3], [])
    if len(entries) != 1: return
    high = entries[0][1]; values = []
    if not tests: return
    for test in tests:
        if not test.get('input_sha'): return
        try:
            with blob_path(test['input_sha']).open('rb') as stream: prefix = stream.read(256)
        except OSError: return
        token = re.match(rb'\s*(-?\d+)(?=\s)', prefix)
        if not token: return
        values.append(int(token[1]))
    if high not in values:
        add('NO_MAXIMUM_INPUT', 'warning', 'tests', f'{call[3]}의 상한 {high}을 사용하는 등록 입력이 없습니다.', 'tests',
            f'첫 입력 토큰을 {validator["name"]}의 첫 readInt/readLong과 대응했습니다. {len(values)}개 입력의 최대 관측값 {max(values)}. 다른 변수나 생성 전 입력은 판정하지 않습니다.')


def build(doc, jobs=()):
    items = []
    def add(code, severity, category, message, section, evidence='', job_id=None):
        items.append(dict(code=code, severity=severity, category=category, message=message,
                          section=section, evidence=evidence, job_id=job_id))
    for caution in cautions(doc):
        category = {'checker':'components','validator':'components','statements':'statements','solutions':'solutions','tests':'tests'}.get(caution['section'],'settings')
        add(caution['code'], 'error' if caution['blocking'] else 'recommendation', category,
            caution['message'], caution['section'], '현재 저장된 문제 자료')
    all_tests = []
    try:
        for name, testset in doc['testsets'].items():
            rows = expanded_tests(testset); all_tests.extend(rows)
            hashes = defaultdict(list)
            for test in rows:
                if test.get('input_sha'): hashes[test['input_sha']].append(test['index'])
            for indices in hashes.values():
                if len(indices) > 1:
                    add('DUPLICATE_INPUT', 'recommendation' if doc['info']['skip_duplicate_validation'] else 'warning', 'tests',
                        f'{name}: 동일 입력 테스트 {len(indices)}개', 'tests', f'SHA-256 일치: {indices[:20]}. 그룹·점수 목적을 확인한 뒤 정리하세요.')
    except Exception as error:
        # Parsing failure is a report item; never imply that invalid DSL was checked.
        from fastapi import HTTPException
        if not isinstance(error, HTTPException): raise
        add('INVALID_GENERATION_SCRIPT','error','tests','생성 스크립트를 해석할 수 없습니다.','tests',str(error.detail))
    constraint_checks(doc, all_tests, add)
    for language, status in translation_status(doc).items():
        if status in {'stale','modified','unreviewed'}:
            label = {'stale':'기준 원문이 변경되었습니다.','modified':'검토 후 번역문이 변경되었습니다.','unreviewed':'번역 검토가 기록되지 않았습니다.'}[status]
            add('TRANSLATION_'+status.upper(), 'warning' if status=='stale' else 'recommendation', 'statements', f'{language}: {label}', 'statements')
    latest = next((job for job in jobs if job.kind in {'verify','package'} and job.result.get('solutions')), None)
    if latest:
        for solution in latest.result['solutions']:
            scopes = solution.get('expectations', [])
            for scope in scopes:
                if scope.get('complete') and not scope.get('skipped') and scope.get('matched') is False:
                    add('EXPECTED_VERDICT_MISMATCH', 'warning', 'solutions', f'{solution["name"]}: 기대 판정 불일치', 'executions',
                        f'{scope["testset"]}/{scope["group"] or "기본"} · 기대 {scope["tag"]}, 실제 {", ".join(scope["verdicts"])} · {scope["tested"]}/{scope["total"]} 실행', latest.id)
        if not all(s.get('skipped') or s.get('expectations_complete', False) for s in latest.result['solutions']):
            add('PARTIAL_VERIFICATION','info','solutions','최근 실행은 일부 범위만 검증했습니다.','executions','미실행 범위를 통과로 간주하지 않습니다.',latest.id)
    else: add('NO_EXECUTION','info','solutions','현재 자료에 대응하는 풀이 검증 기록이 없습니다.','executions','최신 24개 대응 작업에서 확인합니다. 실행은 별도로 요청하세요.')
    for kind, field, section in [('validator-tests','validator_tests','validator'),('checker-tests','checker_tests','checker')]:
        if not doc[field]: continue
        execution = next((j for j in jobs if j.kind==kind), None)
        # Package unit_tests mix checker/validator IDs; use dedicated jobs only.
        if execution and execution.kind==kind:
            rows = execution.result.get('unit_tests',[])
            failed = [r['id'] for r in rows if not r.get('matched')]
            complete = {str(r['id']) for r in rows} == set(doc[field])
            if failed: add('COMPONENT_FAILED','warning','components',f'{section} 자체 테스트 실패','executions',', '.join(failed),execution.id)
            elif not complete: add('COMPONENT_PARTIAL','info','components',f'{section} 자체 테스트가 완료되지 않았습니다.','executions',job_id=execution.id)
        else: add('COMPONENT_UNRUN','recommendation','components',f'{section} 자체 테스트의 독립 실행 기록을 확인하세요.','executions')
    package = next((j for j in jobs if j.kind=='package' and j.status=='completed' and j.result.get('package')), None)
    if not package: add('PACKAGE_UNBUILT','recommendation','packages','현재 자료의 완료된 패키지가 없습니다.','packages')
    counts = {key:sum(i['severity']==key for i in items) for key in ['error','warning','recommendation','info']}
    return {'document_hash':document_hash(doc),'generated_at':now(),'categories':CATEGORIES,'counts':counts,'items':items,
            'evidence_jobs':[j.id for j in jobs], 'method':'자료 규칙 · 저장된 실행 기록 · 제한 리터럴 비교. AI 호출 및 새 코드 실행 없음'}


def report(db, problem_id, doc):
    sha = document_hash(doc)
    ids = db.execute(select(Job.id, Job.finished_at).outerjoin(JobApplication, JobApplication.job_id==Job.id).where(
        Job.problem_id==problem_id, Job.status.in_(['completed','failed','cancelled']),
        or_(Job.document_hash==sha, JobApplication.document_hash==sha)).order_by(Job.created_at.desc(), Job.id.desc()).limit(48)).all()
    unique = list(dict.fromkeys(tuple(row) for row in ids))[:24]
    key = (str(db.get_bind().url),problem_id,sha,tuple(unique))
    with _LOCK:
        entry = _CACHE.get(key)
        if entry and now()-entry['generated_at']<30:
            _CACHE.move_to_end(key)
            return copy.deepcopy(entry)
    by_id = {j.id:j for j in db.scalars(select(Job).where(Job.id.in_([row[0] for row in unique]))).all()} if unique else {}
    result = build(doc,[by_id[row[0]] for row in unique])
    with _LOCK:
        _CACHE[key] = copy.deepcopy(result)
        while len(_CACHE)>128: _CACHE.popitem(last=False)
    return result
