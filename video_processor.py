"""
video_processor.py — Step 2: Video Ingestion & Metadata Module.

Functions:
- get_video_metadata(video_path): ffprobe-based metadata dict.
- download_video_from_url(url, output_dir, max_duration=1800): yt-dlp download,
  capped at 720p and merged to MP4 to stay light on free-server RAM.
- prepare_video_source(uploaded_file_path=None, video_url=None, work_dir=None):
  accepts either an uploaded file or a URL and returns (local_path, metadata).

No API keys are used or stored anywhere in this module.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple

import yt_dlp


class VideoProcessorError(Exception):
    """Raised for any video ingestion / metadata failure."""


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #
def _ffprobe_json(video_path: str) -> dict:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "stream=index,codec_type,width,height,avg_frame_rate",
        "-show_entries", "format=duration",
        "-of", "json",
        video_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise VideoProcessorError(f"ffprobe failed: {proc.stderr.strip()[:300]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise VideoProcessorError(f"ffprobe returned invalid JSON: {exc}")


def _parse_fps(avg_frame_rate: Optional[str]) -> Optional[float]:
    try:
        num_s, den_s = str(avg_frame_rate).split("/")
        num, den = float(num_s), float(den_s)
        if den == 0:
            return None
        return round(num / den, 3)
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def get_video_metadata(video_path) -> Dict:
    """Return duration (s), width, height, fps, has_audio, file_size_mb."""
    path = str(video_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Video file not found: {path}")

    probe = _ffprobe_json(path)
    streams = probe.get("streams", [])
    video_stream = next(
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    if video_stream is None:
        raise VideoProcessorError(f"No video stream found in: {path}")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    duration: Optional[float] = None
    try:
        duration = float(probe.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None

    return {
        "duration": round(duration, 3) if duration is not None else None,
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "fps": _parse_fps(video_stream.get("avg_frame_rate")),
        "has_audio": has_audio,
        "file_size_mb": round(os.path.getsize(path) / (1024 * 1024), 2),
    }


def download_video_from_url(
    url: str, output_dir, max_duration: int = 1800
) -> str:
    """Download a video URL (YouTube etc.) with yt-dlp, max 720p, merged to MP4.

    Returns the local path of the downloaded file.
    Raises VideoProcessorError if the video exceeds max_duration seconds.
    """
    if not url or not isinstance(url, str):
        raise ValueError("A video URL string is required.")
    os.makedirs(output_dir, exist_ok=True)

    # 1) inspect without downloading to enforce the duration cap
    with yt_dlp.YoutubeDL(
        {"quiet": True, "no_warnings": True, "noplaylist": True}
    ) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise VideoProcessorError(f"Could not read video info from URL: {url}")
    if info.get("_type") == "playlist":
        raise VideoProcessorError(
            "Playlists are not supported; provide a single video URL."
        )
    duration = info.get("duration")
    if duration is not None and duration > max_duration:
        raise VideoProcessorError(
            f"Video is {duration / 60:.1f} min, longer than the "
            f"{max_duration / 60:.0f} min limit."
        )

    # 2) download at most 720p, merged to mp4
    outtmpl = os.path.join(str(output_dir), "%(title).60s_[%(id)s].%(ext)s")
    opts = {
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
        expected = ydl.prepare_filename(info)

    mp4_path = os.path.splitext(expected)[0] + ".mp4"
    final = mp4_path if os.path.isfile(mp4_path) else expected
    if not os.path.isfile(final):
        # last resort: newest media file in the output dir
        candidates = sorted(
            (
                p
                for p in Path(output_dir).iterdir()
                if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise VideoProcessorError(
                "Download finished but no video file was found."
            )
        final = str(candidates[0])
    return final


def prepare_video_source(
    uploaded_file_path=None, video_url=None, work_dir=None
) -> Tuple[str, Dict]:
    """Accept an uploaded file OR a video URL; return (local_path, metadata)."""
    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix="video_recap_")
    os.makedirs(work_dir, exist_ok=True)

    if uploaded_file_path:
        src = str(uploaded_file_path)
        if not os.path.isfile(src):
            raise FileNotFoundError(f"Uploaded file not found: {src}")
        # uuid prefix + basename guard against path traversal / collisions
        safe_name = f"{uuid.uuid4().hex}_{Path(src).name}"
        dest = os.path.join(str(work_dir), safe_name)
        shutil.copy2(src, dest)
        local_path = dest
    elif video_url:
        local_path = download_video_from_url(video_url, str(work_dir))
    else:
        raise ValueError("Provide either uploaded_file_path or video_url.")

    metadata = get_video_metadata(local_path)
    return local_path, metadata
