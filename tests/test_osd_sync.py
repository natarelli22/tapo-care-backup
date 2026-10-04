from datetime import datetime
from pathlib import Path
import pytest
from tapo_care_backup.osd_sync import (
    _get_templates,
    parse_filename_datetime,
    detect_video_preroll,
)


def test_parse_filename_datetime():
    filename = "2026-10-02_04-13-16_0_b985444030.mp4"
    dt, date_str, rest = parse_filename_datetime(filename)
    assert dt == datetime(2026, 10, 2, 4, 13, 16)
    assert date_str == "2026-10-02"
    assert rest == "_0_b985444030"

    # Invalid pattern
    dt_inv, _, _ = parse_filename_datetime("invalid_filename.mp4")
    assert dt_inv is None


def test_templates_embedded():
    templates = _get_templates()
    assert len(templates) == 10
    for d in range(10):
        assert d in templates
        arr, norm = templates[d]
        assert arr.shape == (50, 28)
        assert norm > 0.0


def test_detect_video_preroll_with_sample():
    sample_video = Path("/home/caionatarelli/HA/scratch/arquivo3.mp4")
    if not sample_video.exists():
        pytest.skip("Sample video scratch/arquivo3.mp4 not present")

    event_dt = datetime(2026, 10, 2, 4, 13, 16)
    preroll, vis_time, score = detect_video_preroll(sample_video, event_datetime=event_dt)
    assert preroll == 4
    assert vis_time == "04:13:12"
    assert score > 0.90
