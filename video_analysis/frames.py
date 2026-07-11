"""Command-line frame extraction from videos using OpenCV."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

DEFAULT_INTERVAL_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class ExtractedFrame:
    """Metadata for one extracted frame."""

    path: Path
    index: int
    timestamp_seconds: float


@dataclass(frozen=True, slots=True)
class FrameExtractionResult:
    """Result metadata from a frame extraction run."""

    output_dir: Path
    frames: tuple[ExtractedFrame, ...]
    fps: float


def extract_frames(
    video_path: str | Path,
    output_dir: str | Path,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
) -> FrameExtractionResult:
    """Extract one frame per interval from a video."""
    if interval_seconds <= 0:
        raise ValueError("interval seconds must be greater than 0")

    source_path = Path(video_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"Video file not found: {source_path}")

    cv2 = _load_cv2()
    capture = cv2.VideoCapture(str(source_path))
    try:
        if not capture.isOpened():
            raise ValueError(f"Video could not be opened: {source_path}")

        fps = float(capture.get(cv2.CAP_PROP_FPS))
        if fps <= 0:
            raise ValueError(f"Video FPS could not be determined: {source_path}")

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        frame_interval = max(1, round(fps * interval_seconds))
        extracted_frames: list[ExtractedFrame] = []
        frame_number = 0

        while True:
            success, frame = capture.read()
            if not success:
                break

            if frame_number % frame_interval == 0:
                timestamp_seconds = frame_number / fps
                frame_index = len(extracted_frames)
                frame_path = output_path / _frame_filename(frame_index, timestamp_seconds)
                if not cv2.imwrite(str(frame_path), frame):
                    raise ValueError(f"Frame could not be written: {frame_path}")
                extracted_frames.append(
                    ExtractedFrame(
                        path=frame_path,
                        index=frame_index,
                        timestamp_seconds=timestamp_seconds,
                    ),
                )

            frame_number += 1

        return FrameExtractionResult(
            output_dir=output_path,
            frames=tuple(extracted_frames),
            fps=fps,
        )
    finally:
        capture.release()


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Extract timestamped JPG frames from a video.",
    )
    parser.add_argument("video_path", help="Path to the source video file.")
    parser.add_argument("output_dir", help="Directory where JPG frames are written.")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help="Seconds between extracted frames. Defaults to 2.0.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the frame extraction CLI and return a process exit code."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    try:
        result = extract_frames(args.video_path, args.output_dir, args.interval)
    except (FileNotFoundError, ImportError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"Wrote {len(result.frames)} frames to {result.output_dir}")
    return 0


def _frame_filename(frame_index: int, timestamp_seconds: float) -> str:
    return f"frame_{frame_index:06d}_{timestamp_seconds:010.2f}s.jpg"


def _load_cv2() -> ModuleType:
    try:
        import cv2
    except ImportError as error:
        raise ImportError(
            'OpenCV is not installed. Install it with: pip install -e ".[frames]"',
        ) from error

    return cv2


if __name__ == "__main__":
    raise SystemExit(main())
