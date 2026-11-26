"""Findings as diagnostics other tools read: one line per finding.

`--format xcode` writes the compiler diagnostic format,
`<absolute path>:<line>:<column>: warning: <message>`, which Xcode shows in
the editor and the Issue navigator when a Run Script build phase prints it
(and which other tools that read compiler output understand). `--format
github` writes GitHub Actions workflow commands (`::warning file=...::...`),
which GitHub shows as annotations on the pull request's changed lines.

Both list every pattern finding of every analyzed language (the score's
findings: suppressed findings and review candidates are not listed), and a
file that could not be analyzed, so a failed file is not mistaken for a
clean one. Columns are 1-based. Neither changes the exit code: the gate and
threshold options decide it as for every other format.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

_SEVERITIES = ("critical", "high", "medium", "low")
# GitHub annotation levels: critical and high findings stand out as errors on
# the pull request; an annotation never fails a job by itself.
_GITHUB_LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "notice"}


@dataclass(frozen=True)
class Diagnostic:
    path: str  # absolute
    line: int  # 1-based; 0 when unknown
    column: Optional[int]  # 1-based, or None
    severity: str
    rule: str
    message: str
    fix: str


def diagnostics(result: Any) -> List[Diagnostic]:
    """Every finding and every failed file of a project or single-file result,
    in reading order (path, line, column, rule)."""
    from slop_detector.language_registry import all_file_results

    is_project = hasattr(result, "project_path")
    files = all_file_results(result) if is_project else [result]
    found = [_finding(f, issue) for f in files for issue in _issues(f)]
    if is_project:
        found += _failed_files(result)
    return sorted(found, key=lambda d: (d.path, d.line, d.column or 0, d.rule, d.message))


def render_xcode(result: Any) -> str:
    """One `path:line:column: warning: message` line per diagnostic."""
    lines = []
    for d in diagnostics(result):
        where = f"{d.path}:{max(d.line, 1)}" + (f":{d.column}" if d.column else "")
        text = f"{d.message} [{d.rule}, {d.severity}]"
        if d.fix:
            text += f" Fix: {d.fix}"
        lines.append(f"{where}: warning: {_one_line(text)}")
    return "".join(line + "\n" for line in lines)


def render_github(result: Any, root: Optional[str] = None) -> str:
    """One GitHub Actions annotation per diagnostic, most severe first (GitHub
    shows a limited number per step). Paths are relative to `root`, by default
    $GITHUB_WORKSPACE (the repository checkout) or the working directory."""
    base = root or os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
    lines = []
    ordered = sorted(diagnostics(result), key=lambda d: _SEVERITIES.index(d.severity))
    for d in ordered:
        props = [f"file={_escape_property(_relative(d.path, base))}"]
        if d.line:
            props.append(f"line={d.line}")
        if d.column:
            props.append(f"col={d.column}")
        props.append(f"title={_escape_property(f'{d.rule} ({d.severity})')}")
        body = d.message + (f"\nFix: {d.fix}" if d.fix else "")
        lines.append(f"::{_GITHUB_LEVEL[d.severity]} {','.join(props)}::{_escape_data(body)}")
    return "".join(line + "\n" for line in lines)


def gate_line(fmt: str, gate: str, message: str) -> str:
    """The line that says the gate failed the build (it decided the exit code):
    an Xcode build error, or a GitHub error annotation."""
    text = f"AI-SLOP Detector {gate} failed: {_one_line(message)}"
    if fmt == "xcode":
        return f"error: {text}\n"
    return f"::error title={_escape_property('AI-SLOP Detector gate')}::{_escape_data(text)}\n"


# --- reading results ----------------------------------------------------------


def _issues(f: Any) -> List[Any]:
    if hasattr(f, "pattern_issues"):
        return list(getattr(f, "pattern_issues", None) or [])
    return list(getattr(f, "issues", None) or [])


def _severity(issue: Any) -> str:
    raw = getattr(issue, "severity", "low")
    value = str(getattr(raw, "value", raw)).lower()
    return value if value in _SEVERITIES else "low"


def _absolute(path: str) -> str:
    # abspath, not resolve: the path as the build gave it ($SRCROOT may be a
    # symlink), which is the path Xcode matches to the project's file.
    return os.path.abspath(path) if path else path


def _finding(f: Any, issue: Any) -> Diagnostic:
    column = getattr(issue, "column", None)
    return Diagnostic(
        path=_absolute(str(getattr(f, "file_path", "") or "")),
        line=int(getattr(issue, "line", 0) or 0),
        column=column + 1 if isinstance(column, int) and column >= 0 else None,
        severity=_severity(issue),
        rule=str(getattr(issue, "pattern_id", "")),
        message=str(getattr(issue, "message", "")),
        fix=str(getattr(issue, "suggestion", "") or ""),
    )


def _failed_files(result: Any) -> List[Diagnostic]:
    coverage = getattr(result, "scan_coverage", None) or {}
    root = Path(str(getattr(result, "project_path", "") or "."))
    out = []
    for entry in (coverage.get("failed") or {}).get("files", []) or []:
        error = entry.get("error_type") or "error"
        out.append(
            Diagnostic(
                path=_absolute(str(root / str(entry.get("path", "")))),
                line=1,
                column=None,
                severity="high",
                rule="analysis_failed",
                message=f"AI-SLOP could not analyze this file ({error}): it was not measured",
                fix="Check that the file is valid source; scan_coverage.failed lists it",
            )
        )
    return out


# --- text ---------------------------------------------------------------------


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _relative(path: str, base: str) -> str:
    try:
        rel = os.path.relpath(path, base)
    except ValueError:  # another drive on Windows
        return path.replace("\\", "/")
    return (path if rel.startswith("..") else rel).replace("\\", "/")


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")
