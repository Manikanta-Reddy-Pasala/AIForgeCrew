"""Expected values written in plain double quotes count, and a quoted value
is held only as a whole string.

Live case (real OrderService job): 'They should read "1 year", "2 years"'
gave no examples (backticks only), and the turn's test of the WRONG labels
("1 - 1 year") would have counted as holding "1 year" anyway (quotes and
spaces were stripped before matching).
"""
from aiforge_core.runtime.chat_agent._turn import _example_gate as E

REQ = ('When the buckets are one unit wide the labels come out as "1 - 1 years", '
       '"2 - 2 years". They should read "1 year", "2 years", "3 years". '
       'Statements "  501 " and "501" are treated as two accounts; customers read as '
       'having no row get the "Unspecified" treatment. Fix it and cover it with a test.')


def test_plain_quoted_expected_values_only():
    assert E.examples(REQ, expected_only=True) == ['"1 year"', '"2 years"', '"3 years"']


def test_quoted_value_held_only_as_whole_literal():
    wrong = 'assertEquals(List.of("1 - 1 year", "> 1 year"), labels);'
    right = "assertEquals(List.of('1 year', \"> 1 year\"), labels);"
    for txt, want in ((wrong, False), (right, True)):
        assert E._held('"1 year"', E._norm(txt), E._loose(txt), txt) is want


def test_backticked_values_unchanged():
    assert E.examples("It should return `\"abcd\"` -> fine", expected_only=True) == ['"abcd"']
