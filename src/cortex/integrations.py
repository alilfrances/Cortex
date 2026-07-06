from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path
from typing import Any

_GLOBAL_CLAUDE_REGISTRATION = (
    "\n# cortex\n"
    "- **cortex** (`~/.claude/skills/cortex/SKILL.md`) "
    "- Cortex repo context workflow. Trigger: `/cortex`\n"
    "When the user types `/cortex`, invoke the skill before broader repo exploration.\n"
)

_AGENTS_MARKER = "## cortex"
_CLAUDE_MARKER = "## cortex"


def _remove_section(target: Path, marker: str) -> str:
    if not target.exists():
        return "missing"
    content = target.read_text(encoding="utf-8")
    if marker not in content:
        return "missing"
    cleaned = re.sub(r"\n*## cortex\n.*?(?=\n## |\Z)", "", content, flags=re.DOTALL).rstrip()
    if cleaned:
        target.write_text(cleaned + "\n", encoding="utf-8")
    else:
        target.unlink()
    return "removed"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _remove_hook(path: Path) -> str:
    if not path.exists():
        return "missing"
    payload = _load_json(path)
    entries = payload.get("hooks", {}).get("PreToolUse", [])
    filtered = [entry for entry in entries if "cortex" not in str(entry)]
    if len(filtered) == len(entries):
        return "missing"
    payload.setdefault("hooks", {})["PreToolUse"] = filtered
    _write_json(path, payload)
    return "removed"


def uninstall_codex(project_dir: Path) -> dict[str, str]:
    project = project_dir.resolve()
    return {
        "agents": _remove_section(project / "AGENTS.md", _AGENTS_MARKER),
        "hook": _remove_hook(project / ".codex" / "hooks.json"),
    }


def codex_status(project_dir: Path) -> dict[str, bool]:
    project = project_dir.resolve()
    agents = (project / "AGENTS.md").exists() and _AGENTS_MARKER in (project / "AGENTS.md").read_text(encoding="utf-8")
    hook_path = project / ".codex" / "hooks.json"
    hook = hook_path.exists() and "cortex" in hook_path.read_text(encoding="utf-8")
    return {"agents": agents, "hook": hook}


def uninstall_claude(project_dir: Path) -> dict[str, str]:
    project = project_dir.resolve()
    return {
        "claude_md": _remove_section(project / "CLAUDE.md", _CLAUDE_MARKER),
        "hook": _remove_hook(project / ".claude" / "settings.json"),
    }


def claude_status(project_dir: Path) -> dict[str, bool]:
    project = project_dir.resolve()
    claude = (project / "CLAUDE.md").exists() and _CLAUDE_MARKER in (project / "CLAUDE.md").read_text(encoding="utf-8")
    hook_path = project / ".claude" / "settings.json"
    hook = hook_path.exists() and "cortex" in hook_path.read_text(encoding="utf-8")
    return {"claude_md": claude, "hook": hook}


def migrate(project_dir: Path) -> dict[str, str]:
    project = project_dir.resolve()
    return {
        "agents": _remove_section(project / "AGENTS.md", _AGENTS_MARKER),
        "claude_md": _remove_section(project / "CLAUDE.md", _CLAUDE_MARKER),
        "next_step": "Install the Cortex plugin from this repository and run `cortex refresh .` in the target project.",
    }


def install_global_skill(platform: str, home_dir: Path | None = None) -> dict[str, str]:
    home = (home_dir or Path.home()).resolve()
    if platform == "codex":
        dst = home / ".agents" / "skills" / "cortex" / "SKILL.md"
        src_name = "skill-codex.md"
        registration = None
    elif platform == "claude":
        dst = home / ".claude" / "skills" / "cortex" / "SKILL.md"
        src_name = "skill-claude.md"
        registration = home / ".claude" / "CLAUDE.md"
    else:
        raise ValueError(f"Unsupported platform: {platform}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    skill_text = files("cortex").joinpath(src_name).read_text(encoding="utf-8")
    dst.write_text(skill_text, encoding="utf-8")

    status = {"skill": str(dst)}
    if registration is not None:
        registration.parent.mkdir(parents=True, exist_ok=True)
        if registration.exists():
            content = registration.read_text(encoding="utf-8")
            if "# cortex" not in content:
                registration.write_text(content.rstrip() + _GLOBAL_CLAUDE_REGISTRATION, encoding="utf-8")
        else:
            registration.write_text(_GLOBAL_CLAUDE_REGISTRATION.lstrip(), encoding="utf-8")
        status["registration"] = str(registration)
    return status


def _hook_script() -> str:
    return """\
# cortex-hook-start
# Installed by: cortex hook install
if command -v cortex >/dev/null 2>&1; then
  cortex refresh . --commits 50
elif python3 -c "import cortex" >/dev/null 2>&1; then
  python3 -m cortex refresh . --commits 50
fi
# cortex-hook-end
"""


def _checkout_script() -> str:
    return """\
# cortex-checkout-hook-start
# Installed by: cortex hook install
if [ "$3" != "1" ]; then
  exit 0
fi
if command -v cortex >/dev/null 2>&1; then
  cortex refresh . --commits 50
elif python3 -c "import cortex" >/dev/null 2>&1; then
  python3 -m cortex refresh . --commits 50
fi
# cortex-checkout-hook-end
"""


def _git_root(path: Path) -> Path:
    current = path.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError(f"No git repository found at or above {path.resolve()}")


def _install_hook_block(path: Path, marker: str, script: str) -> str:
    if path.exists():
        content = path.read_text(encoding="utf-8")
        if marker in content:
            return "already installed"
        path.write_text(content.rstrip() + "\n\n" + script, encoding="utf-8")
        return "installed"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    path.chmod(0o755)
    return "installed"


def _remove_hook_block(path: Path, start_marker: str, end_marker: str) -> str:
    if not path.exists():
        return "missing"
    content = path.read_text(encoding="utf-8")
    if start_marker not in content:
        return "missing"
    cleaned = re.sub(
        rf"{re.escape(start_marker)}.*?{re.escape(end_marker)}\n?",
        "",
        content,
        flags=re.DOTALL,
    ).strip()
    if not cleaned or cleaned in ("#!/bin/sh", "#!/bin/bash"):
        path.unlink()
    else:
        path.write_text(cleaned + "\n", encoding="utf-8")
    return "removed"


def install_git_hooks(project_dir: Path) -> dict[str, str]:
    root = _git_root(project_dir)
    hooks_dir = root / ".git" / "hooks"
    return {
        "post_commit": _install_hook_block(hooks_dir / "post-commit", "# cortex-hook-start", _hook_script()),
        "post_checkout": _install_hook_block(
            hooks_dir / "post-checkout",
            "# cortex-checkout-hook-start",
            _checkout_script(),
        ),
    }


def uninstall_git_hooks(project_dir: Path) -> dict[str, str]:
    root = _git_root(project_dir)
    hooks_dir = root / ".git" / "hooks"
    return {
        "post_commit": _remove_hook_block(
            hooks_dir / "post-commit",
            "# cortex-hook-start",
            "# cortex-hook-end",
        ),
        "post_checkout": _remove_hook_block(
            hooks_dir / "post-checkout",
            "# cortex-checkout-hook-start",
            "# cortex-checkout-hook-end",
        ),
    }


def git_hook_status(project_dir: Path) -> dict[str, bool]:
    root = _git_root(project_dir)
    hooks_dir = root / ".git" / "hooks"
    post_commit = (hooks_dir / "post-commit").exists() and "# cortex-hook-start" in (
        hooks_dir / "post-commit"
    ).read_text(encoding="utf-8")
    post_checkout = (hooks_dir / "post-checkout").exists() and "# cortex-checkout-hook-start" in (
        hooks_dir / "post-checkout"
    ).read_text(encoding="utf-8")
    return {"post_commit": post_commit, "post_checkout": post_checkout}
