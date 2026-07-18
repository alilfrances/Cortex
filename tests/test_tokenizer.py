import sys
import unittest
from unittest import mock

import cortex.tokenizer as tokenizer_mod
from cortex.tokenizer import CALIBRATION, count_text_tokens, raw_segment_count, truncate_text_to_budget


class TokenizerTests(unittest.TestCase):
    def setUp(self) -> None:
        # Snapshot the tiktoken encoder cache so each test can force either
        # the exact or heuristic path independent of whatever is actually
        # pip-installed in the environment running the suite.
        self._orig_encoding = tokenizer_mod._tiktoken_encoding
        self._orig_unavailable = tokenizer_mod._tiktoken_unavailable
        self._orig_force_stdlib = tokenizer_mod._force_stdlib_segments

    def tearDown(self) -> None:
        tokenizer_mod._tiktoken_encoding = self._orig_encoding
        tokenizer_mod._tiktoken_unavailable = self._orig_unavailable
        tokenizer_mod._force_stdlib_segments = self._orig_force_stdlib

    def test_count_text_tokens_is_deterministic(self) -> None:
        text = "Cortex tracks files, commits, and sections."
        self.assertEqual(count_text_tokens(text), count_text_tokens(text))
        self.assertGreater(count_text_tokens(text), 0)

    def test_truncate_text_to_budget_appends_marker(self) -> None:
        # This assertion targets the stdlib heuristic path promised by the
        # default install. Keep it deterministic when the optional tiktoken
        # extra happens to be present in the test runner; the exact path has a
        # separate test below.
        tokenizer_mod._tiktoken_encoding = None
        tokenizer_mod._tiktoken_unavailable = True
        text = "alpha beta gamma delta epsilon zeta"
        # The stdlib fallback is intentionally exercised even when the
        # optional `regex` extra is installed in the runner.
        with mock.patch.dict(sys.modules, {"regex": None}):
            truncated = truncate_text_to_budget(text, 7)
            self.assertTrue(truncated.endswith("...[truncated]"))
            self.assertLessEqual(count_text_tokens(truncated), 7)

    def test_heuristic_path_used_when_tiktoken_unavailable(self) -> None:
        """P1-4: with `tiktoken` absent (the default, stdlib-only install),
        count_text_tokens must fall back to the calibrated regex-segment
        heuristic instead of raising or silently returning 0.

        `sys.modules['tiktoken'] = None` makes `import tiktoken` raise
        ImportError deterministically -- this is what actually not having
        the package installed looks like to `_get_tiktoken_encoding`, so
        this test is a faithful stand-in for running the suite with the
        `[tokens]` extra never installed, regardless of what the CI/dev
        sandbox running this test happens to have on its path.
        """
        tokenizer_mod._tiktoken_encoding = None
        tokenizer_mod._tiktoken_unavailable = False
        with mock.patch.dict(sys.modules, {"tiktoken": None}):
            text = "def handshake(): return connect_and_verify(peer_id)"
            tokens = count_text_tokens(text, kind="code")
            self.assertGreater(tokens, 0)
            self.assertEqual(
                tokens, max(1, round(raw_segment_count(text) * CALIBRATION["code"]))
            )
            self.assertTrue(tokenizer_mod._tiktoken_unavailable)

    def test_stdlib_segment_override_ignores_optional_regex_module(self) -> None:
        fake_regex = mock.Mock()
        fake_regex.findall.return_value = ["one-fake-token"]
        tokenizer_mod._force_stdlib_segments = True
        with mock.patch.dict(sys.modules, {"regex": fake_regex}):
            self.assertEqual(raw_segment_count("alpha beta"), 3)
        fake_regex.findall.assert_not_called()

    def test_exact_path_used_when_tiktoken_available(self) -> None:
        """When tiktoken *is* importable, count_text_tokens must defer to its
        exact encoder instead of the calibrated heuristic -- kind is then
        irrelevant to the result. Uses a fake encoder so this is verifiable
        without a real (network-fetched) tiktoken encoding.
        """
        fake_encoding = mock.Mock()
        fake_encoding.encode.return_value = list(range(7))
        tokenizer_mod._tiktoken_encoding = fake_encoding
        tokenizer_mod._tiktoken_unavailable = False
        self.assertEqual(count_text_tokens("anything", kind="markdown"), 7)
        fake_encoding.encode.assert_called_once()

    def test_truncate_bounds_output_for_code_kind(self) -> None:
        code = "\n".join(
            f"def handler_{i}(event, context):\n    process(event, context)\n" for i in range(200)
        )
        budget = 40
        truncated = truncate_text_to_budget(code, budget, kind="code")
        self.assertTrue(truncated)
        self.assertLessEqual(count_text_tokens(truncated, kind="code"), budget)

    def test_unknown_kind_falls_back_to_text_factor(self) -> None:
        tokenizer_mod._tiktoken_encoding = None
        tokenizer_mod._tiktoken_unavailable = True
        text = "alpha beta gamma"
        self.assertEqual(count_text_tokens(text, kind="commit"), count_text_tokens(text, kind="text"))

    def test_checked_in_calibration_factors_are_measured(self) -> None:
        self.assertEqual(
            CALIBRATION,
            {"code": 0.74, "markdown": 0.67, "text": 0.77},
        )

    def test_calibration_factors_are_sane(self) -> None:
        """CALIBRATION should only ever scale segment counts down (or leave
        them unchanged): the plan's whole premise is that the regex-segment
        heuristic overestimates real BPE tokens, especially for code. A
        factor > 1 would silently invert that and is almost certainly a
        transcription error when pasting fresh evals/calibrate_tokenizer.py
        output in."""
        for kind, factor in CALIBRATION.items():
            self.assertGreater(factor, 0, kind)
            self.assertLessEqual(factor, 1.0, kind)
