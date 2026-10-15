from app.test_matrix import analyze


def fixture():
    document={'solutions':{name:{'tag':tag,'extra_tags':{}} for name,tag in [('main','MA'),('wrong1','WA'),('wrong2','WA'),('slow','TL')]},'testsets':{'tests':{}}}
    patterns={'main':['AC']*5,'wrong1':['AC','WA','AC','AC','AC'],'wrong2':['WA','WA','AC',None,'WA'],'slow':['AC']*5}
    results={'testsets':{'tests':[{'index':i,'group':''} for i in range(1,6)]},'solutions':[{
        'name':name,'cases':[{'testset':'tests','index':i,'verdict':verdict,'cpu_ms':None,'memory_kb':12 if i==2 else None} for i,verdict in enumerate(verdicts,1) if verdict]
    } for name,verdicts in patterns.items()]}
    return document,results


def test_matrix_exposes_unique_detections_patterns_and_observed_minimum_cover():
    document,results=fixture()
    matrix=analyze(document,results)
    assert matrix['uncovered_solutions']==['slow']
    assert matrix['cover']['test_ids']==['tests/2']
    assert matrix['cover']['covered_solutions']==2
    rows=matrix['rows']
    assert rows[0]['same_pattern_as']==['tests/5']
    assert rows[1]['exclusive_detections']==['wrong1']
    assert rows[2]['no_detection']
    assert not rows[3]['no_detection'] and rows[3]['missing_targets']==['wrong2']
    assert all(row['max_cpu_ms'] is None for row in rows)
    assert rows[1]['max_memory_kb']==12


def test_scope_overrides_and_unassigned_environment_errors_are_not_fake_detections():
    document,results=fixture()
    document['solutions']['wrong1']['group_tags']={'tests':{'small':'OK'}}
    results['testsets']['tests'][1]['group']='small'
    results['solutions'][1]['cases'].append({'testset':'tests','index':0,'verdict':'CE'})
    matrix=analyze(document,results)
    assert 'wrong1' not in matrix['rows'][1]['detected']
    assert matrix['unassigned_errors']==[{'solution':'wrong1','verdict':'CE'}]


def test_duplicate_pattern_metadata_is_bounded_for_large_testsets():
    document,results=fixture()
    results['testsets']['tests']=[{'index':i,'group':''} for i in range(1,1001)]
    for solution in results['solutions']:
        solution['cases']=[{'testset':'tests','index':i,'verdict':'WA','cpu_ms':None,'memory_kb':None} for i in range(1,1001)]
    matrix=analyze(document,results)
    assert matrix['rows'][0]['same_pattern_count']==999
    assert len(matrix['rows'][0]['same_pattern_as'])==5
    assert matrix['cover']['method']=='greedy' and len(matrix['cover']['test_ids'])==1
