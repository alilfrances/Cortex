# Provenance: rust_semver

- Language family: Rust
- Upstream: https://github.com/dtolnay/semver
- Commit: `280ebcb6edac3aa4cdc545dbff8a26c5ac4861fe` (immutable; committer date 2026-06-23T20:02:34-07:00)
- Archive URL: https://github.com/dtolnay/semver/tree/280ebcb6edac3aa4cdc545dbff8a26c5ac4861fe
- License: MIT OR Apache-2.0
- `LICENSE`: verbatim copy of upstream `LICENSE-MIT` (sha256 `23f18e03dc49df91622fe2a76176497404e46ced8a715d9d2b67a7446571cca3`)
- Other upstream license files kept verbatim: `LICENSE-APACHE`, `LICENSE-MIT`
- Snapshot acquired: 2026-10-17 by `scripts/vendor_oss_fixtures.py --fetch` (clone at the pinned commit)
- Upstream tracked files at that commit: 28
- Vendored files (including LICENSE and this file): 21
- Vendored bytes (including LICENSE and this file): 117904
- Inventoried files (everything except this file): 20, 114037 bytes

## Prune rules

Applied to `git ls-files` at the pinned commit. Any path with a dot-prefixed component
(`.github/`, `.gitignore`, ...) is dropped. A path is kept only when it matches an include
pattern (fnmatch; `*` also matches `/`) and no exclude pattern. File contents are
byte-identical to upstream; nothing is edited.

Include:

- `src/*`
- `tests/*`
- `Cargo.toml`
- `build.rs`
- `README.md`

Exclude:

- (none beyond the default rule)

## Notes

Dual-licensed upstream (`MIT OR Apache-2.0`, Cargo.toml). Both upstream license files are kept verbatim; recipients may use either license. `LICENSE` is a byte-identical copy of `LICENSE-MIT` only so every fixture directory has a `LICENSE` file; it does not narrow the grant.

## Inventory

| File | Bytes | SHA-256 |
|---|---:|---|
| `Cargo.toml` | 1261 | `a3386a53d0ad50260a7424689ba2d983abd6c6b5bb09bda48b9fceb3032e8318` |
| `LICENSE` | 1023 | `23f18e03dc49df91622fe2a76176497404e46ced8a715d9d2b67a7446571cca3` |
| `LICENSE-APACHE` | 9723 | `62c7a1e35f56406896d7aa7ca52d0cc0d272ac022b5d2796e7d6905db8a3636a` |
| `LICENSE-MIT` | 1023 | `23f18e03dc49df91622fe2a76176497404e46ced8a715d9d2b67a7446571cca3` |
| `README.md` | 2931 | `f1f736262db1f11353bca2fbdde906bfbb1d54e5eb7497dd110984f5eebb801e` |
| `build.rs` | 645 | `16e1a429f17d6f09f9e19c907ecfe84a6de8804da581008a6b182418a8a017b2` |
| `src/display.rs` | 4603 | `cfe08cb163fd5ba7fa024880a0809afb07f6e465891d8cefb716c693b13958d0` |
| `src/error.rs` | 4091 | `d0311d2d3ec0c1c04f3067a59aed52bac57329e47801be67b09199d145c7f73e` |
| `src/eval.rs` | 4139 | `292eac9472c92db20fcd3a864c5598f55a2f5554008af8f704ff975fcf40dd74` |
| `src/identifier.rs` | 18143 | `dc5d61282418a5eaefca7ebe37a5bcdd61da22a5191454d61ea1ef566004aa9e` |
| `src/impls.rs` | 4729 | `30bac3940b9bab840dc59280832c9afbb342abf47c51740a1b83b43c1646c56b` |
| `src/lib.rs` | 21379 | `e56f4d7c7774c45e61026fb0050955d67601c9ad18bdf93f8921d9898067c119` |
| `src/parse.rs` | 12187 | `2d9cb28a72a813968c16da29a9690f8e53f1ce00727a1dbe67aec3ddb5ed59ed` |
| `src/serde.rs` | 2832 | `e2a9b9dc3cd2cccc250eaffad049de418ef791bf8c4a34111a48f068353e0a37` |
| `tests/node/mod.rs` | 1202 | `73d6103aa122bf3283d3af16aaa08d302fd5ce50f52e9ead8d6be2d596d89487` |
| `tests/test_autotrait.rs` | 422 | `2631b78c9c63f1829c814fa88fcab0c5d77eb4e2d9c0c418d50c33e12a511e90` |
| `tests/test_identifier.rs` | 1428 | `fba0428bf5c43c733d17ca30952822ed6e1af6de1e5ef11bd8fb46c6b10f65fd` |
| `tests/test_version.rs` | 7413 | `bca3dccc0add95f657ebb1e37d9d7ff2235b5a151dafe1c8335a1190ea02af2e` |
| `tests/test_version_req.rs` | 13635 | `edb7440c14655a77af2dfed4ea8eebbd9b637947c46f33164a2b66b62f75d464` |
| `tests/util/mod.rs` | 1228 | `89975b57637b872d75adea6be9a5c08d21fda6f6cf29b01180a2232c0a7d0296` |

## Eval use

`evals/run_evals.py` copies this directory into a temporary git repository, creates
deterministic synthetic commits (fixed dates) so COCHANGE exists, ingests it offline and
scores OSS retrieval tasks, call-edge resolution, and impacted-test selection.
