"""Swift adapter v1: stubs, empty catch blocks, debug prints and markers.

Each rule has the code shape it reports and the shapes it leaves alone:
calls in strings, comments and regex literals are not calls; test code,
initializers a class does not support, #if DEBUG prints and the output of a
command-line program are not reported; a file the scanner cannot read fails,
it is never a file with no findings. Every fixture is valid Swift
(`swiftc -parse` accepts it), except UNTERMINATED.
"""

import pytest

from slop_detector.languages import swift_analyzer, token_rules
from slop_detector.languages.token_scan import SwiftReadError

POS = """import Foundation

func load(id: Int) -> String {
    fatalError("Not implemented")
}

func save() {
    preconditionFailure("TODO: write to disk")
}

func parse() throws {
    do {
        try work()
    } catch {}
}

func show(_ value: Int) {
    print("value", value)
    debugPrint(value)
}

// TODO: support paging
/* FIXME handle the empty state */
func two() { Swift.print(1); dump(2) }
func work() throws {}
"""
POS_EXPECTED = {
    ("swift_fatal_error_stub", 4),
    ("swift_fatal_error_stub", 8),
    ("swift_empty_catch", 14),
    ("swift_debug_print", 18),
    ("swift_debug_print", 19),
    ("swift_todo_comment", 22),
    ("swift_todo_comment", 23),
    ("swift_debug_print", 24),
}
SEVERITY = {
    "swift_fatal_error_stub": "high",
    "swift_empty_catch": "medium",
    "swift_debug_print": "medium",
    "swift_todo_comment": "low",
}

NEG = """import Foundation

let a = "print(x) and fatalError(\\"Not implemented\\") in a string"
let b = #"raw "print(1)" fatalError("TODO")"#
let c = \"\"\"
    print("multi-line")
    fatalError("Not implemented")
    \"\"\"
let d = "interpolated \\(String(describing: "print(1)"))"
// print("in a comment") and fatalError("TODO")
/* outer /* nested print(1) */ still a comment */
func invariant(_ ok: Bool) {
    if !ok { fatalError("Index out of range") }
    guard ok else { preconditionFailure() }
}
struct Logger {
    func print(_ text: String) {}
    func run() { self.print("member"); print("own method") }
}
enum Command { case print(String) }
func route(_ command: Command) {
    if case .print(let text) = command { _ = text }
}
func pass(print: (String) -> Void) { print("a closure parameter") }
func safe() {
    do { try risky() } catch { NSLog("%@", "\\(error)") }
    do { try risky() } catch _ {}
    let x = try? risky()
    _ = x
}
func risky() throws {}
/// Notes about a Todo list are prose, not a marker
// MARK: - TODO is not leading
"""

INITS = """import UIKit

final class Card: UIView {
    init() { super.init(frame: .zero) }
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }
}

class Base {
    init(name: String) {}
}

final class Child: Base {
    init() { super.init(name: "child") }
    override init(name: String) {
        fatalError("init(name:) has not been implemented")
    }
    func later() {
        fatalError("load() has not been implemented")
    }
}
"""

DEBUG = """import Foundation

func a() {
    #if DEBUG
    print("debug only")
    #else
    print("release")
    #endif
}

func b() {
    #if !DEBUG
    print("release")
    #else
    print("debug only")
    #endif
}

func c() {
    #if DEBUG && (os(iOS) || os(visionOS))
    print("debug only")
    #endif
    #if DEBUG || targetEnvironment(simulator)
    print("may be a release build")
    #endif
}
"""

TESTS = """import XCTest
@testable import App

final class AppTests: XCTestCase {
    func testLoad() {
        print("in a test")
        fatalError("Not implemented")
    }
}
"""

UNTERMINATED = 'func f() {\n    let s = "never closed\n    fatalError("TODO")\n}\n'


def _analyze(text, tmp_path, name="Feature.swift"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return swift_analyzer.SwiftAnalyzer().analyze(str(path))


def _found(result):
    return {(i.pattern_id, i.line) for i in result.issues}


def test_each_rule_is_reported_with_its_severity_and_fix(tmp_path):
    result = _analyze(POS, tmp_path)
    assert _found(result) == POS_EXPECTED
    for issue in result.issues:
        assert issue.severity == SEVERITY[issue.pattern_id], issue
        assert issue.suggestion, issue  # every finding carries a fix
        assert issue.suggestion == swift_analyzer.RULES[issue.pattern_id][2]
    assert result.ast_mode is False and result.test_code is False


def test_the_stub_message_is_quoted_in_the_finding(tmp_path):
    result = _analyze(POS, tmp_path)
    stub = next(i for i in result.issues if i.line == 4)
    assert 'fatalError("Not implemented")' in stub.message


def test_two_findings_on_one_line_keep_two_ids(tmp_path):
    result = _analyze(POS, tmp_path)
    on_24 = [i for i in result.issues if i.line == 24]
    assert len(on_24) == 2 and len({i.locator for i in on_24}) == 2, on_24
    assert on_24[0].column == POS.splitlines()[23].index("Swift.print")


def test_strings_comments_members_and_own_names_are_not_findings(tmp_path):
    result = _analyze(NEG, tmp_path)
    assert result.issues == [], [(i.pattern_id, i.line) for i in result.issues]


# Each shape alone, so that one guard cannot stand in for another.
NEG_CASES = {
    "string": 'let a = "print(x) fatalError(\\"TODO\\")"\n',
    "raw string": 'let b = #"print("x") fatalError("TODO")"#\n',
    "multi-line string": 'let c = """\n    print(1)\n    fatalError("TODO")\n    """\n',
    "interpolation": 'let d = "\\(String(describing: "print(1)"))"\n',
    "regex": 'let r = /print\\(1\\)/\nlet e = #/fatalError\\("TODO"\\)/#\n',
    "line comment": '// print(1) fatalError("TODO")\n',
    "nested comment": "/* a /* print(1) */ fatalError() */\n",
    "invariant": 'func f(ok: Bool) { if !ok { fatalError("Index out of range") } }\n',
    "no message": "func f() { fatalError() }\n",
    "message not a literal": "func f() { fatalError(TODO.message) }\n",
    "empty member catch": "let p = upstream.catch {}\n",
    "member call": 'func f(l: L) { l.print("x"); l?.print("y") }\n',
    "own method": 'struct L {\n    func print(_ s: String) {}\n    func run() { print("x") }\n}\n',
    "enum case": "enum C { case print(String) }\n",
    "closure parameter": 'func f(print: (String) -> Void) { print("x") }\n',
    "local closure": 'func f() {\n    let print = { (s: String) in }\n    print("x")\n}\n',
    "explicit discard": "func f() { do { try g() } catch _ {} }\nfunc g() throws {}\n",
    "handled catch": "func f() { do { try g() } catch { report(error) } }\nfunc g() throws {}\n",
    "prose": "/// A Todo list is prose\n// MARK: - TODO not leading\n",
}


@pytest.mark.parametrize("case", sorted(NEG_CASES))
def test_each_shape_that_is_not_a_finding(case, tmp_path):
    result = _analyze(NEG_CASES[case], tmp_path)
    assert result.issues == [], [(i.pattern_id, i.line) for i in result.issues]


def test_an_initializer_the_class_does_not_support_is_not_a_stub(tmp_path):
    result = _analyze(INITS, tmp_path)
    # init?(coder:) and Xcode's fix-it message in another initializer are the
    # idiom; the same message outside an initializer is a stub.
    assert _found(result) == {("swift_fatal_error_stub", 20)}


def test_debug_only_prints_are_not_reported(tmp_path):
    result = _analyze(DEBUG, tmp_path)
    assert _found(result) == {
        ("swift_debug_print", 7),
        ("swift_debug_print", 13),
        ("swift_debug_print", 24),
    }


# Written out, not read from the analyzer: a module dropped from its list
# must fail here, not drop its case.
@pytest.mark.parametrize("module", ["XCTest", "Testing", "Quick", "Nimble", "SnapshotTesting"])
def test_each_test_framework_alone_makes_a_test_file(module, tmp_path):
    result = _analyze(f"import {module}\n\nfunc f() {{ print(1) }}\n", tmp_path, "T.swift")
    assert result.issues == [] and result.test_code is True


def test_a_test_file_reports_nothing(tmp_path):
    result = _analyze(TESTS, tmp_path, "AppTests.swift")
    assert result.issues == [] and result.test_code is True
    testable = _analyze("@testable import App\n\nfunc f() { print(1) }\n", tmp_path, "T.swift")
    assert testable.issues == [] and testable.test_code is True


@pytest.mark.parametrize(
    "name, text",
    [
        ("Tool/main.swift", 'print("usage")\nfatalError("TODO")\n'),
        ("Tool/Helper.swift", 'func usage() { print("usage") }\nfunc f() { fatalError("TODO") }\n'),
        ("script.swift", '#!/usr/bin/swift\nprint("hi")\nfatalError("TODO")\n'),
        ("Cmd.swift", 'import ArgumentParser\nprint("hi")\nfatalError("TODO")\n'),
        ("Plugin.swift", 'import PackagePlugin\nprint("hi")\nfatalError("TODO")\n'),
    ],
)
def test_a_program_prints_its_output_but_its_stubs_are_reported(tmp_path, name, text):
    if name == "Tool/Helper.swift":
        (tmp_path / "Tool").mkdir()
        (tmp_path / "Tool" / "main.swift").write_text("run()\n", encoding="utf-8")
    result = _analyze(text, tmp_path, name)
    assert {i.pattern_id for i in result.issues} == {"swift_fatal_error_stub"}, result.issues


def test_markers_lead_a_comment_line(tmp_path):
    text = (
        "// TODO: a marker\n"
        "// todo: lower case with a colon\n"
        "// FIXME(oleg) with an owner\n"
        "/*\n * HACK inside a block\n */\n"
        "// Todo list rendering is prose\n"
        "// TODOs are prose too\n"
        "let x = 1 // XXX trailing\n"
    )
    result = _analyze(text, tmp_path)
    assert [(i.line, i.column) for i in result.issues] == [(1, 3), (2, 3), (3, 3), (5, 3), (9, 13)]
    assert result.issues[0].message == "Unresolved marker: TODO: a marker"


def test_an_empty_catch_with_a_comment_is_still_empty(tmp_path):
    text = (
        "func f() {\n"
        "    do { try g() } catch {\n        // ignore\n    }\n"
        "    do { try g() } catch let error as URLError { _ = error }\n"
        "}\nfunc g() throws {}\n"
    )
    result = _analyze(text, tmp_path)
    assert _found(result) == {("swift_empty_catch", 2)}


def test_a_combine_catch_operator_is_not_a_catch_block(tmp_path):
    text = (
        "let p = upstream.catch { _ in Just(0) }\nlet q = upstream\n    .catch { _ in Empty() }\n"
    )
    assert _analyze(text, tmp_path).issues == []


def test_a_file_that_cannot_be_read_fails(tmp_path):
    with pytest.raises(SwiftReadError, match="Swift: unterminated string at line 2"):
        _analyze(UNTERMINATED, tmp_path)


def test_the_score_counts_findings_per_100_lines(tmp_path):
    from slop_detector.diagnostic_bands import classify_deficit

    body = "func f() {\n" + "    let _ = 1\n" * 97 + "    NAME\n}\n"
    scores = {}
    for name, call in (
        ("stub", 'fatalError("Not implemented")'),
        ("catch", "do { try g() } catch {}"),
        ("print", "print(1)"),
        ("marker", "// TODO: later"),
    ):
        result = _analyze(body.replace("NAME", call), tmp_path, f"{name}.swift")
        assert result.total_lines == 100, result.total_lines
        scores[name] = result.slop_score
    assert scores == {
        "stub": token_rules.STUB_WEIGHT,
        "catch": token_rules.SWALLOW_WEIGHT,
        "print": token_rules.DEBUG_WEIGHT,
        "marker": 0.0,  # reported, not scored
    }
    # a short file counts as 100 lines: one stub stays clean, three are critical
    one = _analyze('func f() { fatalError("TODO") }\n', tmp_path, "one.swift")
    three = _analyze('func f() { fatalError("TODO") }\n' * 3, tmp_path, "three.swift")
    assert (one.slop_score, one.status) == (25.0, classify_deficit(25.0).value)
    assert three.slop_score == 75.0 and three.status == classify_deficit(75.0).value


def test_documentation_does_not_raise_the_score(tmp_path):
    doc = "/// A documented API.\n" * 40 + "public protocol Store {\n    func load() -> Int\n}\n"
    result = _analyze(doc, tmp_path)
    assert result.issues == [] and result.slop_score == 0.0 and result.status == "clean"
    assert result.ldr_equivalent < 0.1  # measured, not scored
    assert (result.code_lines, result.comment_lines, result.blank_lines) == (3, 40, 0)


def test_a_finding_is_located_in_characters(tmp_path):
    text = 'let s = "é"; print(s)\n'  # e-acute: 2 bytes, 1 character
    result = _analyze(text, tmp_path)
    assert [(i.line, i.column) for i in result.issues] == [(1, text.index("print"))]


# --- project level: the adapter is registered and reaches every surface ------


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


STUB = 'func f() { fatalError("TODO") }\n'
LAYOUT = {
    "App/Sources/Feature.swift": STUB,
    "Pods/Alamofire/Source/Session.swift": STUB,
    "Carthage/Checkouts/Lib/Lib.swift": STUB,
    ".build/checkouts/swift-log/Logger.swift": STUB,
    "DerivedData/App/Build/Gen.swift": STUB,
    "SourcePackages/checkouts/pkg/P.swift": STUB,
    "App/Docs.docc/Resources/Step1.swift": STUB,
    "App/Sources/Model.pb.swift": STUB,
    "App/Sources/Strings+Generated.swift": STUB,
    "App/Sources/Mocks.generated.swift": STUB,
    "App/Sources/API.graphql.swift": STUB,
    "Pods/tool.py": "def h(x):\n    return x + 1\n",
}


def test_dependencies_build_output_and_generated_files_are_excluded_for_swift_only(tmp_path):
    result = _project(tmp_path, LAYOUT)
    assert _rel(result.swift_file_results, tmp_path) == ["App/Sources/Feature.swift"]
    excluded = {
        e["path"]: e["reason"]
        for e in result.scan_coverage["excluded"]["files"]
        if e["language"] == "swift"
    }
    assert set(excluded) == set(LAYOUT) - {"App/Sources/Feature.swift", "Pods/tool.py"}
    # Built-in exclusions of the Swift adapter, not a user's rules.
    assert result.scan_coverage["excluded"]["by_source"]["custom"] == 0
    # A Python file under Pods/ is still analyzed: the exclusion is Swift's.
    assert _rel(result.file_results, tmp_path) == ["Pods/tool.py"]


TEST_LAYOUT = {
    "Sources/Lib/Lib.swift": "public func f() -> Int { 1 }\n",
    "Tests/LibTests/LibTests.swift": STUB,
    "App/AppTests/Mock.swift": STUB,
    "App/AppUITests/Flow.swift": STUB,
    "Modules/NetworkTestSupport/Stub.swift": STUB,
    "Modules/DataMocks/Fake.swift": STUB,
    "Modules/TestKitchen/Experiment.swift": "public func run() -> Int { 1 }\n",
}


def test_test_targets_are_excluded_unless_tests_are_included(tmp_path):
    result = _project(tmp_path, TEST_LAYOUT)
    assert _rel(result.swift_file_results, tmp_path) == [
        "Modules/TestKitchen/Experiment.swift",
        "Sources/Lib/Lib.swift",
    ]
    reasons = {e["reason"] for e in result.scan_coverage["excluded"]["files"]}
    assert all(r.startswith("pattern:**/") for r in reasons), reasons
    assert result.scan_coverage["excluded"]["by_source"]["custom"] == 0

    included = _project(tmp_path, {}, include_tests=True)
    assert len(included.swift_file_results) == len(TEST_LAYOUT)


def test_a_mixed_project_reports_swift_everywhere(tmp_path):
    from slop_detector.ai_report import render_ai_report
    from slop_detector.renderer_markdown import generate_markdown_report
    from slop_detector.renderer_text import generate_text_report
    from slop_detector.service.policy import evaluate_gate

    result = _project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "m"\n',
            "app/core.py": "def f(x):\n    return x + 1\n",
            "ios/App/Feature.swift": STUB * 3,
        },
    )
    analyzed = result.scan_coverage["analyzed"]
    assert analyzed["swift"] == 1 and analyzed["total"] == 2, analyzed
    assert all(f["extension"] != ".swift" for f in result.scan_coverage["unsupported"]["files"])
    for text in (
        generate_markdown_report(result),
        generate_text_report(result),
        render_ai_report(result),
    ):
        assert "swift_fatal_error_stub" in text
    assert "swift_file_results" in result.to_dict()
    gate = evaluate_gate(result, mode="hard", claims_strict=False, read_only=False)
    assert any(p.replace("\\", "/").endswith("Feature.swift") for p in gate.failed_files)


def test_a_swift_file_that_cannot_be_read_is_a_failed_file(tmp_path):
    result = _project(tmp_path, {"Sources/A.swift": STUB, "Sources/Bad.swift": UNTERMINATED})
    failed = result.scan_coverage["failed"]
    assert failed["by_language"] == {"swift": 1}, failed
    assert failed["files"] == [
        {"path": "Sources/Bad.swift", "language": "swift", "error_type": "SwiftReadError"}
    ]
    assert _rel(result.swift_file_results, tmp_path) == ["Sources/A.swift"]


def test_a_swift_item_holds_when_its_file_changes(tmp_path):
    from slop_detector.ai_report_md import render_ai_report_md

    result = _project(tmp_path, {"Sources/A.swift": STUB})
    (tmp_path / "Sources" / "A.swift").write_text(STUB + "// edited\n", encoding="utf-8")
    assert "(VOID:" in render_ai_report_md(result)
