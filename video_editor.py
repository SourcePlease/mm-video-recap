"""
video_editor.py — Step 5: Video Editing, Audio Ducking & Render Module.

render_recap_video(...) trims highlight clips from the source video,
concatenates them, optionally re-times the video with setpts so it matches
the Myanmar voiceover length, converts to 16:9 or 9:16, ducks the original
audio under the voiceover, and burns in (or soft-muxes) Myanmar subtitles.

No API keys are used or stored anywhere in this module.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional

from video_processor import get_video_metadata


class VideoEditorError(Exception):
    """Raised for video editing / rendering failures."""


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #
def _run(cmd: List[str], step: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise VideoEditorError(f"{step} failed: {proc.stderr.strip()[-500:]}")


def find_myanmar_font() -> Optional[Dict[str, str]]:
    """Return {'path','dir','family'} for a Myanmar font, or None."""
    candidates = [
        ("/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf",
         "Noto Sans Myanmar"),
        ("/usr/share/fonts/truetype/noto/NotoSerifMyanmar-Regular.ttf",
         "Noto Serif Myanmar"),
    ]
    for path, family in candidates:
        if os.path.isfile(path):
            return {"path": path, "dir": os.path.dirname(path), "family": family}
    # generic fallback: any Myanmar font known to fontconfig
    try:
        proc = subprocess.run(
            ["fc-list", ":lang=my", "file", "family"],
            capture_output=True, text=True, timeout=30,
        )
        for line in proc.stdout.splitlines():
            if ".ttf" in line or ".otf" in line:
                fpath = line.split(":")[0].strip()
                family = line.split(":")[1].strip().split(",")[0]
                if os.path.isfile(fpath):
                    return {"path": fpath, "dir": os.path.dirname(fpath),
                            "family": family}
    except (OSError, IndexError):
        pass
    return None


def _escape_filter_path(path: str) -> str:
    """Escape a file path for the subtitles filter filename option."""
    p = os.path.abspath(path)
    return (p.replace("\\", "\\\\").replace(":", "\\:")
             .replace("'", "\\'").replace("[", "\\[").replace("]", "\\]"))


def _atempo_chain(factor: float) -> str:
    """Build an atempo filter chain valid for any positive factor."""
    if factor <= 0:
        raise VideoEditorError(f"Invalid speed factor: {factor}")
    parts: List[str] = []
    f = factor
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


def _trim_segments(source: str, highlights: List[Dict], seg_dir: str) -> List[str]:
    os.makedirs(seg_dir, exist_ok=True)
    seg_paths: List[str] = []
    for i, h in enumerate(highlights):
        start = float(h["start"])
        dur = float(h["end"]) - start
        if dur <= 0:
            raise VideoEditorError(f"Highlight {i} has non-positive duration.")
        seg = os.path.join(seg_dir, f"seg_{i:03d}.mp4")
        _run(
            ["ffmpeg", "-y", "-v", "error",
             "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", source,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             seg],
            f"trim segment {i}",
        )
        seg_paths.append(seg)
    return seg_paths


def _concat_segments(seg_paths: List[str], seg_dir: str) -> str:
    list_file = os.path.join(seg_dir, "concat_list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in seg_paths:
            f.write(f"file '{p}'\n")
    out = os.path.join(seg_dir, "concat.mp4")
    _run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", list_file, "-c", "copy", out],
        "concat segments",
    )
    return out


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def render_recap_video(
    source_video_path: str,
    highlights: List[Dict],
    voiceover_path: str,
    srt_path: Optional[str] = None,
    output_path: Optional[str] = None,
    aspect_ratio: str = "16:9",
    video_speed_factor: float = 1.0,
    bg_volume: float = 0.15,
    work_dir: Optional[str] = None,
) -> Dict:
    """Render the final recap video. Returns a result dict.

    Result keys: video_path, subtitle_mode ("burned_in"|"soft"|"none"),
    aspect_ratio, video_speed_factor, duration, width, height, has_audio.
    """
    if aspect_ratio not in ("16:9", "9:16"):
        raise ValueError("aspect_ratio must be '16:9' or '9:16'.")
    if not highlights:
        raise ValueError("highlights သည် အလွတ်ဖြစ်နေပါသည်။")
    for p, label in ((source_video_path, "source video"),
                     (voiceover_path, "voiceover")):
        if not os.path.isfile(str(p)):
            raise FileNotFoundError(f"{label} not found: {p}")
    if srt_path and not os.path.isfile(srt_path):
        raise FileNotFoundError(f"SRT not found: {srt_path}")

    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix="video_recap_edit_")
    seg_dir = os.path.join(work_dir, "segments")
    if output_path is None:
        output_path = os.path.join(work_dir, f"final_recap_{aspect_ratio.replace(':', 'x')}.mp4")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    src_meta = get_video_metadata(source_video_path)
    src_dur = src_meta["duration"] or 0
    has_src_audio = src_meta["has_audio"]
    factor = float(video_speed_factor)
    if factor <= 0:
        raise ValueError("video_speed_factor သည် 0 ထက် ကြီးရမည်။")

    # defensively clamp highlight ends to the real source duration
    safe_highlights = []
    for h in highlights:
        s = max(0.0, float(h["start"]))
        e = min(float(h["end"]), src_dur) if src_dur else float(h["end"])
        if e > s:
            safe_highlights.append({"start": s, "end": e,
                                    "description": h.get("description", "")})
    if not safe_highlights:
        raise VideoEditorError("No valid highlight segments after clamping.")

    try:
        # 1) trim + concat
        seg_paths = _trim_segments(source_video_path, safe_highlights, seg_dir)
        concat_path = _concat_segments(seg_paths, seg_dir)

        # 2) build the final filter graph
        vfilters: List[str] = []
        if abs(factor - 1.0) > 1e-6:
            vfilters.append(f"setpts=PTS/{factor:.4f}")
        if aspect_ratio == "16:9":
            vfilters.append(
                "scale=1280:720:force_original_aspect_ratio=decrease,"
                "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
            )
        else:  # 9:16 vertical center crop for TikTok/Reels/Shorts
            vfilters.append(
                "scale=720:1280:force_original_aspect_ratio=increase,"
                "crop=720:1280,setsar=1"
            )

        subtitle_mode = "none"
        font = find_myanmar_font() if srt_path else None
        if srt_path and font:
            style = (
                f"FontName={font['family']},FontSize=22,"
                "PrimaryColour=&HFFFFFF&,OutlineColour=&H80000000&,"
                "BorderStyle=1,Outline=2,Shadow=0,MarginV=28"
            )
            vfilters.append(
                f"subtitles='{_escape_filter_path(srt_path)}':"
                f"fontsdir='{_escape_filter_path(font['dir'])}':"
                f"force_style='{style}'"
            )
            subtitle_mode = "burned_in"

        afilters: List[str] = []
        if has_src_audio:
            chain = _atempo_chain(factor) if abs(factor - 1.0) > 1e-6 else "anull"
            afilters.append(f"[0:a]{chain},volume={bg_volume}[bg]")
            # normalize=0 keeps the exact bg_volume/1.0 balance (amix would
            # otherwise halve both inputs); alimiter guards against clipping.
            afilters.append(
                "[bg][1:a]amix=inputs=2:duration=longest:"
                "dropout_transition=0:normalize=0,"
                "alimiter=limit=0.95,aresample=44100[aout]"
            )
        else:
            afilters.append("[1:a]aresample=44100[aout]")

        filtergraph = ";".join(
            [f"[0:v]{','.join(vfilters)}[vout]"] + afilters
        )

        cmd = ["ffmpeg", "-y", "-v", "error",
               "-i", concat_path, "-i", voiceover_path]
        soft_srt_out = None
        if srt_path and subtitle_mode != "burned_in":
            # soft subtitles: mux as mov_text track + keep a sidecar .srt
            cmd += ["-i", srt_path]
            subtitle_mode = "soft"
            soft_srt_out = os.path.splitext(output_path)[0] + ".srt"
        cmd += ["-filter_complex", filtergraph,
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-r", "30",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", "-shortest"]
        if subtitle_mode == "soft":
            cmd += ["-map", "2:s", "-c:s", "mov_text"]
        cmd.append(output_path)

        _run(cmd, "final render")
        if soft_srt_out:
            shutil.copy2(srt_path, soft_srt_out)

        meta = get_video_metadata(output_path)
        return {
            "video_path": output_path,
            "subtitle_mode": subtitle_mode,
            "aspect_ratio": aspect_ratio,
            "video_speed_factor": factor,
            "duration": meta["duration"],
            "width": meta["width"],
            "height": meta["height"],
            "has_audio": meta["has_audio"],
            "file_size_mb": meta["file_size_mb"],
            "sidecar_srt": soft_srt_out,
        }
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)
