"""Tests for OpenCV frame extraction."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from pytest import CaptureFixture, MonkeyPatch

from video_analysis.frames import extract_frames, main


def test_extract_frames_from_synthetic_video(tmp_path: Path) -> None:
    """Frame extraction writes timestamped JPG files from a generated video."""
    video_path = tmp_path / "synthetic.avi"
    output_dir = tmp_path / "frames"
    _write_synthetic_video(video_path, fps=4.0, frame_count=10)

    result = extract_frames(video_path, output_dir, interval_seconds=0.5)

    assert result.fps == 4.0
    assert [frame.path.name for frame in result.frames] == [
        "frame_000000_0000000.00s.jpg",
        "frame_000001_0000000.50s.jpg",
        "frame_000002_0000001.00s.jpg",
        "frame_000003_0000001.50s.jpg",
        "frame_000004_0000002.00s.jpg",
    ]
    assert all(frame.path.is_file() for frame in result.frames)
    assert all(cv2.imread(str(frame.path)) is not None for frame in result.frames)


def test_cli_extracts_frames_from_synthetic_video(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """The CLI writes frames and reports the output directory."""
    video_path = tmp_path / "synthetic.avi"
    output_dir = tmp_path / "frames"
    _write_synthetic_video(video_path, fps=5.0, frame_count=10)

    exit_code = main([str(video_path), str(output_dir), "--interval", "1.0"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Wrote 2 frames" in captured.out
    assert sorted(path.name for path in output_dir.glob("*.jpg")) == [
        "frame_000000_0000000.00s.jpg",
        "frame_000001_0000001.00s.jpg",
    ]


def test_cli_missing_video_reports_clear_error(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """Missing input video paths return a clear CLI error without a traceback."""
    missing_video = tmp_path / "missing.avi"
    output_dir = tmp_path / "frames"

    exit_code = main([str(missing_video), str(output_dir)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Video file not found" in captured.err
    assert "Traceback" not in captured.err
    assert not output_dir.exists()


def test_cli_unopenable_video_reports_clear_error(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """Unreadable video files return a clear CLI error without a traceback."""
    bad_video = tmp_path / "bad.avi"
    output_dir = tmp_path / "frames"
    bad_video.write_text("not a video", encoding="utf-8")

    exit_code = main([str(bad_video), str(output_dir)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Video could not be opened" in captured.err
    assert "Traceback" not in captured.err


def test_extract_frames_raises_clear_error_when_fps_is_missing(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Videos with missing FPS metadata raise a clear error."""
    video_path = tmp_path / "missing_fps.avi"
    output_dir = tmp_path / "frames"
    video_path.write_bytes(b"placeholder")

    monkeypatch.setattr(
        "video_analysis.frames._load_cv2",
        lambda: SimpleNamespace(CAP_PROP_FPS=5, VideoCapture=MissingFpsCapture),
    )

    with pytest.raises(ValueError, match="Video FPS could not be determined"):
        extract_frames(video_path, output_dir)


class MissingFpsCapture:
    """A fake OpenCV capture object that opens but reports no FPS."""

    def __init__(self, video_path: str) -> None:
        """Create a fake capture for a video path."""
        self.video_path = video_path

    def isOpened(self) -> bool:
        """Return true so FPS validation is reached."""
        return True

    def get(self, property_id: int) -> float:
        """Return zero FPS for every property lookup."""
        return 0.0

    def release(self) -> None:
        """Release the fake capture object."""


def _write_synthetic_video(video_path: Path, fps: float, frame_count: int) -> None:
    width = 64
    height = 48
    writer = cv2.VideoWriter(
        str(video_path),
        cv2.VideoWriter_fourcc(*"MJPG"),
        fps,
        (width, height),
    )
    assert writer.isOpened()

    try:
        for frame_index in range(frame_count):
            frame = np.full(
                (height, width, 3),
                fill_value=frame_index * 20,
                dtype=np.uint8,
            )
            writer.write(frame)
    finally:
        writer.release()
