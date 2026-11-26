"""Kotlin adapter v1: TODO(), not-implemented throws, empty catch blocks,
printStackTrace(), debug prints in Android code and markers.

Each rule has the code shape it reports and the shapes it leaves alone:
calls in strings, templates, char literals and comments are not calls; test
code, a catch parameter that declares the exception expected, prints outside
Android code and inside if (BuildConfig.DEBUG) are not reported; a file the
scanner cannot read fails, it is never a file with no findings. Every fixture
is valid Kotlin (the tree-sitter Kotlin grammar parses it without an error),
except UNTERMINATED.
"""

import pytest

from slop_detector.languages import kotlin_analyzer, token_rules
from slop_detector.languages.token_scan import KotlinReadError

POS = """package com.example.app

import android.util.Log

class Repository {
    fun load(id: Int): String = TODO("Not yet implemented")

    fun save() {
        throw NotImplementedError()
    }

    fun sync(): Unit = throw UnsupportedOperationException("Not implemented yet")

    fun parse(text: String): Int = error("parse() not implemented")

    fun read() {
        try {
            work()
        } catch (e: Exception) {
        }
    }

    fun write() {
        try {
            work()
        } catch (e: IOException) {
            e.printStackTrace()
        }
    }

    fun show(value: Int) {
        println("value $value")
        System.out.println(value)
    }

    // TODO: support paging
    /* FIXME handle the empty state */
    fun two() { kotlin.TODO(); print(2) }

    private fun work() {}
}
"""
POS_EXPECTED = {
    ("kotlin_todo_call", 6),
    ("kotlin_not_implemented", 9),
    ("kotlin_not_implemented", 12),
    ("kotlin_not_implemented", 14),
    ("kotlin_empty_catch", 19),
    ("kotlin_print_stack_trace", 27),
    ("kotlin_debug_print", 32),
    ("kotlin_debug_print", 33),
    ("kotlin_todo_comment", 36),
    ("kotlin_todo_comment", 37),
    ("kotlin_todo_call", 38),
    ("kotlin_debug_print", 38),
}
SEVERITY = {
    "kotlin_todo_call": "high",
    "kotlin_not_implemented": "high",
    "kotlin_empty_catch": "medium",
    "kotlin_print_stack_trace": "medium",
    "kotlin_debug_print": "medium",
    "kotlin_todo_comment": "low",
}

# Each shape alone, so that one guard cannot stand in for another.
NEG_CASES = {
    "string": 'val a = "TODO() println(1) error(\\"not implemented\\")"\n',
    "template": 'val b = "${map["TODO()"]} $name println(1)"\n',
    "raw string": 'val c = """\n    TODO()\n    println("x")\n""""\n',
    "char": "val d = '\\'' ; val e = '\"'\nval f = TODO\n",
    "line comment": "// call println(1) and TODO() here: commented-out code\nval x = 1\n",
    "nested comment": "/* a /* TODO() */ println(1) */\nval x = 1\n",
    "backtick name": "fun `TODO in a name`() {}\n",
    "member TODO": "fun f(t: Tasks) { t.TODO(); t?.TODO() }\n",
    "declared TODO": "fun TODO(reason: String) {}\n",
    "error without marker": 'fun f(): Nothing = error("Unknown state")\n',
    "throw without marker": 'fun f(): Nothing = throw IllegalStateException("Closed")\n',
    "rethrow": "fun f(e: Exception): Nothing = throw e\n",
    "expected discard": (
        "fun f() {\n"
        "    try { g() } catch (_: IOException) {}\n"
        "    try { g() } catch (ignored: IOException) {}\n"
        "    try { g() } catch (expected: TimeoutException) {}\n"
        "}\n"
    ),
    "handled catch": "fun f() { try { g() } catch (e: IOException) { log(e) } }\n",
    "flow catch": "val f = flow.catch { emit(0) }\n",
    "stack trace of a variable": "fun f(t: Trace) { t.printStackTrace(System.out) }\n",
    "member print": "fun f(w: java.io.PrintWriter) { w.println(1); w.print(2) }\n",
    "prose": "/** A Todo list is prose */\n// See the TODOs file\nval x = 1\n",
}

ANDROID = "import android.app.Activity\n\n"

DEBUG = (
    ANDROID
    + """fun a() {
    if (BuildConfig.DEBUG) {
        println("debug only")
    }
    if (com.example.BuildConfig.DEBUG && verbose) println("debug only")
    if (BuildConfig.DEBUG || forced) {
        println("may be a release build")
    }
    println("release")
}
"""
)

TESTS = """package com.example

import org.junit.Test
import kotlin.test.assertEquals

class RepositoryTest {
    @Test fun loads() {
        println("in a test")
        TODO("write the assertion")
    }
}
"""

UNTERMINATED = 'fun f() {\n    val s = "never closed\n    TODO()\n}\n'


def _analyze(text, tmp_path, name="Repository.kt"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return kotlin_analyzer.KotlinAnalyzer().analyze(str(path))


def _found(result):
    return {(i.pattern_id, i.line) for i in result.issues}


def test_each_rule_is_reported_with_its_severity_and_fix(tmp_path):
    result = _analyze(POS, tmp_path)
    assert _found(result) == POS_EXPECTED
    for issue in result.issues:
        assert issue.severity == SEVERITY[issue.pattern_id], issue
        assert issue.suggestion == kotlin_analyzer.RULES[issue.pattern_id][2], issue
    assert result.ast_mode is False and result.test_code is False


def test_the_not_implemented_message_is_quoted_in_the_finding(tmp_path):
    messages = {i.line: i.message for i in _analyze(POS, tmp_path).issues}
    assert messages[9].startswith("throw NotImplementedError()")
    assert 'UnsupportedOperationException("Not implemented yet")' in messages[12]
    assert 'error("parse() not implemented")' in messages[14]


def test_two_findings_on_one_line_keep_their_columns(tmp_path):
    result = _analyze(POS, tmp_path)
    line = POS.splitlines()[37]
    on_38 = sorted((i.pattern_id, i.column) for i in result.issues if i.line == 38)
    assert on_38 == [
        ("kotlin_debug_print", line.index("print(2)")),
        ("kotlin_todo_call", line.index("kotlin.TODO")),
    ]


@pytest.mark.parametrize("case", sorted(NEG_CASES))
def test_each_shape_that_is_not_a_finding(case, tmp_path):
    result = _analyze(ANDROID + NEG_CASES[case], tmp_path)
    assert result.issues == [], [(i.pattern_id, i.line) for i in result.issues]


def test_a_print_outside_android_code_is_program_output(tmp_path):
    cli = 'fun usage() { println("usage: tool <file>") }\nfun f(): Int = TODO()\n'
    assert {i.pattern_id for i in _analyze(cli, tmp_path, "Cli.kt").issues} == {"kotlin_todo_call"}
    main = ANDROID + 'fun main() { println("desktop") }\n'
    assert _analyze(main, tmp_path, "main.kt").issues == []
    script = ANDROID + 'println("configuring")\nval v: Int = TODO()\n'
    found = {i.pattern_id for i in _analyze(script, tmp_path, "build.gradle.kts").issues}
    assert found == {"kotlin_todo_call"}
    app = _analyze(ANDROID + 'fun f() { println("x") }\n', tmp_path, "Screen.kt")
    assert {i.pattern_id for i in app.issues} == {"kotlin_debug_print"}


def test_debug_only_prints_are_not_reported(tmp_path):
    result = _analyze(DEBUG, tmp_path)
    assert _found(result) == {("kotlin_debug_print", 9), ("kotlin_debug_print", 11)}


# Written out, not read from the analyzer: a package dropped from its list
# must fail here, not drop its case.
@pytest.mark.parametrize(
    "package",
    [
        "org.junit",
        "org.junit.jupiter.api",
        "junit.framework",
        "kotlin.test",
        "io.kotest.core.spec.style",
        "androidx.test.ext.junit",
        "androidx.compose.ui.test",
        "org.robolectric",
        "io.mockk",
        "org.mockito.kotlin",
        "app.cash.turbine",
        "kotlinx.coroutines.test",
        "org.spekframework.spek2",
    ],
)
def test_each_test_framework_alone_makes_a_test_file(package, tmp_path):
    text = f"import {package}.Thing\nimport android.util.Log\n\nfun f(): Int = TODO()\n"
    result = _analyze(text, tmp_path, "T.kt")
    assert result.issues == [] and result.test_code is True


def test_a_test_file_reports_nothing(tmp_path):
    result = _analyze(TESTS, tmp_path, "RepositoryTest.kt")
    assert result.issues == [] and result.test_code is True


def test_markers_lead_a_comment_line(tmp_path):
    text = (
        "// TODO: a marker\n"
        "// todo: lower case with a colon\n"
        '// TODO("Not yet implemented") written as a marker\n'
        "/**\n * HACK inside KDoc\n */\n"
        "val x = 1 // XXX trailing\n"
    )
    result = _analyze(text, tmp_path)
    assert [(i.line, i.column) for i in result.issues] == [(1, 3), (2, 3), (3, 3), (5, 3), (7, 13)]


def test_an_empty_catch_with_a_comment_is_still_empty(tmp_path):
    text = (
        "fun f() {\n"
        "    try { g() } catch (e: IOException) {\n        // ignore\n    }\n"
        "    try { g() } catch (e: IOException) { /* won't happen */ }\n"
        "}\n"
    )
    result = _analyze(text, tmp_path)
    assert _found(result) == {("kotlin_empty_catch", 2), ("kotlin_empty_catch", 5)}


def test_a_file_that_cannot_be_read_fails(tmp_path):
    with pytest.raises(KotlinReadError, match="Kotlin: unterminated string at line 2"):
        _analyze(UNTERMINATED, tmp_path)


def test_the_score_counts_findings_per_100_lines(tmp_path):
    body = ANDROID + "fun f() {\n" + "    val a = 1\n" * 95 + "    NAME\n}\n"
    scores = {}
    for name, call in (
        ("stub", "TODO()"),
        ("throw", "throw NotImplementedError()"),
        ("catch", "try { g() } catch (e: Exception) {}"),
        ("trace", "e.printStackTrace()"),
        ("print", "println(1)"),
        ("marker", "// TODO: later"),
    ):
        result = _analyze(body.replace("NAME", call), tmp_path, f"{name}.kt")
        assert result.total_lines == 100, result.total_lines
        scores[name] = result.slop_score
    assert scores == {
        "stub": token_rules.STUB_WEIGHT,
        "throw": token_rules.STUB_WEIGHT,
        "catch": token_rules.SWALLOW_WEIGHT,
        "trace": token_rules.DEBUG_WEIGHT,
        "print": token_rules.DEBUG_WEIGHT,
        "marker": 0.0,  # reported, not scored
    }


def test_a_finding_is_located_in_characters(tmp_path):
    text = 'val s = "é"; val t: Int = TODO()\n'  # e-acute: 2 bytes, 1 character
    result = _analyze(text, tmp_path)
    assert [(i.line, i.column) for i in result.issues] == [(1, text.index("TODO"))]


# --- project level -----------------------------------------------------------


def _project(tmp_path, files, include_tests=False):
    from slop_detector.core import SlopDetector
    from slop_detector.service.policy import apply_runtime_overrides

    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    detector = SlopDetector(read_only=True)
    apply_runtime_overrides(detector, include_tests=include_tests)
    return detector.analyze_project(str(tmp_path))


def _rel(results, root):
    return sorted(str(r.file_path).replace("\\", "/")[len(str(root)) + 1 :] for r in results)


STUB = "fun f(): Int = TODO()\n"
GRADLE = {
    "app/src/main/java/com/example/App.kt": STUB,
    "app/build.gradle.kts": 'plugins { id("com.android.application") }\n',
    "app/build/generated/ksp/debug/kotlin/Gen.kt": STUB,
    "app/src/test/java/com/example/AppTest.kt": STUB,
    "app/src/testDebug/java/com/example/DebugTest.kt": STUB,
    "app/src/androidTest/java/com/example/UiTest.kt": STUB,
    "shared/src/commonTest/kotlin/SharedTest.kt": STUB,
    "shared/src/androidUnitTest/kotlin/UnitTest.kt": STUB,
    "core/testing/src/main/kotlin/com/example/testing/FakeRepo.kt": STUB,
    "core/data-test/src/main/kotlin/FakeData.kt": STUB,
    "libraries/matrix/test/src/main/kotlin/FakeClient.kt": STUB,
    "libraries/test-utils/src/main/kotlin/Rules.kt": STUB,
    "feature/abtest/src/main/kotlin/com/example/abtest/Experiment.kt": "val x = 1\n",
}


def test_gradle_build_output_and_test_source_sets_are_excluded(tmp_path):
    result = _project(tmp_path, GRADLE)
    assert _rel(result.kotlin_file_results, tmp_path) == [
        "app/build.gradle.kts",
        "app/src/main/java/com/example/App.kt",
        "feature/abtest/src/main/kotlin/com/example/abtest/Experiment.kt",
    ]
    excluded = {e["path"] for e in result.scan_coverage["excluded"]["files"]}
    assert excluded == set(GRADLE) - {
        "app/src/main/java/com/example/App.kt",
        "app/build.gradle.kts",
        "feature/abtest/src/main/kotlin/com/example/abtest/Experiment.kt",
    }
    assert result.scan_coverage["excluded"]["by_source"]["custom"] == 0
    assert all(
        f["extension"] not in (".kt", ".kts") for f in result.scan_coverage["unsupported"]["files"]
    )

    included = _project(tmp_path, {}, include_tests=True)
    # --include-tests analyzes the test sources; Gradle's build/ stays excluded.
    assert len(included.kotlin_file_results) == len(GRADLE) - 1


def test_a_kotlin_file_that_cannot_be_read_is_a_failed_file(tmp_path):
    result = _project(
        tmp_path, {"src/main/kotlin/A.kt": STUB, "src/main/kotlin/Bad.kt": UNTERMINATED}
    )
    failed = result.scan_coverage["failed"]
    assert failed["by_language"] == {"kotlin": 1}, failed
    assert failed["files"] == [
        {"path": "src/main/kotlin/Bad.kt", "language": "kotlin", "error_type": "KotlinReadError"}
    ]


def test_a_mixed_project_reports_kotlin_everywhere(tmp_path):
    from slop_detector.renderer_markdown import generate_markdown_report
    from slop_detector.service.policy import evaluate_gate

    result = _project(
        tmp_path,
        {
            "ios/App/Feature.swift": "func f() {}\n",
            "android/app/src/main/java/App.kt": STUB * 3,
        },
    )
    analyzed = result.scan_coverage["analyzed"]
    assert (analyzed["swift"], analyzed["kotlin"], analyzed["total"]) == (1, 1, 2), analyzed
    assert "kotlin_todo_call" in generate_markdown_report(result)
    gate = evaluate_gate(result, mode="hard", claims_strict=False, read_only=False)
    assert any(p.replace("\\", "/").endswith("App.kt") for p in gate.failed_files)
