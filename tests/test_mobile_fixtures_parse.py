"""Every Swift and Kotlin fixture of the analyzer tests is valid source.

A rule's control is only as good as its fixture: a fixture the compiler
would reject tests a shape no real file has. The fixtures are parsed with the
tree-sitter grammars (independent of the token scanner under test). The
grammars are not a dependency of the package; CI's "Swift and Kotlin
fixtures" job installs them and requires this file not to skip. Locally, the
Swift fixtures were also accepted by `swiftc -parse` (Swift 6.4).
"""

import pytest

from tests import test_kotlin_analyzer as kotlin_tests
from tests import test_swift_analyzer as swift_tests

UNREADABLE = {"UNTERMINATED"}  # the read-failure controls, invalid by design


def _parser(module_name):
    ts = pytest.importorskip("tree_sitter")
    grammar = pytest.importorskip(module_name)
    return ts.Parser(ts.Language(grammar.language()))


def _swift_fixtures():
    out = {n: getattr(swift_tests, n) for n in ("POS", "NEG", "INITS", "DEBUG", "TESTS")}
    out.update({f"NEG_CASES[{k}]": v for k, v in swift_tests.NEG_CASES.items()})
    return out


def _kotlin_fixtures():
    out = {n: getattr(kotlin_tests, n) for n in ("POS", "DEBUG", "TESTS")}
    out.update(
        {f"NEG_CASES[{k}]": kotlin_tests.ANDROID + v for k, v in kotlin_tests.NEG_CASES.items()}
    )
    return out


@pytest.mark.parametrize("name", sorted(_swift_fixtures()))
def test_every_swift_fixture_parses(name):
    tree = _parser("tree_sitter_swift").parse(_swift_fixtures()[name].encode("utf-8"))
    assert not tree.root_node.has_error, name


@pytest.mark.parametrize("name", sorted(_kotlin_fixtures()))
def test_every_kotlin_fixture_parses(name):
    tree = _parser("tree_sitter_kotlin").parse(_kotlin_fixtures()[name].encode("utf-8"))
    assert not tree.root_node.has_error, name


def test_the_read_failure_controls_do_not_parse():
    swift = _parser("tree_sitter_swift").parse(swift_tests.UNTERMINATED.encode("utf-8"))
    kotlin = _parser("tree_sitter_kotlin").parse(kotlin_tests.UNTERMINATED.encode("utf-8"))
    assert swift.root_node.has_error and kotlin.root_node.has_error
