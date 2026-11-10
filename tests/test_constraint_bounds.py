import pytest
from app.constraint_bounds import integer_expression, statement_bounds, validator_bounds
from app.schema import empty_document


@pytest.mark.parametrize('text,expected',[
    (r'1\,000',1000),(r'1{,}000',1000),('1,000',1000),('1000',1000),
    (r'10^{3}',1000),(r'10^3',1000),(r'10 ^ { 3 }',1000),
    (r'2 \cdot 10^3',2000),(r'2 \times 10^{3}',2000),
    (r'\frac{2000}{2}',1000),('1e3',1000),('10^6+7',1000007),
    ('-10^3',-1000),(r'\left(10^3 + 1\right)',1001),
])
def test_whole_integer_expression(text,expected):
    assert integer_expression(text)==expected


@pytest.mark.parametrize('text',[r'1\unknown000',r'1\,000 + K','1000*N','1000abc',
    '1,00','1e999999','10^999999','2^10000','1/3', '__import__("os").system("false")',
    '1000 + f(2)', '1 << 1000000', '[1000]', '1000,2000'])
def test_unknown_or_unbounded_expression_is_never_a_numeric_prefix(text):
    assert integer_expression(text) is None


def doc_for(text):
    doc=empty_document('Bounds');doc['statements']['korean']['input']=text;return doc


@pytest.mark.parametrize('upper',[r'1\,000',r'1{,}000',r'10^{3}',r'10^3','1,000','1e3'])
def test_latex_upper_bounds_are_not_truncated_to_one(upper):
    rows,skipped=statement_bounds(doc_for(r'$1 \le K \le '+upper+r'$; $0 \le A_{i} \le '+upper+'$'))
    assert {(r.name,r.low,r.high) for r in rows}=={('K',1,1000),('A_i',0,1000)}
    assert not skipped


@pytest.mark.parametrize('text,expected',[
    (r'$0 < N < 1001$',(1,1000)), (r'$1001 > N > 0$',(1,1000)),
    (r'$1 \leqslant N \leqslant 10^3$',(1,1000)),
    ('$1$ ≤ $N$ ≤ $1,000$',(1,1000)),
    (r'$\left(1 \le N \le 1000\right)$',(1,1000)),
])
def test_strict_reversed_and_split_math_ranges(text,expected):
    rows,_=statement_bounds(doc_for(text))
    assert len(rows)==1
    assert (rows[0].low,rows[0].high)==expected


def test_multiple_ranges_and_shared_bounds_keep_variable_identity():
    rows,_=statement_bounds(doc_for(r'$1 \le N, M \le 10^3; 0 \le A_i \le 1000$'))
    assert {(r.name,r.low,r.high) for r in rows}=={('N',1,1000),('M',1,1000),('A_i',0,1000)}
    rows,_=statement_bounds(doc_for(r'$1 \le K \le 1000, 0 \le A_i \le 1000$'))
    assert len(rows)==2


def test_formulas_and_code_examples_are_not_misreported_as_limits():
    rows,skipped=statement_bounds(doc_for('```cpp\n1 <= K <= 2\n```\n`1 <= X <= 2`\n'+r'$1 \le K \le 1000+N$'))
    assert not rows and skipped=={'K'}


def test_validator_constant_expressions_and_cpp_operators_keep_their_meaning():
    source='''#define MAX_K 1000
const long long MAX_A = 2 * MAX_K / 2;
int main(){registerValidation(argc,argv);
inf.readInt(1, MAX_K, "K");
inf.readLong(0, MAX_A, "A_i");
inf.readInt(0, (1 << 10)-1, "N");
inf.readInt(0, 10^3, "X");}'''
    _,rows,skipped=validator_bounds(source)
    assert {(r.name,r.low,r.high) for r in rows}=={('K',1,1000),('A_i',0,1000),('N',0,1023),('X',0,9)}
    assert not skipped


def test_comments_strings_conditionals_and_unknown_bounds_do_not_produce_comparisons():
    source='''// inf.readInt(1, 7, "X");
const char* example="inf.readInt(1, 7, \\"X\\");";
int main(){registerValidation(argc,argv);
if(flag){inf.readInt(1, 10, "N");} else inf.readInt(1, 1000, "N");
inf.readInt(1, dynamic, "K");
for(int i=0;i<n;i++){ inf.readInt(0, 1000, "A_i"); }
}'''
    _,rows,skipped=validator_bounds(source)
    assert {(r.name,r.low,r.high) for r in rows}=={('A_i',0,1000)}
    assert skipped=={'N','K'}


def test_preprocessor_branches_do_not_resolve_conditional_constants():
    _,rows,skipped=validator_bounds('#if MODE\n#define MAX 1000\n#endif\ninf.readInt(1,MAX,"N");')
    assert not rows and skipped=={'N'}


def test_shadowed_and_conditional_constants_cannot_create_false_mismatches():
    for source in [
        'const int MAX=1000; void helper(){const int MAX=1;} int main(){inf.readInt(1,MAX,"N");}',
        '#define MAX 1000\n#if MODE\n#define MAX 1\n#endif\ninf.readInt(1,MAX,"N");',
        '#define MAX 1000\n#undef MAX\ninf.readInt(1,MAX,"N");',
    ]:
        _,rows,skipped=validator_bounds(source)
        assert not rows and skipped=={'N'}


def test_local_parameters_or_other_scalar_types_cannot_shadow_a_resolved_constant():
    for source in [
        'const int MAX=1000; void helper(double MAX){inf.readInt(1,MAX,"N");}',
        'const int MAX=1000; int main(){double MAX=1;inf.readInt(1,MAX,"N");}',
    ]:
        _,rows,skipped=validator_bounds(source)
        assert not rows and skipped=={'N'}


def test_subtask_constraints_and_output_ranges_are_not_global_input_limits():
    doc=doc_for(r'50%의 테스트에서 $1 \le N \le 10$')
    rows,skipped=statement_bounds(doc)
    assert skipped=={'N'}
    doc=empty_document('Bounds');doc['statements']['korean']['output']=r'$1 \le N \le 10$'
    assert not statement_bounds(doc)[0]


@pytest.mark.parametrize('text', [
    '$1<N<1001$', '정수 N (1 ≤ N ≤ 1000)이 주어진다.',
    '<ul><li>1 &le; N &le; 1000</li></ul>',
    '<p>1 ≤ <i>N</i> ≤ 10<sup>3</sup></p>',
    '1. (1 ≤ N ≤ 1000)', '* 1 ≤ N ≤ 1000',
])
def test_plain_prose_html_lists_and_compact_math_keep_complete_endpoints(text):
    rows, skipped = statement_bounds(doc_for(text))
    assert [(row.name, row.low, row.high) for row in rows] == [('N', 1 if '<N<' not in text else 2, 1000)]
    assert not skipped


@pytest.mark.parametrize('text', [
    '1 ≤ N ≤ 100만', '1 ≤ N ≤ 100의 제곱', '1 ≤ N ≤ 100배', '1 ≤ N ≤ 100억',
    '<p>0 ≤ N ≤ 100√2</p>', '0 ≤ N ≤ 100∞', '√2 ≤ N ≤ 100',
    '0 ≤ N ≤ 1000 + K', '0 ≤ N ≤ 1000K', '(0 ≤ N ≤ 1000)+K',
    '부분 문제\n1 ≤ N ≤ 10', '$0 ≤ N ≤ 1000\n + K$',
    '### Subtask 1\n\n$1≤N≤10$', '<h3>부분문제 1</h3><p>1 ≤ N ≤ 10</p>',
    '1 ≤ K ≤ N ≤ 100', '1 ≤ P < Q ≤ 100', '- 1 ≤ N ≤ 100',
])
def test_prose_parser_never_cuts_a_symbolic_or_conditional_endpoint_to_numeric_prefix(text):
    rows, skipped = statement_bounds(doc_for(text))
    assert skipped
    assert not rows


@pytest.mark.parametrize('call,name,expected', [
    ('int n = inf.readInt(1, 100);', 'n', (1, 100)),
    ('long long n = inf.readLong(1, 100);', 'n', (1, 100)),
    ('int n = inf.readInteger(1, 100, "n");', 'n', (1, 100)),
    ('int n = inf.readInt(1, 100, "N must be in range");', 'n', (1, 100)),
    ('auto a = inf.readInts(n, 0, 100);', 'a', (0, 100)),
    ('inf.readLongs(n, 0LL, 100LL, "A", 1);', 'A', (0, 100)),
    ('int n=inf.readInt(1,1\'000,"n");', 'n', (1, 1000)),
    ('int n=inf.readInt(1\'000,2\'000,"n");', 'n', (1000, 2000)),
])
def test_common_scalar_and_array_calls_with_proven_names(call, name, expected):
    _, rows, skipped = validator_bounds('int main(){registerValidation(argc,argv);' + call + '}')
    assert len(rows) == 1 and rows[0].name == name and (rows[0].low, rows[0].high) == expected
    assert rows[0].vector == ('readInts' in call or 'readLongs' in call)
    assert not skipped


def test_cpp_digit_separators_and_direct_main_constants_are_safe_integer_expressions():
    assert integer_expression("1'000'000'000'000'000'000LL", cpp=True) == 10**18
    source = "int main(){registerValidation(argc,argv); const long long MX=1'000'000'000'000'000'000LL; long long p=inf.readLong(1LL,MX-500,\"P\");}"
    _, rows, skipped = validator_bounds(source)
    assert [(row.name, row.low, row.high) for row in rows] == [('P', 1, 10**18 - 500)]
    assert not skipped


@pytest.mark.parametrize('source', [
    'int main(){if(flag){const int MAX=100;} inf.readInt(1,MAX,"N");}',
    'int main(){const int MAX=100; {const int MAX=10;} inf.readInt(1,MAX,"N");}',
    'int main(){const int MAX=100;} void helper(){inf.readInt(1,MAX,"N");}',
    'int main(){flag && inf.readInt(1,100,"N");}',
    'int main(){flag || inf.readInt(1,100,"N");}',
    'int main(){flag ? inf.readInt(1,100,"N") : 0;}',
    'const int MAX=100; int main(){for(int MAX : {200,300}) inf.readInt(1,MAX,"N");}',
    'const int MAX=100; int main(){auto [MAX,x]=std::pair<int,int>{200,0};inf.readInt(1,MAX,"N");}',
    'const int MAX=100; void helper(int &MAX){inf.readInt(1,MAX,"N");}',
])
def test_conditional_calls_and_nonvisible_or_shadowed_main_constants_stay_unresolved(source):
    _, rows, skipped = validator_bounds(source)
    assert not rows and skipped == {'N'}


def test_bare_diagnostic_labels_and_indexed_lhs_are_not_guessed():
    _, rows, _ = validator_bounds('inf.readInt(1,100,"N must be valid"); a[i]=inf.readInt(1,100);')
    assert not rows
