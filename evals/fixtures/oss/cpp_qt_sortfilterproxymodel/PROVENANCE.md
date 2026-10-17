# Provenance: cpp_qt_sortfilterproxymodel

- Language family: C/C++ with Qt
- Upstream: https://github.com/oKcerG/SortFilterProxyModel
- Commit: `5a930885b7ea99f7f41c25fce08bf8006ee54e3f` (immutable; committer date 2021-05-03T22:51:37+02:00)
- Archive URL: https://github.com/oKcerG/SortFilterProxyModel/tree/5a930885b7ea99f7f41c25fce08bf8006ee54e3f
- License: MIT
- `LICENSE`: verbatim copy of upstream `LICENSE` (sha256 `8642e24b888e670e3ce2642942a611bfd3c5eb67d72415bdf8f9d4afe554451e`)
- Other upstream license files kept verbatim: (none)
- Snapshot acquired: 2026-10-17 by `scripts/vendor_oss_fixtures.py --fetch` (clone at the pinned commit)
- Upstream tracked files at that commit: 142
- Vendored files (including LICENSE and this file): 86
- Vendored bytes (including LICENSE and this file): 196670
- Inventoried files (everything except this file): 85, 185671 bytes

## Prune rules

Applied to `git ls-files` at the pinned commit. Any path with a dot-prefixed component
(`.github/`, `.gitignore`, ...) is dropped. A path is kept only when it matches an include
pattern (fnmatch; `*` also matches `/`) and no exclude pattern. File contents are
byte-identical to upstream; nothing is edited.

Include:

- `CMakeLists.txt`
- `README.md`
- `SortFilterProxyModel.pri`
- `SortFilterProxyModel.qbs`
- `qqmlsortfilterproxymodel.*`
- `filters/*`
- `sorters/*`
- `proxyroles/*`
- `tests/*`

Exclude:

- (none beyond the default rule)

## Notes

Qt 5 QML plugin: QAbstractProxyModel/QObject subclasses with Q_PROPERTY, signals, slots, qmlRegisterType, CMake + qmake/qbs build files, and Qt Quick Test QML tests.

## Inventory

| File | Bytes | SHA-256 |
|---|---:|---|
| `CMakeLists.txt` | 1381 | `d45ddd3e55d377d5814653aa97044cf5a791b5db1c0ad5dda6f49f9704a34a20` |
| `LICENSE` | 1082 | `8642e24b888e670e3ce2642942a611bfd3c5eb67d72415bdf8f9d4afe554451e` |
| `README.md` | 4430 | `956f15923b678bfdebfeb7a62f8d6d05b15c560dcdbdd8a12fcfbfbabbc9a018` |
| `SortFilterProxyModel.pri` | 2198 | `931249ff32efaf287ae6481b8caa3a63caea1bc22b844a6c33706dcb556398c1` |
| `SortFilterProxyModel.qbs` | 2085 | `f723c95e83713a7f24103e3de7a1336f7d83bf3b370c7fefa424ecd2d8e3f488` |
| `filters/alloffilter.cpp` | 1012 | `c5d1ed945102900c03f9dda5d954d16821970679e0e9cc1290c7d2182faa50b0` |
| `filters/alloffilter.h` | 384 | `9b0123cd230ac3c653652bcbe8053bac156d0bf59a0ab7babaa807c4508867b1` |
| `filters/anyoffilter.cpp` | 1474 | `4f8ba860ba7947d5174d0da917281619f58af97f2e10fb851c866621109f3dbd` |
| `filters/anyoffilter.h` | 384 | `3f7b5c853065474fcaa6ba553b3c45a8a1e18363b74ecc65f33bf56c4195f03d` |
| `filters/expressionfilter.cpp` | 4387 | `788245721d363c977ba75386eb22ea0e5f7a186be27976e9ee0d988144f5a46c` |
| `filters/expressionfilter.h` | 964 | `3143de9da2c13f94703b397c12af6680554973b6a0b8e9b43570fcec166bdfb8` |
| `filters/filter.cpp` | 1861 | `332e748dd4074315fe39d10f75137204498b735b2115c3cba22ec66f88e4b72c` |
| `filters/filter.h` | 1061 | `e93532708eab4c2103fb30ef102d026f29484e197a245247aacecf0919240a75` |
| `filters/filtercontainer.cpp` | 3510 | `0c327cdeeb976c9b2c2f696e2c2dcd2574f47e1fc6c22acd54a43d6ae98f8dfa` |
| `filters/filtercontainer.h` | 1737 | `517581b68c3baf73e64c8624af2e159d433f71538317c7d8206ffd912b6243ec` |
| `filters/filtercontainerfilter.cpp` | 592 | `a725b3a69bcc6eb8f9929faee9e466738238abf428741ad8a0903773ac423b39` |
| `filters/filtercontainerfilter.h` | 760 | `87e4c910db618c7a5b12508780d4f21846467892524a786320d74bd331370952` |
| `filters/filtersqmltypes.cpp` | 1184 | `98ea3797edd2c2df6d5b8ffc296c86a718f1b6923d3028d5ee2dc73a6ad0b908` |
| `filters/indexfilter.cpp` | 2832 | `969176b1f3b6aba71efb8a5f2ab776ccf9409bddbd8ed73bbc9e10c7a153bf26` |
| `filters/indexfilter.h` | 895 | `9ff07bfd6c5a268ae48c78d565d1c9289a581cac32f7f544e9a0cc391a764a04` |
| `filters/rangefilter.cpp` | 3331 | `95b815accc173f8ea14022d6da8fa4d0f025a064ee3e6c12eb25c4c0533a89ca` |
| `filters/rangefilter.h` | 1438 | `6c8e270b9392d56404687b3e7b42ad9c513a49a76967e91d1a8c9775027ca550` |
| `filters/regexpfilter.cpp` | 3284 | `95d53624ee3c086d4b4bfd873162cafcb131ca9b328bfbe2eaa27587af235d59` |
| `filters/regexpfilter.h` | 1544 | `716ece1d5787097e374f5ccd41d70571850d964e7d08b34fe59457a518ef5b9b` |
| `filters/rolefilter.cpp` | 1156 | `82a0ffc9d08a8378ee7f39d7def7212bde5aea2adb27a08b2ba2a2024f3beb1d` |
| `filters/rolefilter.h` | 563 | `0abdf13c4b9617344bc90b01a8223b8b987f82d2f9209426b184fbee3de854e1` |
| `filters/valuefilter.cpp` | 1304 | `a09f2caeb10f2319d2145afbed4f0f021647d518babfc130f736c1c492cf789b` |
| `filters/valuefilter.h` | 584 | `1d01be59e3bd626a66f905eb290b33cf84fd766ab606bbb2dc622d90540a9fa3` |
| `proxyroles/expressionrole.cpp` | 4293 | `e290b9ccbc94b8f4cdd710f112a93343807ac6042c98c9e84f96fe04fe91c0f3` |
| `proxyroles/expressionrole.h` | 953 | `b7a4e1a2ef4752d25db9afee2373cf563dd9912dfd3b8eaa01427ead03f4a69f` |
| `proxyroles/filterrole.cpp` | 1707 | `73e892ffd042b2185707be15396261d0dc70f4353ce8c93f176334657dcb20e2` |
| `proxyroles/filterrole.h` | 700 | `77ac01e0749b499ae4cc3dea2ab23d22417086c5c020385e083bf872795bbcbb` |
| `proxyroles/joinrole.cpp` | 1868 | `48000201daeb1d78d9026a052e58ed3891e871afab2bf791bba8339dc84f4a21` |
| `proxyroles/joinrole.h` | 808 | `c3376f6705131fcdbf36a7eded509bb865acf4b7332e2e207d21f1793c52bc7f` |
| `proxyroles/proxyrole.cpp` | 1165 | `08c5a75fd68d09da59dd5ac2329684765be65a5d29c781f9cfc6e8a3e83c2e6d` |
| `proxyroles/proxyrole.h` | 812 | `dfbe0dd6690dff391ae933aea9a25af80e0ba9c08c5815f0b444f4b8d0900bf9` |
| `proxyroles/proxyrolecontainer.cpp` | 1848 | `46e836b4317cd449b5c3cbb14d741125118e70cca3d83ae70ee6b43645d7ff32` |
| `proxyroles/proxyrolecontainer.h` | 1214 | `ac412079c081b78ca5b6c9e2911a9eb06039334a133a1bbf290409a6258b7571` |
| `proxyroles/proxyrolesqmltypes.cpp` | 814 | `0c974c9335f666c32419ccb60ed7c7d0013aba3a7f250e5181577bba61c60e58` |
| `proxyroles/regexprole.cpp` | 3015 | `c442d37c0508e335a5dbfd48d233a0ff81ef7d1aad5a2ac6ceef0f894b2d823d` |
| `proxyroles/regexprole.h` | 1152 | `d79a04f3e5ad48b34ef4faa4d001320e15d9c90ebf137aa967f0339049b1b4e2` |
| `proxyroles/singlerole.cpp` | 1262 | `49b407b7b45a37500cece055ba1eed5ec97dd7da3594f8cb2beb246391125291` |
| `proxyroles/singlerole.h` | 687 | `8ce452b3b1d8caa05f6ff64b8f1bff53fce5caf955949714cc04850f10f18761` |
| `proxyroles/switchrole.cpp` | 5003 | `730e459e5d7837fd023a8d4a176ad2a411e1dfe7026fe54593d43a10010b9b2d` |
| `proxyroles/switchrole.h` | 1816 | `399c5bf41634d5a6f56bb9cbb0ea0e7874d5ff194d9b7db87ce28a0d9e8cead5` |
| `qqmlsortfilterproxymodel.cpp` | 17965 | `c30a2d1aee2ae3d502f383628ede2476bcd1d678492ba6e9887fbe1482345177` |
| `qqmlsortfilterproxymodel.h` | 5747 | `557a8537c4c59575b56cdb2c13d6657c9ac0f8ec3e2945d7715de3348aae28c9` |
| `sorters/expressionsorter.cpp` | 5140 | `8f8c18a54790991b1d5c78e91b2b740a04c7a98d17136bbc53cd72ae0e421557` |
| `sorters/expressionsorter.h` | 1025 | `37839e7126a1c094b603473ccea151d171522b4c050ebca269a67d4198ca9a8b` |
| `sorters/filtersorter.cpp` | 2225 | `ecbb35cb4600181280fdd9c48b9dac61074f5805342764306f4d4c8a33b67fd5` |
| `sorters/filtersorter.h` | 930 | `3cfc3e4e97748d8f4a2f84e46929e2cfaf3c05cb47dd7fd467805d0453db7eb7` |
| `sorters/rolesorter.cpp` | 1830 | `f3a6a3b13d4c6523e5e4da8b1cf333b69021f3486bc8c041401f5d7ae7e807c8` |
| `sorters/rolesorter.h` | 750 | `1de643ff2cf976a39a2d97b502a3a8d90d0962ccfd4c65ab72a5c47be68d9e15` |
| `sorters/sorter.cpp` | 3405 | `502173d6152a425b2be87541b0b4efd5b9c1ec7ee6873f6ce0da03b708a16f04` |
| `sorters/sorter.h` | 1682 | `9bb5472aad10a53e828601b8215472e65fc9451cd3c5299e14a63e2b4fb5c51b` |
| `sorters/sortercontainer.cpp` | 3518 | `61d89cf09f6c64067c1d0d61e2ab5bb5651fa7161b889ed548e7c28d84297932` |
| `sorters/sortercontainer.h` | 1758 | `71d5c5bbbd78ea3a89a4338cb67780ad309b9a8bb2c85109ac83e57aff398744` |
| `sorters/sortersqmltypes.cpp` | 911 | `d61bb83cf2b1111c5482292fdab70b02ed1ed7f52f603ed99e27e896ae9ad1c5` |
| `sorters/stringsorter.cpp` | 3142 | `073b6654cbe9aad22380ea666923334417b6317505f0ac157b2434c4069baff7` |
| `sorters/stringsorter.h` | 1342 | `b36cd35f255d9b358626f8e8bfe3f59047d93ff25abfdb1e56c079cd3b76fac8` |
| `tests/SortFilterProxyModel.pro` | 821 | `32e99a957b7be37bbccc281850999e30f30cf11ff6a1e82240483eaef73d2c0c` |
| `tests/indexsorter.cpp` | 772 | `d2b38db012c082962b63ba750582b6e84a441c9bfff2d527c1139023a8c5f667` |
| `tests/indexsorter.h` | 579 | `6ddd1899732ee6433d3db0816d0357e773ed6a7611a2324ade5c41925ce5781c` |
| `tests/testroles.cpp` | 1175 | `98d83c675187089a3b821df478e94bc37e135ce8f166a43891b0ff61e571d772` |
| `tests/testroles.h` | 1077 | `fb12bba29ea242e3087060c5adc8cf58329a5ff9fd295a9edc8b845b0095257b` |
| `tests/tst_builtins.qml` | 1752 | `f4f5803d6defbd2a90f439bb3f8b750d8a08a72fddecd21aa5201fc5c75eadf2` |
| `tests/tst_delayed.qml` | 7178 | `d5e0cd25104c72bb26af270d6925722ac0a3f020cd3c59ff064dcbe6c54e56be` |
| `tests/tst_expressionrole.qml` | 984 | `f3ddd9c4e08bba9872b5c0b341f0d9bd629ad23516fb15724121214a1774979e` |
| `tests/tst_filtercontainerattached.qml` | 1461 | `6f1c4f6e353f69008eb1f064e533e2e8565cc4d568314d2593a7ef9d97b01489` |
| `tests/tst_filtercontainers.qml` | 2741 | `c253fef3367044b9533c19e41de44cfac5ba35da8467bc491557fc8cd179c552` |
| `tests/tst_filterrole.qml` | 1288 | `0354975bde18fcc0aab925b49051e72effadf7970cfab35e7025e29b293622d2` |
| `tests/tst_filtersorter.qml` | 1189 | `5ed13743239596af5d843091bde17010fc1e9728e6fd9c872188388ef7e6d68d` |
| `tests/tst_helpers.qml` | 3484 | `4a924d9d2c2ebdfce907744de782f4da499d4dfb2f482d086c18eeadc5a16945` |
| `tests/tst_indexfilter.qml` | 3071 | `60f78fad03d045930937935897ed5fe6b36b7841818ec19f384e0d09454e1988` |
| `tests/tst_joinrole.qml` | 1146 | `b6c140b596124556a6d2f9be488fe614e3bc9ba7562ed9ae7aed6f04248b812b` |
| `tests/tst_proxyroles.qml` | 3556 | `ecc5545a6b5b347ac823c8a9e785944dc8902f6dda2a61a436c169a0bd84baf0` |
| `tests/tst_rangefilter.qml` | 3575 | `0b6b7a89ca6602354b042bb172417391beb7b765ccb5bbcfb69de8140140a46d` |
| `tests/tst_regexprole.qml` | 1816 | `e297811b07a3218effd56f18262008c56bb7087360bb1bfbec047b4d1973fec4` |
| `tests/tst_rolesorter.qml` | 2393 | `d6a8f97ff210e63d4c940ec8222fc0704e6e178156aeaf71fce76d02ec5167d7` |
| `tests/tst_sortercontainerattached.qml` | 4368 | `198a26e8f2f4050504bff75d45b0d4c9971133bf14729f3d7a0b71796abf0fc6` |
| `tests/tst_sorters.qml` | 5784 | `65545b619cbd03bfbfa22a88dda8c9a716a86aee0f3770b604b62807aa8ab38a` |
| `tests/tst_sortfilterproxymodel.cpp` | 73 | `d64643f31ec590dd143123ba398193e1783c0a36b6e8f86d5df66e7d4478026e` |
| `tests/tst_sourceroles.qml` | 1167 | `fd060a7088013176702d45a64a0a1ac2b0df3e096749f0584c286776f00bd17a` |
| `tests/tst_stringsorter.qml` | 3283 | `9f92b1f67dcd84bf964f8e1f358de362a5b8ef7d2e87fc2850552f97e3c60484` |
| `tests/tst_switchrole.qml` | 3034 | `878f6bdb12e4f0f9f7507edf4ffe35434236ab9378c64ce478701a9713d6417c` |

## Eval use

`evals/run_evals.py` copies this directory into a temporary git repository, creates
deterministic synthetic commits (fixed dates) so COCHANGE exists, ingests it offline and
scores OSS retrieval tasks, call-edge resolution, and impacted-test selection.
