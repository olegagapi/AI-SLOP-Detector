"""The one list of language adapters: analyzed languages other than Python.

Every consumer of a project result (discovery, scan coverage, the project
score, the report view and every report, the AI report and work order, the
CI gate, finding summaries, history and telemetry) reads the language
results through this module, so a language added here reaches all of them.
tests/test_language_slot.py proves it with a language registered only in the
test, and fails when a consumer reads a language result directly.

Python is not an adapter: it has its own result type (FileAnalysis, in
`file_results`) and channels (hollow, docstring promises) no adapter has.

An adapter's analyzer returns, per file, an object with: file_path,
total_lines, issues (each with pattern_id, severity as a string, line,
message, suggestion, fingerprint, locator), slop_score (0-100), status
("clean" | "suspicious" | "critical_deficit"), ast_mode, source_sha256,
content_origin and to_dict(). finding_identity.bind_lines sets the last two
and the issue addresses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, FrozenSet, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class LanguageSpec:
    """One analyzed language other than Python."""

    name: str  # key in scan_coverage.analyzed, analysis_modes and progress
    label: str  # in human text
    result_key: str  # ProjectAnalysis attribute and JSON key, e.g. "js_file_results"
    extensions: FrozenSet[str]  # lower-case, with the dot
    analyzer: Callable[[], Any]  # a new analyzer; imported on first use
    fallback_mode: str  # analysis_modes name when the AST parser was not used
    ast_available: Callable[[], bool]
    fallback_not_measured: Callable[[], Tuple[str, ...]]
    # Ignore patterns for this language's files only (build output of its
    # toolchain); other languages' files under the same paths are analyzed.
    extra_ignore: Tuple[str, ...] = ()
    # Test sources by its build tool's layout (Gradle's src/test, an Xcode test
    # target): skipped like the built-in test patterns, so --include-tests (or
    # a config whose ignore list drops them) analyzes them. See ignore_patterns.
    test_ignore: Tuple[str, ...] = ()


def _js_analyzer() -> Any:
    from slop_detector.languages.js_analyzer import JSAnalyzer

    return JSAnalyzer()


def _js_ast() -> bool:
    from slop_detector.languages import js_analyzer

    return bool(js_analyzer._TS_AVAILABLE)


def _js_not_measured() -> Tuple[str, ...]:
    from slop_detector.languages import js_analyzer

    return tuple(js_analyzer.REGEX_FALLBACK_NOT_MEASURED)


def _go_analyzer() -> Any:
    from slop_detector.languages.go_analyzer import GoAnalyzer

    return GoAnalyzer()


def _go_ast() -> bool:
    # The Go analyzer has no tree-sitter path (it reads every file by regex).
    return False


def _go_not_measured() -> Tuple[str, ...]:
    from slop_detector.languages import go_analyzer

    return tuple(go_analyzer.REGEX_FALLBACK_NOT_MEASURED)


def _rust_analyzer() -> Any:
    from slop_detector.languages.rust_analyzer import RustAnalyzer

    return RustAnalyzer()


def _rust_ast() -> bool:
    from slop_detector.languages import rust_analyzer

    return bool(rust_analyzer._TS_AVAILABLE)


def _rust_not_measured() -> Tuple[str, ...]:
    from slop_detector.languages import rust_analyzer

    return tuple(rust_analyzer.REGEX_FALLBACK_NOT_MEASURED)


def _swift_analyzer() -> Any:
    from slop_detector.languages.swift_analyzer import SwiftAnalyzer

    return SwiftAnalyzer()


def _kotlin_analyzer() -> Any:
    from slop_detector.languages.kotlin_analyzer import KotlinAnalyzer

    return KotlinAnalyzer()


def _swift_ast() -> bool:
    from slop_detector.languages import swift_analyzer

    return bool(swift_analyzer.AST_AVAILABLE)


def _swift_not_measured() -> Tuple[str, ...]:
    from slop_detector.languages import swift_analyzer

    return tuple(swift_analyzer.REGEX_FALLBACK_NOT_MEASURED)


def _kotlin_ast() -> bool:
    from slop_detector.languages import kotlin_analyzer

    return bool(kotlin_analyzer.AST_AVAILABLE)


def _kotlin_not_measured() -> Tuple[str, ...]:
    from slop_detector.languages import kotlin_analyzer

    return tuple(kotlin_analyzer.REGEX_FALLBACK_NOT_MEASURED)


# Both forms of a directory pattern: ignore matching tries a "**/" pattern
# without its prefix too, so "**/x/**" also matches x/ at the scan root.
def _anywhere(*names: str) -> Tuple[str, ...]:
    return tuple(f"**/{name}/**" for name in names)


# Order is the analysis order and the order of every listing.
LANGUAGES: Tuple[LanguageSpec, ...] = (
    LanguageSpec(
        name="javascript",
        label="JS/TS",
        result_key="js_file_results",
        extensions=frozenset({".js", ".jsx", ".ts", ".tsx"}),
        analyzer=_js_analyzer,
        fallback_mode="regex_fallback",
        ast_available=_js_ast,
        fallback_not_measured=_js_not_measured,
    ),
    LanguageSpec(
        name="go",
        label="Go",
        result_key="go_file_results",
        extensions=frozenset({".go"}),
        analyzer=_go_analyzer,
        fallback_mode="regex",
        ast_available=_go_ast,
        fallback_not_measured=_go_not_measured,
    ),
    LanguageSpec(
        name="rust",
        label="Rust",
        result_key="rust_file_results",
        extensions=frozenset({".rs"}),
        analyzer=_rust_analyzer,
        fallback_mode="token_scanner",
        ast_available=_rust_ast,
        fallback_not_measured=_rust_not_measured,
        # Cargo's build directory: generated sources, not the project's code.
        extra_ignore=("target/**", "**/target/**"),
    ),
    LanguageSpec(
        name="swift",
        label="Swift",
        result_key="swift_file_results",
        extensions=frozenset({".swift"}),
        analyzer=_swift_analyzer,
        fallback_mode="token_scanner",
        ast_available=_swift_ast,
        fallback_not_measured=_swift_not_measured,
        # Not the project's code: SwiftPM's build and checkout directory, Xcode's
        # build products, CocoaPods and Carthage dependencies, package checkouts
        # of xcodebuild -clonedSourcePackagesDirPath; files written by code
        # generators (SwiftProtobuf, grpc-swift, Sourcery, SwiftGen, Apollo).
        extra_ignore=_anywhere(".build", "DerivedData", "Pods", "Carthage", "SourcePackages")
        # DocC catalogs: tutorial code is documentation, not a compiled source.
        + _anywhere("*.docc")
        + (
            "**/*.pb.swift",
            "**/*.grpc.swift",
            "**/*.generated.swift",
            "**/*+Generated.swift",
            "**/*.graphql.swift",
        ),
        # SwiftPM's Tests/ and Xcode's <Target>Tests / <Target>UITests targets,
        # and test support modules (FooTestSupport, FooMocks, ...).
        test_ignore=_anywhere(
            "Tests",
            "*Tests",
            "*TestSupport",
            "*TestHelpers",
            "*TestUtilities",
            "*TestUtils",
            "*TestFixtures",
            "*Mocks",
        ),
    ),
    LanguageSpec(
        name="kotlin",
        label="Kotlin",
        result_key="kotlin_file_results",
        extensions=frozenset({".kt", ".kts"}),
        analyzer=_kotlin_analyzer,
        fallback_mode="token_scanner",
        ast_available=_kotlin_ast,
        fallback_not_measured=_kotlin_not_measured,
        # Gradle's build/ (generated KSP, kapt and Compose sources) is a
        # default-excluded directory for every language (path_facts).
        # Gradle test source sets: test, testDebug, testFixtures, androidTest,
        # androidTestDebug, and the Multiplatform <target>Test sets
        # (commonTest, jvmTest, iosTest, androidUnitTest, ...); test support
        # in a main source set: a test or testing package or module, and
        # modules named like core-testing, data-test, test-utils.
        test_ignore=("**/src/test*/**", "**/src/androidTest*/**", "**/src/*Test/**")
        + _anywhere("test", "testing", "test-*", "*-test", "*-test-*", "*-testing", "*-testing-*"),
    ),
)


def languages() -> Tuple[LanguageSpec, ...]:
    """The registered adapters (read at call time, so a test can register one)."""
    return LANGUAGES


def spec(name: str) -> LanguageSpec:
    """The adapter called `name`."""
    for item in languages():
        if item.name == name:
            return item
    raise KeyError(name)


def spec_for_suffix(suffix: str) -> Optional[LanguageSpec]:
    """The adapter that reads files with this extension, if any."""
    suffix = suffix.lower()
    return next((item for item in languages() if suffix in item.extensions), None)


def ignore_patterns(adapter: LanguageSpec, patterns: Sequence[str]) -> List[str]:
    """The ignore patterns for `adapter`'s files: the run's patterns, the
    adapter's own exclusions and, while the run excludes tests (its patterns
    keep a built-in test pattern, which --include-tests removes), the
    adapter's test sources. Discovery and scan coverage both ask here, so a
    file they skip is a file coverage lists as excluded."""
    from slop_detector.config import DEFAULT_TEST_IGNORE_PATTERNS

    out = list(patterns) + list(adapter.extra_ignore)
    if any(p in DEFAULT_TEST_IGNORE_PATTERNS for p in patterns):
        out += list(adapter.test_ignore)
    return out


def default_patterns() -> FrozenSet[str]:
    """Every adapter's built-in exclusion pattern (a default, not a user's rule)."""
    return frozenset(p for item in languages() for p in item.extra_ignore + item.test_ignore)


def language_results(result: Any) -> List[Tuple[LanguageSpec, List[Any]]]:
    """(adapter, its file results) for every registered adapter, in order."""
    return [(item, list(getattr(result, item.result_key, []) or [])) for item in languages()]


def adapter_file_results(result: Any) -> List[Any]:
    """Every adapter file result of a project result, in registry order."""
    return [entry for _, results in language_results(result) for entry in results]


def all_file_results(result: Any) -> List[Any]:
    """Python file results, then every adapter's."""
    return list(getattr(result, "file_results", []) or []) + adapter_file_results(result)
