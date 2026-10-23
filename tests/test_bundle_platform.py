"""Task 5.6 (§F fix 7): platform variants (`_win`, `_mac`, `_linux`, ...) are demoted unless the task names the platform."""
from __future__ import annotations

import pytest

from cortex.bundle import PLATFORM_DEMOTION, _platform_of, generate_bundle
from tests.bundle_shapes import OVERRIDE_TASK, make_repo, override_files


def _score(bundle: dict, path: str) -> float:
    return next(item["score"] for item in bundle["items"] if item["path"] == path and item["kind"] == "code")


@pytest.mark.parametrize(
    ("path", "key"),
    [
        ("src/wm_devicechange_win.cpp", "win"),
        ("src/card_windows.cpp", "win"),
        ("src/card_MacOS.mm", "mac"),
        ("src/card_linux.cpp", "linux"),
        ("src/card_android.cpp", "android"),
        ("src/card_ios.mm", "ios"),
        ("src/ios/card.mm", "ios"),
        ("platform/win/card.cpp", "win"),
        ("src/card.cpp", None),
        ("src/window.cpp", None),  # not a `_<platform>` stem
        ("src/winner_card.cpp", None),
        ("src/card_ios_helper.cpp", None),  # the key must end the stem
    ],
)
def test_platform_of_names_the_variant_a_path_belongs_to(path, key):
    assert _platform_of(path) == key


def _overrider_repo(tmp_path):
    """Five overriders of `Base::apply` (with `derived.cpp`); the Windows one sorts first by path, so only the
    demotion keeps it out of the OVERRIDES_PER_METHOD best."""
    files = override_files()
    for stem in ("aaa_win", "p1", "p2", "p3"):
        cls = "".join(part.capitalize() for part in stem.split("_")) + "Impl"
        files[f"src/{stem}.h"] = f'#include "base.h"\nclass {cls} : public Base {{\npublic:\n    void apply(int v) override;\n}};\n'
        files[f"src/{stem}.cpp"] = (
            f'#include "{stem}.h"\nvoid {cls}::apply(int v) {{\n    slot_ = v + 1;\n    if (slot_ > 9) {{\n        slot_ = 0;\n    }}\n}}\n'
        )
    return make_repo(tmp_path / "repo", files)


def _routed(bundle: dict) -> dict[str, dict]:
    return {
        item["path"]: item["metadata"]["call_expansion"]
        for item in bundle["items"]
        if item["metadata"].get("call_expansion", {}).get("type") == "override"
    }


def test_an_overrider_in_a_platform_file_is_ranked_after_the_others(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    repo = _overrider_repo(tmp_path)

    bundle = generate_bundle(repo, task=OVERRIDE_TASK, budget=4000, output_format="json")

    routed = _routed(bundle)
    assert {"src/derived.cpp", "src/p1.cpp", "src/p2.cpp", "src/p3.cpp"} <= set(routed), sorted(routed)
    assert "src/aaa_win.cpp" not in routed, sorted(routed)


def test_the_overrider_of_a_named_platform_keeps_its_place(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    repo = _overrider_repo(tmp_path)

    bundle = generate_bundle(repo, task=OVERRIDE_TASK + " on windows", budget=4000, output_format="json")

    routed = _routed(bundle)
    assert "src/aaa_win.cpp" in routed, sorted(routed)
    assert "src/p3.cpp" not in routed, sorted(routed)  # the fifth by path is the one left out


def _twin_repo(tmp_path):
    body = "// frobnicate the zorble gadget\nint frobnicate_zorble(int v) {\n    return v + 1;\n}\n"
    return make_repo(
        tmp_path / "repo",
        {"src/zorble_win.cpp": body, "src/zorble_gtk.cpp": body, "src/zorble_macos.cpp": body, "src/other.cpp": "int o() { return 0; }\n"},
    )


def test_a_platform_file_scores_below_its_twin_unless_the_task_names_the_platform(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    repo = _twin_repo(tmp_path)

    plain = generate_bundle(repo, task="frobnicate zorble gadget", budget=4000, output_format="json")
    named = generate_bundle(repo, task="frobnicate zorble gadget on windows", budget=4000, output_format="json")

    assert _score(plain, "src/zorble_win.cpp") < _score(plain, "src/zorble_gtk.cpp")
    assert _score(plain, "src/zorble_macos.cpp") < _score(plain, "src/zorble_gtk.cpp")
    assert _score(plain, "src/zorble_win.cpp") <= _score(plain, "src/zorble_gtk.cpp") * (PLATFORM_DEMOTION + 0.05)
    assert _score(named, "src/zorble_win.cpp") == pytest.approx(_score(named, "src/zorble_gtk.cpp"), rel=0.01)
    assert _score(named, "src/zorble_macos.cpp") < _score(named, "src/zorble_gtk.cpp")  # only the named one is spared


def test_a_configured_product_name_stands_for_its_platform(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    repo = _twin_repo(tmp_path)
    (repo / ".cortex").mkdir()
    (repo / ".cortex/config.toml").write_text('[query]\nplatform_terms = { explorer = "win" }\n')

    bundle = generate_bundle(repo, task="frobnicate zorble gadget explorer", budget=4000, output_format="json")

    assert _score(bundle, "src/zorble_win.cpp") == pytest.approx(_score(bundle, "src/zorble_gtk.cpp"), rel=0.01)
    assert _score(bundle, "src/zorble_macos.cpp") < _score(bundle, "src/zorble_gtk.cpp")


def test_the_documented_synonyms_example_bridges_usb_to_the_os_device_event_file(tmp_path, monkeypatch):
    """README / CHANGELOG example for the T2 vocabulary gap: no code change, the `synonyms` config."""
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    repo = make_repo(
        tmp_path / "repo",
        {"src/wm_devicechange_win.cpp": "int onEvent(int code) {\n    return code + 1;\n}\n", "src/other.cpp": "int o() { return 0; }\n"},
    )
    task = "usb unplug"

    before = generate_bundle(repo, task=task, budget=4000, output_format="json")
    (repo / ".cortex").mkdir()
    (repo / ".cortex/config.toml").write_text('[query]\nsynonyms = { usb = ["devicechange"] }\n')
    after = generate_bundle(repo, task=task, budget=4000, output_format="json")

    assert _score(after, "src/wm_devicechange_win.cpp") > _score(before, "src/wm_devicechange_win.cpp") + 20


def test_a_windows_directory_in_ui_code_is_not_a_platform_variant():
    """`ui/windows/mainwindow.cpp` is window management, not the Windows OS; the `_windows` stem suffix
    and the `win`/`win32` directories still are."""
    from cortex.bundle import _platform_of

    assert _platform_of("src/ui/windows/mainwindow.cpp") is None
    assert _platform_of("src/usb/hotplug_windows.cpp") == "win"
    assert _platform_of("src/win32/hotplug.cpp") == "win"
