from app.scoring import score_results


def test_group_dependencies_and_complete_group():
    document = {'info': {'points_enabled': True}, 'testsets': {'tests': {'groups_enabled': True, 'groups': {'a': {'points_policy': 'COMPLETE_GROUP', 'dependencies': []}, 'b': {'points_policy': 'EACH_TEST', 'dependencies': ['a']}}}}}
    prepared = {'tests': [{'index': 1, 'group': 'a'}, {'index': 2, 'group': 'a'}, {'index': 3, 'group': 'b'}]}
    rows = [{'testset': 'tests', 'index': 1, 'group': 'a', 'verdict': 'AC', 'score': 25}, {'testset': 'tests', 'index': 2, 'group': 'a', 'verdict': 'WA', 'score': 0}, {'testset': 'tests', 'index': 3, 'group': 'b', 'verdict': 'AC', 'score': 50}]
    result = score_results(document, prepared, rows)
    assert result['total_score'] == 0
    assert next(g for g in result['groups'] if g['name'] == 'b')['blocked']
    rows[1].update(verdict='AC', score=25)
    assert score_results(document, prepared, rows)['total_score'] == 100
    assert score_results(document, prepared, rows[:1])['total_score'] is None
