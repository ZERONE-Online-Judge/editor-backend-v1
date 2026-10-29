"""Analyze only observed testcase outcomes; missing measurements stay missing."""
from collections import defaultdict
from .expectations import expected_tag
from .domain import case_order
from .stress import minimum_cover

WRONG_TAGS = {'WA','TL','ML','RE','PE','RJ','TM'}
FAILURES = {'WA','TLE','MLE','RE','PE','OLE','IDLENESS_LIMIT','PARTIAL'}


def analyze(document, result):
    solutions = result.get('solutions', [])
    names = [solution['name'] for solution in solutions]
    observations = {(s['name'],c['testset'],c['index']): c for s in solutions for c in s.get('cases',[]) if c['index']>0}
    tests = {(set_name,test['index']): test for set_name,rows in result.get('testsets',{}).items() for test in rows}
    for _,set_name,index in observations:
        tests.setdefault((set_name,index),{'index':index,'group':''})
    rows, signatures, targets = [], defaultdict(list), set()
    order = case_order(document)
    for (set_name,index),test in sorted(tests.items(), key=lambda item: order({"testset": item[0][0], "index": item[0][1]})):
        hits, missing, signature, skipped = [], [], [], []
        cpu, memory = [], []
        eligible = []
        for name in names:
            source = document['solutions'].get(name, {'tag':'NR'})
            tag,_ = expected_tag(source,set_name,test.get('group',''),document['testsets'])
            case = observations.get((name,set_name,index))
            if tag=='NR': skipped.append(name)
            signature.append(case['verdict'] if case else 'SKIP' if tag=='NR' else None)
            if case:
                if case.get('cpu_ms') is not None: cpu.append(case['cpu_ms'])
                if case.get('memory_kb') is not None: memory.append(case['memory_kb'])
            if tag in WRONG_TAGS:
                eligible.append(name)
                targets.add(name)
                if not case or case['verdict'] in {'CE','JUDGE_ERROR'}: missing.append(name)
                elif case['verdict'] in FAILURES: hits.append(name)
        identifier=f'{set_name}/{index}'
        if None not in signature and signature: signatures[tuple(signature)].append(identifier)
        rows.append({'id':identifier,'testset':set_name,'index':index,'group':test.get('group',''),
                     'detected':hits,'missing_targets':missing,'skipped':skipped,'no_detection':bool(eligible) and not hits and not missing,
                     'max_cpu_ms':max(cpu) if cpu else None,'max_memory_kb':max(memory) if memory else None})
    detectors=defaultdict(list)
    for row in rows:
        for name in row['detected']: detectors[name].append(row['id'])
    duplicates={identifier:group for group in signatures.values() if len(group)>1 for identifier in group}
    for row in rows:
        group=duplicates.get(row['id'],[])
        row['same_pattern_as']=[i for i in group[:6] if i!=row['id']][:5]
        row['same_pattern_count']=max(0,len(group)-1)
        row['exclusive_detections']=[name for name in row['detected'] if len(detectors[name])==1]
    coverage=minimum_cover([{'id':row['id'],'findings':[{'solution':name,'verdict':'detected'} for name in row['detected']]} for row in rows])
    return {'version':1,'solutions':names,'targets':sorted(targets),'uncovered_solutions':sorted(targets-set(detectors)),
            'rows':rows,'cover':{'test_ids':coverage['example_ids'],'method':coverage['method'],'covered_solutions':len(detectors)},
            'unassigned_errors':[{'solution':s['name'],'verdict':c['verdict']} for s in solutions for c in s.get('cases',[]) if c['index']==0]}
