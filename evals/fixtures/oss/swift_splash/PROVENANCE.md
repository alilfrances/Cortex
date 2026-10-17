# Provenance: swift_splash

- Language family: Swift
- Upstream: https://github.com/JohnSundell/Splash
- Commit: `2e3f17c2d09689c8bf175c4a84ff7f2ad3353301` (immutable; committer date 2022-06-08T22:13:57+02:00)
- Archive URL: https://github.com/JohnSundell/Splash/tree/2e3f17c2d09689c8bf175c4a84ff7f2ad3353301
- License: MIT
- `LICENSE`: verbatim copy of upstream `LICENSE` (sha256 `f9991c39e961e771241a669e9146933687567bb26d36f9a4838afb06fe6273a9`)
- Other upstream license files kept verbatim: (none)
- Snapshot acquired: 2026-10-17 by `scripts/vendor_oss_fixtures.py --fetch` (clone at the pinned commit)
- Upstream tracked files at that commit: 62
- Vendored files (including LICENSE and this file): 55
- Vendored bytes (including LICENSE and this file): 194152
- Inventoried files (everything except this file): 54, 185716 bytes

## Prune rules

Applied to `git ls-files` at the pinned commit. Any path with a dot-prefixed component
(`.github/`, `.gitignore`, ...) is dropped. A path is kept only when it matches an include
pattern (fnmatch; `*` also matches `/`) and no exclude pattern. File contents are
byte-identical to upstream; nothing is edited.

Include:

- `Sources/*`
- `Tests/*`
- `Package.swift`
- `README.md`
- `Makefile`

Exclude:

- (none beyond the default rule)

## Inventory

| File | Bytes | SHA-256 |
|---|---:|---|
| `LICENSE` | 1069 | `f9991c39e961e771241a669e9146933687567bb26d36f9a4838afb06fe6273a9` |
| `Makefile` | 331 | `1e57e033b09870fddcb954b8214d08259bf94610a2c2a6217f0ceb15bb47b629` |
| `Package.swift` | 1162 | `0d9e7307166f6a922351e0e49d2988d73a9d9e1979b7e781923a57f130309aee` |
| `README.md` | 8684 | `764dce7a8381bb1e924e89268689c6d4683b4fba6c0b773e28d645af30782e50` |
| `Sources/Splash/Extensions/CharacterSet/CharacterSet+Contains.swift` | 335 | `b47914989aa2cc7328feb01d89e99049b2c4e1cf8bbca39ad7cf822ce7800d00` |
| `Sources/Splash/Extensions/Equatable/Equatable+AnyOf.swift` | 356 | `2e9230b62656ce8cab3fef9f1f7db7b3ed76f3cd127e13cf214aabb7b358d2f0` |
| `Sources/Splash/Extensions/Int/Int+IsOdd.swift` | 193 | `0f292fe83611f9e1aeb94fb320171c87af4b317c3182006318887824af70b95e` |
| `Sources/Splash/Extensions/Sequence/Sequence+AnyOf.swift` | 517 | `a3972c8260009753d9e254ff5fa8141105df0202f696ced23b68d6fc2051e1b7` |
| `Sources/Splash/Extensions/Sequence/Sequence+Occurrences.swift` | 341 | `1b6ed8b33cd8ab25793b4b62587510afc7e991af23627e18da2db9d523ae021b` |
| `Sources/Splash/Extensions/Strings/String+HTMLEntities.swift` | 522 | `6d8fa9f79f7d38293ef2e05a28c10629547ca746ecd3df6f1a9799cf05de7701` |
| `Sources/Splash/Extensions/Strings/String+IsNumber.swift` | 192 | `cf81abd534ad2a99c48410b2a54ab52a2b8d113750ea5c5c97cda5da823990fa` |
| `Sources/Splash/Extensions/Strings/String+PrefixChecking.swift` | 521 | `3c8f6a7fb5436d5fdd398b4db9491375248023c6a09b0345d138f5bc746bf9ed` |
| `Sources/Splash/Extensions/Strings/String+Removing.swift` | 256 | `7f48a8cdda88d449632fa3d03726382dcd301dbf40ccaed72252c571d1df4a9e` |
| `Sources/Splash/Extensions/Strings/Substring+HasSuffix.swift` | 318 | `05cce51321ed890d2d5bb70c5a0227ec1ac199f8677adf2f18f3ecfc4ecc86e8` |
| `Sources/Splash/Grammar/Grammar.swift` | 1564 | `fa5faf5c3db3d43bcf935cb355f77cd039a776eee75fd0d29df11601de5dafad` |
| `Sources/Splash/Grammar/SwiftGrammar.swift` | 21879 | `6174d7be740a7d221afcf3d08e7faf36405e1e47fb723ead1c011f429fe3dc1a` |
| `Sources/Splash/Output/AttributedStringOutputFormat.swift` | 1883 | `933fcf841e0f21bffd55ff9c55971a710ee039c3668aa37cb0143f3564e2dec1` |
| `Sources/Splash/Output/HTMLOutputFormat.swift` | 2560 | `e36cc411ae1bb6d827d368711a62c035a0c1f35e4ccc8652ef98250e8be34441` |
| `Sources/Splash/Output/MarkdownDecorator.swift` | 1896 | `eaf057eab4b3826341b2c7e80e2f500eeb8a440c97c2849329d4873f03150834` |
| `Sources/Splash/Output/OutputBuilder.swift` | 976 | `720d0ce8c47d06bd4f7ec037a9b440b722daa64c2fed2c507d82200410426aa7` |
| `Sources/Splash/Output/OutputFormat.swift` | 870 | `6d7988acf3932549df2bde468740e64d279be9d16dfefffbf586d30fc39eb31d` |
| `Sources/Splash/Syntax/SyntaxHighlighter.swift` | 3136 | `bf207456d9b6c814ff0d385a5864ddb65a2a5b912415a1fbc2a70159f4f0c08d` |
| `Sources/Splash/Syntax/SyntaxRule.swift` | 777 | `556ff3a9ea0ff9b97b956808e4c2c3b057234092fba1a42a3d06cfb3dcb12288` |
| `Sources/Splash/Theming/Color.swift` | 408 | `0174eb8fb6f46dd75788b2341ebc5118ffa5857ac6f674ee2f619765b4080109` |
| `Sources/Splash/Theming/Font.swift` | 2485 | `ec1515df42469bf6dba06e966fe5ff4f7633f2d4099c95c1c7ce97486ff3981d` |
| `Sources/Splash/Theming/Theme+Defaults.swift` | 6901 | `c9ff7a8d26e88335dcf734b816156efdd5e077fabe6db32640f86c9706177c0f` |
| `Sources/Splash/Theming/Theme.swift` | 1078 | `fa3c0566286054b6e32d9bb07de71c97da35c6ffc065f4548846e0729920a732` |
| `Sources/Splash/Tokenizing/Segment.swift` | 1777 | `da3564b47d7de5d1086928efd167ddc211b8ca03c00f11cae228ec9ab179fc82` |
| `Sources/Splash/Tokenizing/TokenType.swift` | 1154 | `5a4e66cc419760e94d4cb65050406b4378c35002ea5e8e42792b8f887a7dd4fc` |
| `Sources/Splash/Tokenizing/Tokenizer.swift` | 6972 | `0b48fe321fb90423583a3d123d1c50ae85cf5a89f5ee8dfe27aafeeb0c1d014d` |
| `Sources/SplashHTMLGen/main.swift` | 403 | `d6199d99b40d1dbc2aa392e49ae4a5ad8898dba2136bd7a01b5b9e167e327f6d` |
| `Sources/SplashImageGen/Extensions/CGImage+WriteToURL.swift` | 404 | `57062f323c0a72579f694db48aa2312ea0cf08e8593c591beafd1ef70395940f` |
| `Sources/SplashImageGen/Extensions/CommandLine+Options.swift` | 1404 | `efb12a0eb053dfc5807310bf0ba31e7daad8ae5054ee2e263d97c4d520dceca0` |
| `Sources/SplashImageGen/Extensions/NSGraphicsContext+Fill.swift` | 293 | `004a25e2edee815de4453f6fa1c98a347270a4e7c13c10f6e4ade9564f5eb790` |
| `Sources/SplashImageGen/Extensions/NSGraphicsContext+InitWithSize.swift` | 670 | `4651c6a56a6faade714e327d6bf61da06cf96d2c1aed1ddaefe524b3d471ece2` |
| `Sources/SplashImageGen/main.swift` | 1413 | `2913f8da6872e0e15ac75335633da2cabbb50a40911764b1c35e9aa2d65e81a0` |
| `Sources/SplashMarkdown/main.swift` | 715 | `2299271be9638f8f4ee82be829d314688a4ec729412ea7516c0bea447b9d0bc1` |
| `Sources/SplashTokenizer/TokenizerOutputFormat.swift` | 839 | `17c2766d87be058235085f940cf0e269922670c35b960d20e50378870f444d51` |
| `Sources/SplashTokenizer/main.swift` | 399 | `e303af0e94ae746b27160f0c6a5fc026f7159b9d35316311270757a4b4c215b7` |
| `Tests/SplashTests/Core/SyntaxHighlighterTestCase.swift` | 626 | `1d97b59dc3e3d6f4585c335e68d59e05cfe1fb8cd3b7b490612ea8c433a1fe8e` |
| `Tests/SplashTests/Mocks/OutputBuilderMock.swift` | 786 | `0b5e25959c2e10a8265532ef38f75454d557f74d644683a0a5ce3643d08d075f` |
| `Tests/SplashTests/Mocks/OutputFormatMock.swift` | 275 | `9553b69b03598aa3d123ca52bcfc227d9047c68346e88721e3af2224a5cb4f1f` |
| `Tests/SplashTests/Tests/ClosureTests.swift` | 7509 | `a78351c9a5d066c841bee3ef62c49d24027c5b675c99c01a678a73a6c2e2d980` |
| `Tests/SplashTests/Tests/CommentTests.swift` | 10242 | `487c8a82653c59a3c71dbd140be550a4b067661253d0a8fb7f0a58d1bc2451a0` |
| `Tests/SplashTests/Tests/DeclarationTests.swift` | 46442 | `0d70f79a9a5efcd5c30e25165ad31ee3899a2416efb45d795600689f23432704` |
| `Tests/SplashTests/Tests/EnumTests.swift` | 1573 | `4e3212d75e4f712214aef84ba24ebaca9dc96f8787f42a9ebe5ea1bda2d9c325` |
| `Tests/SplashTests/Tests/FunctionCallTests.swift` | 7150 | `eaa90f06579e5e5019e7bd461d8bb743738792c8d7236f688f4844140ba3abc2` |
| `Tests/SplashTests/Tests/HTMLOutputFormatTests.swift` | 1267 | `7789fb3789ce2aae55d528ad7c28f97b2b655f88bc860b316becda26af55989d` |
| `Tests/SplashTests/Tests/LiteralTests.swift` | 9222 | `edda656784985ba5c8f83b0bca74b72852b71e6133f9a969cfffc753eef54bd7` |
| `Tests/SplashTests/Tests/MarkdownTests.swift` | 2266 | `180f7ac76821e1895ac9c80177e63c45c51510ee6d87a218323379ff96ae3843` |
| `Tests/SplashTests/Tests/OptionalTests.swift` | 1179 | `7d7bf268c08cf6f2eace297667abe41b4ac5fae534c1c4e38f33bc299f55b2fa` |
| `Tests/SplashTests/Tests/PreprocessorTests.swift` | 2871 | `f22dec93a95622a65ebf1f0f8a3fd4a14e3beabbb036d91700c1883584249b91` |
| `Tests/SplashTests/Tests/StatementTests.swift` | 16217 | `d8db870e4edb367b1341d77bc67b133727c612dc38107b95078ae0753badb715` |
| `Tests/SplashTests/Tests/TokenTypeTests.swift` | 438 | `be68e656bac2f0d2be26ce67f56e1259f62403dd8bfdbe5296465605182ef8e1` |

## Eval use

`evals/run_evals.py` copies this directory into a temporary git repository, creates
deterministic synthetic commits (fixed dates) so COCHANGE exists, ingests it offline and
scores OSS retrieval tasks, call-edge resolution, and impacted-test selection.
