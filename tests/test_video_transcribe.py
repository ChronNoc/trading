"""Tests for video transcription CLI behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from video_analysis import transcribe


@dataclass(frozen=True, slots=True)
class FakeSegment:
    """A minimal faster-whisper segment stand-in."""

    start: float
    end: float
    text: str


class FakeWhisperModel:
    """A fake WhisperModel that records transcription calls."""

    def __init__(self) -> None:
        """Create a fake model with no recorded calls."""
        self.calls: list[tuple[str, bool]] = []

    def transcribe(
        self,
        audio: str,
        *,
        vad_filter: bool,
        task: str = "transcribe",
        language: str | None = None,
    ) -> tuple[list[FakeSegment], object]:
        """Return deterministic fake segments."""
        self.calls.append((audio, vad_filter, task, language))
        return (
            [
                FakeSegment(start=1.25, end=2.5, text=" first setup "),
                FakeSegment(start=65.0, end=66.789, text="second setup"),
            ],
            object(),
        )


def test_transcribe_video_writes_json_and_text_with_mocked_model(tmp_path: Path) -> None:
    """A mocked WhisperModel produces both transcript output files."""
    video_path = tmp_path / "session.mp4"
    output_dir = tmp_path / "out"
    video_path.write_bytes(b"not a real video")
    fake_model = FakeWhisperModel()
    factory_calls: list[tuple[str, str, str]] = []

    def fake_model_factory(model_size_or_path: str, *, device: str, compute_type: str) -> FakeWhisperModel:
        factory_calls.append((model_size_or_path, device, compute_type))
        return fake_model

    outputs = transcribe.transcribe_video(video_path, output_dir, model_factory=fake_model_factory)

    assert factory_calls == [("small", "cpu", "int8")]
    assert fake_model.calls == [(str(video_path), True, 'transcribe', None)]
    assert outputs.json_path == output_dir / "transcript.json"
    assert outputs.text_path == output_dir / "transcript.txt"
    assert json.loads(outputs.json_path.read_text(encoding="utf-8")) == {
        "segments": [
            {"start": 1.25, "end": 2.5, "text": "first setup"},
            {"start": 65.0, "end": 66.789, "text": "second setup"},
        ],
    }
    assert outputs.text_path.read_text(encoding="utf-8") == (
        "[00:00:01.250 - 00:00:02.500] first setup\n"
        "[00:01:05.000 - 00:01:06.789] second setup\n"
    )


def test_cli_uses_mocked_whisper_model(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """The CLI path uses the configured faster-whisper options."""
    video_path = tmp_path / "session.mp4"
    output_dir = tmp_path / "transcript"
    video_path.write_bytes(b"not a real video")
    fake_model = FakeWhisperModel()

    def fake_model_factory(model_size_or_path: str, *, device: str, compute_type: str) -> FakeWhisperModel:
        assert model_size_or_path == "small"
        assert device == "cpu"
        assert compute_type == "int8"
        return fake_model

    monkeypatch.setattr(transcribe, "_load_whisper_model_class", lambda: fake_model_factory)

    exit_code = transcribe.main([str(video_path), str(output_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "transcript.json" in captured.out
    assert fake_model.calls == [(str(video_path), True, 'transcribe', None)]
    assert (output_dir / "transcript.json").is_file()
    assert (output_dir / "transcript.txt").is_file()


def test_cli_missing_file_reports_clear_error(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """A missing video path returns a clear CLI error without a traceback."""
    missing_video = tmp_path / "missing.mp4"
    output_dir = tmp_path / "out"

    exit_code = transcribe.main([str(missing_video), str(output_dir)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Video file not found" in captured.err
    assert "Traceback" not in captured.err
    assert not output_dir.exists()
