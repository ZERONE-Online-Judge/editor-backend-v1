#!/usr/bin/env python3
"""Replay an exported problem with Docker and Python's standard library only."""
import argparse
import hashlib
import json
import math
from pathlib import Path
from engine import support
from engine.sandbox import Sandbox, SandboxError, VENDOR
from engine.expectations import evaluate, expected_tag


def replay(args):
    metadata=json.loads(support.checked('replay/environment.json'))
    image=args.image or metadata.get('runtime',{}).get('image_id')
    if not image:
        raise ValueError('Specify --image after loading a runtime image, or build replay/runtime as described in README-LINUX.txt')
    support.configure(image)
    doc=json.loads(support.checked('editor.json'))['document']
    info=doc['info']
    if not 250<=info['time_limit_ms']<=15000 or not 4<=info['memory_limit_mb']<=1024:
        raise ValueError('Problem limits are outside Editor bounds')
    sets={name:support.expanded_tests(config) for name,config in doc['testsets'].items() if not args.testset or name==args.testset}
    if not sets: raise ValueError('Select an existing testset')
    selected=args.solution or list(doc['solutions'])
    if any(name not in doc['solutions'] for name in selected): raise ValueError('Unknown solution')
    resources=list(doc['files'].values())
    sandbox=Sandbox()
    compiled={}
    def program(name,solution=False):
        key=(name,solution)
        if key not in compiled:
            if name.startswith('std::'):
                standard=name.removeprefix('std::')
                if standard not in {'wcmp','fcmp','ncmp','rcmp4','rcmp6','rcmp9'}: raise ValueError('Unknown standard checker')
                content=(VENDOR/(standard+'.cpp')).read_bytes()
                source={'name':standard+'.cpp','kind':'source','language':'cpp17','sha256':hashlib.sha256(content).hexdigest(),'size':len(content)}
            else: source=doc['solutions' if solution else 'files'][name]
            compiled[key]=sandbox.compile(source,resources,is_solution=solution)
        return compiled[key]
    def checker(inp,actual,answer,points):
        result=sandbox.run(program(doc['checker']),files={'test.in':inp,'test.out':actual,'test.ans':answer},args=['test.in','test.out','test.ans'],cpu_ms=10000,memory_mb=512)
        if result['status']!='ok': return 'JUDGE_ERROR',result
        verdict={0:'AC',1:'WA',2:'PE',3:'JUDGE_ERROR'}.get(result['exit_code'],'JUDGE_ERROR')
        if result['exit_code']==7 and info['points_enabled']:
            import re
            match=re.search(r'\bpoints\s+([0-9.eE+-]+)',result['stderr'])
            value=float(match[1]) if match else float('nan')
            maximum=100 if info['checker_percent'] else points
            if math.isfinite(value) and 0<=value<=maximum+1e-8:
                verdict='AC' if value>=maximum-1e-8 else 'PARTIAL' if value>0 else 'WA'
        return verdict,result
    report={'format':'zoj-linux-replay-result-v1','environment':metadata,'solutions':[]}
    try:
        report['image']=sandbox.resolve_image(image)
        for set_name,tests in sets.items():
            for test in tests:
                inp=support.read_blob(test['input_sha'])
                for validator in [doc['validator'],*doc['extra_validators']]:
                    if not validator: raise ValueError('Validator is required')
                    arguments=['--testset',set_name]+(['--group',test['group']] if test.get('group') else [])
                    valid=sandbox.run(program(validator),inp,args=arguments,cpu_ms=10000,memory_mb=512)
                    if valid['status']!='ok' or valid['exit_code']!=0:
                        raise SandboxError(f'{set_name} #{test["index"]}: Validator rejected input\n'+valid['stderr'])
        for name in selected:
            source=doc['solutions'][name]
            if source['tag']=='NR':
                report['solutions'].append({'name':name,'skipped':True,'matched':True,'cases':[]})
                continue
            rows=[]
            error=None
            try:
                solution=program(name,True)
                for set_name,tests in sets.items():
                    for test in tests:
                        if expected_tag(source,set_name,test.get('group',''),doc['testsets'])[0]=='NR': continue
                        inp,answer=support.read_blob(test['input_sha']),support.read_blob(test['answer_sha'])
                        limits={'cpu_ms':info['time_limit_ms'],'memory_mb':info['memory_limit_mb']}
                        if info['interactive']:
                            actual=sandbox.interactive(solution,program(doc['interactor']),inp,answer,**limits)
                        else:
                            actual=sandbox.run(solution,inp,input_file=info['input_file'],output_file=info['output_file'],**limits)
                        if actual['status']!='ok':
                            verdict={'time-limit':'TLE','memory-limit':'MLE','output-limit':'OLE','wall-limit':'IDLENESS_LIMIT' if info['interactive'] else 'JUDGE_ERROR'}.get(actual['status'],'JUDGE_ERROR')
                        elif info['interactive'] and actual.get('stopped_by_interactor'):
                            verdict={1:'WA',2:'PE'}.get(actual.get('interactor_exit_code'),'JUDGE_ERROR')
                        elif actual['exit_code']!=0: verdict='RE'
                        elif info['interactive']: verdict={0:'AC',1:'WA',2:'PE'}.get(actual.get('interactor_exit_code'),'JUDGE_ERROR')
                        else: verdict='AC'
                        comment=actual.get('stderr','')
                        if verdict=='AC':
                            verdict,checked=checker(inp,actual['output'],answer,test.get('points',0))
                            comment=checked.get('stderr','')
                        rows.append({'testset':set_name,'index':test['index'],'group':test.get('group',''),'verdict':verdict,'cpu_ms':actual.get('cpu_ms'),'wall_ms':actual.get('wall_ms'),'memory_kb':actual.get('memory_kb'),'comment':comment[:2048]})
            except SandboxError as exc:
                error=str(exc)
            evaluation=evaluate(doc,source,sets,rows)
            report['solutions'].append({'name':name,'tag':source['tag'],'cases':rows,**evaluation,'error':error,'matched':not error and evaluation['matched']})
        report['verified']=bool(report['solutions']) and all(result['matched'] and result.get('expectations_complete',True) for result in report['solutions'])
        return report
    finally:
        sandbox.close()
        import shutil
        shutil.rmtree(support.get_settings().work_dir,ignore_errors=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',help='Locally available runtime image ID or tag; no automatic build or pull')
    parser.add_argument('--solution',action='append',help='Run this solution; repeat to select several')
    parser.add_argument('--testset',help='Select one materialized testset')
    parser.add_argument('--output',default='replay-result.json')
    args=parser.parse_args()
    try:
        result=replay(args)
        Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps({'verified':result['verified'],'output':args.output,'solutions':[{'name':s['name'],'matched':s['matched']} for s in result['solutions']]},ensure_ascii=False))
        raise SystemExit(0 if result['verified'] else 1)
    except (ValueError,KeyError,OSError,SandboxError) as exc:
        parser.exit(2,str(exc)+'\n')
