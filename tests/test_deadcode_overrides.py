from __future__ import annotations

import subprocess
from pathlib import Path

from cortex.deadcode import analyze_dead_code
from cortex.ingest import ingest_repository

FILES = {
    "src/Base.java": "public class Base {\n    public void onCreate() {}\n    public void unusedBase() {}\n}\n",
    "src/Main.java": (
        "import android.app.Activity;\n"
        "public class Main extends Activity {\n"
        "    @Override\n"
        "    protected void onNewIntent(Intent i) { }\n"
        "    public void plainUnused() { }\n"
        "}\n"
        "class Derived extends Base {\n"
        "    @Override\n"
        "    public void onCreate() { }\n"
        "    public void sameNameAsBaseButNoMarker() { }\n"
        "}\n"
    ),
    "src/w.hpp": (
        "class Widget : public QObject {\n"
        "public:\n"
        "    int paintHook(int x) override;\n"
        "    virtual int vfun(int x);\n"
        "    int plain(int x);\n"
        "};\n"
    ),
    "src/w.cpp": (
        '#include "w.hpp"\n'
        "int Widget::paintHook(int x) { return x; }\n"
        "int Widget::vfun(int x) { return x; }\n"
        "int Widget::plain(int x) { return x; }\n"
    ),
    "src/Cell.swift": (
        "class Cell: UIView {\n"
        "    override func layoutSubviews() { }\n"
        "    func neverCalled() { }\n"
        "}\n"
    ),
    "src/ResolvedBase.java": (
        "class Parent { public void shared() { } }\n"
        "class Child extends Parent { public void shared() { } public void ownOnly() { } }\n"
    ),
}


def _findings(tmp_path: Path, monkeypatch) -> dict[tuple[str, str], dict]:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    for rel, content in FILES.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=repo, check=True)
    ingest_repository(repo)
    return {(item["file"], item["symbol"]): item for item in analyze_dead_code(repo)["findings"]}


def test_framework_overrides_are_low_confidence_and_plain_methods_are_not(tmp_path, monkeypatch):
    findings = _findings(tmp_path, monkeypatch)
    override = "overrides a base-class or framework method"

    for key in (
        ("src/Main.java", "onNewIntent"),     # Android lifecycle, @Override
        ("src/Main.java", "onCreate"),        # @Override of a repo base method
        ("src/w.cpp", "paintHook"),           # out-of-line definition, `override` in the header
        ("src/w.cpp", "vfun"),                # `virtual` in the header
        ("src/Cell.swift", "layoutSubviews"),  # Swift `override func`
    ):
        assert key in findings, key
        assert findings[key]["confidence"] == "low", key
        assert override in findings[key]["reason"], key

    for key in (
        ("src/Main.java", "plainUnused"),
        ("src/Cell.swift", "neverCalled"),
        ("src/ResolvedBase.java", "ownOnly"),
        ("src/Base.java", "unusedBase"),
    ):
        assert key in findings, key
        assert findings[key]["confidence"] == "high", key


def test_same_name_as_a_resolved_base_method_counts_as_an_override(tmp_path, monkeypatch):
    findings = _findings(tmp_path, monkeypatch)
    shared = [item for key, item in findings.items() if key == ("src/ResolvedBase.java", "shared")]
    # `Parent.shared` and `Child.shared` collapse to one graph label per file; whichever is
    # reported, the one in the subclass must not be high confidence.
    assert all(item["confidence"] != "high" for item in shared)
