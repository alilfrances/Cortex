# Tokenizer Calibration

Measured on 2026-07-17 with:

```bash
python3 evals/calibrate_tokenizer.py
```

The script compares Cortex's dependency-free stdlib `re` segmenter with `tiktoken` `o200k_base` over the current repository, grouped by indexed source kind. It deliberately enables the tokenizer's stdlib-segmenter override so the result describes the default install even when the optional third-party `regex` module is present.

| Kind | Files | Raw segments | Exact tokens | Factor | Mean file ratio | Mean-vs-aggregate drift |
|---|---:|---:|---:|---:|---:|---:|
| code | 108 | 294683 | 219081 | 0.743 | 0.736 | -1.0% |
| markdown | 19 | 78865 | 52804 | 0.670 | 0.658 | -1.7% |
| text | 11 | 2568 | 1978 | 0.770 | 0.803 | 4.3% |

Checked-in rounded factors:

```python
CALIBRATION = {
    "code": 0.74,
    "markdown": 0.67,
    "text": 0.77,
}
```

Each rounded factor's aggregate estimate is within 1% of the exact corpus total, well inside the P1-4 ±15% acceptance bound. Re-run the script and update this evidence when the calibration corpus or target encoding changes substantially.
