# Provenance: go_mux

- Language family: Go
- Upstream: https://github.com/gorilla/mux
- Commit: `db9d1d0073d27a0a2d9a8c1bc52aa0af4374d265` (immutable; committer date 2024-06-19T19:50:04-04:00)
- Archive URL: https://github.com/gorilla/mux/tree/db9d1d0073d27a0a2d9a8c1bc52aa0af4374d265
- License: BSD-3-Clause
- `LICENSE`: verbatim copy of upstream `LICENSE` (sha256 `0b77fdea862f869ef4f201ac5649bdbd38ccb055c7f6454dffae9e2adb4941c1`)
- Other upstream license files kept verbatim: (none)
- Snapshot acquired: 2026-10-17 by `scripts/vendor_oss_fixtures.py --fetch` (clone at the pinned commit)
- Upstream tracked files at that commit: 27
- Vendored files (including LICENSE and this file): 22
- Vendored bytes (including LICENSE and this file): 256132
- Inventoried files (everything except this file): 21, 252502 bytes

## Prune rules

Applied to `git ls-files` at the pinned commit. Any path with a dot-prefixed component
(`.github/`, `.gitignore`, ...) is dropped. A path is kept only when it matches an include
pattern (fnmatch; `*` also matches `/`) and no exclude pattern. File contents are
byte-identical to upstream; nothing is edited.

Include:

- `*.go`
- `go.mod`
- `README.md`
- `Makefile`

Exclude:

- (none beyond the default rule)

## Inventory

| File | Bytes | SHA-256 |
|---|---:|---|
| `LICENSE` | 1481 | `0b77fdea862f869ef4f201ac5649bdbd38ccb055c7f6454dffae9e2adb4941c1` |
| `Makefile` | 950 | `778dc10a05e9c6fd88fb065fe209a2efdb90bedf112b8a1944f0552aa867cb64` |
| `README.md` | 25673 | `3901b1f75356be1033fb66199166fc310eff47d07e43c9e4f311c928de32e932` |
| `bench_test.go` | 2119 | `06c718a2b0deaf9ba9e12e7e5d581603fd3efeef26f366039b70a15e62bc14b8` |
| `doc.go` | 11238 | `ce31609fc8771d867b51319781dac696d2b97c4ca5b322f3b91b93999b3ce35b` |
| `example_authentication_middleware_test.go` | 1154 | `16e7966505cc82a6a19b3317968bbe35319ad6872135e6f5e1ce7f1f71f94e4e` |
| `example_cors_method_middleware_test.go` | 1179 | `65c33b4e9ecae368b3401e64652027d596d6b68fd4473c8771e5ebc43d3040c9` |
| `example_route_test.go` | 1752 | `bed389e44add76b1a9b9273760c5a6f2268364521a8ab67e151048cde16543e7` |
| `example_route_vars_test.go` | 744 | `3b8387bea2383204b2b1770418247fa62c312c181f52078bef87da278ec34f7c` |
| `go.mod` | 39 | `f291c6cca97f4c0e46c8fc4dc1005380d225800a42f28774928bec80f2c553bd` |
| `middleware.go` | 3507 | `70ba00e0414acaf92b47fff2550a2ebcdbbe6832306b570220691c461b098d5f` |
| `middleware_test.go` | 19923 | `d5c004efaaefb626922e9f2324c21cb690f4d1039627651b960d2cc21b93da09` |
| `mux.go` | 19917 | `c707fbb675cd43278c1c315e4075f0b4f0e9b34479cecb099c12ad09a1e75e76` |
| `mux_httpserver_test.go` | 1460 | `f1b5c848cc26b8684482536054668875e09b431ff17e1ace666ff9570df3a29c` |
| `mux_test.go` | 100196 | `2a78bbea1d3037b19e2576fdf19410e8f862bd6026e1db6cdf006aa1e9bdf814` |
| `old_test.go` | 18009 | `fa4da62a2c5959e3c2eb674035897765fb8293347502e2af08ba2679899672cc` |
| `regexp.go` | 10933 | `e4b46b57ad1cba48774a473a4ebb8521cad94f97bdab52d35d64f7becb90aa29` |
| `regexp_test.go` | 3166 | `c330ccc7a483e426e5ae01719865b5248f96288acc98eae70dbb19db2305cd16` |
| `route.go` | 24397 | `c38f996705a96623f9fd50b3e8d584786c3d591ffe1b304730f08f2bd8cc00db` |
| `route_test.go` | 3899 | `f7ef82f4d7b5dd7831249384e93fed9580595ad5170128b703e5f03995f1faa1` |
| `test_helpers.go` | 766 | `bac7640e2e55975905b28b5b2179a32edef8f730a6dc57b937d1eaf4686441c1` |

## Eval use

`evals/run_evals.py` copies this directory into a temporary git repository, creates
deterministic synthetic commits (fixed dates) so COCHANGE exists, ingests it offline and
scores OSS retrieval tasks, call-edge resolution, and impacted-test selection.
