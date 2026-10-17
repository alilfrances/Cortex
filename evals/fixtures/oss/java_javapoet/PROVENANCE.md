# Provenance: java_javapoet

- Language family: Java
- Upstream: https://github.com/square/javapoet
- Commit: `b9017a9503b76e11b4ad4c1a9f050e2d29112cb0` (immutable; committer date 2024-10-10T14:04:58-07:00)
- Archive URL: https://github.com/square/javapoet/tree/b9017a9503b76e11b4ad4c1a9f050e2d29112cb0
- License: Apache-2.0
- `LICENSE`: verbatim copy of upstream `LICENSE.txt` (sha256 `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`)
- Other upstream license files kept verbatim: (none)
- Snapshot acquired: 2026-10-17 by `scripts/vendor_oss_fixtures.py --fetch` (clone at the pinned commit)
- Upstream tracked files at that commit: 50
- Vendored files (including LICENSE and this file): 43
- Vendored bytes (including LICENSE and this file): 493784
- Inventoried files (everything except this file): 42, 486587 bytes

## Prune rules

Applied to `git ls-files` at the pinned commit. Any path with a dot-prefixed component
(`.github/`, `.gitignore`, ...) is dropped. A path is kept only when it matches an include
pattern (fnmatch; `*` also matches `/`) and no exclude pattern. File contents are
byte-identical to upstream; nothing is edited.

Include:

- `src/main/*`
- `src/test/*`
- `pom.xml`
- `README.md`

Exclude:

- (none beyond the default rule)

## Inventory

| File | Bytes | SHA-256 |
|---|---:|---|
| `LICENSE` | 11358 | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |
| `README.md` | 28886 | `77f861703d12ec802224ff2f098c78b0e17edb56f891ddd3634dadf735aa996d` |
| `pom.xml` | 4867 | `b7601a2b8fa2bb08c599fea578478453b91915a3696217afb97b640cbe6f2332` |
| `src/main/java/com/squareup/javapoet/AnnotationSpec.java` | 9860 | `11c705a612adea403097a5988dd3790be0815c6e485d79f9f20070ba9b4f8ccd` |
| `src/main/java/com/squareup/javapoet/ArrayTypeName.java` | 3575 | `353e4506213f68ab4f69128b6ebbc8f45536560f7fdd76df8eb2e447409ab0b1` |
| `src/main/java/com/squareup/javapoet/ClassName.java` | 11188 | `5720dfc422e48b0e3b3cc6fb71c2c0fe1b0ef7ab310e68e6c8d454452fedcdf6` |
| `src/main/java/com/squareup/javapoet/CodeBlock.java` | 16305 | `29990fd9d84952e06bd4b47be201e5a07431b82f884e9f9d13a2624052b89cd3` |
| `src/main/java/com/squareup/javapoet/CodeWriter.java` | 18315 | `d37bfd04070cf632a2d447ed85427a14b249de75db7f1a7eb0a313f721e276c2` |
| `src/main/java/com/squareup/javapoet/FieldSpec.java` | 5530 | `70e9b5d0692a18e2c9049faef62f83e29fd3dc654eb82e6796cd67721559ceaa` |
| `src/main/java/com/squareup/javapoet/JavaFile.java` | 11407 | `66bbaf6c24386fdf62e4f2e65941aae3fece9fe6dd66f0073c942ddfb2b9641e` |
| `src/main/java/com/squareup/javapoet/LineWrapper.java` | 5456 | `5bce40fb319f3a9a95d50edaf5b19198f22a5884c7b85c2dc3fcf189095a10b3` |
| `src/main/java/com/squareup/javapoet/MethodSpec.java` | 18301 | `5f246598e9ad6595f39cccbdd2bfed045a8e9053e67e9381dbda7b1027a3d34d` |
| `src/main/java/com/squareup/javapoet/NameAllocator.java` | 6071 | `ce692da91e539b64d33ea0ba76fac827d8cd304898f4f6a552e0f2e606109590` |
| `src/main/java/com/squareup/javapoet/ParameterSpec.java` | 6535 | `e31e5dae8820622254356a38f57c6b5df43295c86c7fa416d0b436661538f19e` |
| `src/main/java/com/squareup/javapoet/ParameterizedTypeName.java` | 5336 | `d14dc1dba47318d3a23f7a175bfb6ef9693ca06489d05d2c682891279513ed52` |
| `src/main/java/com/squareup/javapoet/TypeName.java` | 15225 | `94e9c9651dd369425bebe85ec654dd862834597500fe89a7ab4c3f6343857483` |
| `src/main/java/com/squareup/javapoet/TypeSpec.java` | 31297 | `c750e1988351104ed08e1c1c4ee67ded6da71073dd954103fc87e363c5230c57` |
| `src/main/java/com/squareup/javapoet/TypeVariableName.java` | 6456 | `42ee5bf519d5a23dace7b62d47a8c8648e241ece364c06ac90a2d31bd4d33bd8` |
| `src/main/java/com/squareup/javapoet/Util.java` | 4697 | `e5685706506bc936dc39a3796e5e789aac3e6d3d2759e52e793e98f442e475c2` |
| `src/main/java/com/squareup/javapoet/WildcardTypeName.java` | 4714 | `7f80605e2d21f13febf0d874dbb47bc1076808ec3c014a17f7520087aaef5460` |
| `src/test/java/ClassNameNoPackageTest.java` | 1240 | `4782d8cdd99bf70ce9b59a8878deb6e51e433ce0a6cfc7d9dce60ed60d83a5c5` |
| `src/test/java/com/squareup/javapoet/AbstractTypesTest.java` | 11681 | `20720cb0eb0c2986f85812143630c902b1944b31bac7f9b3e8632e13ef207264` |
| `src/test/java/com/squareup/javapoet/AnnotatedTypeNameTest.java` | 9077 | `3906ff4f7429232bd2bfb1c839ccce4d93469f6b5d2c1cf85d13480dafc843fc` |
| `src/test/java/com/squareup/javapoet/AnnotationSpecTest.java` | 14012 | `eccd501ae733485bbc57d09b012a2ba5f11fc55ead169584c056a59f61389f7e` |
| `src/test/java/com/squareup/javapoet/ClassNameTest.java` | 8665 | `8f02afbf9d8abebedd2882ed843b52c7eb77434e24abcb0fff5a85cf083a380b` |
| `src/test/java/com/squareup/javapoet/CodeBlockTest.java` | 12069 | `b09a615190916150ff15a0932c8b3f165ffdb02b441351071a51ef448c860e27` |
| `src/test/java/com/squareup/javapoet/CodeWriterTest.java` | 644 | `9000b166bc7ee07ce732a96f3c63114ffa7c73868df86fc1213a8d02800774a4` |
| `src/test/java/com/squareup/javapoet/FieldSpecTest.java` | 2328 | `c75f2f83abc0e6f3c09b71e9077e369e95f5b80f55c27e613bfc625f79a2dca0` |
| `src/test/java/com/squareup/javapoet/FileReadingTest.java` | 5025 | `bfc58858beb9540d0464ba9636476357df86f115dc79dea358ef3511c3cb2031` |
| `src/test/java/com/squareup/javapoet/FileWritingTest.java` | 8918 | `2cbf104e77ebf65c3f29fd795b1f53cd2507a274c0e3333be0d5bdca1f63494f` |
| `src/test/java/com/squareup/javapoet/JavaFileTest.java` | 35493 | `d8d8d4e1513259347e2e143a5b16d5761bd64ef5cd7f9d89ff7d578ec3d267a7` |
| `src/test/java/com/squareup/javapoet/LineWrapperTest.java` | 7494 | `a0573b36033afd4cb99789a64a5bfae520a1a8b9337310e7b68c354a4adc1a08` |
| `src/test/java/com/squareup/javapoet/MethodSpecTest.java` | 17901 | `36d0c615e8d50fdf0dc26096fe2373f9e32c940f777bdf8ff95d513bbae2c119` |
| `src/test/java/com/squareup/javapoet/NameAllocatorTest.java` | 4345 | `ef840603b63b5f4e71c82548d546a99a85d14bdd5b4dfa7dbd19d962ac16036f` |
| `src/test/java/com/squareup/javapoet/ParameterSpecTest.java` | 5294 | `426519c066c95fb9a058a74d1560a363750f1d6979d71c79855059364412f49e` |
| `src/test/java/com/squareup/javapoet/TestFiler.java` | 3230 | `7238b636919b4f21b760f3af1680b6f5a7a45e38214a4bc5657fdfa59f14a47a` |
| `src/test/java/com/squareup/javapoet/TestUtil.java` | 520 | `1473b760e07201caa10a0192beed1de510031070bb7d734b2a6830ef1fa9391d` |
| `src/test/java/com/squareup/javapoet/TypeNameTest.java` | 8477 | `185816fae791699caab05bd8506ada93ccbd8d9fff6789f7074d5692e106b5a9` |
| `src/test/java/com/squareup/javapoet/TypeSpecTest.java` | 94473 | `66dc394e573e12cb36c4136720beddaee15bd0164a068619e95e916c0d21304d` |
| `src/test/java/com/squareup/javapoet/TypesEclipseTest.java` | 5447 | `c108797c231a3bde6ce2d89a819d3d75d5434056288e448eabcb9e3c75b9ac96` |
| `src/test/java/com/squareup/javapoet/TypesTest.java` | 1177 | `788a8e971f76455527d924b9120c71aac081e135888d5e17d5da6e60260137f1` |
| `src/test/java/com/squareup/javapoet/UtilTest.java` | 3698 | `d1c34cb4033c4e8de608c6576fc5d54a1a0a53ebb88665c8138042befe0cec5e` |

## Eval use

`evals/run_evals.py` copies this directory into a temporary git repository, creates
deterministic synthetic commits (fixed dates) so COCHANGE exists, ingests it offline and
scores OSS retrieval tasks, call-edge resolution, and impacted-test selection.
