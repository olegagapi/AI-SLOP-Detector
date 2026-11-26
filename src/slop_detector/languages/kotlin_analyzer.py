"""Kotlin analyzer (v1): unfinished-code markers in .kt and .kts files.

Rules (outside test code):
  kotlin_todo_call          TODO() call: throws NotImplementedError         high
  kotlin_not_implemented    throw NotImplementedError(...), or an exception
                            or error() whose message says not implemented  high
  kotlin_empty_catch        a catch block with no code                     medium
  kotlin_print_stack_trace  e.printStackTrace()                            medium
  kotlin_debug_print        println() / print() / System.out.println() in
                            Android or Compose code                        medium
  kotlin_todo_comment       TODO/FIXME/HACK/XXX comment                    low

The token scanner (languages.token_scan) reads every file; it is always
available and measures every rule. Calls inside strings, string templates,
char literals and comments are not calls. Not reported:

- test code: a file that imports a test framework (JUnit, kotlin.test,
  Kotest, AndroidX Test, Robolectric, MockK, Mockito, Turbine,
  kotlinx.coroutines.test, Compose UI test); Gradle test source sets
  (src/test, src/androidTest, src/commonTest, ...) are excluded from the scan
  by path, see language_registry;
- a print outside Android and Compose code: in a file that imports no
  android.* or androidx.* package (a command-line tool, Gradle build logic, a
  server), in a script (.kts), or in a file with a top-level `fun main` (a
  desktop or web entry point), standard output is the program's output; in an
  app its text
  goes to logcat's System.out tag, which a release build should not write;
- a print inside `if (BuildConfig.DEBUG)`;
- a member call (`writer.println(`) other than System.out / System.err, a
  call to a function of the same name the file declares (`fun println`), a
  Flow `.catch { }` operator.

A file the scanner cannot read raises KotlinReadError (a failed file in scan
coverage), never a file with no findings.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Sequence, Set

from slop_detector.finding_identity import bind_lines, read_source
from slop_detector.languages import token_rules as tr
from slop_detector.languages.token_scan import Comment, KotlinReadError, Tok, scan

logger = logging.getLogger(__name__)

__all__ = ["KotlinAnalyzer", "KotlinReadError", "RULES", "REGEX_FALLBACK_NOT_MEASURED"]

# The token scanner is the only reader and measures every rule; there is no
# tree-sitter path, so a result never says ast_mode True.
REGEX_FALLBACK_NOT_MEASURED: tuple = ()
AST_AVAILABLE = False

RULES: Dict[str, tr.Rule] = {
    "kotlin_todo_call": (
        "high",
        "TODO() left in the code: it throws NotImplementedError when reached",
        "Implement the code path, or return a result or throw an exception the caller can "
        "handle. Changing the TODO() reason does not resolve this finding.",
    ),
    "kotlin_not_implemented": (
        "high",
        "The code path is not implemented: it throws when reached",
        "Implement the code path, or throw an exception that names the real failure for the "
        "caller to handle. Rewording the message alone does not resolve this finding.",
    ),
    "kotlin_empty_catch": (
        "medium",
        "Empty catch block: the exception is discarded without a trace",
        "Handle the exception: recover, rethrow it, or log it. A comment in the empty block "
        "does not resolve this finding.",
    ),
    "kotlin_print_stack_trace": (
        "medium",
        "printStackTrace() writes the exception to standard error, which an Android app "
        "or a server does not keep",
        "Handle the exception: log it through a logger, recover, or rethrow it.",
    ),
    "kotlin_debug_print": (
        "medium",
        "Debug print left in the code",
        "Remove the debug print, log through a logger (android.util.Log, Timber, SLF4J), "
        "or put it inside if (BuildConfig.DEBUG).",
    ),
    "kotlin_todo_comment": (
        "low",
        "Unresolved marker",
        "Complete the work. If it is tracked elsewhere, replace the marker with the "
        "reference: a marker with a ticket number is still a marker.",
    ),
}
WEIGHTS = {
    "kotlin_todo_call": tr.STUB_WEIGHT,
    "kotlin_not_implemented": tr.STUB_WEIGHT,
    "kotlin_empty_catch": tr.SWALLOW_WEIGHT,
    "kotlin_print_stack_trace": tr.DEBUG_WEIGHT,
    "kotlin_debug_print": tr.DEBUG_WEIGHT,
}

TEST_PACKAGES = (
    "org.junit",
    "junit.framework",
    "kotlin.test",
    "io.kotest",
    "androidx.test",
    "androidx.compose.ui.test",
    "org.robolectric",
    "io.mockk",
    "org.mockito",
    "app.cash.turbine",
    "kotlinx.coroutines.test",
    "org.spekframework",
)
# Code that runs in an Android app (Compose Multiplatform shares androidx.*):
# there a print is a debug leftover. Elsewhere standard output is the program's.
APP_PACKAGES = ("android", "androidx")
PRINT_CALLS = {"println", "print"}
_PRINT_QUALIFIERS = [("kotlin", "io"), ("System", "out"), ("System", "err")]
_DECLARATIONS = {"fun", "class", "interface", "object", "val", "var", "typealias"}
# A catch parameter named like this declares the exception expected and the
# empty block intended (the convention of detekt's EmptyCatchBlock rule).
_INTENDED_DISCARD = re.compile(r"_|ignore\w*|expected\w*")
_BUILD_CONFIG_DEBUG = re.compile(r"(?:[\w.]*\.)?BuildConfig\.DEBUG")


class KotlinAnalyzer:
    """Analyzes .kt and .kts files for unfinished-code markers."""

    def analyze(self, file_path: str) -> tr.MobileFileAnalysis:
        content, digest = read_source(file_path)
        tokens, comments = scan(content, "kotlin")
        result = analyze_tokens(file_path, content, tokens, comments)
        return bind_lines(result, content, digest)


def analyze_tokens(
    file_path: str, content: str, tokens: List[Tok], comments: List[Comment]
) -> tr.MobileFileAnalysis:
    test_code = is_test_file(tokens)
    issues: List[tr.MobileIssue] = []
    if not test_code:
        issues += _todo_issues(tokens)
        issues += _not_implemented_issues(tokens)
        issues += [
            tr.issue(RULES, "kotlin_empty_catch", tokens[k])
            for k in tr.empty_catches(tokens, True, _INTENDED_DISCARD.fullmatch)
        ]
        issues += _stack_trace_issues(tokens)
        if _is_app_code(file_path, tokens):
            issues += _print_issues(tokens)
        issues += tr.marker_issues(comments, RULES, "kotlin_todo_comment")
    return tr.build_result(file_path, content, tokens, comments, issues, WEIGHTS, test_code)


# --- context ------------------------------------------------------------------


def imports(tokens: Sequence[Tok]) -> List[str]:
    """The dotted names the file imports (`import a.b.C`, `import a.b.*`)."""
    found = []
    for k, tok in enumerate(tokens):
        if tok.kind != "ident" or tok.text != "import" or tr.is_punct(tokens, k - 1, "."):
            continue
        parts: List[str] = []
        j = k + 1
        while j < len(tokens) and tokens[j].kind == "ident" and tokens[j].line == tok.line:
            parts.append(tokens[j].text)
            if not tr.is_punct(tokens, j + 1, "."):
                break
            j += 2
        if parts:
            found.append(".".join(parts))
    return found


def is_test_file(tokens: Sequence[Tok]) -> bool:
    """The file imports a test framework."""
    return any(
        name == package or name.startswith(package + ".")
        for name in imports(tokens)
        for package in TEST_PACKAGES
    )


def _is_app_code(file_path: str, tokens: Sequence[Tok]) -> bool:
    """The file imports an android.* or androidx.* package and is neither a
    script (.kts) nor a program's entry point (a top-level `fun main`)."""
    if file_path.lower().endswith(".kts"):
        return False
    if not any(name.split(".")[0] in APP_PACKAGES for name in imports(tokens)):
        return False
    return not _has_top_level_main(tokens)


def _has_top_level_main(tokens: Sequence[Tok]) -> bool:
    """A `fun main(` outside every brace: the file is a program's entry point."""
    depth = 0
    for k, tok in enumerate(tokens):
        if tok.kind == "punct" and tok.text in "{}":
            depth += 1 if tok.text == "{" else -1
        elif depth == 0 and tok.text == "fun" and k + 2 < len(tokens):
            if tokens[k + 1].text == "main" and tr.is_punct(tokens, k + 2, "("):
                return True
    return False


def _is_debug_condition(condition: str) -> bool:
    """BuildConfig.DEBUG, or it joined to other conditions by a top-level &&."""
    return tr.implies(condition, lambda term: bool(_BUILD_CONFIG_DEBUG.fullmatch(term)))


# --- rules --------------------------------------------------------------------


def _start(tokens: Sequence[Tok], k: int) -> Tok:
    """The first token of the qualified name ending at k."""
    return tokens[k - 2 * (len(tr.qualified_path(tokens, k)) - 1)]


def _todo_issues(tokens: Sequence[Tok]) -> List[tr.MobileIssue]:
    return [
        tr.issue(RULES, "kotlin_todo_call", _start(tokens, k))
        for k in range(len(tokens))
        if tr.free_call(tokens, k, {"TODO"}, [("kotlin",)], _DECLARATIONS)
    ]


def _not_implemented_issues(tokens: Sequence[Tok]) -> List[tr.MobileIssue]:
    issues = []
    for k, tok in enumerate(tokens):
        if tok.kind == "ident" and tok.text == "throw":
            text = _thrown_stub(tokens, k)
            start = tok
        elif tr.free_call(tokens, k, {"error"}, [("kotlin",)], _DECLARATIONS):
            text = _error_stub(tokens, k)
            start = _start(tokens, k)
        else:
            continue
        if text:
            issues.append(tr.issue(RULES, "kotlin_not_implemented", start, text))
    return issues


def _stub_message(message: Optional[Tok]) -> str:
    """The message literal, shortened, when it says the code is not
    implemented; "" otherwise (no literal, or another message)."""
    if message is None or not tr.NOT_IMPLEMENTED.search(message.text):
        return ""
    return tr.short(message.text)


def _thrown_stub(tokens: Sequence[Tok], k: int) -> str:
    """The finding's text for the `throw` at k when it throws a stub: a
    NotImplementedError, or an exception whose message says not implemented."""
    j = k + 1
    while tr.is_punct(tokens, j + 1, ".") and j + 2 < len(tokens):
        j += 2  # throw kotlin.NotImplementedError(
    if j >= len(tokens) or tokens[j].kind != "ident" or not tr.is_punct(tokens, j + 1, "("):
        return ""
    name = tokens[j].text
    if name == "NotImplementedError":
        return f"throw {name}(): the code path is not implemented"
    quoted = _stub_message(tr.first_argument(tokens, j))
    return f"throw {name}({quoted}): the code path is not implemented" if quoted else ""


def _error_stub(tokens: Sequence[Tok], k: int) -> str:
    """The finding's text for the error(...) call at k whose message says not
    implemented, or ""."""
    quoted = _stub_message(tr.first_argument(tokens, k))
    return f"error({quoted}): the code path is not implemented" if quoted else ""


def _stack_trace_issues(tokens: Sequence[Tok]) -> List[tr.MobileIssue]:
    return [
        tr.issue(RULES, "kotlin_print_stack_trace", tok)
        for k, tok in enumerate(tokens)
        if tok.kind == "ident"
        and tok.text == "printStackTrace"
        and tr.is_punct(tokens, k - 1, ".")
        and tr.is_punct(tokens, k + 1, "(")
        and tr.is_punct(tokens, k + 2, ")")
    ]


def _print_issues(tokens: Sequence[Tok]) -> List[tr.MobileIssue]:
    debug = tr.condition_spans(tokens, _is_debug_condition)
    own: Set[str] = tr.declared_names(tokens, {"fun", "val", "var"}) & PRINT_CALLS
    issues = []
    for k, tok in enumerate(tokens):
        if not tr.free_call(tokens, k, PRINT_CALLS - own, _PRINT_QUALIFIERS, _DECLARATIONS):
            continue
        start = _start(tokens, k)
        if tr.inside(debug, start.line, start.col):
            continue
        name = ".".join(tr.qualified_path(tokens, k))
        issues.append(
            tr.issue(RULES, "kotlin_debug_print", start, f"{name}() debug print left in the code")
        )
    return issues
