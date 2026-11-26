"""`review` / `audit` and `health` read every analyzed language.

Before, the introduced/inherited attribution, the findings list and the
review targets read Python results only: in an iOS or Android repository
`review` reported no introduced file next to a new stub, while its gate
verdict (which judges every language) said otherwise.
"""

from pathlib import Path

from slop_detector.core import SlopDetector
from slop_detector.operations_payloads import build_audit_payload, build_health_payload

FILES = {
    "ios/New.swift": 'func f() { fatalError("Not implemented") }\n',
    "ios/Old.swift": "func g() -> Int { 1 }\n",
    "android/src/main/kotlin/Sync.kt": "fun s(): Int = TODO()\n",
    "tools/convert.py": "def f(x):\n    return x + 1\n",
}


def _scan(tmp_path):
    for rel, text in FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return SlopDetector(read_only=True).analyze_project(str(tmp_path))


def test_review_attributes_every_language(tmp_path):
    result = _scan(tmp_path)
    changed = ["ios/New.swift", "android/src/main/kotlin/Sync.kt"]
    payload = build_audit_payload(result, Path(tmp_path), get_changed_files_func=lambda *a: changed)
    attribution = payload["attribution"]
    assert sorted(attribution["introduced_files"]) == sorted(changed)
    assert sorted(attribution["inherited_files"]) == ["ios/Old.swift", "tools/convert.py"]
    issues = {Path(f["file_path"]).name: f["issues"] for f in payload["findings"]}
    assert issues["New.swift"] == ["swift_fatal_error_stub"]
    assert issues["Sync.kt"] == ["kotlin_todo_call"]


def test_health_targets_rank_every_language_by_score(tmp_path):
    targets = build_health_payload(_scan(tmp_path))["targets"]
    names = [Path(t["file_path"]).name for t in targets]
    assert set(names[:2]) == {"New.swift", "Sync.kt"}, names  # 25 points each, the rest 0
    assert all(t["deficit_score"] >= t2["deficit_score"] for t, t2 in zip(targets, targets[1:]))
