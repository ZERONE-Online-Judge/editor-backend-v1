"""Bounded generator loops; no expression evaluation or shell execution."""
import re
import shlex
from .execution_limits import MAX_TESTS_PER_SET

LOOP = re.compile(r"repeat\s+([A-Za-z_][A-Za-z0-9_]{0,31})\s+in\s+(-?\d{1,19})\s*\.\.\s*(-?\d{1,19})(?:\s+step\s+(-?\d{1,19}))?")
VARIABLE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def parse(source, occupied=()):
    root, stack = [], []
    current = root
    for number, line in enumerate(source.splitlines(), 1):
        value = line.strip()
        if not value or value.startswith('#'): continue
        if value == 'end':
            if not stack: raise ValueError(f'{number}행: 대응하는 repeat가 없습니다.')
            current = stack.pop()
        elif value.startswith('repeat '):
            match = LOOP.fullmatch(value)
            if not match or len(stack) >= 4: raise ValueError(f'{number}행: repeat 변수 in 시작..끝 [step 증가량] 형식을 확인하세요. 중첩은 최대 4단계입니다.')
            variable, low, high, step = match.groups()
            low, high, step = int(low), int(high), int(step) if step else 1
            if not all(-(2**63) <= v < 2**63 for v in [low, high, step]) or not step or (high-low)*step < 0:
                raise ValueError(f'{number}행: 범위와 증가 방향이 올바르지 않습니다.')
            if abs(high-low)//abs(step)+1 > MAX_TESTS_PER_SET: raise ValueError(f'{number}행: 반복은 최대 {MAX_TESTS_PER_SET}회입니다.')
            body = []
            current.append(('repeat',number,(variable,range(low,high+(1 if step>0 else -1),step)),body))
            stack.append(current)
            current = body
        else: current.append(('line',number,line,None))
    if stack: raise ValueError('repeat 블록을 end로 닫으세요.')
    used, result, steps = set(occupied), [], 0
    def expand(nodes, variables, group=''):
        nonlocal steps
        for kind, number, payload, body in nodes:
            steps += 1
            if steps > 20000: raise ValueError('스크립트 확장 한도를 넘었습니다.')
            if kind == 'repeat':
                variable, values = payload
                for value in values:
                    steps += 1
                    if steps > 20000: raise ValueError('스크립트 확장 한도를 넘었습니다.')
                    expand(body,{**variables,variable:value},group)
                continue
            def replace(match):
                if match[1] not in variables: raise ValueError(f'{number}행: 정의하지 않은 변수 {match[0]}')
                return str(variables[match[1]])
            tokens = shlex.split(VARIABLE.sub(replace,payload),comments=True)
            if not tokens: continue
            if tokens[0] == 'group':
                if len(tokens)!=2: raise ValueError(f'{number}행: group 그룹명 또는 group - 를 사용하세요.')
                group = '' if tokens[1]=='-' else tokens[1]
                continue
            if tokens.count('>')!=1 or len(tokens)<3 or tokens[-2]!='>': raise ValueError(f'{number}행: generator 인자 > 번호 또는 > $ 형식을 사용하세요.')
            command, destination = tokens[:-2],tokens[-1]
            if any(t in {'|',';','&&','||','<','>>'} for t in command): raise ValueError(f'{number}행: 셸 연산자는 사용할 수 없습니다.')
            index = next((i for i in range(1,MAX_TESTS_PER_SET+1) if i not in used),MAX_TESTS_PER_SET+1) if destination=='$' else int(destination)
            if index<1 or index>MAX_TESTS_PER_SET or index in used: raise ValueError(f'{number}행: 테스트 번호 중복 또는 범위 초과')
            used.add(index)
            result.append({'index':index,'command':shlex.join(command),**({'group':group} if group else {})})
    expand(root,{})
    return result
