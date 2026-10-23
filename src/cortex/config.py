from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_RELATIVE_PATH = ".cortex/config.toml"

# Platform variants of a file (`wm_devicechange_win.cpp`, a `mac/` directory): the words that name each
# platform. A task that uses one of the words (or a `platform_terms` product of that platform) is about it.
PLATFORM_SUFFIXES: dict[str, frozenset[str]] = {
    "win": frozenset({"windows", "win", "win32"}),
    "mac": frozenset({"mac", "macos", "osx", "darwin"}),
    "linux": frozenset({"linux"}),
    "android": frozenset({"android"}),
    "ios": frozenset({"ios"}),
}


@dataclass(frozen=True)
class CortexConfig:
    connect_functions: list[str] = field(default_factory=lambda: ["connect"])
    noise_identifiers: list[str] = field(default_factory=list)
    skip_dirs: list[str] = field(default_factory=list)
    synonyms: dict[str, list[str]] = field(default_factory=dict)
    # product name -> platform key of PLATFORM_SUFFIXES: a task naming the product names the platform
    platform_terms: dict[str, str] = field(default_factory=dict)


def _string_list(section: dict, key: str, default: list[str], config_path: Path) -> list[str]:
    raw = section.get(key, default)
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"malformed Cortex config at {config_path}: '{key}' must be a list of strings")
    return list(raw)


def _platform_key(value: object) -> str | None:
    """The key of PLATFORM_SUFFIXES a configured platform names (`win`, or any word of its set)."""
    if not isinstance(value, str):
        return None
    word = value.strip().lower()
    return next((key for key, words in PLATFORM_SUFFIXES.items() if word == key or word in words), None)


def _section(data: dict, key: str, config_path: Path) -> dict:
    raw = data.get(key, {})
    if not isinstance(raw, dict):
        raise ValueError(f"malformed Cortex config at {config_path}: '[{key}]' must be a table")
    return raw


def load_config(repo_root: Path) -> CortexConfig:
    """Repo-local Cortex config from .cortex/config.toml; defaults when absent."""
    config_path = repo_root / CONFIG_RELATIVE_PATH
    if not config_path.exists():
        return CortexConfig()
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
        raise ValueError(f"malformed Cortex config at {config_path}: {exc}") from exc

    parsing = _section(data, "parsing", config_path)
    ingest = _section(data, "ingest", config_path)
    query = _section(data, "query", config_path)

    raw_synonyms = query.get("synonyms", {})
    if not isinstance(raw_synonyms, dict):
        raise ValueError(f"malformed Cortex config at {config_path}: 'synonyms' must be a table")
    synonyms: dict[str, list[str]] = {}
    for key, values in raw_synonyms.items():
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError(
                f"malformed Cortex config at {config_path}: synonym {key!r} must map to a list of strings"
            )
        synonyms[str(key)] = list(values)

    raw_platforms = query.get("platform_terms", {})
    if not isinstance(raw_platforms, dict):
        raise ValueError(f"malformed Cortex config at {config_path}: 'platform_terms' must be a table")
    platform_terms: dict[str, str] = {}
    for product, platform in raw_platforms.items():
        key = _platform_key(platform)
        if key is None:
            raise ValueError(
                f"malformed Cortex config at {config_path}: platform_terms {product!r} must map to one of "
                f"{', '.join(PLATFORM_SUFFIXES)} (or a word naming one), not {platform!r}"
            )
        platform_terms[str(product)] = key

    return CortexConfig(
        connect_functions=_string_list(parsing, "connect_functions", ["connect"], config_path),
        noise_identifiers=_string_list(parsing, "noise_identifiers", [], config_path),
        skip_dirs=_string_list(ingest, "skip_dirs", [], config_path),
        synonyms=synonyms,
        platform_terms=platform_terms,
    )
