"""What the Swift and Kotlin analyzers share: their result shape, delimiter
matching, marker comments, empty catch blocks and the file score.

Both analyzers read a file with languages.token_scan and report from its
tokens and comments; this module holds the parts that do not depend on the
language.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from slop_detector.diagnostic_bands import classify_deficit
from slop_detector.languages.token_scan import Comment, Tok

# Score weights per 100 lines, as for Rust: a stub (a call that fails when
# reached) weighs like todo!(), a debug print like dbg!(); an empty catch block
# weighs between them. A marker comment is reported, not scored.
#
# Unlike Rust and Go, logic density is measured and not scored. On the pinned
# public corpus (13 iOS and Android repositories, 2026-11-26) the Rust density
# penalty put 24 files with no finding in the suspicious band, all of them
# documented interfaces (okhttp's Call.kt, WebSocket.kt): KDoc and DocC on a
# public API is documentation, not a warning sign.
STUB_WEIGHT = 25.0
SWALLOW_WEIGHT = 10.0
DEBUG_WEIGHT = 5.0
# A file shorter than this counts as this long: one stub in a 30-line file is
# 25 points, not 83 (one stub alone never fails --ci-mode hard).
MIN_DENSITY_LINES = 100

_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = set(_OPEN.values())

# A message that says the code is not written yet: "Not implemented", "not yet
# implemented", "has not been implemented", "Unimplemented", "implement me",
# and the TODO / FIXME markers (upper case: "Todo" is also a domain noun).
NOT_IMPLEMENTED = re.compile(
    r"(?i:\bnot\s+(?:yet\s+|been\s+)?implemented\b|\bunimplemented\b|\bimplement\s+me\b)"
    r"|\b(?:TODO|FIXME)\b"
)

# A marker leading a comment line: TODO, FIXME, HACK or XXX in upper case, or
# any case when a colon or a parenthesis follows ("todo: ...", "Fixme(...)").
# "Todo list" in prose is not a marker.
_MARKER = re.compile(r"(?:TODO|FIXME|HACK|XXX)\b|(?i:todo|fixme|hack|xxx)\s*[:(]")
_COMMENT_LEAD = re.compile(r"^\s*(?://[/!]?|/\*[*!]?|\*+(?!/))?\s*")

Span = Tuple[Tuple[int, int], Tuple[int, int]]  # ((line, col), (line, col)), inclusive


@dataclass
class MobileIssue:
    """A finding in a Swift or Kotlin file."""

    pattern_id: str
    severity: str  # critical | high | medium | low
    line: int
    column: int
    message: str
    suggestion: Optional[str] = None
    fingerprint: Optional[str] = None
    locator: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "severity": self.severity,
            "line": self.line,
            "column": self.column,
            "message": self.message,
            "suggestion": self.suggestion,
            "fingerprint": self.fingerprint,
            "locator": self.locator,
        }


@dataclass
class MobileFileAnalysis:
    """Analysis result for a Swift or Kotlin file."""

    file_path: str
    total_lines: int
    code_lines: int
    comment_lines: int
    blank_lines: int
    # Code lines / total lines (measured, not scored: see STUB_WEIGHT).
    ldr_equivalent: float
    issues: List[MobileIssue] = field(default_factory=list)
    # True when the file is test code (it imports a test framework): it is
    # analyzed and counted, and reports no finding.
    test_code: bool = False
    ast_mode: bool = False
    slop_score: float = 0.0
    status: str = "clean"  # clean | suspicious | critical_deficit
    source_sha256: str = ""
    content_origin: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path,
            "total_lines": self.total_lines,
            "code_lines": self.code_lines,
            "comment_lines": self.comment_lines,
            "blank_lines": self.blank_lines,
            "ldr_equivalent": round(self.ldr_equivalent, 4),
            "test_code": self.test_code,
            "ast_mode": self.ast_mode,
            "slop_score": round(self.slop_score, 2),
            "status": self.status,
            "issues": [i.to_dict() for i in self.issues],
            "source_sha256": self.source_sha256,
            "content_origin": self.content_origin,
        }


Rule = Tuple[str, str, str]  # severity, message, fix


def issue(rules: Dict[str, Rule], pattern_id: str, tok: Any, message: str = "") -> MobileIssue:
    """A finding of `pattern_id` at a token or comment position."""
    severity, default_message, fix = rules[pattern_id]
    return MobileIssue(pattern_id, severity, tok.line, tok.col, message or default_message, fix)


# --- tokens -------------------------------------------------------------------


def is_punct(tokens: Sequence[Tok], k: int, text: str) -> bool:
    return 0 <= k < len(tokens) and tokens[k].kind == "punct" and tokens[k].text == text


def match(tokens: Sequence[Tok], k: int) -> int:
    """Index of the delimiter closing the one opened at k (or the last token)."""
    depth = 0
    for j in range(k, len(tokens)):
        tok = tokens[j]
        if tok.kind != "punct":
            continue
        if tok.text in _OPEN:
            depth += 1
        elif tok.text in _CLOSE:
            depth -= 1
            if depth == 0:
                return j
    return len(tokens) - 1


def span_of(tokens: Sequence[Tok], first: int, last: int) -> Span:
    """The source span from token `first` to the end of token `last`."""
    a, b = tokens[first], tokens[max(first, min(last, len(tokens) - 1))]
    return ((a.line, a.col), (b.end_line, b.col + len(b.text)))


def inside(spans: Iterable[Span], line: int, col: int) -> bool:
    return any(a <= (line, col) <= b for a, b in spans)


def qualified_path(tokens: Sequence[Tok], k: int) -> Tuple[str, ...]:
    """The dotted path ending at identifier k: ("Swift", "print") for
    `Swift.print`, ("print",) for a bare name."""
    path = [tokens[k].text]
    j = k
    while j >= 2 and is_punct(tokens, j - 1, ".") and tokens[j - 2].kind == "ident":
        path.insert(0, tokens[j - 2].text)
        j -= 2
    return tuple(path)


def free_call(
    tokens: Sequence[Tok],
    k: int,
    names: Set[str],
    qualifiers: Sequence[Tuple[str, ...]],
    declarations: Set[str],
) -> bool:
    """tokens[k] calls one of `names` as a free function: followed by `(`, not
    a member call (`x.print(`) unless qualified by one of `qualifiers`
    (`Swift.print(`), and not a declaration (`func print(`)."""
    tok = tokens[k]
    if tok.kind != "ident" or tok.text not in names or not is_punct(tokens, k + 1, "("):
        return False
    path = qualified_path(tokens, k)
    if len(path) > 1:
        # Swift.print( is a free call; x.Swift.print( and x?.Swift.print( are not.
        return not _after_member_access(tokens, k - 2 * (len(path) - 1)) and (
            path[:-1] in qualifiers
        )
    if _after_member_access(tokens, k):
        return False
    prev = tokens[k - 1] if k > 0 else None
    return not (prev is not None and prev.kind == "ident" and prev.text in declarations)


def _after_member_access(tokens: Sequence[Tok], k: int) -> bool:
    """tokens[k] follows `.` or `?` (x.name, x?.name)."""
    return is_punct(tokens, k - 1, ".") or is_punct(tokens, k - 1, "?")


def first_argument(tokens: Sequence[Tok], k: int) -> Optional[Tok]:
    """The string literal that is the first argument of the call at k (its `(`
    at k + 1), or None."""
    arg = tokens[k + 2] if k + 2 < len(tokens) else None
    if arg is None or arg.kind != "lit" or arg.text[:1] not in ('"', "#", "$"):
        return None
    return arg


def declared_names(tokens: Sequence[Tok], keywords: Set[str]) -> Set[str]:
    """Names the file declares or labels: after a declaration keyword (`func
    print`, `let print`, `val println`), and every name followed by `:` (a
    parameter `print: (String) -> Void`, or an argument label). A call to one
    of them may be the file's own function, not the standard library's."""
    names = set()
    for k in range(len(tokens) - 1):
        tok, nxt = tokens[k], tokens[k + 1]
        if tok.kind == "ident" and tok.text in keywords and nxt.kind == "ident":
            names.add(nxt.text)
        elif (
            tok.kind == "ident"
            and is_punct(tokens, k + 1, ":")
            and not is_punct(tokens, k + 2, ":")
        ):
            names.add(tok.text)
    return names


def short(literal: str, limit: int = 60) -> str:
    """A message literal for a finding's text, on one line and bounded."""
    text = " ".join(literal.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def split_top(condition: str, separator: str) -> List[str]:
    """`condition` split at `separator` outside parentheses."""
    parts, depth, start, k = [], 0, 0, 0
    while k < len(condition):
        c = condition[k]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and condition.startswith(separator, k):
            parts.append(condition[start:k])
            k += len(separator)
            start = k
            continue
        k += 1
    parts.append(condition[start:])
    return parts


def implies(condition: str, accepts: Any) -> bool:
    """The condition holds only when a term `accepts` holds: that term joined to
    the rest by && at the top level, with no top-level ||."""
    if len(split_top(condition, "||")) > 1:
        return False
    terms = [
        t[1:-1] if t.startswith("(") and t.endswith(")") else t for t in split_top(condition, "&&")
    ]
    return any(accepts(term) for term in terms)


def empty_catches(tokens: Sequence[Tok], needs_paren: bool, declared: Any) -> List[int]:
    """Indexes of `catch` clauses whose block holds no code (a comment in it is
    not code) and whose binding `declared` does not accept as an intended
    discard. A clause follows a `}`; Kotlin's takes a parenthesized parameter,
    so a `.catch { }` operator or a `catch { }` lambda is not one."""
    found = []
    for k, tok in enumerate(tokens):
        if tok.kind != "ident" or tok.text != "catch" or not is_punct(tokens, k - 1, "}"):
            continue
        if needs_paren and not is_punct(tokens, k + 1, "("):
            continue
        if _discard_declared(tokens, k + 2 if needs_paren else k + 1, declared):
            continue
        body = _block_after(tokens, k + 1)
        if body is not None and is_punct(tokens, body + 1, "}"):
            found.append(k)
    return found


def _discard_declared(tokens: Sequence[Tok], at: int, declared: Any) -> bool:
    """The catch clause's binding (the token at `at`) declares the discard."""
    return at < len(tokens) and bool(declared(tokens[at].text))


def _block_after(tokens: Sequence[Tok], j: int) -> Optional[int]:
    """Index of the first `{` from j outside parentheses and brackets, or None
    when a `}` or an unbalanced `)` comes first."""
    depth = 0
    while j < len(tokens):
        t = tokens[j]
        if t.kind == "punct":
            if t.text in "([":
                depth += 1
            elif t.text in ")]":
                depth -= 1
                if depth < 0:
                    return None
            elif t.text == "{" and depth == 0:
                return j
            elif t.text == "}":
                return None
        j += 1
    return None


def condition_spans(tokens: Sequence[Tok], is_debug: Any) -> List[Span]:
    """Spans of `if (<condition>) { ... }` blocks whose condition `is_debug`
    accepts (the joined condition text); without braces, the rest of the
    statement's line."""
    spans = []
    for k, tok in enumerate(tokens):
        if tok.kind != "ident" or tok.text != "if" or not is_punct(tokens, k + 1, "("):
            continue
        close = match(tokens, k + 1)
        if not is_debug("".join(t.text for t in tokens[k + 2 : close])):
            continue
        if is_punct(tokens, close + 1, "{"):
            spans.append(span_of(tokens, close + 1, match(tokens, close + 1)))
        elif close + 1 < len(tokens):
            line = tokens[close + 1].line
            last = close + 1
            while last + 1 < len(tokens) and tokens[last + 1].line == line:
                last += 1
            spans.append(span_of(tokens, close + 1, last))
    return spans


# --- comments -----------------------------------------------------------------


def marker_issues(
    comments: Sequence[Comment], rules: Dict[str, Rule], rule: str
) -> List[MobileIssue]:
    """TODO/FIXME/HACK/XXX markers leading a comment line."""
    issues: List[MobileIssue] = []
    severity, message, fix = rules[rule]
    for comment in comments:
        for offset, segment in enumerate(comment.text.split("\n")):
            lead = _COMMENT_LEAD.match(segment)
            rest = segment[lead.end() if lead else 0 :]
            if not _MARKER.match(rest):
                continue
            line = comment.line + offset
            column = (comment.col if offset == 0 else 0) + len(segment) - len(rest)
            text = rest.strip()
            if text.endswith("*/"):
                text = text[:-2].rstrip()
            issues.append(MobileIssue(rule, severity, line, column, f"{message}: {text}", fix))
    return issues


# --- the result ---------------------------------------------------------------


def build_result(
    file_path: str,
    content: str,
    tokens: Sequence[Tok],
    comments: Sequence[Comment],
    issues: List[MobileIssue],
    weights: Dict[str, float],
    test_code: bool,
) -> MobileFileAnalysis:
    """Line counts, the score (the findings' `weights` per 100 lines, a file
    shorter than MIN_DENSITY_LINES counting as that long) and the status."""
    lines = content.splitlines()
    total = len(lines)
    code: Set[int] = {n for t in tokens for n in range(t.line, t.end_line + 1)}
    commented: Set[int] = {
        n for c in comments for n in range(c.line, c.line + c.text.count("\n") + 1)
    } - code
    blank = sum(1 for n, text in enumerate(lines, 1) if not text.strip() and n not in commented)
    ldr = len(code) / total if total else 0.0
    issues = sorted(issues, key=lambda i: (i.line, i.column, i.pattern_id))
    per_100 = 100.0 / max(total, MIN_DENSITY_LINES)
    score = min(sum(weights.get(i.pattern_id, 0.0) for i in issues) * per_100, 100.0)
    return MobileFileAnalysis(
        file_path=file_path,
        total_lines=total,
        code_lines=len(code),
        comment_lines=len(commented),
        blank_lines=blank,
        ldr_equivalent=ldr,
        issues=issues,
        test_code=test_code,
        ast_mode=False,
        slop_score=score,
        status=classify_deficit(score).value,
    )
