"""
video_editor.py — Step 5: Video Editing, Audio Ducking & Render Module.

render_recap_video(...) trims highlight clips from the source video,
concatenates them, optionally re-times the video with setpts so it matches
the Myanmar voiceover length, converts to 16:9 or 9:16, applies multiple
user-positioned blur boxes, ducks the original audio under the voiceover,
and burns in (or soft-muxes) Myanmar subtitles — either with the legacy
centered style or with exact (x_pct, y_pct) position + font size via
generated ASS (\\pos) for pixel-faithful Canvas WYSIWYG.

No API keys are used or stored anywhere in this module.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple

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
# Step 10: multi-blur boxes + exact subtitle engine
# --------------------------------------------------------------------------- #
def _normalize_blur_boxes(
    blur_boxes: Optional[List[Dict]], out_w: int, out_h: int
) -> List[Dict]:
    """Validate blur boxes; convert pct coords to even pixel rects.

    Each box: {"x_pct","y_pct","w_pct","h_pct","strength" 1..50}.
    Returns pixel rects clamped to the frame with boxblur radius/power.
    Degenerate boxes are dropped.
    """
    boxes: List[Dict] = []
    for b in blur_boxes or []:
        try:
            x = float(b.get("x_pct", 0))
            y = float(b.get("y_pct", 0))
            w = float(b.get("w_pct", 0))
            h = float(b.get("h_pct", 0))
            s = int(float(b.get("strength", 22)))
        except (TypeError, ValueError, AttributeError):
            continue
        s = max(1, min(50, s))
        bx = max(0, int(round(x * out_w)))
        by = max(0, int(round(y * out_h)))
        bw = int(round(w * out_w))
        bh = int(round(h * out_h))
        bw = min(bw, out_w - bx)
        bh = min(bh, out_h - by)
        bw -= bw % 2  # even dims for yuv420p crop
        bh -= bh % 2
        if bw < 4 or bh < 4:
            continue
        # Map UI strength 1..50 onto boxblur's hard limits
        # (luma_radius < 36, chroma_radius < 21 for yuv420p).
        radius = 2 + int(round((s - 1) * 33 / 49))  # 1->2, 22->16, 50->35
        power = min(3, 1 + s // 17)  # 1->1, 22->2, 50->3
        chroma_radius = max(1, min(20, radius // 2))
        boxes.append({"x": bx, "y": by, "w": bw, "h": bh,
                      "radius": radius, "power": power,
                      "chroma_radius": chroma_radius, "strength": s})
    return boxes


def _blur_filter_steps(base_label: str, boxes: List[Dict]
                       ) -> Tuple[List[str], str]:
    """Build split->crop->boxblur->overlay steps for every box.

    Returns (filter_strings, final_label). Each box is cropped from a split
    copy, blurred, and overlaid back at its exact position.
    """
    steps: List[str] = []
    cur = base_label
    for i, b in enumerate(boxes):
        steps.append(f"[{cur}]split=2[keep{i}][cr{i}]")
        steps.append(
            f"[cr{i}]crop={b['w']}:{b['h']}:{b['x']}:{b['y']},"
            f"boxblur=luma_radius={b['radius']}:luma_power={b['power']}:"
            f"chroma_radius={b['chroma_radius']}:chroma_power=1"
            f"[bl{i}]"
        )
        steps.append(f"[keep{i}][bl{i}]overlay={b['x']}:{b['y']}[bx{i}]")
        cur = f"bx{i}"
    return steps, cur


_SRT_TS_RE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)"
)


def _parse_srt_cues(srt_path: str) -> List[Tuple[float, float, str]]:
    """Parse an SRT file into (start_s, end_s, text) cues."""
    with open(srt_path, encoding="utf-8-sig") as f:
        content = f.read()
    cues: List[Tuple[float, float, str]] = []
    for block in re.split(r"\r?\n[ \t]*\r?\n", content.strip()):
        lines = [ln for ln in block.strip().splitlines() if ln.strip() != ""]
        if not lines:
            continue
        ts_idx = next(
            (i for i, ln in enumerate(lines) if _SRT_TS_RE.search(ln)), None
        )
        if ts_idx is None:
            continue
        m = _SRT_TS_RE.search(lines[ts_idx])
        assert m is not None
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        text = "\n".join(lines[ts_idx + 1:]).strip()
        if end > start and text:
            cues.append((start, end, text))
    return cues


def _ass_timestamp(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs == 100:  # rounding carry
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    return (text.replace("\\", "\\\\")
                 .replace("{", "\\{")
                 .replace("}", "\\}")
                 .replace("\n", "\\N"))


def srt_to_ass(srt_path: str, ass_path: str, font_family: str,
               font_size: int, pos_x: int, pos_y: int,
               play_w: int, play_h: int) -> int:
    """Convert SRT to ASS with every cue pinned to \\pos(pos_x, pos_y).

    White text + black outline, exact PlayResX/PlayResY so the position
    matches the Canvas 1:1. Returns the number of cues written.
    """
    cues = _parse_srt_cues(srt_path)
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("[Script Info]\n")
        f.write("ScriptType: v4.00+\n")
        f.write(f"PlayResX: {play_w}\n")
        f.write(f"PlayResY: {play_h}\n")
        f.write("WrapStyle: 0\n")
        f.write("ScaledBorderAndShadow: yes\n")
        f.write("\n[V4+ Styles]\n")
        f.write(
            "Format: Name, Fontname, Fontsize, PrimaryColour, "
            "SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
            "MarginV, Encoding\n"
        )
        f.write(
            f"Style: Default,{font_family},{font_size},&H00FFFFFF,"
            f"&H000019FF,&HFF000000,&H80000000,1,0,0,0,100,100,0,0,1,2,0,"
            f"2,10,10,10,1\n"
        )
        f.write("\n[Events]\n")
        f.write(
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\n"
        )
        for start, end, text in cues:
            f.write(
                f"Dialogue: 0,{_ass_timestamp(start)},{_ass_timestamp(end)},"
                f"Default,,0,0,0,,{{\\pos({pos_x},{pos_y})}}"
                f"{_escape_ass_text(text)}\n"
            )
    return len(cues)


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
    blur_boxes: Optional[List[Dict]] = None,
    subtitle_config: Optional[Dict] = None,
) -> Dict:
    """Render the final recap video. Returns a result dict.

    blur_boxes: [{"x_pct","y_pct","w_pct","h_pct","strength" 1..50}, ...] —
        each box is blurred onto the final frame at its exact position.
    subtitle_config: {"enabled": True, "x_pct": 0.5, "y_pct": 0.82,
        "font_size_pct": 0.055} — font size as a fraction of the output
        width, so the burned-in size matches the canvas preview exactly
        (legacy "font_size" px at the 400px reference still works) — when
        are burned via generated ASS with \\pos(x,y) so the Canvas preview
        matches the render 1:1. Omit (None) for the legacy centered style.

    Result keys: video_path, subtitle_mode ("burned_in"|"soft"|"none"),
    aspect_ratio, video_speed_factor, duration, width, height, has_audio,
    blur_boxes_applied, subtitle_position.
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
        out_w, out_h = (1280, 720) if aspect_ratio == "16:9" else (720, 1280)
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

        vchain: List[str] = [f"[0:v]{','.join(vfilters)}[vbase]"]

        # 2a) multi blur boxes at exact output-resolution positions
        boxes = _normalize_blur_boxes(blur_boxes, out_w, out_h)
        cur_label = "vbase"
        if boxes:
            steps, cur_label = _blur_filter_steps(cur_label, boxes)
            vchain.extend(steps)

        # 2b) subtitles: exact ASS position/size, legacy style, or none
        subtitle_mode = "none"
        subtitle_position = None
        font = find_myanmar_font() if srt_path else None
        cfg = subtitle_config or {}
        cfg_enabled = cfg.get("enabled", True)
        use_exact = (
            cfg.get("x_pct") is not None
            or cfg.get("y_pct") is not None
            or cfg.get("font_size") is not None
            or cfg.get("font_size_pct") is not None
        )
        if srt_path and font and cfg_enabled:
            if use_exact:
                try:
                    fx = float(cfg.get("x_pct", 0.5))
                    fy = float(cfg.get("y_pct", 0.82))
                    # Prefer font_size_pct (fraction of output width) so the
                    # burned-in size matches the canvas preview exactly at
                    # any output resolution. Legacy font_size is px at the
                    # 400px reference width.
                    if cfg.get("font_size_pct") is not None:
                        fpct = float(cfg["font_size_pct"])
                        fs = int(round(fpct * out_w))
                    else:
                        fpct = float(cfg.get("font_size", 22)) / 400.0
                        fs = int(round(fpct * out_w))
                except (TypeError, ValueError):
                    fx, fy, fpct, fs = 0.5, 0.82, 0.055, int(round(0.055 * out_w))
                fx = min(1.0, max(0.0, fx))
                fy = min(1.0, max(0.0, fy))
                fpct = min(0.30, max(0.02, fpct))
                fs = min(200, max(8, fs))
                pos_x, pos_y = int(round(fx * out_w)), int(round(fy * out_h))
                ass_path = os.path.join(work_dir, "subtitles_exact.ass")
                n_cues = srt_to_ass(srt_path, ass_path, font["family"], fs,
                                    pos_x, pos_y, out_w, out_h)
                if n_cues == 0:
                    raise VideoEditorError("SRT တွင် စာတန်းမတွေ့ပါ။")
                vchain.append(
                    f"[{cur_label}]subtitles="
                    f"'{_escape_filter_path(ass_path)}':"
                    f"fontsdir='{_escape_filter_path(font['dir'])}'[vout]"
                )
                subtitle_mode = "burned_in"
                subtitle_position = {"x_pct": fx, "y_pct": fy,
                                     "font_size": fs,
                                     "font_size_pct": fpct,
                                     "pos_x": pos_x, "pos_y": pos_y,
                                     "cues": n_cues}
            else:
                style = (
                    f"FontName={font['family']},FontSize=22,Bold=1,"
                    "PrimaryColour=&HFFFFFF&,OutlineColour=&H80000000&,"
                    "BorderStyle=1,Outline=2,Shadow=0,MarginV=28"
                )
                vchain.append(
                    f"[{cur_label}]subtitles='{_escape_filter_path(srt_path)}':"
                    f"fontsdir='{_escape_filter_path(font['dir'])}':"
                    f"force_style='{style}'[vout]"
                )
                subtitle_mode = "burned_in"
        else:
            vchain.append(f"[{cur_label}]null[vout]")

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

        filtergraph = ";".join(vchain + afilters)

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
            "blur_boxes_applied": len(boxes),
            "subtitle_position": subtitle_position,
        }
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)
