"""Command-line video transcription using faster-whisper."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

MODEL_NAME = "small"
COMPUTE_TYPE = "int8"
VAD_FILTER = True
TRANSCRIPT_JSON_FILENAME = "transcript.json"
TRANSCRIPT_TEXT_FILENAME = "transcript.txt"


class SegmentLike(Protocol):
    """Protocol for segment objects returned by faster-whisper."""

    start: float
    end: float
    text: str


class WhisperModelLike(Protocol):
    """Protocol for the faster-whisper model instance used by this module."""

    def transcribe(
        self,
        audio: str,
        *,
        vad_filter: bool,
    ) -> tuple[Iterable[SegmentLike], object]:
        """Transcribe an audio or video path into iterable segment objects."""


class WhisperModelFactory(Protocol):
    """Protocol for constructing a faster-whisper model."""

    def __call__(
        self,
        model_size_or_path: str,
        *,
        compute_type: str,
    ) -> WhisperModelLike:
        """Return a configured faster-whisper model instance."""


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """A normalized transcript segment."""

    start: float
    end: float
    text: str

    def to_json_dict(self) -> dict[str, float | str]:
        """Return this segment in the transcript JSON schema."""
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class TranscriptOutputs:
    """Paths and segment data produced by a transcription run."""

    json_path: Path
    text_path: Path
    segments: tuple[TranscriptSegment, ...]


def format_timestamp(seconds: float) -> str:
    """Format seconds as a human-readable HH:MM:SS.mmm timestamp."""
    if seconds < 0:
        raise ValueError("timestamp seconds must be non-negative")

    total_milliseconds = int(round(seconds * 1000))
    milliseconds = total_milliseconds % 1000
    total_seconds = total_milliseconds // 1000
    seconds_part = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60

    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}.{milliseconds:03d}"


def write_transcript_files(
    output_dir: str | Path,
    segments: tuple[TranscriptSegment, ...],
) -> TranscriptOutputs:
    """Write transcript JSON and text files to the output directory."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    json_path = output_path / TRANSCRIPT_JSON_FILENAME
    text_path = output_path / TRANSCRIPT_TEXT_FILENAME

    json_payload = {
        "segments": [segment.to_json_dict() for segment in segments],
    }
    json_path.write_text(
        json.dumps(json_payload, indent=2) + "\n",
        encoding="utf-8",
    )

    text_lines = [
        f"[{format_timestamp(segment.start)} - {format_timestamp(segment.end)}] {segment.text}"
        for segment in segments
    ]
    text_path.write_text("\n".join(text_lines) + ("\n" if text_lines else ""), encoding="utf-8")

    return TranscriptOutputs(json_path=json_path, text_path=text_path, segments=segments)


def transcribe_video(
    video_path: str | Path,
    output_dir: str | Path,
    model_factory: WhisperModelFactory | None = None,
    *,
    language: str | None = None,
    task: str = "transcribe",
) -> TranscriptOutputs:
    """Transcribe (or translate to English) a video and write transcript outputs.

    ``task="translate"`` makes Whisper emit English text for non-English
    speech, which the English-keyword rule extractor needs. ``language``
    (e.g. ``he``) skips auto-detection for faster, more reliable results.
    """
    if task not in ("transcribe", "translate"):
        raise ValueError("task must be 'transcribe' or 'translate'")
    source_path = Path(video_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"Video file not found: {source_path}")

    whisper_model_factory = model_factory or _load_whisper_model_class()
    model = whisper_model_factory(MODEL_NAME, compute_type=COMPUTE_TYPE)
    transcribe_kwargs: dict[str, object] = {"vad_filter": VAD_FILTER, "task": task}
    if language is not None:
        transcribe_kwargs["language"] = language
    raw_segments, _transcription_info = model.transcribe(
        str(source_path),
        **transcribe_kwargs,
    )
    segments = tuple(_normalize_segment(segment) for segment in raw_segments)

    return write_transcript_files(output_dir, segments)


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Transcribe a video into transcript.json and transcript.txt.",
    )
    parser.add_argument("video_path", help="Path to the source video file.")
    parser.add_argument("output_dir", help="Directory where transcript files are written.")
    parser.add_argument(
        "--language",
        default=None,
        help="Spoken language code (e.g. 'he' for Hebrew); default auto-detects.",
    )
    parser.add_argument(
        "--task",
        choices=("transcribe", "translate"),
        default="transcribe",
        help="'translate' emits English text for non-English speech.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the transcription CLI and return a process exit code."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    try:
        outputs = transcribe_video(args.video_path, args.output_dir, language=args.language, task=args.task)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except ImportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"Wrote {outputs.json_path}")
    print(f"Wrote {outputs.text_path}")
    return 0


def _load_whisper_model_class() -> WhisperModelFactory:
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise ImportError(
            'faster-whisper is not installed. Install it with: pip install -e ".[transcribe]"',
        ) from error

    return WhisperModel


def _normalize_segment(segment: SegmentLike) -> TranscriptSegment:
    return TranscriptSegment(
        start=float(segment.start),
        end=float(segment.end),
        text=segment.text.strip(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
