"""
video_processor.py — Step 2: Video Ingestion & Metadata Module.

Functions:
- get_video_metadata(video_path): ffprobe-based metadata dict.
- download_video_from_url(url, output_dir, max_duration=1800): yt-dlp download,
  capped at 720p and merged to MP4 to stay light on free-server RAM.
  YouTube often answers shared cloud IPs with HTTP 403, so the download uses
  the pip-installed Deno runtime and retries with other player clients.
- prepare_video_source(uploaded_file_path=None, video_url=None, work_dir=None):
  accepts either an uploaded file or a URL and returns (local_path, metadata).

Optional environment variable:
- YTDLP_PROXY: proxy URL (http://user:pass@host:port, socks5://...) used for
  URL downloads when YouTube blocks the server's own IP address.

No API keys are used or stored anywhere in this module.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple

import yt_dlp

_LOG = logging.getLogger(__name__)


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
# yt-dlp helpers
# --------------------------------------------------------------------------- #
# YouTube often refuses media requests that come from shared cloud IPs
# (Streamlit Cloud, Hugging Face Spaces, ...): metadata extraction works but
# the download itself fails with "HTTP Error 403: Forbidden". Which player
# client gets through changes by video and by day, so the download is retried
# with other clients. ``None`` means yt-dlp's own pick (with a JavaScript
# runtime that is tv_samsung, visionos, web). The fallbacks are ordered by how
# little they need: tv / web_embedded serve plain HTTPS formats without a GVS
# PO token, web_safari adds HLS formats (also token-free), and android_vr goes
# last because its HTTPS/DASH formats want a PO token in yt-dlp 2026.10.
_PLAYER_CLIENT_ATTEMPTS: Tuple[Optional[Tuple[str, ...]], ...] = (
    None,
    ("tv", "web_embedded"),
    ("web_safari",),
    ("android_vr",),
)

# Lower-case fragments of yt-dlp errors that another client may get around.
_BLOCK_MARKERS = (
    "http error 403",
    "forbidden",
    "not a bot",
    "requested format is not available",
    "only images are available",
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean_msg(msg: object) -> str:
    """yt-dlp message without colour codes, "ERROR:" prefix or edge spaces."""
    text = _ANSI_RE.sub("", str(msg)).strip()
    return re.sub(r"^ERROR:\s*", "", text)


def _looks_blocked(exc: BaseException) -> bool:
    text = _clean_msg(exc).lower()
    return any(marker in text for marker in _BLOCK_MARKERS)


class _YtdlpLogger:
    """Send yt-dlp's messages to ``logging`` instead of stdout/stderr.

    The old ``no_warnings`` option hid every yt-dlp warning, including "No
    supported JavaScript runtime could be found" — the usual reason a YouTube
    download ends in HTTP 403. Warnings and errors now reach the app log.
    """

    def debug(self, msg: str) -> None:  # progress / info lines: not needed
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        _LOG.warning("yt-dlp: %s", _clean_msg(msg))

    def error(self, msg: str) -> None:
        _LOG.error("yt-dlp: %s", _clean_msg(msg))


def _ydl_opts(clients: Optional[Tuple[str, ...]]) -> dict:
    """yt-dlp options shared by the inspect pass and the download pass.

    No ``js_runtimes`` option is needed: yt-dlp looks for Deno by itself,
    including the interpreter's scripts directory where the ``deno`` wheel
    (requirements.txt: ``yt-dlp[deno]``) installs it. Without any JavaScript
    runtime yt-dlp falls back to a single player client and many YouTube
    downloads end in HTTP 403.
    """
    opts: dict = {
        "noplaylist": True,
        "quiet": True,
        "logger": _YtdlpLogger(),
    }
    proxy = os.environ.get("YTDLP_PROXY", "").strip()
    if proxy:
        opts["proxy"] = proxy
    if clients:
        opts["extractor_args"] = {"youtube": {"player_client": list(clients)}}
    return opts


def _download_error_message(exc: BaseException) -> str:
    detail = _clean_msg(exc)
    if _looks_blocked(exc):
        return (
            "YouTube refused the download from this server (HTTP 403 / bot "
            "check) - common on shared cloud IPs. Please download the video "
            "yourself and upload the file instead. "
            "(YouTube က ဒီ server ကနေ ဒေါင်းလုဒ်လုပ်တာကို ပိတ်ထားပါတယ်။ "
            "ဗီဒီယိုကို ကိုယ်တိုင် ဒေါင်းလုဒ်ဆွဲပြီး ဖိုင်အဖြစ် တင်ပေးပါ။) "
            f"[{detail[:160]}]"
        )
    return f"Could not download the video: {detail[:300]}"


def _download_once(
    url: str, output_dir, max_duration: int, clients: Optional[Tuple[str, ...]]
) -> str:
    """One inspect + download pass using the given YouTube player clients."""
    # 1) inspect without downloading to enforce the duration cap
    with yt_dlp.YoutubeDL(_ydl_opts(clients)) as ydl:
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
    opts = _ydl_opts(clients)
    opts.update(
        {
            "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "merge_output_format": "mp4",
            "outtmpl": outtmpl,
        }
    )
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
    Raises VideoProcessorError if the video exceeds max_duration seconds, or
    if every YouTube player client is refused (HTTP 403 / bot check).
    """
    if not url or not isinstance(url, str):
        raise ValueError("A video URL string is required.")
    os.makedirs(output_dir, exist_ok=True)

    last_exc: Optional[BaseException] = None
    for clients in _PLAYER_CLIENT_ATTEMPTS:
        try:
            return _download_once(url, output_dir, max_duration, clients)
        except VideoProcessorError:
            raise
        except yt_dlp.utils.YoutubeDLError as exc:
            last_exc = exc
            if not _looks_blocked(exc):
                break
            _LOG.warning(
                "yt-dlp was refused with player_client=%s (%s); "
                "trying the next client",
                ",".join(clients) if clients else "default",
                _clean_msg(exc)[:160],
            )
    if last_exc is None:  # pragma: no cover - the attempt list is never empty
        raise VideoProcessorError("Could not download the video.")
    raise VideoProcessorError(_download_error_message(last_exc)) from last_exc


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
