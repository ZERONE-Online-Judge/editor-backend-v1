"""Conservative, bounded parsing of explicit integer ranges. Never execute source."""
import ast
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import html
from itertools import islice
import re

LIMIT = 2**63
NAME = r'[A-Za-z][A-Za-z0-9]*(?:_(?:[A-Za-z0-9]+|\{[A-Za-z0-9]+\}))?'
CHAIN = re.compile(rf'(<=|>=|≤|≥|<|>)\s*({NAME}(?:\s*,\s*{NAME})*)\s*(<=|>=|≤|≥|<|>)')


def variable(value):
    value = re.sub(r'_\{([A-Za-z0-9]+)\}', r'_\1', value.strip())
    return value if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', value) else None


def strip_comments(source):
    return re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*[\s\S]*?\*/',
        lambda m: m[0] if not m[0].startswith('/') else re.sub(r'[^\n]', ' ', m[0]), source)


def normalize_math(value):
    value = html.unescape(value).replace('−', '-').replace('×', '*').replace('·', '*')
    value = re.sub(r'\\(?:leqslant|leq|le)(?![A-Za-z])', '≤', value)
    value = re.sub(r'\\(?:geqslant|geq|ge)(?![A-Za-z])', '≥', value)
    value = re.sub(r'\\(?:lt)(?![A-Za-z])', '<', value)
    value = re.sub(r'\\(?:gt)(?![A-Za-z])', '>', value)
    value = re.sub(r'\\(?:left|right)(?![A-Za-z])', '', value)
    value = re.sub(r'\\(?:times|cdot)(?![A-Za-z])', '*', value)
    value = re.sub(r'\\(?:quad|qquad)(?![A-Za-z])', ';', value)
    value = re.sub(r'\\[,!:; ]|\\(?:thinspace|medspace)(?![A-Za-z])', '', value)
    value = re.sub(r'\\(?:mathrm|mathit|mathbf|operatorname)\{([A-Za-z][A-Za-z0-9_]*)\}', r'\1', value)
    value = value.replace('{,}', ',').replace('~', ' ')
    return value


def integer_expression(value, *, cpp=False, constants=None):
    """Whitelist integer arithmetic with strict whole-expression consumption."""
    if len(value) > 256: return None
    value = value.strip()
    if not cpp:
        value = normalize_math(value)
        for _ in range(4):
            updated = re.sub(r'\\(?:d?frac)\{([^{}]+)\}\{([^{}]+)\}', r'((\1)/(\2))', value)
            if updated == value: break
            value = updated
        value = value.replace('^', '**').replace('{', '(').replace('}', ')')
    else:
        value = re.sub(r'(?<=\d)[uUlL]{1,3}\b', '', value)
    value = re.sub(r'(?<![\w,])\d{1,3}(?:,\d{3})+(?![\w,])', lambda m:m[0].replace(',', ''), value)
    value = value.strip()
    if not value or '\\' in value or len(value) > 256: return None
    try:
        tree = ast.parse(value, mode='eval')
        if sum(1 for _ in ast.walk(tree)) > 64: return None
        def read(node):
            if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
                text = ast.get_source_segment(value, node)
                # Decimal preserves integer scientific notation without float rounding.
                result = Decimal(text) if not re.match(r'0[xXbBoO]', text) else Decimal(int(text, 0))
                if not result.is_finite() or abs(result)>LIMIT or result != result.to_integral_value(): raise ValueError()
                result = int(result)
            elif isinstance(node, ast.Name) and node.id in (constants or {}):
                result = constants[node.id]
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
                result = read(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
            elif isinstance(node, ast.BinOp):
                a, b = read(node.left), read(node.right)
                if isinstance(node.op, ast.Add): result = a+b
                elif isinstance(node.op, ast.Sub): result = a-b
                elif isinstance(node.op, ast.Mult): result = a*b
                elif isinstance(node.op, ast.Div):
                    if not b or not cpp and a % b: raise ValueError()
                    result = (abs(a)//abs(b)) * (-1 if (a<0)!=(b<0) else 1)
                elif isinstance(node.op, ast.Pow) and not cpp and 0 <= b <= 63: result = a**b
                elif isinstance(node.op, ast.LShift) and cpp and a>=0 and 0<=b<=62: result = a<<b
                elif isinstance(node.op, ast.RShift) and cpp and a>=0 and 0<=b<=62: result = a>>b
                elif isinstance(node.op, ast.BitXor) and cpp and a>=0 and b>=0: result = a^b
                else: raise ValueError()
            else: raise ValueError()
            if abs(result) > LIMIT: raise ValueError()
            return result
        return read(tree.body)
    except (ValueError, SyntaxError, InvalidOperation, OverflowError, RecursionError):
        return None


@dataclass(frozen=True)
class Bound:
    name: str
    low: int
    high: int
    expression: str
    location: str
    start: int = 0


def statement_bounds(doc):
    from .statements import effective
    result, skipped = [], set()
    for language in doc['statements']:
        statement = effective(doc, language)
        for field in ('legend', 'input'):
            raw = statement.get(field, '')
            raw = re.sub(r'```[\s\S]*?```|`[^`\n]*`', '', raw)
            math = re.compile(r'\$\$([\s\S]*?)\$\$|\$([^$]*?)\$|\\\(([\s\S]*?)\\\)|\\\[([\s\S]*?)\\\]')
            for paragraph in re.split(r'\n\s*\n',raw):
                if re.search(r'\b(?:if|when|subtask)\b|부분\s*문제|\d+\s*[%점]|(?:인|일)\s*경우',paragraph,re.I):
                    for conditional in CHAIN.finditer(normalize_math(paragraph.replace('$',''))):
                        skipped.update(variable(n) for n in conditional[2].split(',') if variable(n))
            units = [next(g for g in m.groups() if g is not None) for m in math.finditer(raw)]
            units += math.sub(lambda m: '' if CHAIN.search(normalize_math(next(g for g in m.groups() if g is not None))) else next(g for g in m.groups() if g is not None), raw).splitlines()
            for unit in units:
                text = normalize_math(unit)
                # Commas between ranges are distinct from grouped decimal digits.
                for part in re.split(r';|,\s+(?=[+\-(\d{][^;]*(?:≤|≥|<=|>=|<|>))', text):
                    matches = list(CHAIN.finditer(part))
                    if not matches: continue
                    # Several adjacent comparisons, prose, or unknown expressions
                    # must not be reduced to the numeric prefix of a bound.
                    for match in matches:
                        names = [variable(n) for n in match[2].split(',')]
                        left = part[:match.start()].strip()
                        right = part[match.end():].strip()
                        # Parentheses around a complete range are punctuation.
                        if left.startswith('(') and right.endswith(')') and left.count('(')==left.count(')')+1:
                            left, right = left[1:], right[:-1]
                        right = right.removesuffix('.').strip()
                        low, high = integer_expression(left), integer_expression(right)
                        lo_op, hi_op = match[1], match[3]
                        if lo_op in {'>', '>=', '≥'} and hi_op in {'>', '>=', '≥'}:
                            low, high = high, low
                            lo_op, hi_op = hi_op.replace('>', '<').replace('≥','≤'), lo_op.replace('>', '<').replace('≥','≤')
                        elif lo_op not in {'<', '<=', '≤'} or hi_op not in {'<', '<=', '≤'}:
                            low = high = None
                        if low is None or high is None or len(matches)!=1:
                            skipped.update(n for n in names if n); continue
                        low += int(lo_op=='<'); high -= int(hi_op=='<')
                        if low>high:
                            skipped.update(n for n in names if n); continue
                        for name in names:
                            if name: result.append(Bound(name,low,high,part.strip(),f'{language} · {field}'))
    return result, skipped


def closing(source, start, opening='(', end=')'):
    depth, quote, escaped = 0, None, False
    for i in range(start,len(source)):
        ch=source[i]
        if quote:
            if escaped: escaped=False
            elif ch=='\\': escaped=True
            elif ch==quote: quote=None
            continue
        if ch in {'"',"'"}: quote=ch
        elif ch==opening: depth+=1
        elif ch==end:
            depth-=1
            if depth==0:return i
    return None


def arguments(text):
    parts=[]; start=0; depth=0; quote=None; escaped=False
    for i,ch in enumerate(text):
        if quote:
            if escaped: escaped=False
            elif ch=='\\': escaped=True
            elif ch==quote: quote=None
        elif ch in {'"',"'"}:quote=ch
        elif ch in '([{':depth+=1
        elif ch in ')]}':depth-=1
        elif ch==',' and depth==0:parts.append(text[start:i].strip());start=i+1
    return [*parts,text[start:].strip()]


def conditional_ranges(source):
    spans=[]
    if len(re.findall(r"\b(?:if|switch|else)\b",source))>256:return [(0,len(source))]
    for match in re.finditer(r'\b(?:if(?:\s+constexpr)?|switch)\s*\(|\belse\b',source):
        begin=match.end()
        if source[begin-1]=='(':
            close=closing(source,begin-1)
            if close is None:continue
            begin=close+1
        while begin<len(source) and source[begin].isspace():begin+=1
        end=closing(source,begin,'{','}') if begin<len(source) and source[begin]=='{' else source.find(';',begin)
        if end is not None and end>=0:spans.append((match.start(),end+1))
    stack=[];offset=0
    for line in source.splitlines(keepends=True):
        if re.match(r'\s*#\s*if',line):stack.append(offset)
        elif re.match(r'\s*#\s*endif',line) and stack:spans.append((stack.pop(),offset+len(line)))
        offset+=len(line)
    spans.extend((pos,len(source)) for pos in stack)
    return spans


def validator_bounds(source):
    source=strip_comments(source)
    masked=re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',lambda m:re.sub(r'[^\n]',' ',m[0]),source)
    conditionals=conditional_ranges(masked)
    pattern=r'(?m)^\s*#\s*define\s+([A-Za-z_]\w*)[ \t]+([^\n]+)|\b(?:constexpr|const)\s+(?:(?:unsigned|signed|long|int|auto|size_t|int64_t)\s+)+([A-Za-z_]\w*)\s*=\s*([^;]+);'
    definitions=list(islice(re.finditer(pattern,masked),257))
    matches=list(islice(re.finditer(r'\binf\.(readInt|readLong)\s*\(',masked),513))
    if len(definitions)>256 or len(matches)>512:return source,[],set()
    positions={m.start() for m in definitions}; depths={}; depth=0
    for i,ch in enumerate(masked):
        if i in positions:depths[i]=depth
        if ch=='{':depth+=1
        elif ch=='}':depth-=1
    # Only unconditional file-scope constants are resolved. Any shadowing,
    # conditional definition or #undef makes the name ambiguous, never guessed.
    unstable=set(re.findall(r'(?m)^\s*#\s*undef\s+(\w+)',masked))
    declarations=[]
    for m in definitions:
        name,expr=m[1] or m[3],m[2] or m[4]
        if depths[m.start()] or any(a<=m.start()<b for a,b in conditionals):unstable.add(name)
        else:declarations.append((m.end(),name,expr))
    for _,name,_ in declarations:
        for declaration in re.finditer(r'\b(?:[A-Za-z_]\w*\s+)+'+re.escape(name)+r'\s*(?=[=,;)])',masked):
            if not any(m.start()<=declaration.start()<m.end() for m in definitions):unstable.add(name)
    result=[];skipped=set()
    for match in matches:
        constants={}
        for end,name,expr in declarations:
            if end>match.start():break
            value=integer_expression(expr,cpp=True,constants=constants) if name not in unstable else None
            if value is None:constants.pop(name,None)
            else:constants[name]=value
        close=closing(source,match.end()-1)
        if close is None:continue
        args=arguments(source[match.end():close])
        if len(args)!=3 or not re.fullmatch(r'"[A-Za-z][A-Za-z0-9_]*"',args[2]):continue
        name=variable(args[2][1:-1]); low=integer_expression(args[0],cpp=True,constants=constants); high=integer_expression(args[1],cpp=True,constants=constants)
        if low is None or high is None or low>high or "?" in masked[masked.rfind(";",0,match.start())+1:match.start()] or any(a<=match.start()<b for a,b in conditionals):skipped.add(name);continue
        line=source.count('\n',0,match.start())+1
        result.append(Bound(name,low,high,source[match.start():close+1],str(line),match.start()))
    return source,result,skipped
