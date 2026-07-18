"""Managed, isolated Tree-sitter runtime.

The runtime is deliberately implemented with the Python standard library.  A
plugin launch may therefore use this module before Cortex (or any optional
package) is importable.  Wheels are treated as signed-by-digest zip archives:
only artifacts named by ``runtime/runtime-lock.json`` are accepted and they
are extracted into an owner-controlled, versioned directory.

The manager is intentionally conservative.  If setup cannot be completed it
returns a degraded status instead of changing the interpreter or raising into
a host MCP process.  Parser code never calls this module to download anything;
only an explicit ensure/setup operation may do so.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import shutil
import ssl
import stat
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterator

LOCK_PATH = Path(__file__).resolve().with_name("runtime-lock.json")
RUNTIME_SCHEMA = 1
RUNTIME_VERSION = "0.8.0"
DEFAULT_TIMEOUT = 30.0
STALE_LOCK_SECONDS = 120.0
MAX_ARCHIVE_FILES = 10_000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
SUPPORTED_GRAMMARS = (
    "javascript", "typescript", "tsx", "go", "rust", "swift", "java", "ruby", "c", "cpp", "qml",
)
# Cortex calls the grammar qml while language-pack 1.12.x calls it qmljs.
GRAMMAR_ALIASES = {"qml": "qmljs"}
PARSER_PLATFORM_ALIASES = {
    "linux-glibc-arm64": "linux-aarch64",
    "linux-glibc-x86_64": "linux-x86_64",
    "windows-arm64": "windows-aarch64",
}


@dataclass(frozen=True)
class RuntimeStatus:
    ready: bool
    runtime_version: str
    parser_version: str
    language_pack_version: str
    lock_digest: str
    python_abi: str
    platform: str
    cache_path: str
    target_path: str
    requested_grammars: tuple[str, ...]
    downloaded_grammars: tuple[str, ...]
    missing_grammars: tuple[str, ...]
    backend_counts: dict[str, int]
    degraded_reason: str | None = None
    last_attempt: float | None = None
    repair_hint: str = "cortex runtime repair"
    source: str = "managed"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["requested_grammars"] = list(self.requested_grammars)
        value["downloaded_grammars"] = list(self.downloaded_grammars)
        value["missing_grammars"] = list(self.missing_grammars)
        return value


def _load_lock() -> dict[str, Any]:
    try:
        payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": RUNTIME_SCHEMA, "runtime_version": RUNTIME_VERSION, "packages": [], "grammars": list(SUPPORTED_GRAMMARS)}
    if not isinstance(payload, dict):
        raise ValueError("runtime lock must be an object")
    return payload


def lock_digest(lock: dict[str, Any] | None = None) -> str:
    payload = lock if lock is not None else _load_lock()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def python_abi() -> str:
    return getattr(sys.implementation, "cache_tag", None) or f"cp{sys.version_info.major}{sys.version_info.minor}"


def _platform_key() -> str | None:
    system = sys.platform
    machine = platform.machine().lower()
    if system == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "macos-arm64"
        if machine in {"x86_64", "amd64"}:
            return "macos-x86_64"
    elif system.startswith("win"):
        if machine in {"arm64", "aarch64"}:
            return "windows-arm64"
        if machine in {"x86_64", "amd64"}:
            return "windows-x86_64"
    elif sys.platform.startswith("linux"):
        libc = platform.libc_ver()[0].lower()
        if libc in {"", "glibc", "gnu libc", "libc"}:
            if machine in {"aarch64", "arm64"}:
                return "linux-glibc-arm64"
            if machine in {"x86_64", "amd64"}:
                return "linux-glibc-x86_64"
    return None


def runtime_dir() -> Path:
    # Explicit runtime dir is useful for tests, CI, administrators, and direct
    # launches.  Plugin data takes precedence in a host install so the plugin
    # never writes to the checkout.
    for name in ("CLAUDE_PLUGIN_DATA", "PLUGIN_DATA", "CORTEX_RUNTIME_DIR"):
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser().resolve()
    return (Path.home() / ".cortex" / "runtime").resolve()


def _target_path(root: Path, lock: dict[str, Any]) -> Path:
    return root / f"{lock.get('runtime_version', RUNTIME_VERSION)}-{python_abi()}-{_platform_key() or 'unsupported'}"


def _cache_path(target: Path) -> Path:
    return target / "parser-cache"


def _marker_path(target: Path) -> Path:
    return target / "ready.json"


def _status_path(root: Path) -> Path:
    return root / "status.json"


def _make_private(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _harden_tree(root: Path) -> None:
    """Apply owner-only permissions to every published runtime entry."""
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        _make_private(path, 0o700 if path.is_dir() or path.suffix in {".so", ".dylib", ".dll", ".pyd"} else 0o600)
    _make_private(root, 0o700)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _make_private(tmp, 0o600)
    os.replace(tmp, path)
    _make_private(path, 0o600)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _stale_lock(lock: Path) -> bool:
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return False
    try:
        owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
        pid = int(owner.get("pid", 0))
    except (OSError, ValueError, TypeError):
        return age >= STALE_LOCK_SECONDS
    return not _pid_alive(pid)


@contextlib.contextmanager
def _lock(root: Path, timeout: float = 20.0) -> Iterator[None]:
    """Cross-process lock using an atomic lock directory (stdlib only)."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _make_private(root, 0o700)
    lock = root / ".install.lock"
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock.mkdir(mode=0o700)
            _write_json(lock / "owner.json", {"pid": os.getpid(), "created_at": time.time()})
            break
        except FileExistsError:
            if _stale_lock(lock):
                stale = root / f".stale-lock-{os.getpid()}"
                try:
                    os.replace(lock, stale)
                except OSError:
                    pass
                else:
                    shutil.rmtree(stale, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for Cortex runtime lock")
            time.sleep(0.05)
    try:
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def _cleanup_staging(root: Path) -> None:
    for path in root.glob(".staging-*"):
        try:
            if path.is_dir() and time.time() - path.stat().st_mtime > 60:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def _artifact_for(lock: dict[str, Any], package: str, version: str | None = None) -> dict[str, Any] | None:
    for item in lock.get("packages", []):
        if item.get("name") != package:
            continue
        if version and item.get("version") != version:
            continue
        platform_key = _platform_key()
        cp_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
        candidates = [
            artifact for artifact in item.get("artifacts", [])
            if artifact.get("platform") in {platform_key, "any"}
        ]
        # Prefer an exact CPython wheel; the language pack is abi3 and may
        # legitimately only advertise cp310, while tree-sitter publishes
        # one wheel per CPython minor.
        exact = [a for a in candidates if cp_tag in str(a.get("filename", ""))]
        if exact:
            return exact[0]
        abi3 = [a for a in candidates if "abi3" in str(a.get("filename", ""))]
        if abi3:
            return abi3[0]
        # A CPython-specific wheel for a different minor is never compatible.
        # Future Python minors degrade instead of attempting to load one.
        # A lock may be used by a local fake server with a single explicit
        # artifact.  Platform validation still occurs when platform_tags is
        # present, but don't silently choose a different real platform wheel.
        if len(item.get("artifacts", [])) == 1 and item["artifacts"][0].get("platform") in {None, ""}:
            return item["artifacts"][0]
    return None


def _validate_artifact_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https" and parsed.netloc:
        return
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}:
        return
    raise ValueError("runtime artifact URL must use HTTPS (loopback HTTP is test-only)")


def _artifact_download_url(locked_url: str, filename: str) -> str:
    mirror = os.environ.get("CORTEX_RUNTIME_ARTIFACT_MIRROR", "").strip()
    if not mirror:
        return locked_url
    _validate_artifact_url(mirror)
    return mirror.rstrip("/") + "/" + urllib.parse.quote(filename)


def _download(url: str, destination: Path, timeout: float = DEFAULT_TIMEOUT) -> None:
    _validate_artifact_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "cortex-runtime/0.8"})
    # urllib honors HTTPS_PROXY/HTTP_PROXY/NO_PROXY. Some embedded Python
    # builds do not expose the OS trust store through ssl.create_default_context;
    # certifi is used only as a CA bundle fallback (never as verify=False).
    context = None
    if url.lower().startswith("https:"):
        cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
        if cafile:
            context = ssl.create_default_context(cafile=cafile)
        else:
            try:
                import certifi
                context = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                context = ssl.create_default_context()
    kwargs = {"timeout": timeout}
    if context is not None:
        kwargs["context"] = context
    with urllib.request.urlopen(request, **kwargs) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def _verify_digest(path: Path, expected: str) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest.lower() != str(expected).lower():
        raise ValueError(f"SHA-256 mismatch for {path.name}")


def _safe_archive_path(name: str) -> Path:
    normalized = name.replace("\\", "/")
    pure = Path(normalized)
    first = normalized.split("/", 1)[0]
    if not normalized or normalized.startswith(("/", "\\")) or ":" in first or pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe archive path: {name}")
    return pure


def _wheel_tags(filename: str) -> tuple[str, str, str]:
    if not filename.endswith(".whl"):
        raise ValueError("runtime artifacts must be wheels")
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        raise ValueError(f"invalid wheel filename: {filename}")
    return parts[-3], parts[-2], parts[-1]


def _safe_extract_wheel(archive: Path, target: Path, expected_tags: list[str] | None = None, *, package_name: str = "", package_version: str = "", allowed_dependencies: set[str] | None = None) -> None:
    py_tag, abi_tag, platform_tag = _wheel_tags(archive.name)
    if expected_tags and not any(tag in {py_tag, abi_tag, platform_tag} or tag == f"{py_tag}-{abi_tag}-{platform_tag}" for tag in expected_tags):
        raise ValueError(f"unexpected wheel tag for {archive.name}")
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_FILES or sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
            raise ValueError(f"wheel exceeds extraction limits: {archive.name}")
        metadata_entries = [info for info in infos if info.filename.endswith('.dist-info/METADATA')]
        if not metadata_entries:
            raise ValueError(f"wheel metadata missing for {archive.name}")
        metadata = zf.read(metadata_entries[0]).decode('utf-8', errors='replace')
        declared_name = next((line.split(':', 1)[1].strip() for line in metadata.splitlines() if line.startswith('Name:')), '')
        declared_version = next((line.split(':', 1)[1].strip() for line in metadata.splitlines() if line.startswith('Version:')), '')
        if package_name and declared_name.lower().replace('-', '_') != package_name.lower().replace('-', '_'):
            raise ValueError(f"wheel package name mismatch: {archive.name}")
        if package_version and declared_version != package_version:
            raise ValueError(f"wheel version mismatch: {archive.name}")
        if allowed_dependencies is not None:
            allowed_normalized = {name.lower().replace('-', '_') for name in allowed_dependencies}
            for line in metadata.splitlines():
                if line.startswith('Requires-Dist:'):
                    raw_dependency = line.split(':', 1)[1].strip()
                    marker = raw_dependency.split(';', 1)[1].strip().lower() if ';' in raw_dependency else ''
                    if 'extra ==' in marker:
                        continue
                    dependency = raw_dependency.split(';', 1)[0].split('[', 1)[0].strip()
                    dependency_name = dependency.split(' ', 1)[0].split('=', 1)[0].split('<', 1)[0].split('>', 1)[0].split('!', 1)[0].split('~', 1)[0].lower().replace('-', '_')
                    if dependency_name not in allowed_normalized:
                        raise ValueError(f"unlisted runtime dependency: {dependency_name}")
        for info in infos:
            name = info.filename
            pure = _safe_archive_path(name)
            if name.endswith("/"):
                continue
            # Wheels are zip archives and must not smuggle links into the
            # isolated target.  Unix mode bits are available in ZipInfo.
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ValueError(f"symlink in wheel: {name}")
            destination = target / pure
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory = destination.parent
            while True:
                _make_private(directory, 0o700)
                if directory == target or directory.parent == directory:
                    break
                directory = directory.parent
            with zf.open(info) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
            _make_private(destination, 0o700 if name.endswith(".so") else 0o600)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return ""
    entries = list(root.rglob("*"))
    if any(item.is_symlink() for item in entries):
        return ""
    for path in sorted((item for item in entries if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            return ""
    return digest.hexdigest()


def _cache_digest(cache: Path) -> str:
    return _tree_digest(cache)


def _ready_marker(lock: dict[str, Any], target: Path) -> dict[str, Any] | None:
    if target.is_symlink() or (target / "site").is_symlink() or _cache_path(target).is_symlink():
        return None
    try:
        marker = json.loads(_marker_path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if marker.get("schema_version") != RUNTIME_SCHEMA or marker.get("lock_digest") != lock_digest(lock):
        return None
    if marker.get("python_abi") != python_abi() or marker.get("platform") != _platform_key():
        return None
    # Cached startup verifies actual package/parser bytes, not only mutable
    # mtimes. This catches same-size cache replacement and partial corruption.
    if marker.get("cache_digest") != _tree_digest(_cache_path(target)):
        return None
    if marker.get("site_digest") != _tree_digest(target / "site"):
        return None
    return marker


def _parser_state(cache: Path, requested: list[str]) -> tuple[list[str], list[str]]:
    """Inspect a cache without downloading or importing a parser bundle."""
    downloaded = set()
    manifest = cache / "manifest.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
        downloaded.update(str(x) for x in value.get("downloaded", []))
    except (OSError, ValueError):
        pass
    # language-pack stores each downloaded grammar below the cache.  The
    # manifest is authoritative for fake/offline bundles, while this fallback
    # makes status useful with a package-created cache too.
    try:
        for child in cache.rglob("*"):
            if child.is_file():
                for grammar in requested:
                    if grammar in child.name.lower():
                        downloaded.add(grammar)
    except OSError:
        pass
    return sorted(downloaded), sorted(set(requested) - downloaded)


def status() -> dict[str, Any]:
    """Return a socket-free, deterministic local capability payload."""
    lock = _load_lock()
    root = runtime_dir()
    target = _target_path(root, lock)
    requested = tuple(str(x) for x in lock.get("grammars", SUPPORTED_GRAMMARS))
    marker = _ready_marker(lock, target)
    downloaded, missing = _parser_state(_cache_path(target), list(requested))
    previous_status: dict[str, Any] = {}
    try:
        previous_status = json.loads(_status_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if marker:
        downloaded = sorted(set(downloaded) | set(marker.get("downloaded_grammars", [])))
        missing = sorted(set(requested) - set(downloaded))
    reason = None
    if _platform_key() is None:
        reason = "unsupported-platform"
    elif not marker:
        try:
            reason = previous_status.get("degraded_reason") or "runtime-not-ready"
        except (OSError, ValueError):
            reason = "runtime-not-ready"
    return RuntimeStatus(
        ready=bool(marker and not missing),
        runtime_version=str(lock.get("runtime_version", RUNTIME_VERSION)),
        parser_version=str(lock.get("tree_sitter_version", "0.26.0")),
        language_pack_version=str(lock.get("language_pack_version", "1.12.5")),
        lock_digest=lock_digest(lock),
        python_abi=python_abi(),
        platform=_platform_key() or "unsupported",
        cache_path=str(_cache_path(target)),
        target_path=str(target),
        requested_grammars=requested,
        downloaded_grammars=tuple(downloaded),
        missing_grammars=tuple(missing),
        backend_counts={},
        degraded_reason=reason if not marker or missing else None,
        last_attempt=previous_status.get("last_attempt"),
    ).to_dict()


def _prefetch_and_verify(target: Path, cache: Path, requested: list[str], offline: bool, lock: dict[str, Any] | None = None) -> list[str]:
    manifest_env = "TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL"
    existing_manifest_url = os.environ.get(manifest_env, "")
    if not offline and existing_manifest_url.endswith("/.network-disabled"):
        os.environ.pop(manifest_env, None)
    if lock is not None:
        manifest = lock.get("parser_manifest", {}).get("platforms", {})
        if _platform_key() not in manifest:
            raise ValueError("parser manifest has no artifact for this platform")
    sys.path.insert(0, str(target))
    os.environ["CORTEX_PARSER_CACHE"] = str(cache)
    import importlib
    pack = importlib.import_module("tree_sitter_language_pack")
    options = getattr(pack, "PackConfig", None)
    if options is not None:
        pack.configure(options(cache_dir=str(cache), languages=[]))
    names = [GRAMMAR_ALIASES.get(x, x) for x in requested]
    if offline:
        cached_files = [path.name.lower() for path in cache.rglob("*") if path.is_file()]
        missing_files = [name for name in names if not any(f"tree_sitter_{name}." in filename for filename in cached_files)]
        if missing_files:
            raise RuntimeError(f"offline parser cache is missing: {', '.join(sorted(missing_files))}")
    else:
        pack.prefetch(names)
        platform_manifest = lock.get("parser_manifest", {}).get("platforms", {}).get(_platform_key(), {}) if lock else {}
        expected_sha = str(platform_manifest.get("sha256", "")).lower()
        expected_size = int(platform_manifest.get("size", 0) or 0)
        parser_platform = PARSER_PLATFORM_ALIASES.get(_platform_key() or "", _platform_key() or "")
        bundle = cache.parent / "bundles" / f"{parser_platform}-{expected_sha}.tar.zst"
        if not expected_sha or not bundle.is_file():
            raise ValueError("language-pack did not retain the locked parser bundle")
        if expected_size and bundle.stat().st_size != expected_size:
            raise ValueError("locked parser bundle size mismatch")
        _verify_digest(bundle, expected_sha)
    loaded = []
    for grammar in requested:
        pack.get_language(GRAMMAR_ALIASES.get(grammar, grammar))
        loaded.append(grammar)
    # Keep a small local attestation.  It also makes offline status independent
    # of private implementation details of a particular language-pack build.
    _write_json(cache / "manifest.json", {"runtime_schema": RUNTIME_SCHEMA, "downloaded": loaded})
    # The verified archive and upstream mutable manifest are installation-only.
    # The published runtime retains only the selected grammar libraries covered
    # by the ready marker's cache digest.
    shutil.rmtree(cache.parent / "bundles", ignore_errors=True)
    (cache.parent / "manifest.json").unlink(missing_ok=True)
    (cache.parent / ".download.lock").unlink(missing_ok=True)
    return loaded


def _verify_bundle_checksum(bundle: Path) -> None:
    expected = os.environ.get("CORTEX_RUNTIME_BUNDLE_SHA256", "").strip().lower()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        raise ValueError("offline bundles require CORTEX_RUNTIME_BUNDLE_SHA256")
    _verify_digest(bundle, expected)


def _verify_bundle_contents(bundle_root: Path, lock: dict[str, Any], requested: list[str]) -> None:
    bundle_lock_path = bundle_root / "runtime-lock.json"
    manifest_path = bundle_root / "bundle-manifest.json"
    if not bundle_lock_path.is_file() or not manifest_path.is_file():
        raise ValueError("offline bundle is missing its lock or manifest")
    try:
        bundle_lock = json.loads(bundle_lock_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("invalid offline bundle metadata") from exc
    expected_lock = lock_digest(lock)
    if lock_digest(bundle_lock) != expected_lock or manifest.get("lock_digest") != expected_lock:
        raise ValueError("offline bundle lock digest mismatch")
    if manifest.get("schema_version") != RUNTIME_SCHEMA:
        raise ValueError("unsupported offline bundle schema")
    if manifest.get("runtime_version") != lock.get("runtime_version"):
        raise ValueError("offline bundle runtime version mismatch")
    if manifest.get("platform") != _platform_key():
        raise ValueError("offline bundle platform mismatch")
    if list(manifest.get("grammars", [])) != requested:
        raise ValueError("offline bundle grammar set mismatch")
    cache = bundle_root / "parser-cache"
    if not cache.is_dir() or manifest.get("cache_digest") != _tree_digest(cache):
        raise ValueError("offline parser cache digest mismatch")


def _install_wheels(lock: dict[str, Any], stage: Path, *, offline: bool, bundle: Path | None = None) -> None:
    wheel_dir = stage / "wheels"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    packages = lock.get("packages", [])
    if not packages:
        raise ValueError("runtime lock contains no packages")
    bundle_wheels: dict[str, Path] = {}
    if bundle:
        candidate = bundle / "wheels"
        if candidate.is_dir():
            bundle_wheels = {p.name: p for p in candidate.iterdir() if p.is_file()}
    for package in packages:
        artifact = _artifact_for(lock, str(package.get("name")), str(package.get("version", "")))
        if artifact is None:
            raise ValueError(f"no locked artifact for {_platform_key() or 'unsupported platform'}: {package.get('name')}")
        filename = str(artifact.get("filename", ""))
        if not filename.endswith(".whl"):
            raise ValueError("unlisted or non-wheel runtime artifact")
        source = bundle_wheels.get(filename)
        temporary = False
        if source is None:
            if offline or os.environ.get("CORTEX_RUNTIME_NETWORK", "1") == "0":
                raise RuntimeError("network-disabled and offline artifact is missing")
            source = wheel_dir / filename
            _download(_artifact_download_url(str(artifact.get("url", "")), filename), source)
            temporary = True
        expected_size = int(artifact.get("size_bytes", artifact.get("size", 0)) or 0)
        if expected_size and source.stat().st_size != expected_size:
            raise ValueError(f"size mismatch for {filename}")
        _verify_digest(source, str(artifact.get("sha256", "")))
        _safe_extract_wheel(
            source,
            stage / "site",
            artifact.get("tags"),
            package_name=str(package.get("name", "")),
            package_version=str(package.get("version", "")),
            allowed_dependencies={str(item.get("name", "")) for item in packages},
        )
        if temporary:
            source.unlink(missing_ok=True)


def setup(*, force: bool = False, offline_bundle: str | Path | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Ensure the locked parser runtime is ready, returning status on failure."""
    lock = _load_lock()
    root = runtime_dir()
    target = _target_path(root, lock)
    requested = [str(x) for x in lock.get("grammars", SUPPORTED_GRAMMARS)]
    attempt = time.time()
    # A failed host launch must not hammer a proxy or offline machine on every
    # SessionStart. Explicit setup/repair and lock-version changes bypass this
    # bounded backoff.
    if not force and not offline_bundle and os.environ.get("CORTEX_RUNTIME_BUNDLE") is None:
        try:
            previous = json.loads(_status_path(root).read_text(encoding="utf-8"))
            if (previous.get("lock_digest") == lock_digest(lock)
                    and previous.get("last_attempt")
                    and attempt - float(previous["last_attempt"]) < 60
                    and not previous.get("ready", False)):
                return previous
        except (OSError, ValueError, TypeError):
            pass
    if _platform_key() is None:
        result = status()
        result.update({"ready": False, "degraded_reason": "unsupported-platform", "last_attempt": attempt})
        _write_json(_status_path(root), result)
        return result
    try:
        with _lock(root, timeout=timeout):
            _cleanup_staging(root)
            backup = root / ".last-known-good"
            for candidate in (target, backup):
                if candidate.is_symlink():
                    candidate.unlink()
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            if not force and _ready_marker(lock, target):
                result = status()
                result["last_attempt"] = attempt
                _write_json(_status_path(root), result)
                return result
            bundle_root: Path | None = None
            bundle_arg = offline_bundle or os.environ.get("CORTEX_RUNTIME_BUNDLE")
            if bundle_arg:
                bundle_path = Path(bundle_arg).expanduser().resolve()
                _verify_bundle_checksum(bundle_path)
                bundle_root = root / ".bundle-extract"
                shutil.rmtree(bundle_root, ignore_errors=True)
                bundle_root.mkdir(parents=True, mode=0o700)
                with zipfile.ZipFile(bundle_path) as archive:
                    infos = archive.infolist()
                    if len(infos) > MAX_ARCHIVE_FILES or sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
                        raise ValueError("offline bundle exceeds extraction limits")
                    for info in infos:
                        pure = _safe_archive_path(info.filename)
                        if ((info.external_attr >> 16) & 0o170000) == stat.S_IFLNK:
                            raise ValueError("symlink in offline bundle")
                        destination = bundle_root / pure
                        if info.is_dir():
                            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                            _make_private(destination, 0o700)
                        else:
                            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                            _make_private(destination.parent, 0o700)
                            with archive.open(info) as src, destination.open("wb") as dst:
                                shutil.copyfileobj(src, dst)
                _verify_bundle_contents(bundle_root, lock, requested)
            stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
            _make_private(stage, 0o700)
            previous_cache = os.environ.get("CORTEX_PARSER_CACHE")
            previous_manifest_url = os.environ.get("TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL")
            try:
                _install_wheels(lock, stage, offline=bool(bundle_root) or os.environ.get("CORTEX_RUNTIME_NETWORK", "1") == "0", bundle=bundle_root)
                cache = _cache_path(stage)
                cache.mkdir(parents=True, mode=0o700)
                if bundle_root and (bundle_root / "parser-cache").is_dir():
                    shutil.copytree(bundle_root / "parser-cache", cache, dirs_exist_ok=True)
                loaded = _prefetch_and_verify(stage / "site", cache, requested, offline=bool(bundle_root) or os.environ.get("CORTEX_RUNTIME_NETWORK", "1") == "0", lock=lock)
                marker = {
                    "schema_version": RUNTIME_SCHEMA,
                    "runtime_version": lock.get("runtime_version", RUNTIME_VERSION),
                    "lock_digest": lock_digest(lock),
                    "python_abi": python_abi(),
                    "platform": _platform_key(),
                    "downloaded_grammars": loaded,
                    "cache_digest": _cache_digest(_cache_path(stage)),
                    "site_digest": _tree_digest(stage / "site"),
                    "created_at": attempt,
                }
                _write_json(_marker_path(stage), marker)
                # Keep last-known-good target until the new fully-attested
                # target is complete, then publish with one atomic rename.
                if target.exists():
                    shutil.rmtree(backup, ignore_errors=True)
                    os.replace(target, backup)
                os.replace(stage, target)
                _harden_tree(target)
                # setup() may be called in-process by CLI/tests; remove the
                # staging import path and expose only the atomically published
                # target.
                for entry in (str(target / "site"), str(stage / "site")):
                    while entry in sys.path:
                        sys.path.remove(entry)
                sys.path.insert(0, str(target / "site"))
                os.environ["CORTEX_PARSER_CACHE"] = str(_cache_path(target))
                _disable_parser_network(target)
                shutil.rmtree(backup, ignore_errors=True)
            except Exception:
                stage_site = str(stage / "site")
                while stage_site in sys.path:
                    sys.path.remove(stage_site)
                for name, value in (
                    ("CORTEX_PARSER_CACHE", previous_cache),
                    ("TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL", previous_manifest_url),
                ):
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
                shutil.rmtree(stage, ignore_errors=True)
                if backup.exists():
                    if target.exists():
                        shutil.rmtree(target, ignore_errors=True)
                    os.replace(backup, target)
                raise
            finally:
                if bundle_root:
                    shutil.rmtree(bundle_root, ignore_errors=True)
        result = status()
        result["last_attempt"] = attempt
        _write_json(_status_path(root), result)
        return result
    except Exception as exc:
        shutil.rmtree(root / ".bundle-extract", ignore_errors=True)
        result = status()
        result.update({"ready": False, "degraded_reason": f"{type(exc).__name__}: {exc}", "last_attempt": attempt, "repair_hint": "cortex runtime repair"})
        _write_json(_status_path(root), result)
        return result


def public_capability(value: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return parser provenance without exposing user/plugin filesystem paths."""
    payload = dict(value if value is not None else status())
    payload.pop("cache_path", None)
    payload.pop("target_path", None)
    if not payload.get("ready", False) and payload.get("degraded_reason"):
        payload["degraded_reason"] = "runtime-not-ready"
    return payload


def ensure_runtime(**kwargs: Any) -> dict[str, Any]:
    """Public alias used by launchers; failures are represented as status."""
    return setup(**kwargs)


def target_path() -> Path | None:
    """Return the verified target for launcher ``sys.path`` setup."""
    lock = _load_lock()
    target = _target_path(runtime_dir(), lock)
    return target if _ready_marker(lock, target) else None


def _disable_parser_network(target: Path) -> None:
    # Cache misses after publication must fail locally rather than turn normal
    # ingest/query into an artifact-network path.
    os.environ["TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL"] = (target / ".network-disabled").as_uri()


def configure_parser_environment() -> Path | None:
    target = target_path()
    if target is None:
        return None
    cache = _cache_path(target)
    os.environ["CORTEX_PARSER_CACHE"] = str(cache)
    _disable_parser_network(target)
    site = target / "site"
    if str(site) not in sys.path:
        sys.path.insert(0, str(site))
    return target


def repair() -> dict[str, Any]:
    return setup(force=True)


# Names used by early integrations and straightforward test doubles.
runtime_status = status
runtime_ensure = ensure_runtime
runtime_setup = setup
runtime_repair = repair
setup_runtime = setup
repair_runtime = repair
get_runtime_status = status
ensure = ensure_runtime