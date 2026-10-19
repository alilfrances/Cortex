# Provenance: ruby_rack

- Language family: Ruby
- Upstream: https://github.com/rack/rack
- Commit: `a9833c8f3bd6b6d1e0ab35de00a1f1a16b5095f5` (immutable; committer date 2026-09-08T12:16:22+12:00)
- Archive URL: https://github.com/rack/rack/tree/a9833c8f3bd6b6d1e0ab35de00a1f1a16b5095f5
- License: MIT
- `LICENSE`: verbatim copy of upstream `MIT-LICENSE` (sha256 `1b7ebcb5691b416620794892b6ff40473485770dbcc6c63bd33409d4cfd0cefc`)
- Other upstream license files kept verbatim: (none)
- Snapshot acquired: 2026-10-17 by `scripts/vendor_oss_fixtures.py --fetch` (clone at the pinned commit)
- Upstream tracked files at that commit: 186
- Vendored files (including LICENSE and this file): 100
- Vendored bytes (including LICENSE and this file): 775708
- Inventoried files (everything except this file): 99, 763656 bytes

## Prune rules

Applied to `git ls-files` at the pinned commit. Any path with a dot-prefixed component
(`.github/`, `.gitignore`, ...) is dropped. A path is kept only when it matches an include
pattern (fnmatch; `*` also matches `/`) and no exclude pattern. File contents are
byte-identical to upstream; nothing is edited.

Include:

- `lib/*`
- `test/*.rb`
- `rack.gemspec`
- `Gemfile`
- `Rakefile`
- `README.md`

Exclude:

- (none beyond the default rule)

## Notes

Non-Ruby test data (multipart bodies, CGI assets, .ru rackup files) is pruned; the specs are never executed by the eval harness.

## Inventory

| File | Bytes | SHA-256 |
|---|---:|---|
| `Gemfile` | 400 | `07d7305ca8a7a4399aae0d62dea08b43a4240a6904948d5a84bc676be1542204` |
| `LICENSE` | 1108 | `1b7ebcb5691b416620794892b6ff40473485770dbcc6c63bd33409d4cfd0cefc` |
| `README.md` | 14906 | `961026e6d859d147d94d922f7473032f3dd5479c9734cb8dec65a9de55174177` |
| `Rakefile` | 3913 | `80c65239662f9493673d400980c6be73aa2034881a7eb81b32d076891e7142e6` |
| `lib/rack/auth/abstract/handler.rb` | 850 | `ee35b5a54188a0feb38bf75fdb512eaae52fb2f49eb8164f410e54926a2cca7a` |
| `lib/rack/auth/abstract/request.rb` | 860 | `38b216377787c2fb848108608d1f4766ee83e3c2db25089cedf1fd90dde47202` |
| `lib/rack/auth/basic.rb` | 1180 | `4f9876e34944ecdf8b0a9c93b9866934f0d7d47d8d2c1021c2f8c586244f1c75` |
| `lib/rack/bad_request.rb` | 164 | `d4a2f4d0398bf56e72dced29aee03359047227e13ec2868985e740ad2e55a92b` |
| `lib/rack/body_proxy.rb` | 1486 | `9b4d87c9c93c964a274255a7dee18869ca89dfe906629218b39f01ca65469195` |
| `lib/rack/builder.rb` | 9136 | `31c0edc6a3614aa25431d1ff31b7a389664ff6d2acfc4cdf2861d244d21cf140` |
| `lib/rack/cascade.rb` | 2212 | `c461e454e11b6230b58b1b62dc1977ef5a843d67527cb8fd8bd57398ae1c9677` |
| `lib/rack/common_logger.rb` | 3210 | `6ea29fbf55753cc461ea06e6f720d973b0ae72bb92c43e70d83cfe62ab44b620` |
| `lib/rack/conditional_get.rb` | 3041 | `383fecf39f70f2a78608184c520219465375dce7bf459d7b5b26c94289531a53` |
| `lib/rack/config.rb` | 410 | `9fa2303f67b99e7f62e6636ac6e0d483b33b18baf7f20164c6d6dff85602190d` |
| `lib/rack/constants.rb` | 2818 | `f98bc22e72688ba48e2e410536bd8d4e8109951a056bcad4b49c2a0b9b977c1b` |
| `lib/rack/content_length.rb` | 806 | `3c60e3d13da315acf2a2c575d4823c29278255fac9df97c738d7a8a741fff042` |
| `lib/rack/content_type.rb` | 695 | `6e0db9593f557163b714dc90a268e6bee133ef3e43273548efc35fac30e8b5d0` |
| `lib/rack/deflater.rb` | 6323 | `aad6c1d0e15c611a555373e6582eee958e4c27912bf8bf7c36c66d1b815e51d1` |
| `lib/rack/directory.rb` | 6638 | `b22e8c2082ce2954bfc7720b2f0d51babcbcb30c82a98daed02a12917af08b6a` |
| `lib/rack/etag.rb` | 1971 | `beebe0e8352218d2a1132fbb13c43660d6bb8df01c45aeecc0a22bf9e397e3a2` |
| `lib/rack/events.rb` | 5296 | `7e49a77efad026e0835545dc75f666be35e58b5678b38c431fd2c6142d244e2c` |
| `lib/rack/files.rb` | 6265 | `e06cad90631e344df0981e91ad5575efc5ac56fa7b860a5f41bd9ba2c9acf36d` |
| `lib/rack/head.rb` | 457 | `2ae5d300e202fa5a8d734c801a88ef33cf99146798f58eb42e4f6f06711a6009` |
| `lib/rack/headers.rb` | 4734 | `c970fdbcb9153d306c18ec32f64a1f3ce51b50ab56e7b9d3a34efc27a3567807` |
| `lib/rack/lint.rb` | 44990 | `6938f81030d2e1e6deddec78744222eb7abb3db0bd5d5f5ff9aaff81640dc41f` |
| `lib/rack/lock.rb` | 573 | `3892cb1813907223cc40d8897054150375ccd95704e4002a51a00af7d0fde752` |
| `lib/rack/media_type.rb` | 1885 | `43c91a573c63edb47155ad4d7cf784d310f050e67e4d275a96bddb476d561834` |
| `lib/rack/method_override.rb` | 1817 | `72334f29d5834c10f1d3ec10ad7d8f51604c80ad707f2a2162dc2e0291ebc4a8` |
| `lib/rack/mime.rb` | 33420 | `448ead50764bc25830ac36ca713899f7515efb3018eaa205005c3a354cfaa730` |
| `lib/rack/mock.rb` | 63 | `e29a9fba2772ac6469c4ff19c2b64de6f4a33fc208235ea8cfc23cae7fe1087b` |
| `lib/rack/mock_request.rb` | 5539 | `474e045b4cd25ddeb9adfc00abae5a56b27dbaeab5706ac5560eee45e6d9beca` |
| `lib/rack/mock_response.rb` | 3994 | `7b9a78e416f4f5431da8cffeb1d1caa603f826d1c9e5e0e325b2a349d67bb061` |
| `lib/rack/multipart/generator.rb` | 2502 | `5a822e74d18d5ff5f8c1a60bc063536e4183f8dc0901036a10331230a9a1d3d9` |
| `lib/rack/multipart/parser.rb` | 21604 | `fca0baa004ca660aed3a3d90f810c07b2fb0243410b388ee7449afa8f3a04571` |
| `lib/rack/multipart/uploaded_file.rb` | 3547 | `0ca512df969689c23ce4f24087ed2e37b5e5539cb8107e0eb610b1b27c75cac7` |
| `lib/rack/multipart.rb` | 1900 | `ad376c017ad040d9c8524d19701ba9b5b9fcfc489c5d523f70bce2a934c7e787` |
| `lib/rack/null_logger.rb` | 1213 | `87ddd6ff62799fcf7a80f6ca06c406977ed8fe67a71abf9b82547d6ca2790cab` |
| `lib/rack/query_parser.rb` | 8758 | `937d2827ac10d7c47e201617a9b001a9bd344d48714f4603756cb39c09801201` |
| `lib/rack/ractorize.rb` | 760 | `98b7156f680bc32bbf04e6e42acec2707f56eecdfd80f19755bb203901dd849f` |
| `lib/rack/recursive.rb` | 1822 | `92ffcb956de9b06b2fc2516395e1574c19cb0f5743c702b1806a8ac4147d7483` |
| `lib/rack/reloader.rb` | 3095 | `6103d8e0138cc7e9c16ff58c57a527f81387679d02723b5ad94494e4f187e151` |
| `lib/rack/request.rb` | 27974 | `1f49190842216d0e0c9a88cf0cab283733d0c67d838292ee3615545ee2a704fd` |
| `lib/rack/response.rb` | 11981 | `0e96b707e75207379df37045d0d50bcd7fb05da20bc56e5f00bf225835762b21` |
| `lib/rack/rewindable_input.rb` | 3240 | `1bee33bbed6030b4a78dfe8fb045aa40a757ab7d6b3389c3f9b2e025fc8d27c4` |
| `lib/rack/runtime.rb` | 870 | `c07bc0de1850728c77b2e5436c930abdfc3b0decb036a27dc7d77a58c28122bf` |
| `lib/rack/sendfile.rb` | 7020 | `702a096bdf29b799a890eaa85db2cf4ac8e0975fd47907871add7dc8599cc5fe` |
| `lib/rack/show_exceptions.rb` | 14440 | `2100c2e82a31c8e013b8cb8a99854c7f9e5fb6b5dbc710a3c39eeb2f1fbf749d` |
| `lib/rack/show_status.rb` | 3644 | `7c6c4fafe025b8bb73b1d5030801d903c5b0a4bf74ae6996fd65ed73a8823667` |
| `lib/rack/static.rb` | 6398 | `199b3d6e51012adce3259f570d8f34d21e1d647224efcb9e426fa85d0c6a2f0c` |
| `lib/rack/tempfile_reaper.rb` | 1055 | `2af23288b82076ddab36bb18bf69361ee10affddea265dcb0a35a19f3c83e4d5` |
| `lib/rack/urlmap.rb` | 2886 | `d4bc42acde4fb45f6753c71647899ac6cef4c4b8b2fde501a3b74cf19e0feb4c` |
| `lib/rack/utils.rb` | 24227 | `7e0dc4fb3350fef32c934b3a3c6fcabb98c3d21e45e5390e1d6176ca46449105` |
| `lib/rack/version.rb` | 391 | `27be3b79c14bd1c700a11e88d40673be617d505e0463a2ea69e2150e498b244b` |
| `lib/rack.rb` | 2344 | `9dbb02dfaf2fe254aeb4316f293abbec289fe589210e80a60e1ef3f47daa612d` |
| `rack.gemspec` | 1425 | `40c56b87deb696baf8941f17e0eba8c922dcbb1999c5657bb8cdc1ba649a43e8` |
| `test/builder/an_underscore_app.rb` | 138 | `133fa094024e552160453656de3a610a2477a43285f8906c427ae7639398e8df` |
| `test/cgi/rackup_stub.rb` | 108 | `915b811bc47b199de8a4e6210c2202f5b86e9415d9083343498814455f5602f1` |
| `test/gemloader.rb` | 329 | `5ffe383c22f78ea472ce7b5316579427bec1216145777d5c3c7a95f2f079fddb` |
| `test/helper.rb` | 1134 | `1f9fd551b38c74a4bad477cd0b3cc7644d4f2f9adbee7397cec7e24140f1ed7e` |
| `test/psych_fix.rb` | 273 | `6b1536c8632b80c5e0069d7d613c74345fac266292614a6cbfe856845edccfe8` |
| `test/spec_auth_basic.rb` | 3073 | `52e64547ee8a752dbf5eb8da324be3ef13ea28eabc647ed77b8558d94d224e2c` |
| `test/spec_body_proxy.rb` | 3259 | `12df5bf135b001ae1b31d3a3137089710798d4c8f41ca7fbec6336aad7dc2c9f` |
| `test/spec_builder.rb` | 10132 | `b0a838efb7b488f8b0f64363ebafc4758d7dc05ec13b27d9bf20c9fe03e42d58` |
| `test/spec_cascade.rb` | 3157 | `3d6711823463967ace119ecc15b3737957ea449052201bcf71561ec688f9d60f` |
| `test/spec_common_logger.rb` | 4647 | `f2fbcd8213f91cf1f08ceb0c5a798899a60aaa7786399682fb57d8591b08c6f5` |
| `test/spec_conditional_get.rb` | 5198 | `ac0881e48b36050c87fe9e3dc9c284b00be8146b63249184b57c6e2edb90f3ea` |
| `test/spec_config.rb` | 671 | `0b90f2f4633095f7f92da05572cf325a43326a2441c635a5ec29496916a3c209` |
| `test/spec_content_length.rb` | 2954 | `d3bce42a71b3bd47ad6b98f14a535bb341f901cddc367e691439480a51459c32` |
| `test/spec_content_type.rb` | 1379 | `a5063332d834c35d36b95e4610e38ea42b6e5bccb96c5ecb6a67bf1c851bb6a3` |
| `test/spec_deflater.rb` | 16801 | `1b109b3b1da826cdb5a981d81f1e8f307e1b7c80079f8e783d2883cf6b6c154e` |
| `test/spec_directory.rb` | 7684 | `4e16a44a002a271a6b64bc9cabd072fcbcaed9da50f006599259f6ae37b4304b` |
| `test/spec_etag.rb` | 4618 | `3e2eebe643ff4f6f87ce8a421d4c802fad1583b5391b2687d0c62c26fb284b69` |
| `test/spec_events.rb` | 4373 | `23c8340f338d3036ae149b3e0e077f46a0d53b3a01b9ddb1938d6a75ec39067d` |
| `test/spec_files.rb` | 11624 | `88c4204bf9beac88235c12ca801ee75854371c941594bdb28398f4a9d27931fd` |
| `test/spec_head.rb` | 1472 | `4ff5eb5d9848ee2aa21f6aed99b087ab206e749c0cf6deb1200ce29ef2d28708` |
| `test/spec_headers.rb` | 14901 | `b5dd6e922092b09d64e1be836f3e127a0a0f0e4a1425e38781024246fa5999f2` |
| `test/spec_lint.rb` | 42999 | `7d7e35ade875c75b44422d7f0ddda97a71d26009ade9c37d65a4cf58b4b8bd27` |
| `test/spec_lock.rb` | 4896 | `00fed5b68bbfe079bcbdf67b625df78b44d23def24b248dcd887c3705e05b6e2` |
| `test/spec_media_type.rb` | 2274 | `08e6c69b1871cca042761c7d405f8ada37018eea8c06374bc40fb4ed26e3979e` |
| `test/spec_method_override.rb` | 6857 | `712c6ad0702f291f47e96691e9f59c5c9ca2168bf15dbf6ddb96c5b9bfd09273` |
| `test/spec_mime.rb` | 1909 | `b93ee46b22c050c8ee4c9832320ab141d512eb665addf372b982c2c98c820a9a` |
| `test/spec_mock_request.rb` | 11384 | `67fea10e75f26fdd69d7ec4edfa5d2ed52017cf904e10f577f1aa38529199056` |
| `test/spec_mock_response.rb` | 11333 | `775a1431085591f13ce3facacb4319b997b204324a20bf30d7f0c99791bc063a` |
| `test/spec_multipart.rb` | 54000 | `9ab063a6687a7d3a2d2357bd2ad2704f2a46d62339b9dc0ad1c6bebb7f2b8fca` |
| `test/spec_null_logger.rb` | 655 | `0ad193d37cfaffa1470699273763985a968fe2d743838054fe9201285eef0559` |
| `test/spec_query_parser.rb` | 3645 | `1392c3bb89e777c1e326698d3d5df330d331acef7c1555a4541003eddf685c9b` |
| `test/spec_recursive.rb` | 2050 | `c4d4bc514e1527897319e08442804ea6e945bb137cc1323a8ac2e63c84f550c0` |
| `test/spec_request.rb` | 86720 | `aa134a5c89e695bfe0ed73d358eb313b1fc97e31218802da8376d5b123e45296` |
| `test/spec_response.rb` | 24922 | `2295c3f5cb62986a3d6b7c2c2cdf97dbf68a795ca79d0d7eaecd5563dd230868` |
| `test/spec_rewindable_input.rb` | 4499 | `f266f04e3bef533242657abd9e5a75e25f21a147099eb924dba6a736d21e62a2` |
| `test/spec_runtime.rb` | 1722 | `d230ab42bb40032bc9be0698ae03aaecf9e7b1c2385e9a4378cb301f99c3649c` |
| `test/spec_sendfile.rb` | 12278 | `f504eafe39f85f8ce57f912664be3bd236d2db0108487fa760e950d705132308` |
| `test/spec_show_exceptions.rb` | 5747 | `7b37fc7786b3767b740ce6fdcd94ab44c5dcf755629222d24780e22d75231070` |
| `test/spec_show_status.rb` | 3849 | `751722e7278814259a5baec33555707bc614a56a497d1d08c3eda2ae70f42237` |
| `test/spec_static.rb` | 10301 | `6fa232627ca18f104d6d55caa876f331fdd334da3556c917448afe6189a5e3cb` |
| `test/spec_tempfile_reaper.rb` | 4561 | `881d5ed2ee16268902e64fbea092bc0a4a4d0e0989df6f87da768b6c3dfb6190` |
| `test/spec_urlmap.rb` | 9737 | `367b53ae94db05721d3b003ab72a225247d871e793f36b03e52715a953cea4b5` |
| `test/spec_utils.rb` | 34632 | `a0c0090520e20d3c597469d361c0e52372fb049d8cc3899e096c96a332bfee3b` |
| `test/spec_version.rb` | 505 | `fdc7fcf1b2454178e2c2e4317c99d27c18ca2d73e22d37169c0079dfb5f765f0` |

## Eval use

`evals/run_evals.py` copies this directory into a temporary git repository, creates
deterministic synthetic commits (fixed dates) so COCHANGE exists, ingests it offline and
scores OSS retrieval tasks, call-edge resolution, and impacted-test selection.
