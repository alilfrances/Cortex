from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .models import CommitRecord


def discover_repo_root(repo_path: Path) -> Path:
    candidate = repo_path.resolve()
    result = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"{candidate} is not inside a git repository")
    return Path(result.stdout.strip())


def detect_repo_root(repo_path: Path | str) -> Path:
    return discover_repo_root(Path(repo_path))


_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"
_HEADER_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?\x1f")


def _split_records(stdout: str) -> list[str]:
    """Split `git log` output on the record separator that opens each header.

    A path containing a literal 0x1E would otherwise start a bogus record, so
    a chunk only starts a new record when it begins with a full object id
    followed by the field separator; anything else is glued back on.
    """
    records: list[str] = []
    for chunk in stdout.split(_RECORD_SEP):
        if _HEADER_RE.match(chunk) or not records:
            if chunk:
                records.append(chunk)
        else:
            records[-1] += _RECORD_SEP + chunk
    return records


def _parse_record(chunk: str) -> CommitRecord | None:
    # The header (`%H %an %at %s`, never containing a newline or NUL) ends at
    # the first newline when the commit lists files, or at the NUL terminator
    # when it does not (merges without -m, --allow-empty commits).
    cut = min((i for i in (chunk.find("\n"), chunk.find("\0")) if i >= 0), default=len(chunk))
    header, body = chunk[:cut], chunk[cut + 1:]
    parts = header.split(_FIELD_SEP, maxsplit=3)
    if len(parts) != 4:
        return None
    sha, author, authored_at, summary = parts
    try:
        timestamp = int(authored_at)
    except ValueError:
        return None

    files: list[str] = []
    renames: list[tuple[str, str]] = []
    tokens = body.split("\0")
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if not status:
            continue
        if status[0] in "RC":
            if index + 1 >= len(tokens):
                break  # truncated record
            old_path, new_path = tokens[index], tokens[index + 1]
            index += 2
            if not new_path:
                continue
            files.append(new_path)
            if status[0] == "R" and old_path and old_path != new_path:
                renames.append((old_path, new_path))
            continue
        if index >= len(tokens):
            break
        path = tokens[index]
        index += 1
        if path:
            files.append(path)
    return CommitRecord(
        sha=sha,
        author=author,
        authored_at=timestamp,
        summary=summary,
        files=files,
        renames=renames,
    )


def collect_recent_commits(repo_path: Path, limit: int) -> list[CommitRecord]:
    """The newest ``limit`` commits, newest first, with rename detection.

    Uses ``--name-status -M -z``: paths come back verbatim (no C-quoting of
    non-ASCII or special characters), a rename ``R<score> old new`` puts the
    new path in ``files`` and ``(old, new)`` in ``renames``, and a commit that
    lists no files (a merge, an ``--allow-empty`` commit) is its own record
    with ``files == []`` instead of swallowing the next commit's header.
    Merge commits list no files (no ``-m``), as before.
    """
    if limit <= 0:
        return []

    format_string = "%x1e%H%x1f%an%x1f%at%x1f%s"
    result = subprocess.run(
        [
            "git", "-C", str(repo_path),
            "-c", "log.showSignature=false",
            "log", f"-{limit}",
            f"--pretty=format:{format_string}",
            "--name-status", "-M", "-z", "--no-color",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return []

    records: list[CommitRecord] = []
    for chunk in _split_records(result.stdout):
        record = _parse_record(chunk)
        if record is not None:
            records.append(record)
    return records


def canonical_commit_files(commits: Iterable[CommitRecord | Mapping[str, Any]]) -> list[list[str]]:
    """Each commit's touched paths mapped to the name that file has *now*.

    ``commits`` must be newest-first (as :func:`collect_recent_commits` returns
    them). The result is index-aligned with ``commits``; within a commit, paths
    are normalised (``./`` and backslashes), blank entries dropped, and
    duplicates removed (first occurrence wins, so git's order is kept).

    History is followed across renames newest-to-oldest: once a commit renames
    ``old -> new``, every *older* commit's ``old`` means whatever ``new`` is
    called today. The rename target ``new`` stops being an alias for older
    commits -- before the rename that name belonged to a different file (or
    none) -- so a later re-use of a name never merges unrelated histories.
    Alias values are always already canonical, so no chain is followed at
    lookup time and rename cycles (``a -> b`` then ``b -> a``) cannot loop.
    Commits without ``renames`` (hand-built records, old databases) are
    returned unchanged apart from normalisation and de-duplication.
    """
    alias: dict[str, str] = {}
    result: list[list[str]] = []
    for commit in commits:
        files, renames = _commit_parts(commit)
        canonical: list[str] = []
        seen: set[str] = set()
        for path in files:
            mapped = alias.get(path, path)
            if mapped not in seen:
                seen.add(mapped)
                canonical.append(mapped)
        result.append(canonical)
        if renames:
            targets = [(old, alias.get(new, new)) for old, new in renames]
            for _old, new in renames:
                alias.pop(new, None)
            for old, target in targets:
                if old == target:
                    alias.pop(old, None)
                else:
                    alias[old] = target
    return result


def canonical_path_map(commits: Iterable[CommitRecord | Mapping[str, Any]]) -> dict[str, str]:
    """``{historical path: current path}`` for every path renamed in ``commits``.

    ``commits`` newest-first. Each renamed-away path maps to the name its file
    has at the newest commit; when the same old name was renamed away more
    than once, the most recent rename wins (the mapping older commits see is
    available per commit from :func:`canonical_commit_files`). Paths that were
    never renamed are absent. Deterministic and cycle-safe.
    """
    mapping: dict[str, str] = {}
    alias: dict[str, str] = {}
    for commit in commits:
        _files, renames = _commit_parts(commit)
        if not renames:
            continue
        targets = [(old, alias.get(new, new)) for old, new in renames]
        for _old, new in renames:
            alias.pop(new, None)
        for old, target in targets:
            if old == target:
                alias.pop(old, None)
                continue
            alias[old] = target
            mapping.setdefault(old, target)
    return dict(sorted(mapping.items()))


def _commit_parts(commit: CommitRecord | Mapping[str, Any]) -> tuple[list[str], list[tuple[str, str]]]:
    if isinstance(commit, Mapping):
        raw_files = commit.get("files", ()) or ()
        raw_renames = commit.get("renames", ()) or ()
    else:
        raw_files = getattr(commit, "files", ()) or ()
        raw_renames = getattr(commit, "renames", ()) or ()
    files = [normalise_path(path) for path in raw_files if str(path).strip()]
    renames = [
        (normalise_path(pair[0]), normalise_path(pair[1]))
        for pair in raw_renames
        if len(pair) == 2 and str(pair[0]).strip() and str(pair[1]).strip()
    ]
    return files, renames


def normalise_path(path: Any) -> str:
    value = str(path).replace("\\", "/")
    if value.startswith("./"):
        value = value[2:]
    return value
