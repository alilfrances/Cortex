# Fix plan: accept `tree-sitter-language-pack` upgrades (Dependabot #27)

Status: proposed
Affects: cortex 0.10.0 (`pyproject.toml`, `src/cortex/runtime.py`,
`src/cortex/runtime-lock.json`, `runtime/runtime-lock.json`,
`src/cortex/structural/treesitter_backend.py`, `src/cortex/structural/qml_backend.py`,
`.github/dependabot.yml`)

## Summary of the diagnosis

Dependabot PR #27 bumps `tree-sitter-language-pack` from `1.12.5` to `1.22.1` in
`pyproject.toml`. CI fails with 278 test failures: tree-sitter extraction
silently degrades to the regex backend (`LOW` instead of `EXTRACTED`
provenance), and some parser loads raise:

```
DownloadError: Failed to read manifest from
file:///home/runner/.cortex/runtime/0.8.0-cpython-311-linux-glibc-x86_64/.network-disabled
```

It is tempting to read this as "upstream moved to a runtime-download model that
Cortex's offline runtime cannot accept". That is not the cause. `1.12.5`
already downloads parsers at runtime, and Cortex already handles that: the
managed runtime prefetches a hash-locked parser archive at setup time and then
disables the network. The newer package still supports that model.

The failure comes from three Cortex-side defects that compose:

1. **Two pins that must agree, only one of which Dependabot can update.** The
   language-pack version is pinned in `pyproject.toml` *and* in
   `runtime-lock.json` (two identical copies), which also carries per-platform
   wheel and parser-archive SHA-256s. Dependabot only edits `pyproject.toml`, so
   every bump creates a split-brain install: site-packages gets `1.22.1`, the
   managed runtime and its parser cache stay on `1.12.5`.
2. **Parser code imports whichever copy is first on `sys.path`, with no version
   check.** `treesitter_backend.py:67` and `qml_backend.py:28` run
   `import tree_sitter_language_pack` *before* `configure_parser_environment()`
   puts the runtime's `site/` on `sys.path`. Under the bump, the process gets the
   site-packages `1.22.1` and points it at a cache built by `1.12.5`.
3. **`runtime.py` hard-codes the `1.12.x` cache layout.** The cache layout
   changed between the two versions (below), so even a correctly locked `1.22.1`
   runtime would fail `cortex runtime setup` today.

Defect 1 is why a bump breaks. Defect 2 is why it breaks as 278 confusing
regex-fallback failures instead of one clear error. Defect 3 is what has to
change in the code to actually accept the new version.

## Evidence

Reproduced locally (Linux x86_64, Python 3.13, `tree-sitter==0.26.0`) by
prefetching the 11 locked grammars with each version, then loading them with
`TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL` pointed at a missing
`.network-disabled` file, as Cortex does after setup:

| package | cache built by | network | grammars loaded |
|---|---|---|---|
| `1.22.1` | `1.12.5` | disabled | **0 / 11: `DownloadError` (the CI failure)** |
| `1.22.1` | `1.22.1` | disabled | 11 / 11 |
| `1.22.1` | `1.22.1`, after Cortex's post-setup cleanup | disabled | 11 / 11 |

Cache layout per version:

```
1.12.5   <cache_dir>/libtree_sitter_<name>.so
         <cache_dir>/../bundles/<platform>-<sha256>.tar.zst      (setup-time only)
         <cache_dir>/../manifest.json, ../.download.lock         (setup-time only)

1.22.1   <cache_dir>/tree-sitter-language-pack/v1.22.1/libs/libtree_sitter_<name>.so
         <cache_dir>/tree-sitter-language-pack/v1.22.1/bundles/<platform>-<sha256>.tar.zst
         <cache_dir>/tree-sitter-language-pack/v1.22.1/manifest.json, .download.lock
```

What did *not* change, so needs no work:

- The public API Cortex uses: `PackConfig(cache_dir=, languages=)`,
  `configure`, `prefetch`, `get_language`, `DownloadError`.
- The QML grammar is still named `qmljs`, and `tsx`/`cpp` are unchanged.
- The `TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL` override is still honored, so
  the `.network-disabled` trick still blocks downloads.
- Wheel `Requires-Dist` is still only `tree-sitter>=0.23`, so the extractor's
  allowed-dependency set is unchanged.
- PyPI publishes all six locked wheel platforms for `1.22.1` with the same tags
  (`macosx_10_12_x86_64`, `macosx_11_0_arm64`, `manylinux_2_34_{x86_64,aarch64}`,
  `win_amd64`, `win_arm64`).
- The GitHub release publishes all six `parsers-<platform>.tar.zst` archives.
- The bundle filename's hash is still the archive's SHA-256, so the existing
  digest check carries over.

The 17 → 278 jump between #27's first CI run (Oct 13, on `04f6dab`) and its
latest (on `0a98209`) is consistent with 0.10.0 adding many tree-sitter-dependent
tests (call graph, goldens, evals). Both runs fail the same way. `main` itself is
green with the `1.12.5` pin before and after 0.10.0.

## Fix

Ordered so each step is independently mergeable and leaves `main` green.

### Step 1: make `runtime.py` layout-agnostic

`_prefetch_and_verify` (`src/cortex/runtime.py:498`):

- `:526` looks for the verified archive only at
  `cache.parent / "bundles" / f"{platform}-{sha}.tar.zst"`. Search both known
  locations: `cache.parent / "bundles"` (1.12.x) and
  `cache / "tree-sitter-language-pack" / f"v{version}" / "bundles"` (1.2x),
  with `version` taken from the lock. Fail as today if neither holds the locked
  file.
- `:542-544` deletes the setup-only artifacts (`bundles/`, upstream
  `manifest.json`, `.download.lock`) only from `cache.parent`. Delete them from
  whichever layout was used. Without this, the mutable upstream manifest ends up
  inside the cache digest that the ready marker covers.
- The offline check (`tree_sitter_{name}.` substring under `cache.rglob`) and
  `_parser_state` already recurse, so they work with `libs/`. Add a test to keep
  it that way.

Tests: extend the fake-pack tests in `tests/test_runtime_bootstrap.py` to
produce each layout and assert setup verifies the archive and leaves no
setup-only files behind.

### Step 2: fail loudly on version skew

In `_language_for_suffix` (`treesitter_backend.py:61`) and the QML loader
`_language()` (`qml_backend.py:27`):

- Call `configure_parser_environment()` (or a new
  `runtime.import_language_pack()` helper) *before* importing the package, so the
  runtime copy wins when nothing has imported it yet.
- After import, compare the module's distribution version with the lock's
  `language_pack_version`. On mismatch, raise a dedicated error such as
  `RuntimeError("language-pack version skew: imported 1.22.1, runtime locked 1.12.5")`.
  The dispatcher already falls back to regex on parser errors
  (`structural/__init__.py:138`), so behavior stays
  degraded but safe. Also record `degraded_reason="language-pack-version-skew"`
  in `runtime.status()` so `cortex_status` reports it and doesn't just show
  `LOW` provenance.

If something imported the site-packages copy first, `sys.modules` makes it
stick for the life of the process. That is acceptable: the point is to
report it, not to hot-swap modules.

### Step 3: add a pin-consistency test

Add `tests/test_runtime_lock_matches_pyproject.py`: parse `pyproject.toml` and
assert that its `tree-sitter` and `tree-sitter-language-pack` pins equal the
lock's `tree_sitter_version` / `language_pack_version`, and that both lock
copies list a `packages[]` entry at that version for every documented platform.

This changes the cost of an unsynced Dependabot bump from "278 confusing
failures" to one failure whose message says
`run scripts/update_runtime_lock.py --language-pack <version>`.

### Step 4: lock-regeneration script

New `scripts/update_runtime_lock.py --language-pack X.Y.Z [--tree-sitter A.B.C]`,
standard library only (like `runtime.py`):

1. Read wheel `url`, `size` and `sha256` for each locked platform from
   `https://pypi.org/pypi/<name>/<version>/json`. Fail if any platform is
   missing, rather than silently dropping it.
2. Download each `parsers-<platform>.tar.zst` from the GitHub release and record
   `sha256`, `size` and `url`. The GitHub REST API is not needed. Downloading
   and hashing locally also guards against an asset being replaced after it is
   listed.
3. Rewrite both lock copies identically with sorted keys, the `pyproject.toml`
   pin, `parser_manifest.source`, and `language_pack_version`.
4. Print a diff summary. It never touches the installed runtime.

### Step 5: perform the upgrade to `1.22.1`

On a regular (non-Dependabot) branch:

- Run the Step 4 script for `1.22.1`.
- Bump the lock's `runtime_version` and `RUNTIME_VERSION` (`runtime.py:36`)
  `0.8.0 → 0.9.0`. The runtime directory is named by runtime version, so
  existing `0.8.0` installs stay intact for rollback, and upgrades get a clean
  directory instead of a rebuild over the old layout. The lock-digest check in
  the ready marker would force a rebuild anyway; a new directory just makes it
  explicit.
- Replace the hard-coded `"1.12.5"` fallback in `status()`
  (`runtime.py:483`) with a value read from the lock, and update the comment at
  `runtime.py:44`.
- Update `README.md` and `CHANGELOG.md`, which mention `1.12.5`.

### Step 6: validate grammar behavior

`1.22.0` refreshed 25 bundled grammars, including **Swift**, one of Cortex's
call-graph languages. Releases between `1.12.5` and `1.21.x` may have changed
others; I have not audited every changelog entry.

- Run the full CI suite, the golden tests (`tests/test_golden_*.py`), the
  offline OSS evals, the explicit regex-fallback suite, and `perf_ingest`.
- Where goldens change, review each diff as a grammar change: accept
  improvements, and treat lost symbols or edges as regressions to investigate.
  Do not regenerate goldens wholesale.
- Run `runtime-release.yml` via `workflow_dispatch` on all four OS runners.
  Only Linux x86_64 has been exercised so far.

### Step 7: make future bumps routine

Keep Dependabot enabled for this package. With Steps 1–4 in place, a future
bump fails one clear test, and the fix is to run one script on a branch and
merge.

Optional follow-up: a workflow that runs the script on Dependabot PRs that
touch `pyproject.toml` and pushes the regenerated lock back. This needs a token
that can push to Dependabot branches. Workflows triggered by Dependabot get a
read-only `GITHUB_TOKEN` by default, so the exact permission setup needs
verifying before committing to it. Pushing to a Dependabot branch also stops
Dependabot from rebasing that PR. Defer this until manual regeneration
proves to be a chore.

## What happens to PR #27

Don't push the lock regeneration onto Dependabot's branch. Do Steps 1–5 in a
regular PR, then close #27 with a comment pointing at it. Dependabot will
not reopen the same version once `pyproject.toml` already pins it.

## Validation checklist

- [ ] Steps 1–3 land first with `1.12.5` still pinned. CI stays green, and the
      new tests pass against both cache layouts.
- [ ] After Step 5, `python -m cortex runtime setup` succeeds on a clean machine
      and on one with an existing `0.8.0` runtime.
- [ ] `cortex_status` reports `ready: true`, 11/11 grammars, and
      `language_pack_version: 1.22.1`.
- [ ] Forcing a skew (site-packages on one version, runtime on another) produces
      `degraded_reason: language-pack-version-skew`, not silent `LOW`
      provenance.
- [ ] Full CI, goldens, evals and coverage floors pass; golden diffs reviewed.
- [ ] Runtime bundles build and attest on all four `runtime-release.yml`
      platforms.

## Open questions

- macOS and Windows cache layouts are assumed to match Linux's versioned layout.
  They have not been checked. Step 6's `workflow_dispatch` run covers this.
- The size of Swift (and other) grammar changes is unknown until the goldens run.
- Whether to support `1.12.x` and `1.2x` layouts permanently, or drop the old
  layout once `0.9.0` runtimes are the floor. Supporting both in Step 1 costs a
  few lines and keeps rollback cheap.
