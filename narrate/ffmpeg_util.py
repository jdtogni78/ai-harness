"""Thin wrappers around ffmpeg/ffprobe binaries."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List

FFMPEG = os.environ.get("NARRATE_FFMPEG") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = os.environ.get("NARRATE_FFPROBE") or shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"


def probe_duration(path: Path) -> float:
    out = subprocess.check_output([
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    return float(out.strip())


def to_wav(src: Path, dst: Path, *, sample_rate: int = 44100) -> None:
    subprocess.run([
        FFMPEG, "-y", "-loglevel", "error",
        "-i", str(src), "-ar", str(sample_rate), "-ac", "2", str(dst),
    ], check=True)


def concat_audio(wavs: List[Path], dst: Path) -> None:
    list_path = dst.with_suffix(".concat.txt")
    with list_path.open("w") as f:
        for w in wavs:
            f.write(f"file '{w.resolve()}'\n")
    subprocess.run([
        FFMPEG, "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c", "copy", str(dst),
    ], check=True)


def mux(video: Path, audio: Path, dst: Path) -> None:
    subprocess.run([
        FFMPEG, "-y", "-loglevel", "error",
        "-i", str(video), "-i", str(audio),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        str(dst),
    ], check=True)
