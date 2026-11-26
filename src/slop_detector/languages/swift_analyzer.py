"""Swift analyzer (v1): unfinished-code markers in .swift files.

Rules (outside test code):
  swift_fatal_error_stub  fatalError() / preconditionFailure() whose message
                          says the code is not implemented               high
  swift_empty_catch       a catch block with no code                     medium
  swift_debug_print       print() / debugPrint() / dump() outside #if DEBUG medium
  swift_todo_comment      TODO/FIXME/HACK/XXX comment                    low

The token scanner (languages.token_scan) reads every file; it is always
available and measures every rule. Calls inside strings, comments and regex
literals are not calls. Not reported:

- test code: a file that imports XCTest, Testing, Quick, Nimble or
  SnapshotTesting, or has an @testable import (Xcode test targets and SwiftPM
  Tests/ directories are excluded from the scan by path, see language_registry);
- `fatalError` in an `init?(coder:)` / `init(coder:)` body: the template
  UIKit and AppKit require for a view built only in code;
- a fatalError() whose message does not say "not implemented" (an invariant
  or an unreachable branch, which is not unfinished code);
- debug prints inside `#if DEBUG` (they are not in a release build), and in a
  command-line program: main.swift, a script (#!), or a file that imports
  ArgumentParser, where print() is the program's output;
- a call to a function of the same name the file declares (`func print`), a
  member call (`logger.print(`), and an enum case named `print`.

A file the scanner cannot read raises SwiftReadError (a failed file in scan
coverage), never a file with no findings.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

from slop_detector.finding_identity import bind_lines, read_source
from slop_detector.languages import token_rules as tr
from slop_detector.languages.token_scan import Comment, SwiftReadError, Tok, scan

logger = logging.getLogger(__name__)

__all__ = ["SwiftAnalyzer", "SwiftReadError", "RULES", "REGEX_FALLBACK_NOT_MEASURED"]

# The token scanner is the only reader and measures every rule; there is no
# tree-sitter path, so a result never says ast_mode True.
REGEX_FALLBACK_NOT_MEASURED: tuple = ()
AST_AVAILABLE = False

RULES: Dict[str, tr.Rule] = {
    "swift_fatal_error_stub": (
        "high",
        "fatalError() marks the code as not implemented: it crashes when reached",
        "Implement the code path, or throw an error the caller can handle. Rewording the "
        "message does not resolve this finding: the call still crashes.",
    ),
    "swift_empty_catch": (
        "medium",
        "Empty catch block: the error is discarded without a trace",
        "Handle the error: recover, rethrow it, or report it to the caller or a logger. "
        "A comment in the empty block does not resolve this finding.",
    ),
    "swift_debug_print": (
        "medium",
        "Debug print left in the code",
        "Remove the debug print, log through a logger (os.Logger), or put it inside "
        "#if DEBUG so it is not in a release build.",
    ),
    "swift_todo_comment": (
        "low",
        "Unresolved marker",
        "Complete the work. If it is tracked elsewhere, replace the marker with the "
        "reference: a marker with a ticket number is still a marker.",
    ),
}
WEIGHTS = {
    "swift_fatal_error_stub": tr.STUB_WEIGHT,
    "swift_empty_catch": tr.SWALLOW_WEIGHT,
    "swift_debug_print": tr.DEBUG_WEIGHT,
}

STUB_CALLS = {"fatalError", "preconditionFailure"}
PRINT_CALLS = {"print", "debugPrint", "dump"}
TEST_MODULES = {"XCTest", "Testing", "Quick", "Nimble", "SnapshotTesting"}
# Modules of programs whose print() is their output: a command-line tool and a
# SwiftPM plugin (its print() is what Xcode or the terminal shows the user).
CLI_MODULES = {"ArgumentParser", "PackagePlugin"}
_QUALIFIERS = [("Swift",)]
_DECLARATIONS = {"func", "case"}
_NAME_DECLARATIONS = {"func", "let", "var", "case"}
_IMPORT_KINDS = {"typealias", "struct", "class", "enum", "protocol", "let", "var", "func"}
# The message Xcode's fix-it writes for an initializer a subclass does not
# support: fatalError("init(coder:) has not been implemented").
_INIT_FIX_IT = re.compile(r'^"init\([^"]*\) has not been implemented"$')


class SwiftAnalyzer:
    """Analyzes .swift files for unfinished-code markers."""

    def analyze(self, file_path: str) -> tr.MobileFileAnalysis:
        content, digest = read_source(file_path)
        tokens, comments = scan(content, "swift")
        result = analyze_tokens(file_path, content, tokens, comments)
        return bind_lines(result, content, digest)


def analyze_tokens(
    file_path: str, content: str, tokens: List[Tok], comments: List[Comment]
) -> tr.MobileFileAnalysis:
    test_code = is_test_file(tokens)
    issues: List[tr.MobileIssue] = []
    if not test_code:
        issues += _stub_issues(tokens)
        issues += [
            tr.issue(RULES, "swift_empty_catch", tokens[k])
            for k in tr.empty_catches(tokens, False, lambda name: name == "_")
        ]
        if not _is_program(file_path, content, tokens):
            issues += _print_issues(tokens)
        issues += tr.marker_issues(comments, RULES, "swift_todo_comment")
    return tr.build_result(file_path, content, tokens, comments, issues, WEIGHTS, test_code)


# --- context ------------------------------------------------------------------


def imports(tokens: Sequence[Tok]) -> List[str]:
    """The modules the file imports (`import Foo`, `import struct Foo.Bar`)."""
    found = []
    for k, tok in enumerate(tokens):
        if tok.kind != "ident" or tok.text != "import" or tr.is_punct(tokens, k - 1, "."):
            continue
        j = k + 1
        if j < len(tokens) and tokens[j].text in _IMPORT_KINDS:
            j += 1
        if j < len(tokens) and tokens[j].kind == "ident":
            found.append(tokens[j].text)
    return found


def is_test_file(tokens: Sequence[Tok]) -> bool:
    """The file imports a test framework or has an @testable import."""
    if any(module in TEST_MODULES for module in imports(tokens)):
        return True
    return any(
        tr.is_punct(tokens, k - 1, "@") and tok.text == "testable" for k, tok in enumerate(tokens)
    )


def _is_program(file_path: str, content: str, tokens: Sequence[Tok]) -> bool:
    """A command-line program or a SwiftPM plugin, whose print() calls are its
    output: main.swift and the files beside it (an executable target), a
    script (#!), a file that imports ArgumentParser or PackagePlugin."""
    directory, name = os.path.split(file_path)
    if name == "main.swift" or os.path.isfile(os.path.join(directory, "main.swift")):
        return True
    if content.startswith("#!"):
        return True
    return any(module in CLI_MODULES for module in imports(tokens))


def _init_spans(tokens: Sequence[Tok]) -> Dict[str, List[tr.Span]]:
    """Initializer bodies: {"coder": [init?(coder:) bodies], "other": [...]}."""
    spans: Dict[str, List[tr.Span]] = {"coder": [], "other": []}
    for k, tok in enumerate(tokens):
        if tok.kind != "ident" or tok.text != "init" or tr.is_punct(tokens, k - 1, "."):
            continue
        found = _init_body(tokens, k)
        if found is not None:
            spans[found[0]].append(found[1])
    return spans


def _init_body(tokens: Sequence[Tok], k: int) -> Optional[Tuple[str, tr.Span]]:
    """("coder" or "other", the body's span) of the initializer declared at k
    (`init`, `init?` or `init!`), or None for a call or a body-less requirement."""
    j = k + 1
    if j < len(tokens) and tokens[j].text in ("?", "!"):
        j += 1
    if not tr.is_punct(tokens, j, "("):
        return None
    kind = "coder" if j + 1 < len(tokens) and tokens[j + 1].text == "coder" else "other"
    body = tr.match(tokens, j) + 1
    while body < len(tokens) and tokens[body].text in ("throws", "rethrows", "async"):
        body += 1
    if not tr.is_punct(tokens, body, "{"):
        return None
    return kind, tr.span_of(tokens, body, tr.match(tokens, body))


def _condition(tokens: Sequence[Tok], k: int) -> str:
    """The condition of the directive at k: the tokens after it on its line."""
    line = tokens[k].line
    parts = []
    for tok in tokens[k + 1 :]:
        if tok.line != line:
            break
        parts.append(tok.text)
    return "".join(parts)


def _implies_debug(condition: str) -> bool:
    """`DEBUG`, or DEBUG joined to other conditions by a top-level &&
    (`DEBUG && (os(iOS) || os(visionOS))`)."""
    return tr.implies(condition, lambda term: term == "DEBUG")


def debug_spans(tokens: Sequence[Tok]) -> List[tr.Span]:
    """Spans of the #if branches that are compiled only in a debug build:
    `#if DEBUG` (and DEBUG && ...), and the #else of `#if !DEBUG`."""
    spans: List[tr.Span] = []
    # one frame per open #if: [branch is debug, branch start, #if was !DEBUG, saw #elseif]
    stack: List[list] = []
    for k, tok in enumerate(tokens):
        if tok.kind == "directive":
            _directive(tokens, k, stack, spans)
    for frame in stack:  # an #if left open runs to the end of the file
        _close_branch(tokens, frame, len(tokens) - 1, spans)
    return spans


def _directive(tokens: Sequence[Tok], k: int, stack: List[list], spans: List[tr.Span]) -> None:
    """Open, switch or close the #if branch at the directive k."""
    text = tokens[k].text
    if text == "#if":
        condition = _condition(tokens, k)
        stack.append([_implies_debug(condition), k, condition == "!DEBUG", False])
        return
    if not stack or text not in ("#elseif", "#else", "#endif"):
        return
    frame = stack[-1]
    _close_branch(tokens, frame, k, spans)
    if text == "#endif":
        stack.pop()
    elif text == "#elseif":
        frame[0], frame[1], frame[3] = _implies_debug(_condition(tokens, k)), k, True
    else:  # #else: debug only after `#if !DEBUG` with no #elseif
        frame[0], frame[1] = frame[2] and not frame[3], k


def _close_branch(tokens: Sequence[Tok], frame: list, k: int, spans: List[tr.Span]) -> None:
    if frame[0]:
        spans.append(tr.span_of(tokens, frame[1], k))


# --- rules --------------------------------------------------------------------


def _stub_issues(tokens: Sequence[Tok]) -> List[tr.MobileIssue]:
    inits = _init_spans(tokens)
    issues = []
    for k, tok in enumerate(tokens):
        if not tr.free_call(tokens, k, STUB_CALLS, _QUALIFIERS, _DECLARATIONS):
            continue
        message = tr.first_argument(tokens, k)
        if message is None or not tr.NOT_IMPLEMENTED.search(message.text):
            continue
        # An initializer the class does not support: init?(coder:) whatever its
        # message, any other initializer with Xcode's fix-it message.
        if tr.inside(inits["coder"], tok.line, tok.col):
            continue
        if _INIT_FIX_IT.match(message.text) and tr.inside(inits["other"], tok.line, tok.col):
            continue
        start = tokens[k - 2] if len(tr.qualified_path(tokens, k)) > 1 else tok
        issues.append(
            tr.issue(
                RULES,
                "swift_fatal_error_stub",
                start,
                f"{tok.text}({tr.short(message.text)}) marks the code as not implemented: "
                "it crashes when reached",
            )
        )
    return issues


def _print_issues(tokens: Sequence[Tok]) -> List[tr.MobileIssue]:
    debug = debug_spans(tokens)
    own: Set[str] = tr.declared_names(tokens, _NAME_DECLARATIONS) & PRINT_CALLS
    issues = []
    for k, tok in enumerate(tokens):
        if not tr.free_call(tokens, k, PRINT_CALLS - own, _QUALIFIERS, _DECLARATIONS):
            continue
        start = tokens[k - 2] if len(tr.qualified_path(tokens, k)) > 1 else tok
        if tr.inside(debug, start.line, start.col):
            continue
        issues.append(
            tr.issue(
                RULES, "swift_debug_print", start, f"{tok.text}() debug print left in the code"
            )
        )
    return issues
