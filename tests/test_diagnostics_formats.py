"""--format xcode and --format github: one diagnostic per finding.

xcode: `<absolute path>:<line>:<column>: warning: <message>`, the compiler
format an Xcode Run Script build phase turns into warnings in the editor.
github: GitHub Actions workflow commands, shown as pull request annotations.
Both list every finding of every language and every file that could not be
analyzed; neither changes the exit code; neither prints the progress box or
a summary into the machine output.
"""

from __future__ import annotations

import os
import re

import pytest

from slop_detector.cli import main
from slop_detector.renderer_diagnostics import diagnostics, render_github, render_xcode

SWIFT = (
    "import UIKit\n\n"
    "final class Profile {\n"
    "    func load() {\n"
    '        fatalError("Not implemented")\n'
    "    }\n"
    "    // TODO: paging\n"
    "}\n"
)
KOTLIN = "fun sync(): Int = TODO()\n"
PYTHON = "def f(x):\n    try:\n        return int(x)\n    except:\n        pass\n"
BROKEN = 'let s = "never closed\n'
XCODE_LINE = re.compile(r"^(/.+|[A-Za-z]:\\.+):(\d+)(?::(\d+))?: warning: .+ \[[a-z_]+, [a-z]+\]")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "app"
    for rel, text in {
        "ios/Profile.swift": SWIFT,
        "android/src/main/kotlin/Sync.kt": KOTLIN,
        "tools/convert.py": PYTHON,
        "ios/Broken.swift": BROKEN,
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _scan(root):
    from slop_detector.core import SlopDetector

    return SlopDetector(read_only=True).analyze_project(str(root))


def test_every_language_and_every_failed_file_is_a_diagnostic(project):
    found = {(os.path.basename(d.path), d.rule) for d in diagnostics(_scan(project))}
    assert {
        ("Profile.swift", "swift_fatal_error_stub"),
        ("Profile.swift", "swift_todo_comment"),
        ("Sync.kt", "kotlin_todo_call"),
        ("convert.py", "bare_except"),
        ("Broken.swift", "analysis_failed"),
    } <= found, found


def test_the_xcode_format_is_the_compiler_diagnostic_line(project):
    text = render_xcode(_scan(project))
    lines = text.splitlines()
    assert lines and all(XCODE_LINE.match(line) for line in lines), lines
    stub = next(line for line in lines if "swift_fatal_error_stub" in line)
    path = str(project / "ios" / "Profile.swift")
    # line 5, column 9 (1-based): where fatalError starts
    assert stub.startswith(f"{os.path.abspath(path)}:5:9: warning: ")
    assert "Fix: Implement the code path" in stub
    assert all(line.count("\n") == 0 for line in lines)


def test_the_github_format_is_one_annotation_per_finding_most_severe_first(project):
    text = render_github(_scan(project), root=str(project))
    lines = text.splitlines()
    assert all(re.match(r"^::(error|warning|notice) file=[^,]+,", line) for line in lines), lines
    stub = next(line for line in lines if "swift_fatal_error_stub" in line)
    assert stub.startswith(
        "::error file=ios/Profile.swift,line=5,col=9,title=swift_fatal_error_stub (high)::"
    )
    assert "%0AFix: " in stub  # the newline before the fix is escaped
    marker = next(line for line in lines if "swift_todo_comment" in line)
    assert marker.startswith("::notice ")
    levels = [line.split(" ", 1)[0] for line in lines]
    order = {"::error": 0, "::warning": 1, "::notice": 2}
    assert levels == sorted(levels, key=order.get)


def test_github_properties_and_messages_are_escaped():
    from slop_detector.renderer_diagnostics import _escape_data, _escape_property

    assert _escape_property("a,b:c%d\ne") == "a%2Cb%3Ac%25d%0Ae"
    assert _escape_data("50% done\r\nnext: a, b") == "50%25 done%0D%0Anext: a, b"


def test_the_cli_writes_only_diagnostics_and_keeps_the_exit_code(project, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(project.parent / "home"))
    code = main(["--project", str(project), "--format", "xcode", "--read-only"])
    out, err = capsys.readouterr()
    assert code == 0
    assert out.splitlines() and all(XCODE_LINE.match(line) for line in out.splitlines())
    assert "Analyzing" not in out and "Score" not in out
    code = main(
        ["--project", str(project), "--format", "github", "--read-only", "--fail-threshold", "1"]
    )
    out, err = capsys.readouterr()
    assert code == 1 and "FAIL" in err  # the threshold decides the exit code, on stderr
    assert all(line.startswith("::") for line in out.splitlines())


def test_diagnostics_can_be_written_to_a_file(project, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out_file = tmp_path / "slop.txt"
    assert main(["--project", str(project), "--format", "xcode", "--output", str(out_file)]) == 0
    assert "swift_fatal_error_stub" in out_file.read_text(encoding="utf-8")


def test_a_single_file_scan_lists_its_findings(project):
    from slop_detector.core import SlopDetector

    result = SlopDetector(read_only=True).analyze_language_file(
        __import__("slop_detector.language_registry", fromlist=["spec"]).spec("swift"),
        str(project / "ios" / "Profile.swift"),
    )
    assert {d.rule for d in diagnostics(result)} == {"swift_fatal_error_stub", "swift_todo_comment"}


def test_interactive_refuses_a_diagnostics_format(project, capsys):
    code = main(["--project", str(project), "--format", "xcode", "--interactive"])
    assert code == 2 and "--format xcode/github" in capsys.readouterr().err


def test_a_failed_gate_ends_the_diagnostics_with_one_error(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "ios"
    root.mkdir()
    (root / "Stubs.swift").write_text('func f() { fatalError("TODO") }\n' * 3, encoding="utf-8")
    code = main(["--project", str(root), "--format", "xcode", "--ci-mode", "hard", "--no-history"])
    lines = capsys.readouterr().out.splitlines()
    assert code == 1
    assert lines[-1].startswith("error: AI-SLOP Detector --ci-mode hard failed: ")
    assert sum(line.startswith("error:") for line in lines) == 1
    code = main(["--project", str(root), "--format", "github", "--ci-mode", "hard", "--no-history"])
    lines = capsys.readouterr().out.splitlines()
    assert code == 1 and lines[-1].startswith("::error title=AI-SLOP Detector gate::")

    (root / "Stubs.swift").write_text('func f() { fatalError("TODO") }\n', encoding="utf-8")
    code = main(["--project", str(root), "--format", "xcode", "--ci-mode", "hard", "--no-history"])
    out = capsys.readouterr().out
    assert code == 0 and "error:" not in out and "warning:" in out
