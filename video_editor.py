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
    """Return {'path','dir','family'} for a Myanmar font, or None.

    Preference: the Unicode font bundled with the app (Padauk, from the
    user's Myanmar-Unicode-Fonts collection — Zawgyi fonts are never used),
    then the system Noto fonts, then any fontconfig Myanmar font.
    """
    bundled_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fonts")
    bundled = os.path.join(bundled_dir, "Padauk.ttf")
    if os.path.isfile(bundled):
        return {"path": bundled, "dir": bundled_dir, "family": "Padauk"}
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


# Bundled Unicode subtitle fonts (from the user's Myanmar-Unicode-Fonts
# collection; Zawgyi fonts are never included). Key = the name shown in the
# Subtitles tab; value = (ASS family name, regular file, bold file or None).
# NOTE: Myanmar Thuriya's internal family name is "Myanmar Head One".
SUBTITLE_FONTS = {
    "Padauk": ("Padauk", "Padauk.ttf", "Padauk-bold.ttf"),
    "Noto Sans Myanmar": ("Noto Sans Myanmar", "NotoSansMyanmar.ttf",
                          "NotoSansMyanmar-Bold.ttf"),
    "Pyidaungsu": ("Pyidaungsu", "Pyidaungsu.ttf", None),
    "Myanmar Text": ("Myanmar Text", "MyanmarText.ttf",
                     "MyanmarText-Bold.ttf"),
    "Myanmar3": ("Myanmar3", "Myanmar3.ttf", None),
    "Masterpiece Uni Round": ("Masterpiece Uni Round",
                              "MasterpieceUniRound.ttf", None),
    "Myanmar Sagar": ("Myanmar Sagar", "MyanmarSagar.ttf", None),
    "Myanmar Sans Pro": ("Myanmar Sans Pro", "MyanmarSansPro.ttf", None),
    "Myanmar Square": ("Myanmar Square", "MyanmarSquare.ttf", None),
    "Myanmar Thuriya": ("Myanmar Head One", "MyanmarThuriya.ttf", None),
    "TharLon": ("TharLon", "TharLon.ttf", None),
    "ThanLwin": ("ThanLwin", "ThanLwin.ttf", None),
    "Cherry Unicode": ("Cherry Unicode", "CherryUnicode.ttf", None),
    "Gantgaw": ("Gantgaw", "Gantgaw.ttf", None),
    "Jasmine Unicode": ("Jasmine Unicode", "JasmineUnicode.ttf", None),
    "YoeYar-One": ("YoeYar-One", "YoeYarOne.ttf", None),
    "Myanmar Angoun": ("Myanmar Angoun", "MyanmarAngoun.ttf", None),
    "Myanmar Sabae": ("Myanmar Sabae", "MyanmarSabae.ttf", None),
    "Myanmar Tagu": ("Myanmar Tagu", "MyanmarTagu.ttf", None),
    "Myanmar Taungyi": ("Myanmar Taungyi", "MyanmarTaungyi.ttf", None),
}

# Subtitle text colours (ASS PrimaryColour, &HAABBGGRR — note the byte
# order is Alpha-Blue-Green-Red, so Red is &H000000FF, NOT &H00FF0000).
# Outline stays black.
SUBTITLE_COLORS = {
    "White": "&H00FFFFFF",
    "Yellow": "&H0000FFFF",
    "Green": "&H0000FF00",
    "Red": "&H000000FF",
}

# Same colours as RGB tuples for the Pillow PNG subtitle renderer.
SUBTITLE_COLOR_RGB = {
    "White": (255, 255, 255),
    "Yellow": (255, 255, 0),
    "Green": (0, 255, 0),
    "Red": (255, 0, 0),
}


def _raqm_available() -> bool:
    """True when Pillow was built with libraqm (Myanmar complex shaping)."""
    try:
        from PIL import features
        return bool(features.check("raqm"))
    except Exception:
        return False


def _subtitle_font_file(display_name: str) -> str:
    """Path to the font file used for a Subtitles-tab choice.

    Prefers the Bold face when the font ships one (matches the ASS
    Bold=-1 behaviour); otherwise the Regular face. The thick black
    stroke adds weight for Regular-only fonts.
    """
    entry = SUBTITLE_FONTS.get(display_name) or SUBTITLE_FONTS["Pyidaungsu"]
    _family, regular, bold_file = entry
    fonts_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fonts")
    if bold_file and os.path.isfile(os.path.join(fonts_dir, bold_file)):
        return os.path.join(fonts_dir, bold_file)
    return os.path.join(fonts_dir, regular)


def _wrap_subtitle_lines(text: str, font, max_width: int) -> List[str]:
    """Greedy word-wrap for one cue (Myanmar phrases split on spaces)."""
    if font.getlength(text) <= max_width:
        return [text]
    words = text.split(" ")
    lines: List[str] = []
    cur = ""
    for w in words:
        cand = (cur + " " + w).strip()
        if cur and font.getlength(cand) > max_width:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines or [text]


def _render_subtitle_pngs(srt_path: str, font_file: str, font_size: int,
                          out_w: int, out_h: int, pos_x: int, pos_y: int,
                          rgb: tuple, work_dir: str) -> List[Dict]:
    """Render every SRT cue as a full-size transparent PNG (Pillow RAQM).

    RAQM (HarfBuzz) shapes Myanmar correctly: the e-vowel (ေ) reorders to
    the front and stacked consonants (ပါဌ်ဆင့်/ကင်းစီး) combine with no
    "+" sign — which the server's libass cannot do. Returns a list of
    {"path", "start", "end"} for the FFmpeg overlay chain. The full cue
    text is drawn as one continuous run (no tags inside a cluster).
    """
    from PIL import Image, ImageDraw, ImageFont

    if not _raqm_available():
        raise VideoEditorError(
            "Pillow RAQM (libraqm) မရှိပါ — မြန်မာစာ ပုံဖော်မှု မရပါ။")
    font = ImageFont.truetype(font_file, font_size,
                              layout_engine=ImageFont.Layout.RAQM)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + max(4, font_size // 10)
    stroke_w = max(2, int(round(font_size * 0.1)))
    fill = (rgb[0], rgb[1], rgb[2], 255)
    stroke = (0, 0, 0, 255)
    max_width = int(out_w * 0.92)
    out: List[Dict] = []
    for i, (start, end, text) in enumerate(_parse_srt_cues(srt_path)):
        lines = _wrap_subtitle_lines(text, font, max_width)
        img = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # Stack lines upward from pos_y (last line's baseline sits at pos_y).
        for li, line in enumerate(lines):
            y = pos_y - (len(lines) - 1 - li) * line_h
            draw.text((pos_x, y), line, font=font, fill=fill,
                      stroke_width=stroke_w, stroke_fill=stroke,
                      anchor="ms")
        png_path = os.path.join(work_dir, f"sub_{i}.png")
        img.save(png_path)
        out.append({"path": png_path, "start": float(start), "end": float(end)})
    return out


def _build_subtitle_concat(pngs: List[Dict], out_w: int, out_h: int,
                           work_dir: str, fps: float = 30.0) -> str:
    """Build an FFmpeg concat list for the subtitle PNGs + blank gaps.

    One concat video input + ONE overlay filter replaces N overlay
    filters: 30 per-cue overlays made a 28s render take 6.5 minutes
    locally (and likely timed out / OOM'd on Streamlit Cloud). The
    concat demuxer emits each PNG for its cue window and a transparent
    blank PNG for the gaps, so a single overlay shows the right cue at
    the right time. Returns the concat list file path.

    Segment boundaries are quantized to frame boundaries (1/fps) so
    rounding cannot drift over many cues: each boundary is rounded
    once, and segment durations are the differences of rounded
    boundaries (never independently rounded durations).
    """
    from PIL import Image

    blank_path = os.path.join(work_dir, "sub_blank.png")
    Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0)).save(blank_path)
    # Contiguous segments: blank gaps + cue PNGs, in time order.
    # (Overlapping cues: the later cue is delayed to the previous end.)
    bounds: List[tuple] = []
    cursor = 0.0
    for png in sorted(pngs, key=lambda p: (float(p["start"]), float(p["end"]))):
        s, e = float(png["start"]), float(png["end"])
        if e <= s:
            continue
        s = max(s, cursor)
        if s > cursor + 1e-9:
            bounds.append((cursor, s, None))
        bounds.append((s, e, png["path"]))
        cursor = e
    if not bounds:
        bounds.append((0.0, 1.0, None))
    # Quantize boundaries to frames and drop zero-length segments.
    qbounds: List[tuple] = []
    prev_f = 0
    for s, e, path in bounds:
        sf = int(round(s * fps))
        ef = int(round(e * fps))
        sf = max(sf, prev_f)  # keep segments contiguous, no drift
        if ef <= sf:
            continue
        qbounds.append((sf, ef, path))
        prev_f = ef
    if not qbounds:
        qbounds.append((0, int(round(fps)), None))
    list_path = os.path.join(work_dir, "sub_list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for sf, ef, path in qbounds:
            dur = (ef - sf) / fps
            f.write(f"file '{path or blank_path}'\nduration {dur:.4f}\n")
    return list_path


def get_subtitle_font(display_name: str):
    """Resolve a Subtitles-tab font choice to (family, bold_ok, fonts_dir).

    Falls back to Padauk for an unknown name. Bold is only reported
    available when the chosen font actually ships a Bold file.
    """
    entry = SUBTITLE_FONTS.get(display_name) or SUBTITLE_FONTS["Padauk"]
    family, _regular, bold_file = entry
    fonts_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fonts")
    bold_ok = bool(bold_file) and os.path.isfile(
        os.path.join(fonts_dir, bold_file))
    return family, bold_ok, fonts_dir


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
    # Newlines become a space (NOT the ASS \N break): a \N in the middle
    # of a consonant+diacritic cluster would split the cluster and render
    # a dotted circle. The full cue text is sent as one continuous line.
    return (text.replace("\\", "\\\\")
                 .replace("{", "\\{")
                 .replace("}", "\\}")
                 .replace("\n", " "))


def srt_to_ass(srt_path: str, ass_path: str, font_family: str,
               font_size: int, pos_x: int, pos_y: int,
               play_w: int, play_h: int,
               font_bold: bool = True,
               primary_colour: str = "&H00FFFFFF") -> int:
    """Convert SRT to ASS with every cue pinned to \\pos(pos_x, pos_y).

    Coloured text (primary_colour, ASS &HAABBGGRR) + black outline, exact
    PlayResX/PlayResY so the position matches the Canvas 1:1. Bold is only
    requested when the chosen font actually ships a Bold face — otherwise
    libass could substitute another family's Bold and mix fonts.
    Returns the number of cues written.
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
        # Step 13 style: Yellow text + thick black outline + soft shadow.
        # Bold uses the ASS boolean -1 (true) / 0 (false). FontSize stays
        # dynamic (font_size_pct of the output width) so the burned-in size
        # matches the canvas preview.
        f.write(
            f"Style: Default,{font_family},{font_size},{primary_colour},"
            f"&H000000FF,&H00000000,&H00000000,{(-1 if font_bold else 0)},"
            f"0,0,0,100,100,0,0,1,3.5,1,"
            f"2,10,10,20,1\n"
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
        cfg = subtitle_config or {}
        cfg_enabled = cfg.get("enabled", True)
        # Subtitles-tab choices: font family + text colour. Fall back to the
        # system Noto font only when the bundled fonts dir is missing.
        _fam, _bold_ok, _fonts_dir = get_subtitle_font(
            str(cfg.get("font_family", "Pyidaungsu")))
        _colour = SUBTITLE_COLORS.get(str(cfg.get("color", "Yellow")),
                                      SUBTITLE_COLORS["Yellow"])
        font = None
        if srt_path and os.path.isdir(_fonts_dir):
            font = {"family": _fam, "dir": _fonts_dir, "bold": _bold_ok,
                    "file": _subtitle_font_file(
                        str(cfg.get("font_family", "Pyidaungsu")))}
        elif srt_path:
            _sys = find_myanmar_font()
            if _sys:
                font = {"family": _sys["family"], "dir": _sys["dir"],
                        "bold": True, "file": _sys["path"]}
        # Subtitle PNG overlays (Pillow RAQM — correct Myanmar shaping) or
        # ASS fallback when RAQM is unavailable, or none when disabled.
        subtitle_concat = None
        if srt_path and font and cfg_enabled:
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
            _rgb = SUBTITLE_COLOR_RGB.get(
                str(cfg.get("color", "Yellow")), (255, 255, 0))
            _sub_pos = {"x_pct": fx, "y_pct": fy, "font_size": fs,
                        "font_size_pct": fpct, "pos_x": pos_x, "pos_y": pos_y,
                        "font_family": font["family"],
                        "color": str(cfg.get("color", "Yellow"))}
            if _raqm_available():
                subtitle_pngs = _render_subtitle_pngs(
                    srt_path, font["file"], fs, out_w, out_h,
                    pos_x, pos_y, _rgb, work_dir)
                if not subtitle_pngs:
                    raise VideoEditorError("SRT တွင် စာတန်းမတွေ့ပါ။")
                # One concat video input + ONE overlay (see
                # _build_subtitle_concat for why N overlays are too slow).
                subtitle_concat = _build_subtitle_concat(
                    subtitle_pngs, out_w, out_h, work_dir)
                vchain.append(
                    f"[{cur_label}][2:v]overlay=0:0:format=auto[vout]"
                )
                subtitle_mode = "burned_in"
                subtitle_position = dict(_sub_pos, method="raqm_png",
                                         cues=len(subtitle_pngs))
            else:
                # Fallback: ASS burn-in (server libass shapes Myanmar
                # poorly, but it beats no subtitles when RAQM is missing).
                ass_path = os.path.join(work_dir, "subtitles_exact.ass")
                n_cues = srt_to_ass(srt_path, ass_path, font["family"], fs,
                                    pos_x, pos_y, out_w, out_h,
                                    font_bold=bool(font.get("bold", True)),
                                    primary_colour=_colour)
                if n_cues == 0:
                    raise VideoEditorError("SRT တွင် စာတန်းမတွေ့ပါ။")
                vchain.append(
                    f"[{cur_label}]subtitles="
                    f"'{_escape_filter_path(ass_path)}':"
                    f"fontsdir='{_escape_filter_path(font['dir'])}'[vout]"
                )
                subtitle_mode = "burned_in"
                subtitle_position = dict(_sub_pos, method="ass", cues=n_cues)
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
        if subtitle_concat:
            cmd += ["-f", "concat", "-safe", "0", "-i", subtitle_concat]
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
