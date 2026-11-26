"""Token scanner for Swift and Kotlin sources (no parser dependency).

`scan(content, dialect)` reads a source into tokens and comments, the way the
Rust analyzer's token scanner does: a string literal (with everything inside
its interpolations) is one `lit` token, a comment is a comment, and the rest
is identifiers and punctuation. Rules then work on code only: a call written
inside a string or a comment is not a call.

Read as one literal:
  swift   "..." and \"\"\"...\"\"\" strings (\\( ... ) interpolations, nested
          strings in them), raw strings #"..."# / #\"\"\"...\"\"\"# with any
          number of #, extended regex literals #/.../#, and bare /.../ regex
          literals where an expression starts (after `(`, `=`, `,`, `:`, an
          operator, `return`, ...; never after a value, so `a / b / c` is
          division)
  kotlin  "..." strings (escapes, $name and ${ ... } templates), \"\"\"...\"\"\"
          raw strings (templates, no escapes; the closing run of quotes may be
          longer than three), multi-dollar strings ($$"..."), char literals
          'x' / '\\n' / '\\u0041'
Both:     // and (nested) /* */ comments, `backtick` identifiers (the token is
          the name inside the backticks).

Swift `#if`, `#else`, `#elseif`, `#endif` and every other `#name` become one
`directive` token whose text is the whole `#name`.

A source neither dialect can read (an unterminated string, comment, char
literal or backtick name) raises SourceReadError: the file fails in scan
coverage, it is never a file with no findings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

DIALECTS = ("swift", "kotlin")


class SourceReadError(ValueError):
    """A source the token scanner cannot read."""


class SwiftReadError(SourceReadError):
    """A Swift file the token scanner cannot read."""


class KotlinReadError(SourceReadError):
    """A Kotlin file the token scanner cannot read."""


_ERRORS = {"swift": SwiftReadError, "kotlin": KotlinReadError}
_LABELS = {"swift": "Swift", "kotlin": "Kotlin"}


@dataclass(frozen=True)
class Tok:
    kind: str  # ident | punct | lit | directive
    text: str
    line: int  # 1-based
    col: int  # 0-based, in characters
    end_line: int


@dataclass(frozen=True)
class Comment:
    line: int
    col: int
    text: str


# After one of these (or at the start of the file), a Swift `/` starts a bare
# regex literal when one closes on the same line; after anything else it is
# division.
_REGEX_AFTER_PUNCT = frozenset("(,=:[!&|?{};+-*%<>~^")
_REGEX_AFTER_WORDS = frozenset({"return", "case", "try", "await", "throw", "in", "where"})


def scan(content: str, dialect: str) -> Tuple[List[Tok], List[Comment]]:
    """Tokens and comments of a Swift or Kotlin source (see the module)."""
    if dialect not in DIALECTS:
        raise ValueError(f"unknown dialect {dialect!r}")
    return _Scanner(content, dialect).run()


class _Scanner:
    def __init__(self, content: str, dialect: str) -> None:
        self.src = content
        self.swift = dialect == "swift"
        self.error = _ERRORS[dialect]
        self.label = _LABELS[dialect]
        self.tokens: List[Tok] = []
        self.comments: List[Comment] = []

    # --- errors and positions ------------------------------------------------

    def fail(self, what: str, at: int) -> SourceReadError:
        line = self.src.count("\n", 0, at) + 1
        return self.error(f"not readable {self.label}: unterminated {what} at line {line}")

    # --- the top level ---------------------------------------------------------

    def run(self) -> Tuple[List[Tok], List[Comment]]:
        src = self.src
        i, line, line_start = 0, 1, 0
        n = len(src)
        if src.startswith("#!"):  # a script's interpreter line is neither code nor a comment
            i = src.find("\n")
            i = n if i < 0 else i
        while i < n:
            ch = src[i]
            if ch.isspace():
                if ch == "\n":
                    line, line_start = line + 1, i + 1
                i += 1
                continue
            kind, end = self.token_at(i)
            text = src[i:end]
            start_line, start_col = line, i - line_start
            if "\n" in text:
                line += text.count("\n")
                line_start = i + text.rfind("\n") + 1
            if kind == "comment":
                self.comments.append(Comment(start_line, start_col, text))
            else:
                if kind == "ident" and text.startswith("`"):
                    text = text[1:-1]
                self.tokens.append(Tok(kind, text, start_line, start_col, line))
            i = end
        return self.tokens, self.comments

    def token_at(self, i: int) -> Tuple[str, int]:
        """(kind, end index) of the token or comment starting at i."""
        comment = self.comment_end(i)
        if comment is not None:
            return "comment", comment
        literal = self.literal_end(i, top_level=True)
        if literal is not None:
            return "lit", literal
        return self.word_at(i)

    def word_at(self, i: int) -> Tuple[str, int]:
        """(kind, end index) of the identifier, directive, number or
        punctuation at i."""
        ch = self.src[i]
        if ch == "`":
            return "ident", self.backtick_end(i)
        if self.swift and ch == "#":
            j = self.word_end(i + 1)
            return ("directive", j) if j > i + 1 else ("punct", i + 1)
        if ch.isalpha() or ch == "_" or (self.swift and ch == "$"):
            return "ident", self.word_end(i + 1)
        if ch.isdigit():
            return "lit", self.number_end(i)
        return "punct", i + 1

    def backtick_end(self, i: int) -> int:
        """Index after a `name` whose opening backtick is at i (one line)."""
        end = self.src.find("`", i + 1)
        newline = self.src.find("\n", i + 1)
        if end < 0 or (0 <= newline < end):
            raise self.fail("backtick name", i)
        return end + 1

    # --- pieces shared by the top level and interpolations ---------------------

    def comment_end(self, k: int) -> Optional[int]:
        """Index after the comment starting at k (a line comment ends before
        its newline), or None."""
        if self.src.startswith("//", k):
            end = self.src.find("\n", k)
            return len(self.src) if end < 0 else end
        if self.src.startswith("/*", k):
            return self.block_comment_end(k)
        return None

    def run_length(self, k: int, ch: str) -> int:
        """How many `ch` characters start at k."""
        j = k
        while j < len(self.src) and self.src[j] == ch:
            j += 1
        return j - k

    def word_end(self, k: int) -> int:
        src = self.src
        while k < len(src) and (src[k].isalnum() or src[k] == "_"):
            k += 1
        return k

    def number_end(self, k: int) -> int:
        """End of a number: alphanumerics and `_`, and a `.` only before a digit
        (so `1..10` is a number, a range operator and a number)."""
        src = self.src
        while k < len(src):
            c = src[k]
            if c.isalnum() or c == "_":
                k += 1
            elif c == "." and k + 1 < len(src) and src[k + 1].isdigit():
                k += 1
            else:
                break
        return k

    def block_comment_end(self, k: int) -> int:
        """Index after a (nested) block comment whose `/*` is at k."""
        src = self.src
        depth, j = 1, k + 2
        while j < len(src) and depth:
            if src.startswith("/*", j):
                depth, j = depth + 1, j + 2
            elif src.startswith("*/", j):
                depth, j = depth - 1, j + 2
            else:
                j += 1
        if depth:
            raise self.fail("block comment", k)
        return j

    def literal_end(self, i: int, top_level: bool = False) -> Optional[int]:
        """End index of the string, char or regex literal starting at i, or None.
        A bare Swift regex is read at the top level only (not in an
        interpolation, where the token before it is not known)."""
        if self.swift:
            return self.swift_literal_end(i, top_level)
        return self.kotlin_literal_end(i)

    def skip_nested(self, k: int, opener: str, closer: str) -> int:
        """Index after the `closer` that ends a region whose `opener` is just
        before k: code, with strings and comments skipped whole."""
        src = self.src
        depth, opened = 1, k - 1
        while k < len(src):
            skipped = self.comment_end(k)
            if skipped is None:
                skipped = self.literal_end(k)
            if skipped is not None:
                k = skipped
                continue
            c = src[k]
            if c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    return k + 1
            k += 1
        raise self.fail("interpolation", opened)

    # --- Swift -------------------------------------------------------------------

    def swift_literal_end(self, i: int, top_level: bool) -> Optional[int]:
        src, ch = self.src, self.src[i]
        if ch == "#":
            hashes = self.run_length(i, "#")
            after = src[i + hashes : i + hashes + 1]
            if after == '"':
                return self.swift_string_end(i + hashes, hashes)
            if after == "/":
                return self.swift_regex_end(i + hashes, hashes)
            return None
        if ch == '"':
            return self.swift_string_end(i, 0)
        if ch == "/" and top_level and self.regex_allowed():
            return self.bare_regex_end(i)
        return None

    def swift_string_end(self, q: int, hashes: int) -> int:
        """Index after a Swift string whose first quote is at q, opened after
        `hashes` # signs (a raw string closes with as many)."""
        src = self.src
        multiline = src.startswith('"""', q)
        quote = '"""' if multiline else '"'
        close = quote + "#" * hashes
        escape = "\\" + "#" * hashes
        k = q + len(quote)
        while k < len(src):
            if src.startswith(escape, k):
                after = k + len(escape)
                if src[after : after + 1] == "(":
                    k = self.skip_nested(after + 1, "(", ")")
                else:
                    k = after + 1
                continue
            if src.startswith(close, k):
                return k + len(close)
            if src[k] == "\n" and not multiline:
                break
            k += 1
        raise self.fail("string", q)

    def swift_regex_end(self, s: int, hashes: int) -> int:
        """Index after an extended regex literal #/.../# whose `/` is at s."""
        src = self.src
        close = "/" + "#" * hashes
        k = s + 1
        while k < len(src):
            if src[k] == "\\":
                k += 2
                continue
            if src.startswith(close, k):
                return k + len(close)
            k += 1
        raise self.fail("regex literal", s)

    def regex_allowed(self) -> bool:
        """A bare `/` starts a regex literal where an expression starts."""
        if not self.tokens:
            return True
        last = self.tokens[-1]
        if last.kind == "punct":
            return last.text in _REGEX_AFTER_PUNCT
        return last.kind == "ident" and last.text in _REGEX_AFTER_WORDS

    def bare_regex_end(self, s: int) -> Optional[int]:
        """Index after a bare /.../ regex literal at s, or None (division):
        it may not start or end with a space and closes on its own line."""
        src = self.src
        if s + 1 >= len(src) or src[s + 1] in " \t\n/*":
            return None
        close = self.regex_close(s + 1)
        if close is None or src[close - 1] in " \t":
            return None
        return close + 1

    def regex_close(self, k: int) -> Optional[int]:
        """Index of the `/` closing a regex body that starts at k: the first one
        outside a [...] class and not escaped, on the same line; or None."""
        src, in_class = self.src, 0
        while k < len(src) and src[k] != "\n":
            c = src[k]
            if c == "\\":
                k += 2
                continue
            if c == "[":
                in_class += 1
            elif c == "]" and in_class:
                in_class -= 1
            elif c == "/" and not in_class:
                return k
            k += 1
        return None

    # --- Kotlin ------------------------------------------------------------------

    def kotlin_literal_end(self, i: int) -> Optional[int]:
        src, ch = self.src, self.src[i]
        if ch == '"':
            return self.kotlin_string_end(i, 1)
        if ch == "$":
            dollars = self.run_length(i, "$")
            if src[i + dollars : i + dollars + 1] == '"':
                return self.kotlin_string_end(i + dollars, dollars)
            return None
        if ch == "'":
            return self.kotlin_char_end(i)
        return None

    def kotlin_string_end(self, q: int, dollars: int) -> int:
        """Index after a Kotlin string whose first quote is at q; a template
        block opens with `dollars` $ signs and a `{`."""
        src = self.src
        template = "$" * dollars + "{"
        raw = src.startswith('"""', q)
        k = q + (3 if raw else 1)
        while k < len(src):
            if src.startswith(template, k):
                k = self.skip_nested(k + len(template), "{", "}")
                continue
            c = src[k]
            if raw:
                if src.startswith('"""', k):
                    return k + self.run_length(k, '"')
            elif c == "\\":
                k += 2
                continue
            elif c == '"':
                return k + 1
            elif c == "\n":
                break
            k += 1
        raise self.fail("string", q)

    def kotlin_char_end(self, q: int) -> int:
        """Index after a char literal whose opening quote is at q."""
        src = self.src
        k = q + 1
        if src[k : k + 1] == "\\":
            k += 2
            while k < len(src) and src[k] != "'" and src[k] != "\n" and k - q < 8:
                k += 1
        else:
            k += 1
        if src[k : k + 1] != "'":
            raise self.fail("char literal", q)
        return k + 1
