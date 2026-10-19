# Provenance: ts_p_queue

- Language family: JavaScript/TypeScript
- Upstream: https://github.com/sindresorhus/p-queue
- Commit: `7f272ab5ff2254c0dc2537b7977e88ede1cd55e8` (immutable; committer date 2026-10-13T00:56:40+07:00)
- Archive URL: https://github.com/sindresorhus/p-queue/tree/7f272ab5ff2254c0dc2537b7977e88ede1cd55e8
- License: MIT
- `LICENSE`: verbatim copy of upstream `license` (sha256 `5c932d88256b4ab958f64a856fa48e8bd1f55bc1d96b8149c65689e0c61789d3`)
- Other upstream license files kept verbatim: (none)
- Snapshot acquired: 2026-10-17 by `scripts/vendor_oss_fixtures.py --fetch` (clone at the pinned commit)
- Upstream tracked files at that commit: 27
- Vendored files (including LICENSE and this file): 22
- Vendored bytes (including LICENSE and this file): 318564
- Inventoried files (everything except this file): 21, 314836 bytes

## Prune rules

Applied to `git ls-files` at the pinned commit. Any path with a dot-prefixed component
(`.github/`, `.gitignore`, ...) is dropped. A path is kept only when it matches an include
pattern (fnmatch; `*` also matches `/`) and no exclude pattern. File contents are
byte-identical to upstream; nothing is edited.

Include:

- `source/*`
- `test/*`
- `test-d/*`
- `package.json`
- `tsconfig.json`
- `readme.md`
- `bench.ts`

Exclude:

- (none beyond the default rule)

## Inventory

| File | Bytes | SHA-256 |
|---|---:|---|
| `LICENSE` | 1117 | `5c932d88256b4ab958f64a856fa48e8bd1f55bc1d96b8149c65689e0c61789d3` |
| `bench.ts` | 2091 | `8d14a8bb632e7f9aa16929079fb75b5ed8568b4382e1bec27126156d26fa6e94` |
| `package.json` | 1750 | `56afa5077b5f6d49ee9e4b22e811041de523bc981adf3171003bce0522f203a5` |
| `readme.md` | 30085 | `da4c31fd3a51c874e450ba734ad91852c54100ec47d41aa9f8efea9d43dbcc24` |
| `source/index.ts` | 44078 | `38c619a96e713bb0929cf9c97635bb55ef6fc8cf558431ed47cbb7673299ea8e` |
| `source/lower-bound.ts` | 536 | `f5da31a5384648467beab7deba908b0c729c875823f7e6e578543837306a1f0b` |
| `source/options.ts` | 6043 | `c3db400807b8680fe8db0934685f7e3b6bd82ecaa60742d6fbca183fc43420d1` |
| `source/priority-queue.ts` | 4531 | `1bb3354b697cd97e4b0923add52f2f862bdba1988c064ffc1940ea47950fed49` |
| `source/queue.ts` | 354 | `af5137d512d886f95627b43e3423fb1bd9164efcb34fee22c4b962fd8a96610f` |
| `test/advanced.ts` | 71447 | `0c96421de885fb27bd20a7996daad338ac085de87bcde8a3af2bef948e7c35e6` |
| `test/basic.ts` | 47729 | `01da851fc44b86ac1ebf742d9dbbe8e8ee36e82c1741843dd76ad6c1f7ada487` |
| `test/debug.ts` | 12892 | `30a12930cec84823b3f817a9947a6f2846412a57df0643bdfcc028cf60f77f12` |
| `test/fixtures/types.ts` | 8398 | `b25d67c88de24e6bb363a191f7c94d9ca03110379d8df6207df8fa8a94951fd5` |
| `test/helpers.ts` | 1671 | `44e601bee2fa0ede7e595eec2d8fe7d95089f28437a82431b5fe8a42584427cd` |
| `test/priority-queue.ts` | 12268 | `b81f9617581474f0c523380416c28464c59181b45c6733313135d04ad46ed639` |
| `test/rate-limit.ts` | 32997 | `91cf02276f819948799edcdcbdaff90c6151cc80e837c82b6345ef53255b7759` |
| `test/strict.ts` | 19812 | `c303871447efd92999a78470f0a46aa6a2ccf7495ae1900365062decbedf4cdb` |
| `test/types.ts` | 1909 | `920126ddcc8d5d1ab257bd9bdb22f7cd9b30c6e8011bdf773768fbdb59e45703` |
| `test/validation.ts` | 13911 | `76c9bbf4f317bcb4be9e008defd459e04f16b40f834a950ff4df9391387335b9` |
| `test-d/index.test-d.ts` | 1051 | `a3ca9491e10a373021ec87e2a554db907b8cdf1a694883db8703a1665bf21f22` |
| `tsconfig.json` | 166 | `920b01c0f9b1c9b402f676daa02c04c2141dd8620bd5ab7d257e247b324ba575` |

## Eval use

`evals/run_evals.py` copies this directory into a temporary git repository, creates
deterministic synthetic commits (fixed dates) so COCHANGE exists, ingests it offline and
scores OSS retrieval tasks, call-edge resolution, and impacted-test selection.
