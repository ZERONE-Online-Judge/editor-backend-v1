"""Compact, evidence-based outcomes independent of worker lifecycle status."""


def needs_expectation_snapshot(job):
    from .expectations import VERSION
    result = job.result
    return (job.kind == 'verify' and job.status == 'completed'
            and result.get('expectations_version') != VERSION
            and bool(result.get('testsets')) and bool(result.get('solutions')))


def counts(total, passed=0, failed=0):
    return {'total': total, 'passed': passed, 'failed': failed,
            'unconfirmed': max(0, total - passed - failed)}


def assessment(kind, status, code, *, totals=None, partial=False, **fields):
    return {'kind': kind, 'status': status, 'code': code, 'partial': partial,
            **({'counts': totals} if totals is not None else {}), **fields}


def verification(job, result):
    rows = [row for row in result.get('solutions', []) if not row.get('skipped') and row.get('tag') != 'NR']
    rows = [row for row in rows if row.get('cases') or row.get('judge_runs') or row.get('total_tests') != 0]
    units = result.get('unit_tests', [])
    unit_total = sum(result.get('unit_test_totals', {}).values())
    total = len(rows) + max(unit_total, len(units))
    passed = failed = 0
    for row in rows:
        scopes = [scope for scope in row.get('expectations', []) if not scope.get('skipped')]
        finished = row.get('state') in {None, 'completed'}
        verdicts = {case.get('verdict') for case in row.get('cases', [])}
        if 'CE' in verdicts:
            failed += 1
            continue
        if 'JUDGE_ERROR' in verdicts:
            # An unavailable judge result cannot establish content correctness.
            continue
        if scopes:
            if any(scope.get('matched') is False for scope in scopes):
                failed += 1
            elif (finished and row.get('complete', True) and row.get('expectations_complete') is True
                  and row.get('matched') is True and all(scope.get('matched') is True for scope in scopes)):
                passed += 1
        elif finished and row.get('complete', True) and row.get('cases'):
            if row.get('matched') is False:
                failed += 1
            elif row.get('matched') is True and row.get('expectations_complete', False):
                passed += 1
    passed += sum(row.get('matched') is True for row in units)
    failed += sum(row.get('matched') is False for row in units)
    totals = counts(total, passed, failed)
    if failed:
        return assessment('expectations', 'failed', 'expectations_differ', totals=totals,
                          partial=bool(totals['unconfirmed']))
    if not total:
        return assessment('expectations', 'empty' if result.get('solutions') else 'unknown',
                          'no_targets' if result.get('solutions') else 'missing_evidence')
    # A true worker completion is insufficient: require explicit, complete checks.
    if passed == total and result.get('verified') is True:
        return assessment('expectations', 'passed', 'expectations_match', totals=totals)
    return assessment('expectations', 'incomplete', 'incomplete_checks', totals=totals, partial=True)


def component(job, result):
    rows = result.get('unit_tests', [])
    expected = result.get('unit_test_totals', {}).get(job.kind)
    total = max(expected or 0, len(rows))
    passed = sum(row.get('matched') is True for row in rows)
    failed = sum(row.get('matched') is False for row in rows)
    totals = counts(total, passed, failed)
    if failed:
        return assessment('component', 'failed', 'component_differs', totals=totals,
                          partial=bool(totals['unconfirmed']) or job.status != 'completed')
    if total and passed == total and result.get('verified') is True:
        return assessment('component', 'passed', 'component_matches', totals=totals)
    if expected == 0 or (job.status == 'completed' and rows == [] and result.get('verified') is False):
        return assessment('component', 'empty', 'no_targets')
    return assessment('component', 'incomplete' if total else 'unknown',
                      'incomplete_checks' if total else 'missing_evidence', totals=totals, partial=True)


def preparation(job, result):
    mode = job.options.get('prepare_mode') or result.get('prepare_mode') or 'full'
    kind = 'input' if mode in {'inputs', 'validate'} else 'preparation'
    rows = [row for values in result.get('testsets', {}).values() for row in values]
    total = max(result.get('preparation_total', 0), len(rows))
    failed = sum(row.get('validator') == 'INVALID' for row in rows)
    passed = sum(row.get('validator') == 'VALID' and (kind == 'input' or bool(row.get('answer_sha'))) for row in rows)
    totals = counts(total, passed, failed)
    if failed:
        return assessment(kind, 'failed', 'input_rejected', totals=totals,
                          partial=job.status != 'completed' or bool(totals['unconfirmed']))
    if total and passed == total and job.status == 'completed':
        return assessment(kind, 'passed', 'input_valid' if kind == 'input' else 'preparation_ready', totals=totals)
    if total == 0 and result.get('preparation_total') == 0:
        return assessment(kind, 'empty', 'no_targets')
    return assessment(kind, 'incomplete' if total else 'unknown',
                      'incomplete_checks' if total else 'missing_evidence', totals=totals, partial=job.status != 'completed')


def stress(job, result):
    stats = result.get('stress', {})
    examples = result.get('counterexamples') or ([result['counterexample']] if result.get('counterexample') else [])
    found = max(stats.get('counterexample_count', 0), len(examples))
    iterations = stats.get('iterations_completed', result.get('iterations', 0))
    if found:
        return assessment('stress', 'found', 'counterexample_found', partial=job.status != 'completed',
                          found=found, iterations=iterations,
                          uncovered=len(result.get('uncovered_candidates', [])))
    if job.status == 'completed' and (stats or 'iterations' in result):
        return assessment('stress', 'not_found', 'counterexample_not_found', iterations=iterations, found=0)
    return assessment('stress', 'incomplete' if stats else 'unknown',
                      'incomplete_checks' if stats else 'missing_evidence', partial=job.status != 'completed')


def summarize(job, result):
    """Never infer successful checking from status=completed or progress=100."""
    if job.status in {'queued', 'running'}:
        check = assessment('pending', 'pending', job.status)
    elif result.get('assessment_failure'):
        evidence = result['assessment_failure']
        check = assessment(evidence['kind'], 'failed', evidence['code'], partial=True)
    elif job.kind in {'verify', 'package'}:
        if job.kind == 'package' and (job.options.get('verify') is False or result.get('verification_skipped')):
            check = assessment('expectations', 'not_run', 'verification_skipped')
        else:
            check = verification(job, result)
    elif job.kind in {'validator-tests', 'checker-tests'}:
        check = component(job, result)
    elif job.kind == 'prepare':
        check = preparation(job, result)
    elif job.kind == 'stress':
        check = stress(job, result)
    elif job.kind == 'render':
        rows = list(result.get('statements', {}).values())
        passed = sum(row.get('html') == 'OK' and row.get('pdf') == 'OK' for row in rows)
        failed = sum(row.get('html') == 'FAILED' or row.get('pdf') == 'FAILED' for row in rows)
        status = 'failed' if failed else 'passed' if rows and passed == len(rows) and job.status == 'completed' else 'unknown'
        check = assessment('render', status, 'render_failed' if failed else 'render_ready' if status == 'passed' else 'missing_evidence',
                           totals=counts(len(rows), passed, failed), partial=job.status != 'completed')
    else:
        check = assessment('none', 'unknown', 'missing_evidence')
    return {'execution': job.status, 'assessment': check}
