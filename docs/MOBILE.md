# iOS and Android projects

AI-SLOP Detector reads Swift (`.swift`) and Kotlin (`.kt`, `.kts`) with the
base install: no extra, no Xcode, no JDK, no Gradle. It runs anywhere Python
3.8+ runs, so a pull request check can run on a Linux runner. This page says
what it reads in an Xcode, SwiftPM, CocoaPods or Gradle project, and how to
put it in an Xcode build phase, a Gradle task, CI and pre-commit.

Every rule, with what it reports and what it leaves alone:
[PATTERNS.md, Swift](PATTERNS.md#swift) and [Kotlin](PATTERNS.md#kotlin).

## What a scan reads

```bash
pip install ai-slop-detector          # or: pipx install ai-slop-detector
slop-detector --project .             # from the repository root
```

| | Swift | Kotlin |
|---|---|---|
| Stubs (high) | `fatalError("Not implemented")`, `preconditionFailure("TODO")` | `TODO()`, `TODO("Not yet implemented")`, `throw NotImplementedError()`, `error("not implemented")` |
| Swallowed errors (medium) | `catch {}` | `catch (e: Exception) {}`, `e.printStackTrace()` |
| Debug prints (medium) | `print()`, `debugPrint()`, `dump()` | `println()`, `print()`, `System.out.println()` in Android and Compose code |
| Markers (low, not scored) | `// TODO:`, `// FIXME`, `// HACK`, `// XXX` | the same |

Not reported: test code (a file that imports XCTest, Swift Testing, Quick,
Nimble; JUnit, kotlin.test, Kotest, AndroidX Test, MockK, ...), the
`init?(coder:)` template, prints inside `#if DEBUG` or
`if (BuildConfig.DEBUG)`, prints of a command-line program or a script, and a
call inside a string, an interpolation or a comment.

Skipped without configuration, and listed as excluded in the scan coverage
(`scan_coverage.excluded`, the Markdown and HTML reports):

| Layout | Skipped for that language |
|---|---|
| CocoaPods, Carthage, SwiftPM, Xcode | `Pods/`, `Carthage/`, `.build/`, `SourcePackages/`, `DerivedData/` |
| Swift code generators | `*.pb.swift`, `*.grpc.swift`, `*.generated.swift` (Sourcery), `*+Generated.swift` (SwiftGen), `*.graphql.swift` (Apollo) |
| DocC | `*.docc/` (tutorial code) |
| Gradle | `build/` (generated KSP, kapt and Compose sources) for every language |
| Xcode and SwiftPM tests | `Tests/`, `*Tests/` (`AppTests`, `AppUITests`), `*TestSupport/`, `*TestHelpers/`, `*TestUtilities/`, `*TestUtils/`, `*TestFixtures/`, `*Mocks/` |
| Gradle tests | `src/test*/`, `src/androidTest*/`, `src/*Test/` (`commonTest`, `jvmTest`, `androidUnitTest`, ...), a `test` or `testing` package or module, modules named `test-*`, `*-test`, `*-test-*`, `*-testing`, `*-testing-*` |

`--include-tests` analyzes the test directories. A production directory whose
name matches a test pattern (one that ends in `Tests` or `Mocks`, a Gradle
module named `ab-test`) is skipped too: check the excluded list once when you
adopt the tool. Other directories are excluded with `ignore` in
`.slopconfig.yaml` ([CONFIGURATION.md](CONFIGURATION.md)):

```yaml
ignore:
  - "tests/**"            # keep the built-in test defaults when you list your own
  - "Snapshots/**"        # fixture code read by snapshot tests
  - "Vendor/**"
```

Objective-C, Java and Dart files are not analyzed; scan coverage lists them as
unsupported.

## Xcode: findings as build warnings

`--format xcode` prints one `path:line:column: warning: message` line per
finding, which Xcode shows in the editor and the Issue navigator when a Run
Script build phase prints it.

1. In the app target: **Build Phases > + > New Run Script Phase**, named
   "AI-SLOP Detector", after **Compile Sources**. Uncheck **Based on dependency
   analysis** so it runs on every build.
2. Script:

   ```sh
   # Xcode starts the script with a minimal PATH; add where pipx, uv or Homebrew install.
   export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
   if ! command -v slop-detector >/dev/null 2>&1; then
     echo "warning: AI-SLOP Detector is not installed (pipx install ai-slop-detector)"
     exit 0
   fi
   slop-detector --project "$SRCROOT" --format xcode --read-only
   ```

3. In the target's **Build Settings**, set **User Script Sandboxing**
   (`ENABLE_USER_SCRIPT_SANDBOXING`) to **No**. With the sandbox on (the default
   for targets created in Xcode 15 and later), a script cannot read the
   project's source files and the phase fails.

`--read-only` writes nothing: no history, no cache. A scan of a 1,300-file
Swift app took 3 seconds on an Apple M-series laptop (wikipedia-ios, measured
2026-11-26); set `$SRCROOT/..` or a sub-folder when the target's sources are
elsewhere.

To fail the build on the hard gate (a file at score 70 or more), replace the
last line with the line below. The gate is refused together with
`--read-only`; `--no-history` keeps the scan from recording history. A failed
gate adds one `error:` line that says why, and Xcode stops the build.

```sh
slop-detector --project "$SRCROOT" --format xcode --ci-mode hard --no-history
```

## Android: a Gradle task

In the root `build.gradle.kts`:

```kotlin
plugins {
    base // a root `check` task to hang the scan on
    // ... your existing `apply false` plugins
}

val slopDetector by tasks.registering(Exec::class) {
    group = "verification"
    description = "AI-SLOP Detector: unfinished code, hard gate"
    workingDir = rootDir
    commandLine("slop-detector", "--project", ".", "--ci-mode", "hard", "--ci-report", "--no-history")
}

tasks.named("check") { dependsOn(slopDetector) }
```

`./gradlew slopDetector` prints the gate report and fails the task when the
gate fails (the build stops with the gate's exit code). The task reads no
Gradle inputs, so it runs on every invocation, and it is compatible with the
configuration cache. Without the `base` plugin, run `./gradlew slopDetector`
on its own in CI.

## Pull requests: GitHub Actions

The analyzer needs no Xcode or Android SDK; an `ubuntu-latest` runner scans
an iOS app as well as an Android one.

```yaml
name: AI-SLOP
on: pull_request
permissions:
  contents: read
jobs:
  slop:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install ai-slop-detector
      # Every finding as an annotation on the pull request's lines.
      - run: slop-detector --project . --format github --read-only
      # Fail the check on the hard gate, with the gate report in the log.
      - run: slop-detector --project . --ci-mode hard --ci-report --no-history
```

`--format github` writes GitHub workflow commands; critical and high
findings are errors, medium warnings and low notices (an annotation does not
fail the job; the gate step does). GitHub shows a limited number of
annotations per step, so they are written most severe first. Paths are
relative to `$GITHUB_WORKSPACE`, the checkout.

## Adopting it in an existing app

The hard gate judges files: a file fails at a deficit score of 70 or more,
which a Swift or Kotlin file reaches with three stubs in 100 lines (a stub
weighs 25, an empty catch 10, a debug print 5 per 100 lines; one stub alone
never fails). An app with old findings can start with:

- `slop-detector --project . --ci-mode soft --ci-report`: the report, never a
  failure;
- `slop-detector review . --base origin/main --json`: which files of the
  change carry findings (`attribution.introduced_files`) apart from the ones
  that were there before, and the gate verdict (`should_fail_build`);
- `--fail-threshold 30`: fail when the project's weighted score passes 30.

## pre-commit

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/flamehaven01/AI-SLOP-Detector
    rev: <tag>  # the first release with Swift and Kotlin analysis, or later
    hooks:
      - id: slop-detector-patterns   # each changed file, fails above score 50
```

The hooks run for every analyzed language, Swift and Kotlin included (they
need pre-commit 2.9 or later). Without pre-commit, a plain git hook that scans
the staged files of every analyzed language with the hard gate (and refuses a
staged file with unstaged changes) is installed from Python:

```bash
python -c "from slop_detector.git_integration import GitIntegration; GitIntegration.install_pre_commit_hook('.')"
```

## In the results

- JSON: `swift_file_results` and `kotlin_file_results`, one object per file
  (`slop_score`, `status`, `issues` with `pattern_id`, `severity`, `line`,
  `column`, `message`, `suggestion`), and `test_code: true` for a file that
  imports a test framework.
- `scan_coverage.analysis_modes`: `token_scanner` for both languages; the
  scanner measures every rule.
- A file the scanner cannot read (an unterminated string or comment) is a
  failed file of its language in `scan_coverage.failed`
  (`SwiftReadError`, `KotlinReadError`), never a file with no findings;
  `--format xcode` and `github` list it.

## Limits

- Inline suppression comments (`slop-disable-next-line`), `--disable` and
  `patterns.disabled` apply to Python rules only: a Swift or Kotlin rule id in
  `patterns.disabled` has no effect, and `--disable` with one stops with exit
  code 2. Swift and Kotlin findings are excluded by path (`ignore`), or resolved.
- An `ignore` list in `.slopconfig.yaml` replaces the built-in list. Keep
  `tests/**` in it: without a built-in test pattern the Xcode and Gradle test
  directories are analyzed (the dependency and build-output exclusions apply
  either way).
- Rules read tokens, not types: `print(...)` is the standard library's unless
  the file declares a function, value or parameter named `print`; a call
  inside a string interpolation is not read.
- The token scanner reads Swift 6 and Kotlin 2 syntax. Before release it was
  checked against the tree-sitter grammars on 13 public iOS and Android
  repositories (about 20,000 files): every string, char literal and comment
  boundary agreed on Kotlin; on Swift, 7 files differed, each one a
  tree-sitter misreading (a `"/*"` inside a string, `#warning("...")`).
