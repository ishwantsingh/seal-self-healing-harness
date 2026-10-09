"""Timing safeguards for narrated recordings; no credentials or model calls."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "demo_narration", Path(__file__).resolve().parents[1] / "scripts" / "narrate_demo.py"
)
narration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(narration)


@pytest.mark.parametrize("segments", [
    [],
    [{"start": 0, "end": 4, "text": "Outside video"}],
    [{"start": 2, "end": 1, "text": "Reversed"}],
    [{"start": 0, "end": float("nan"), "text": "Nonfinite"}],
    [{"start": 0, "end": 1, "text": " "}],
    [{"start": 0, "end": 2, "text": "First"}, {"start": 1, "end": 3, "text": "Overlap"}],
])
def test_invalid_windows_are_rejected_before_paid_generation(segments):
    with pytest.raises(ValueError):
        narration.validate_plan(segments, 3)


def test_overlong_speech_is_not_truncated_or_excessively_accelerated():
    assert narration.speech_speed(2, 3) == 1
    assert 1 < narration.speech_speed(3, 3) < 1.18
    with pytest.raises(ValueError, match="shorten the script"):
        narration.speech_speed(4, 2)


def test_subtitles_follow_offset_and_speech_speed():
    text = "A timed sentence."
    alignment = {"characters": list(text),
                 "character_start_times_seconds": [i / 10 for i in range(len(text))],
                 "character_end_times_seconds": [(i + 1) / 10 for i in range(len(text))]}
    blocks = narration.caption_blocks(alignment, 5, 2)
    assert blocks == [(5, 5.85, text)]
    assert narration.srt_time(65.125) == "00:01:05,125"
    with pytest.raises(ValueError, match="incomplete"):
        narration.caption_blocks({**alignment, "characters": ["A"]}, 0, 1)
