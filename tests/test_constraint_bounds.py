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
