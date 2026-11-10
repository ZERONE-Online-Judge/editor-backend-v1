"""Conservative, bounded parsing of explicit integer ranges. Never execute source."""
import ast
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import html
from html.parser import HTMLParser
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
        value = re.sub(r"\b(?:0[xX][0-9A-Fa-f]+(?:'[0-9A-Fa-f]+)+|0[bB][01]+(?:'[01]+)+|[0-9]+(?:'[0-9]+)+)(?:[uUlL]{0,3})\b", lambda m: m[0].replace("'", ''), value)
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


MAX_ENTRIES = 512
MAX_STATEMENT_CHARS = 524288


@dataclass(frozen=True)
class Bound:
    name: str
    low: int | None
    high: int | None
    expression: str
    location: str
    start: int = 0
    reason_code: str = ''
    vector: bool = False
    naming: str = 'label'


class StatementText(HTMLParser):
    """Preserve block boundaries and simple math markup, never render HTML."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'pre', 'code'}:
            self.hidden += 1
        elif not self.hidden:
            if tag in {'p', 'div', 'li', 'br', 'tr', 'h1', 'h2', 'h3', 'h4'}: self.parts.append('\n')
            elif tag == 'sub': self.parts.append('_')
            elif tag == 'sup': self.parts.append('^(')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'pre', 'code'}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden:
            if tag in {'p', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'h4'}: self.parts.append('\n')
            elif tag == 'sup': self.parts.append(')')

    def handle_data(self, value):
        if not self.hidden: self.parts.append(value)


def statement_text(value):
    value = re.sub(r'```[\s\S]*?```|`[^`\n]*`', '', value)
    # Only actual known HTML tags consume angle brackets. Compact math such
    # as 1<N<10 must never be swallowed as an incomplete custom tag.
    tags = 'p|div|li|br|tr|h1|h2|h3|h4|ul|ol|table|tbody|thead|td|th|span|b|i|em|strong|u|sub|sup|script|style|pre|code|a|img'
    value = re.sub(r'<(?!(?:/?(?:' + tags + r')(?:\s[^<>]*|/?)>|!--))', '&lt;', value, flags=re.I)
    parser = StatementText()
    parser.feed(value)
    parser.close()
    return ''.join(parser.parts)


CONDITIONAL = re.compile(r'\b(?:if|when|subtask|example|sample)\b|부분\s*문제|예제|예를|\d+\s*[%점]|(?:인|일)\s*경우', re.I)
EXPRESSION_CHARS = r'A-Za-z0-9_+*/^\\{},(). \t-'


def statement_ends(part, match, math_only):
    left, right = part[:match.start()].strip(), part[match.end():].strip()
    if not math_only:
        left = re.sub(r'^(?:\d+[.)]\s+|\*\s+)', '', left)
        tail = re.search(rf'[{EXPRESSION_CHARS}]+$', left)
        prefix = left[:tail.start()] if tail else left
        if re.search(r'[^가-힣\s:;,.*•·\[\]\-]', prefix): return left, right
        left = tail[0].strip() if tail else ''
    # An unmatched opening parenthesis immediately before the range is its
    # prose wrapper, not part of the lower endpoint's arithmetic expression.
    depth = 0
    openings = []
    for i, char in enumerate(left):
        if char == '(': openings.append(i)
        elif char == ')' and openings: openings.pop()
    if openings:
        left = left[openings[-1] + 1:]
        for i, char in enumerate(right):
            if char == '(': depth += 1
            elif char == ')':
                if depth == 0:
                    suffix = right[i + 1:].strip().removesuffix('.')
                    if re.search(r'[+*/^=<>≤≥]|\d', suffix): return left, right
                    right = right[:i]
                    break
                depth -= 1
    if not math_only:
        head = re.match(rf'[{EXPRESSION_CHARS}]+', right)
        suffix = right[head.end():].strip() if head else right
        if suffix and (re.search(r'[^가-힣\s,.!?。:;“”‘’]', suffix) or re.search(
                r'^(?:십|백|천|만|억|조|배|승|분의|퍼센트)|제곱|거듭제곱|루트|이상|이하|초과|미만', suffix)):
            return left, right
        right = head[0].strip() if head else ''
    return left.strip(), right.removesuffix('.').strip()


def parse_statement(doc):
    result, notices = [], []
    remaining = MAX_STATEMENT_CHARS
    math = re.compile(r'\$\$([\s\S]*?)\$\$|\$([^$]*?)\$|\\\(([\s\S]*?)\\\)|\\\[([\s\S]*?)\\\]')
    sections = list(doc['statements'].items())
    if doc.get('common_statement'): sections.insert(0, ('공통 지문', {'legend': doc['common_statement']}))
    for language, statement in sections:
        for field in ('legend', 'input', 'notes'):
            raw = statement.get(field, '')
            if len(raw) > remaining:
                notices.append({'code': 'statement_scan_limit', 'message': '지문 분석은 전체 512KiB까지 수행합니다. 나머지 내용을 비교하지 않았습니다.'})
                return [replace(entry, reason_code='analysis_truncated') for entry in result], notices
            remaining -= len(raw)
            derived_html = bool(re.search(r'<(?:p|div|li|br|tr|h[1-6]|sub|sup|span|i|b)\b', raw, re.I))
            raw = statement_text(raw)
            conditional_field = any(CONDITIONAL.search(line) and not CHAIN.search(normalize_math(line)) for line in raw.splitlines())
            conditional_spans = []
            offset = 0
            for paragraph in re.split(r'(\n\s*\n)', raw):
                if CONDITIONAL.search(paragraph): conditional_spans.append((offset, offset + len(paragraph)))
                offset += len(paragraph)
            def conditional_at(position):
                return conditional_field or any(begin <= position < end for begin, end in conditional_spans)
            units = []
            for match in math.finditer(raw):
                value = next(group for group in match.groups() if group is not None)
                units.append((raw.count('\n', 0, match.start()) + 1, value, True, conditional_at(match.start())))
            def plain_math(match):
                index = next(i for i, group in enumerate(match.groups(), 1) if group is not None)
                value = match[index]
                if CHAIN.search(normalize_math(value)):
                    return re.sub(r'[^\n]', ' ', match[0])
                before = match.start(index) - match.start()
                return ' ' * before + value + ' ' * (match.end() - match.end(index))
            plain = math.sub(plain_math, raw)
            offset = 0
            for line_no, line in enumerate(plain.splitlines(keepends=True), 1):
                units.append((line_no, line, False, conditional_at(offset)))
                offset += len(line)
            for line_no, unit, math_only, conditional in units:
                text = normalize_math(unit)
                for part in re.split(r';|,\s+(?=[+\-(\d{][^;]*(?:≤|≥|<=|>=|<|>))', text):
                    matches = list(CHAIN.finditer(part))
                    for match in matches:
                        names = [variable(n) for n in match[2].split(',')]
                        left, right = statement_ends(part, match, math_only)
                        low, high = integer_expression(left), integer_expression(right)
                        lo_op, hi_op = match[1], match[3]
                        reason = ''
                        if lo_op in {'>', '>=', '≥'} and hi_op in {'>', '>=', '≥'}:
                            low, high = high, low
                            lo_op, hi_op = hi_op.replace('>', '<').replace('≥', '≤'), lo_op.replace('>', '<').replace('≥', '≤')
                        elif lo_op not in {'<', '<=', '≤'} or hi_op not in {'<', '<=', '≤'}:
                            reason = 'unsupported_comparison'
                        if conditional: reason = 'conditional_statement'
                        elif len(matches) != 1: reason = 'multiple_comparisons'
                        elif not math_only and re.match(r'^\s*-\s+\d', part): reason = 'ambiguous_list_prefix'
                        elif low is None or high is None: reason = reason or 'symbolic_statement_bound'
                        if low is not None: low += int(lo_op == '<')
                        if high is not None: high -= int(hi_op == '<')
                        if low is not None and high is not None and low > high: reason = 'invalid_range'
                        for name in names:
                            if name:
                                result.append(Bound(name, low, high, part.strip(), f'{language} · {field} · ' + (f'추출 텍스트 {line_no}행' if derived_html else str(line_no)), reason_code=reason))
                                if len(result) >= MAX_ENTRIES:
                                    notices.append({'code': 'statement_entry_limit', 'message': '지문 범위는 최대 512개까지 분석합니다. 나머지는 비교하지 않았습니다.'})
                                    return [replace(entry, reason_code='analysis_truncated') for entry in result], notices
    return result, notices


def statement_bounds(doc):
    entries, _ = parse_statement(doc)
    return [entry for entry in entries if not entry.reason_code], {entry.name for entry in entries if entry.reason_code}


def closing(source, start, opening='(', end=')'):
    depth, quote, escaped = 0, None, False
    for i in range(start,len(source)):
        ch=source[i]
        if quote:
            if escaped: escaped=False
            elif ch=='\\': escaped=True
            elif ch==quote: quote=None
            continue
        if ch == "'" and i > 0 and i + 1 < len(source) and source[i - 1].isalnum() and source[i + 1].isalnum(): continue
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
        elif ch == "'" and i > 0 and i + 1 < len(text) and text[i - 1].isalnum() and text[i + 1].isalnum(): continue
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


def assignment_name(masked, start):
    prefix = masked[max(masked.rfind(';', 0, start), masked.rfind('{', 0, start), masked.rfind('}', 0, start)) + 1:start]
    match = re.fullmatch(r'\s*(?:(?:(?:const|unsigned|signed|long|int|auto|size_t|int64_t|std::vector\s*<[^<>]+>|vector\s*<[^<>]+>)\s+)+)?([A-Za-z][A-Za-z0-9_]*)\s*=\s*', prefix)
    return match[1] if match else None


def parse_validator(raw_source):
    source = strip_comments(raw_source)
    mask_source = re.sub(r"\b(?:0[xX][0-9A-Fa-f]+(?:'[0-9A-Fa-f]+)+|0[bB][01]+(?:'[01]+)+|[0-9]+(?:'[0-9]+)+)(?:[uUlL]{0,3})\b", lambda m: m[0].replace("'", '_'), source)
    masked = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', lambda m: re.sub(r'[^\n]', ' ', m[0]), mask_source)
    conditionals = conditional_ranges(masked)
    pattern = r'(?m)^\s*#\s*define\s+([A-Za-z_]\w*)[ \t]+([^\n]+)|\b(?:constexpr|const)\s+(?:(?:unsigned|signed|long|int|auto|size_t|int64_t)\s+)+([A-Za-z_]\w*)\s*=\s*([^;]+);'
    definitions = list(islice(re.finditer(pattern, masked), 257))
    matches = list(islice(re.finditer(r'\binf\.(readInt|readLong|readInteger|readInts|readLongs)\s*\(', masked), MAX_ENTRIES + 1))
    notices = []
    if len(definitions) > 256:
        return source, [], [{'code': 'validator_constant_limit', 'message': 'Validator 상수 선언이 256개를 초과하여 안전하게 분석할 수 없습니다.'}]
    if len(matches) > MAX_ENTRIES:
        notices.append({'code': 'validator_entry_limit', 'message': 'Validator 정수 입력은 최대 512개까지 분석합니다. 나머지는 비교하지 않았습니다.'})
        matches = matches[:MAX_ENTRIES]
    positions = {m.start() for m in definitions}
    depths = {}
    depth = 0
    for i, char in enumerate(masked):
        if i in positions: depths[i] = depth
        if char == '{': depth += 1
        elif char == '}': depth -= 1
    main = re.search(r'\bmain\s*\(', masked)
    main_range = None
    if main:
        parameters_end = closing(masked, main.end() - 1)
        if parameters_end is not None:
            body = parameters_end + 1
            while body < len(masked) and masked[body].isspace(): body += 1
            if body < len(masked) and masked[body] == '{':
                body_end = closing(masked, body, '{', '}')
                if body_end is not None: main_range = (body, body_end)
    unstable = set(re.findall(r'(?m)^\s*#\s*undef\s+(\w+)', masked))
    names = [m[1] or m[3] for m in definitions]
    unstable.update(name for name in names if names.count(name) != 1)
    declarations = []
    for match in definitions:
        name, expr = match[1] or match[3], match[2] or match[4]
        in_main = bool(main_range and main_range[0] < match.start() < main_range[1] and depths[match.start()] == 1)
        prefix = masked[max(masked.rfind(';', 0, match.start()), masked.rfind('{', 0, match.start()), masked.rfind('}', 0, match.start())) + 1:match.start()]
        local_allowed = in_main and not prefix.strip() and not match[1]
        if ((depths[match.start()] and not local_allowed) or any(a <= match.start() < b for a, b in conditionals)):
            unstable.add(name)
        else:
            declarations.append((match.end(), name, expr, main_range if local_allowed else None))
    # Reject any other declaration of a resolved name, including parameters
    # and different scalar types. This deliberately over-rejects shadowing.
    for _, name, _, _ in declarations:
        for declaration in re.finditer(r'\b(?:[A-Za-z_]\w*\s+)+' + re.escape(name) + r'\s*(?=[=,;):])', masked):
            if not any(m.start() <= declaration.start() < m.end() for m in definitions): unstable.add(name)
        if re.search(r'\b(?:[A-Za-z_]\w*\s+)+(?:\(\s*)?[*&]+\s*' + re.escape(name) + r'\s*(?=[=,;):])', masked): unstable.add(name)
        for binding in re.finditer(r'\bauto\s*[&*]?\s*\[([^\]]+)\]', masked):
            if re.search(r'\b' + re.escape(name) + r'\b', binding[1]): unstable.add(name)
        for assignment in re.finditer(r'\b' + re.escape(name) + r'\s*=(?!=)', masked):
            if not any(m.start() <= assignment.start() < m.end() for m in definitions): unstable.add(name)
    result = []
    for match in matches:
        constants = {}
        for end, name, expr, scope in declarations:
            if end > match.start(): break
            allowed = not scope or scope[0] < match.start() < scope[1]
            value = integer_expression(expr, cpp=True, constants=constants) if allowed and name not in unstable else None
            if value is None: constants.pop(name, None)
            else: constants[name] = value
        close = closing(source, match.end() - 1)
        line = str(source.count('\n', 0, match.start()) + 1)
        inferred = assignment_name(masked, match.start())
        if close is None:
            result.append(Bound(inferred or '', None, None, source[match.start():match.start() + 512], line, match.start(), 'unsupported_call'))
            continue
        args = arguments(source[match.end():close])
        vector = match[1] in {'readInts', 'readLongs'}
        name_index = 3 if vector else 2
        name = None
        if len(args) > name_index and re.fullmatch(r'"[A-Za-z][A-Za-z0-9_]*"', args[name_index]):
            name = variable(args[name_index][1:-1])
        elif len(args) >= name_index:
            name = inferred
        allowed_args = len(args) in ({3, 4, 5} if vector else {2, 3})
        if vector and len(args) == 5 and integer_expression(args[4], cpp=True, constants=constants) is None: allowed_args = False
        endpoints = args[1:3] if vector else args[:2]
        low = integer_expression(endpoints[0], cpp=True, constants=constants) if len(endpoints) == 2 else None
        high = integer_expression(endpoints[1], cpp=True, constants=constants) if len(endpoints) == 2 else None
        reason = ''
        prefix = masked[max(masked.rfind(';', 0, match.start()), masked.rfind('{', 0, match.start())) + 1:match.start()]
        if not allowed_args: reason = 'unsupported_call'
        elif not name: reason = 'missing_name'
        elif re.search(r'\?|&&|\|\|', prefix) or any(a <= match.start() < b for a, b in conditionals): reason = 'conditional_validator'
        elif low is None or high is None: reason = 'symbolic_validator_bound'
        elif low > high: reason = 'invalid_range'
        result.append(Bound(name or inferred or '', low, high, source[match.start():close + 1], line, match.start(), reason, vector, 'label' if name and len(args) > name_index and re.fullmatch(r'"[A-Za-z][A-Za-z0-9_]*"', args[name_index]) else 'assignment'))
    if notices:
        result = [replace(entry, reason_code='analysis_truncated') for entry in result]
    return source, result, notices


def validator_bounds(source):
    source, entries, _ = parse_validator(source)
    return source, [entry for entry in entries if not entry.reason_code], {entry.name for entry in entries if entry.reason_code and entry.name}
