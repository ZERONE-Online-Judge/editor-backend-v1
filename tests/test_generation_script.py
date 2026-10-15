import pytest
from fastapi import HTTPException
from app.domain import parse_script, expanded_tests


def test_loop_expansion_preserves_quoted_arguments_groups_and_occupied_indices():
    rows = parse_script('repeat seed in 1..3\n group small\n gen "two words" {seed} > $\nend\ngen 99 > $', [1])
    assert rows == [
        {'index':2,'command':"gen 'two words' 1",'group':'small'},
        {'index':3,'command':"gen 'two words' 2",'group':'small'},
        {'index':4,'command':"gen 'two words' 3",'group':'small'},
        {'index':5,'command':'gen 99'}]


def test_nested_ranges_can_address_explicit_numbers_and_descending_values():
    rows = parse_script('repeat i in 5..1 step -2\nrepeat seed in 10..11\ngen {i} {seed} > $\nend\nend')
    assert [row['command'] for row in rows] == ['gen 5 10','gen 5 11','gen 3 10','gen 3 11','gen 1 10','gen 1 11']
    assert parse_script('repeat i in 1..2\ngen {i} > {i}\nend')[-1]['index'] == 2


@pytest.mark.parametrize('script',[
    'repeat i in 1..1001\ngen {i} > $\nend',
    'repeat i in 1..1000\nrepeat j in 1..1000\ngen {j} > $\nend\nend',
    'repeat i in 1..3 step 0\nend', 'repeat i in 3..1\nend',
    'repeat i in 1..2\ngen {x} > $\nend', 'repeat i in 1..2\ngen {i} > $',
    'end', 'gen 1 | cat > $', 'repeat i in 1..2\ngen {i} > 1\nend',
])
def test_invalid_or_unbounded_scripts_are_rejected(script):
    with pytest.raises(HTTPException) as error: parse_script(script)
    assert error.value.detail['code'] == 'invalid_script'


def test_script_groups_must_exist_and_be_enabled():
    config={'tests':{},'script':'group missing\ngen 1 > $','groups':{},'groups_enabled':False}
    with pytest.raises(HTTPException) as error: expanded_tests(config)
    assert error.value.detail['code']=='invalid_script_group'
