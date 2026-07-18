# Managed parser runtime security

Cortex's parser runtime is an isolated, versioned directory owned by the
plugin/user. It never writes system site-packages or the repository. Each
wheel is selected from `runtime/runtime-lock.json`, restricted to a documented
platform tag, downloaded over HTTPS, checked against its SHA-256 digest, and
safely extracted without symlinks, path traversal, sdists, or build scripts.
A ready marker is published only after every supported grammar loads and is
keyed by the lock digest, Python ABI, and platform.

The first setup may contact only the locked wheel URLs and the language-pack
parser artifact service (or an administrator-configured mirror via
`CORTEX_RUNTIME_ARTIFACT_MIRROR` and
`TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL`). Mirror responses are held to the
same committed digests. Proxy, custom-CA, and platform trust-store settings
remain available. Requests carry
no repository path, source text, graph, or company metadata. Normal ingest and
query are cache-only. `CORTEX_RUNTIME_NETWORK=0` prohibits setup/parser
network access; administrators can distribute a checksum-verified offline
bundle with `scripts/build_runtime_bundle.py` and attest its checksum/SBOM.
Offline setup requires that trusted digest via `--bundle-sha256` (or
`CORTEX_RUNTIME_BUNDLE_SHA256`); a bundle cannot self-attest with only an
adjacent checksum file.

The lock, staging directory, cross-process lock, atomic publication, owner-only
permissions, and last-known-good retention limit partial-install and cache
replacement attacks. A corrupt or unsupported runtime degrades to regex mode;
`cortex runtime status` is local and socket-free, while explicit `setup` or
`repair` is the controlled recovery path. Remove the selected versioned
runtime directory to roll back, then reinstall/repair from a trusted bundle.

This artifact egress is separate from source egress. Parser setup downloads
third-party binaries only. The only Cortex feature that uploads source content
is `cortex enrich --allow-code-upload`, which requires an explicit user flag.
Semantic model setup is local-cache/model setup and is not part of parser
bootstrap.
