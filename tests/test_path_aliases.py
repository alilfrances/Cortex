"""B.6 (0.12.2): lowercase concatenated file stems (`widgetwaitready.cpp`) are split into words."""
from __future__ import annotations

import pytest

from cortex.bundle import _tokenize_text


@pytest.mark.xfail(strict=True, reason="B.6: concatenated lowercase stems are one token")
def test_a_concatenated_stem_is_split_by_the_classes_of_the_file():
    """Hard gate: with the file's class `WidgetWaitReadyStep`, the path yields `wait` and `ready`; the
    unsplit stem stays so an exact-name query still matches."""
    terms = _tokenize_text("src/widgetwaitready.cpp", labels=("WidgetWaitReadyStep",))

    assert {"wait", "ready", "widget"} <= terms
    assert "widgetwaitready" in terms
