import unittest

from cortex.tokenizer import count_text_tokens, truncate_text_to_budget


class TokenizerTests(unittest.TestCase):
    def test_count_text_tokens_is_deterministic(self) -> None:
        text = "Cortex tracks files, commits, and sections."
        self.assertEqual(count_text_tokens(text), count_text_tokens(text))
        self.assertGreater(count_text_tokens(text), 0)

    def test_truncate_text_to_budget_appends_marker(self) -> None:
        text = "alpha beta gamma delta epsilon zeta"
        truncated = truncate_text_to_budget(text, 8)
        self.assertTrue(truncated.endswith("...[truncated]"))
        self.assertLessEqual(count_text_tokens(truncated), 8)
