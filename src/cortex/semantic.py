"""Optional, local-only static semantic retrieval.

The core Cortex install never needs this module's optional dependencies.  When
Model2Vec is installed, the model lifecycle is deliberately split in two:
``setup_model`` is the only function that accepts the authoritative remote
model id (``minishlab/potion-code-16M``); every runtime path resolves a
Cortex-managed directory below ``CORTEX_DATA_DIR`` and loads only that local
path.  Ingest and query catch all semantic failures and fall back to the
stdlib/graph path.

Vectors are intentionally stored in SQLite as float32 blobs.  A vector
 database would add another service and another dependency for a feature that
is normally only a few thousand symbol chunks, so query-time ranking is a
stable brute-force cosine scan.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import GraphNode, SourceRecord
from .store import CortexStore, data_root

# Verified against Model2Vec's installed-package documentation and the
# provider's Hugging Face model endpoint.  Keep the provider-qualified id here,
# but use it only in setup_model().  Runtime loading below never passes this
# value to a model loader.
MODEL_ID = "minishlab/potion-code-16M"
MODEL_NAME = "potion-code-16M"
MODEL_DIR_NAME = "potion-code-16M"
MANIFEST_NAME = "cortex-semantic-model.json"
SEMANTIC_EXCERPT_LINES = 12

try:  # Optional dependency: model2vec itself pulls numpy and its tokenizer.
    from model2vec import StaticModel as _StaticModel
except Exception:  # pragma: no cover - depends on the host environment
    _StaticModel = None  # type: ignore[assignment,misc]

if _StaticModel is not None:
    try:  # Optional dependency: importing cortex must remain stdlib-only.
        import numpy as _numpy
    except Exception:  # pragma: no cover - depends on the host environment
        _numpy = None  # type: ignore[assignment]
else:
    _numpy = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class _ModelDetails:
    path: Path
    version: str
    ready: bool
    reason: str


_LOADED_MODEL: Any | None = None
_LOADED_MODEL_KEY: tuple[str, str] | None = None


def semantic_enabled() -> bool:
    """Return whether optional semantic work is enabled.

    ``CORTEX_SEMANTIC=0`` is useful for reproducible/off evals and for users
    who installed the extra but want the default lexical path for a session.
    It never enables semantic retrieval by itself: a local managed model is
    still required.
    """

    return os.environ.get("CORTEX_SEMANTIC", "1") != "0"


def dependencies_installed() -> bool:
    """Whether both optional semantic runtime dependencies are available."""

    return _StaticModel is not None and _numpy is not None


def model_path() -> Path:
    """The only runtime model location, always below ``CORTEX_DATA_DIR``."""

    return data_root() / "semantic" / MODEL_DIR_NAME


def _manifest_path(path: Path | None = None) -> Path:
    return (path or model_path()) / MANIFEST_NAME


def _artifact_version(path: Path) -> str:
    """Hash the local model artifacts to get a truthful model version.

    Model2Vec exposes the provider id but not a universally stable package
    revision on ``StaticModel``. Hashing the runtime artifacts ties every
    embedding row to the bytes that produced it without inventing a revision
    or claiming a download succeeded. The generated README/model card is
    excluded because Model2Vec embeds the temporary setup-directory name in
    it even when the actual model artifacts are unchanged.
    """

    digest = hashlib.sha256()
    for artifact in sorted(
        (
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file()
            and candidate.name != MANIFEST_NAME
            and candidate.name.lower() != "readme.md"
        ),
        key=lambda candidate: candidate.relative_to(path).as_posix(),
    ):
        relative = artifact.relative_to(path).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        try:
            digest.update(artifact.read_bytes())
        except OSError:
            continue
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _model_file_key(path: Path) -> tuple[tuple[str, int, int], ...]:
    """Cheap local stat key used to avoid hashing model bytes on every ingest."""

    if not path.is_dir():
        return ()
    entries: list[tuple[str, int, int]] = []
    for candidate in path.rglob("*"):
        if not candidate.is_file():
            continue
        try:
            stat = candidate.stat()
        except OSError:
            continue
        entries.append((candidate.relative_to(path).as_posix(), int(stat.st_size), int(stat.st_mtime_ns)))
    return tuple(sorted(entries))


def _model_details() -> _ModelDetails:
    path = model_path()
    return _cached_model_details(str(path), _model_file_key(path))


@lru_cache(maxsize=8)
def _cached_model_details(path_string: str, _file_key: tuple[tuple[str, int, int], ...]) -> _ModelDetails:
    path = Path(path_string)
    if not dependencies_installed():
        missing: list[str] = []
        if _StaticModel is None:
            missing.append("model2vec")
        if _numpy is None:
            missing.append("numpy")
        return _ModelDetails(path, "", False, f"semantic extra is not installed ({', '.join(missing)})")

    try:
        path.resolve().relative_to(data_root().resolve())
    except ValueError:
        return _ModelDetails(path, "", False, "local semantic model is outside CORTEX_DATA_DIR")

    manifest_path = _manifest_path(path)
    if not path.is_dir() or not manifest_path.is_file():
        return _ModelDetails(path, "", False, "local model is not set up; run `cortex semantic setup`")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _ModelDetails(path, "", False, "local semantic model manifest is unreadable")
    if manifest.get("model_id") != MODEL_ID:
        return _ModelDetails(path, "", False, "local semantic model is not the configured potion-code-16M model")

    # Model2Vec 0.8.x saves config/tokenizer/safetensors.  Requiring the
    # artifacts prevents a stale manifest or an interrupted setup from making
    # runtime believe a usable model exists.
    has_config = (path / "config.json").is_file()
    has_tokenizer = (path / "tokenizer.json").is_file()
    has_embeddings = any(path.rglob("*.safetensors"))
    if not (has_config and has_tokenizer and has_embeddings):
        return _ModelDetails(path, "", False, "local semantic model files are incomplete; rerun `cortex semantic setup`")

    version = str(manifest.get("model_version") or "")
    actual_version = _artifact_version(path)
    if not version or version != actual_version:
        return _ModelDetails(path, "", False, "local semantic model manifest does not match its files; rerun `cortex semantic setup`")
    return _ModelDetails(path, version, True, "ready")


def semantic_status(store: CortexStore | None = None, repo_path: Path | None = None) -> dict[str, Any]:
    """Return a no-network status payload suitable for ``cortex_overview``.

    ``enabled`` reflects the environment switch. ``active`` is stricter: it
    requires enabled dependencies and a verified local model, plus at least
    one current-model chunk whenever a repo/store is supplied.
    """

    details = _model_details()
    indexed_chunks = 0
    has_repo = store is not None and repo_path is not None
    if details.ready and has_repo:
        try:
            indexed_chunks = store.count_chunk_embeddings(repo_path, MODEL_ID, details.version)  # type: ignore[union-attr]
        except Exception:
            indexed_chunks = 0
    enabled = semantic_enabled()
    installed = dependencies_installed()
    model_ready = bool(installed and details.ready)
    active = bool(enabled and model_ready and (not has_repo or indexed_chunks > 0))
    if not enabled:
        reason = "disabled by CORTEX_SEMANTIC=0"
    elif not installed:
        reason = details.reason
    elif not model_ready:
        reason = details.reason
    elif has_repo and indexed_chunks == 0:
        reason = "model ready; no current-model chunks (run `cortex ingest` to build them)"
    elif active:
        reason = "active"
    else:
        reason = "enabled but not active"
    return {
        "installed": installed,
        "enabled": enabled,
        "active": active,
        "model_ready": model_ready,
        "indexed_chunks": indexed_chunks,
        "reason": reason,
        "model_id": MODEL_ID,
        "model_version": details.version or None,
    }


def _write_manifest(path: Path, version: str) -> None:
    package_version = None
    try:
        import model2vec  # type: ignore[import-not-found]

        package_version = getattr(model2vec, "__version__", None)
    except Exception:
        pass
    payload = {
        "model_id": MODEL_ID,
        "model_name": MODEL_NAME,
        "model_version": version,
        "model2vec_version": package_version,
        "created_at": int(time.time()),
    }
    _manifest_path(path).write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def setup_model(*, force: bool = False) -> dict[str, Any]:
    """Download and cache the configured model during explicit setup only.

    This is intentionally the sole remote-loading path in Cortex.  The
    caller-facing CLI uses this function for ``cortex semantic setup``;
    ingest/query never call it.
    """

    if not dependencies_installed():
        return semantic_status()
    current = _model_details()
    if current.ready and not force:
        return semantic_status()

    target = model_path()
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f".{target.name}.setup-{os.getpid()}-{time.time_ns()}"
    try:
        # This is the one and only provider-qualified remote identifier use.
        model = _StaticModel.from_pretrained(MODEL_ID, force_download=force)  # type: ignore[union-attr]
        model.save_pretrained(temporary)
        if not (temporary / "config.json").is_file() or not (temporary / "tokenizer.json").is_file():
            raise RuntimeError("Model2Vec setup did not produce a complete local model")
        if not any(temporary.rglob("*.safetensors")):
            raise RuntimeError("Model2Vec setup did not produce safetensors embeddings")
        version = _artifact_version(temporary)
        _write_manifest(temporary, version)
        if target.exists():
            shutil.rmtree(target)
        temporary.replace(target)
        clear_model_cache()
    except Exception as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        return {
            **semantic_status(),
            "model_ready": False,
            "reason": f"semantic setup failed: {type(exc).__name__}: {exc}",
        }
    return semantic_status()


def clear_model_cache() -> None:
    global _LOADED_MODEL, _LOADED_MODEL_KEY
    _LOADED_MODEL = None
    _LOADED_MODEL_KEY = None
    _cached_model_details.cache_clear()


def semantic_runtime_ready() -> bool:
    """Verify that the local model can actually be loaded, without network."""

    return _load_local_model() is not None


def _load_local_model() -> tuple[Any, _ModelDetails] | None:
    """Load only the verified local path; never pass ``MODEL_ID`` here."""

    global _LOADED_MODEL, _LOADED_MODEL_KEY
    if not semantic_enabled():
        return None
    details = _model_details()
    if not details.ready or _StaticModel is None:
        return None
    key = (str(details.path.resolve()), details.version)
    if _LOADED_MODEL is not None and _LOADED_MODEL_KEY == key:
        return _LOADED_MODEL, details

    # Model2Vec's local loader is already path-aware.  Offline flags provide a
    # second guard against a provider cache lookup if an optional dependency
    # changes its implementation in the future.
    old_hf = os.environ.get("HF_HUB_OFFLINE")
    old_transformers = os.environ.get("TRANSFORMERS_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        model = _StaticModel.from_pretrained(str(details.path), force_download=False)  # type: ignore[union-attr]
    except Exception:
        return None
    finally:
        if old_hf is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = old_hf
        if old_transformers is None:
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
        else:
            os.environ["TRANSFORMERS_OFFLINE"] = old_transformers
    _LOADED_MODEL = model
    _LOADED_MODEL_KEY = key
    return model, details


def _encode(model: Any, texts: Sequence[str]) -> list[list[float]]:
    encoded = model.encode(list(texts))
    if _numpy is not None:
        matrix = _numpy.asarray(encoded, dtype=_numpy.float32)
        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        if matrix.ndim != 2:
            raise ValueError("semantic model returned a non-matrix embedding")
        return [[float(value) for value in row] for row in matrix]
    # This path is mostly useful for faithful fake models in tests.  The real
    # Model2Vec package requires numpy, so it is never the normal production
    # path.
    rows = list(encoded)
    return [[float(value) for value in row] for row in rows]


def _vector_blob(vector: Sequence[float]) -> bytes:
    if _numpy is not None:
        return _numpy.asarray(vector, dtype=_numpy.float32).tobytes()
    return struct.pack(f"<{len(vector)}f", *[float(value) for value in vector])


def vector_from_blob(blob: bytes) -> list[float]:
    if _numpy is not None:
        return [float(value) for value in _numpy.frombuffer(blob, dtype=_numpy.float32)]
    if len(blob) % 4:
        raise ValueError("float32 vector blob has an invalid byte length")
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _is_comment_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(("#", "//", "/*", "*", "*/", "<!--", "--"))


def _leading_comments(lines: list[str], start: int) -> list[str]:
    """Collect the contiguous comment block immediately before a symbol."""

    index = max(0, start - 2)
    found = False
    selected: list[str] = []
    while index >= 0:
        line = lines[index]
        if _is_comment_line(line):
            found = True
            selected.append(line)
            index -= 1
            continue
        if not found and not line.strip():
            index -= 1
            continue
        break
    selected.reverse()
    return selected


def _docstring_lines(lines: list[str], start: int, end: int) -> list[str]:
    """Best-effort first docstring/comment string after a declaration."""

    if start >= len(lines):
        return []
    selected: list[str] = []
    upper = min(len(lines), max(start + 1, end))
    for index in range(start, upper):
        stripped = lines[index].strip()
        if not stripped:
            if selected:
                break
            continue
        if not selected and (stripped.startswith('"""') or stripped.startswith("'''")):
            delimiter = stripped[:3]
            selected.append(lines[index])
            if stripped.count(delimiter) >= 2 and len(stripped) > 3:
                return selected
            for following in range(index + 1, upper):
                selected.append(lines[following])
                if delimiter in lines[following]:
                    return selected
            return selected
        # C/C++/QML leading comments are handled by _leading_comments; do not
        # mistake the first executable body line for a docstring.
        break
    return selected


def symbol_chunk_text(source: SourceRecord, node: GraphNode, *, excerpt_lines: int = SEMANTIC_EXCERPT_LINES) -> str:
    """Build the deterministic symbol chunk required by P1-7."""

    lines = source.content.splitlines()
    start = int(node.span_start or 1)
    end = int(node.span_end or start)
    start = max(1, min(start, len(lines) or 1))
    end = max(start, min(end, len(lines) or start))
    signature = node.signature or (lines[start - 1] if lines and start <= len(lines) else node.label)
    leading = _leading_comments(lines, start)
    docstring = _docstring_lines(lines, start, end)
    excerpt = lines[start - 1 : min(end, start + max(1, excerpt_lines) - 1)]

    sections: list[str] = [signature]
    if leading:
        sections.append("\n".join(leading))
    if docstring and docstring != excerpt[: len(docstring)]:
        sections.append("\n".join(docstring))
    if excerpt:
        sections.append("\n".join(excerpt))
    return "\n".join(section for section in sections if section).strip()


def _source_hash(source: SourceRecord) -> str:
    return source.content_hash or hashlib.sha256(source.content.encode("utf-8", errors="replace")).hexdigest()


def index_embeddings(
    store: CortexStore,
    repo_root: Path,
    sources: Sequence[SourceRecord],
    nodes: Sequence[GraphNode],
    *,
    replace_paths: Iterable[str] = (),
) -> None:
    """Delete owned rows and best-effort index changed symbol chunks."""

    paths = sorted(set(str(path) for path in replace_paths))
    try:
        if paths:
            store.delete_chunk_embeddings_for_paths(repo_root, paths)
    except Exception:
        # A semantic table failure must not break graph ingest.
        return
    if not semantic_enabled():
        return
    if not _model_details().ready:
        return
    source_by_path = {source.path: source for source in sources}
    chunks: list[tuple[GraphNode, SourceRecord, str]] = []
    for node in sorted(
        (
            candidate
            for candidate in nodes
            if candidate.granularity == "symbol"
            and candidate.span_start is not None
            and candidate.source_ref in source_by_path
        ),
        key=lambda candidate: (candidate.source_ref, int(candidate.span_start or 0), candidate.node_id),
    ):
        source = source_by_path.get(node.source_ref)
        if source is None:
            continue
        chunks.append((node, source, symbol_chunk_text(source, node)))
    if not chunks:
        return
    loaded = _load_local_model()
    if loaded is None:
        return
    model, details = loaded
    try:
        vectors = _encode(model, [chunk[2] for chunk in chunks])
        if len(vectors) != len(chunks):
            raise ValueError("semantic model returned a different number of vectors than chunks")
        rows = []
        for (node, source, _text), vector in zip(chunks, vectors, strict=True):
            if not vector:
                continue
            rows.append(
                {
                    "node_id": node.node_id,
                    "source_path": source.path,
                    "source_hash": _source_hash(source),
                    "model_id": MODEL_ID,
                    "model_version": details.version,
                    "vector": _vector_blob(vector),
                    "dimension": len(vector),
                }
            )
        if rows:
            store.save_chunk_embeddings(repo_root, rows)
    except Exception:
        # Encoding/provider failures are explicitly fail-soft.  The owned rows
        # were deleted above, so stale vectors cannot be returned as current.
        return


def ranked_paths(
    store: CortexStore,
    repo_root: Path,
    task: str,
    *,
    limit: int = 200,
) -> list[str]:
    """Embed a task locally and return deterministic file paths by cosine."""

    if not task or not semantic_enabled():
        return []
    details = _model_details()
    if not details.ready:
        return []
    # Avoid loading a tens-of-MB model for a repository that has no current
    # model-version chunks yet (e.g. setup completed but full ingest has not
    # run). This keeps the inactive/empty semantic path cheap.
    try:
        rows = store.fetch_chunk_embeddings(repo_root, MODEL_ID, details.version)
    except Exception:
        return []
    if not rows:
        return []
    loaded = _load_local_model()
    if loaded is None:
        return []
    model, details = loaded
    try:
        query_vectors = _encode(model, [task])
        if not query_vectors:
            return []
        query = query_vectors[0]
    except Exception:
        return []

    def cosine(vector: Sequence[float]) -> float:
        if _numpy is not None:
            left = _numpy.asarray(query, dtype=_numpy.float32)
            right = _numpy.asarray(vector, dtype=_numpy.float32)
            if left.shape != right.shape:
                return float("-inf")
            denominator = float(_numpy.linalg.norm(left) * _numpy.linalg.norm(right))
            return float(_numpy.dot(left, right) / denominator) if denominator else float("-inf")
        if len(query) != len(vector):
            return float("-inf")
        dot = sum(float(a) * float(b) for a, b in zip(query, vector))
        left_norm = sum(float(a) * float(a) for a in query) ** 0.5
        right_norm = sum(float(b) * float(b) for b in vector) ** 0.5
        return dot / (left_norm * right_norm) if left_norm and right_norm else float("-inf")

    ranked: list[tuple[float, str, str]] = []
    for row in rows:
        try:
            score = cosine(vector_from_blob(row["vector"]))
        except Exception:
            continue
        if not math.isfinite(score):
            continue
        ranked.append((score, str(row["source_path"]), str(row["node_id"])))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    paths: list[str] = []
    seen: set[str] = set()
    for _score, path, _node_id in ranked:
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
        if len(paths) >= limit:
            break
    return paths


__all__ = [
    "MODEL_ID",
    "MODEL_NAME",
    "SEMANTIC_EXCERPT_LINES",
    "clear_model_cache",
    "dependencies_installed",
    "index_embeddings",
    "model_path",
    "ranked_paths",
    "semantic_enabled",
    "semantic_status",
    "semantic_runtime_ready",
    "setup_model",
    "symbol_chunk_text",
    "vector_from_blob",
]
