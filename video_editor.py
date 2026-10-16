"""
video_editor.py — Step 5: Video Editing, Audio Ducking & Render Module.

render_recap_video(...) trims highlight clips from the source video,
concatenates them, converts to 16:9 or 9:16, applies multiple
user-positioned blur boxes, ducks the original audio under the voiceover,
and burns in Myanmar subtitles with exact (x_pct, y_pct) position + font
size via Pillow RAQM PNG overlays (pixel-faithful Canvas WYSIWYG, correct
Myanmar shaping) — or soft-muxes the SRT when burn-in is off. The video
speed is ALWAYS 1.0 (no setpts): AI Recap (full narration) and Pro
(dialogue dubbing) both keep the original video timing untouched and
fit the AUDIO to the video, never the video to the audio.

No API keys are used or stored anywhere in this module.
"""
from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

from video_processor import get_video_metadata


class VideoEditorError(Exception):
    """Raised for video editing / rendering failures."""


# Pro/AI audio fit: when narration is longer than its video window even
# after the TTS rate ladder (+0% -> +10% -> +20%), the narration audio
# is fitted to the window with atempo up to PRO_TTS_FIT_MAX, then the
# tail is trimmed (last resort — the prompt word-cap should prevent
# this). The VIDEO is never slowed: speed is always 1.0.
PRO_TTS_FIT_MAX = 1.25


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #
def _run(cmd: List[str], step: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise VideoEditorError(f"{step} failed: {proc.stderr.strip()[-500:]}")


def _run_ffmpeg_progress(cmd: List[str], step: str, total_s: float,
                          on_progress: Optional[Callable[[float], None]]
                          ) -> None:
    """Run an ffmpeg command, reporting 0..1 progress via -progress.

    ``total_s`` is the expected output duration (seconds); progress is
    out_time_ms / total. ``on_progress`` is called only when the
    fraction advances by >= 0.01 (or at completion), so callers are
    not flooded. stderr goes to a temp file (never a pipe) so a big
    error dump cannot deadlock the stdout reader. Raises
    VideoEditorError with the tail of stderr on failure, like _run.
    """
    if on_progress is None:
        _run(cmd, step)
        return
    cmd = list(cmd)
    # -progress must come before the output file; insert it right
    # after the leading "ffmpeg" token.
    if cmd and cmd[0] == "ffmpeg":
        cmd[1:1] = ["-progress", "pipe:1", "-nostats"]
    else:
        cmd = ["ffmpeg", "-progress", "pipe:1", "-nostats"] + cmd
    total_us = max(1.0, float(total_s)) * 1_000_000.0
    last = -1.0
    err_fd, err_path = tempfile.mkstemp(prefix="mmr_ffmpeg_err_")
    os.close(err_fd)
    try:
        with open(err_path, "w") as err_f:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=err_f, text=True)
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_ms="):
                    try:
                        frac = int(line.split("=", 1)[1]) / total_us
                    except ValueError:
                        continue
                    frac = min(1.0, max(0.0, frac))
                    if frac - last >= 0.01:
                        last = frac
                        on_progress(frac)
            rc = proc.wait(timeout=3600)
        if rc != 0:
            with open(err_path, "r", errors="replace") as ef:
                tail = ef.read().strip()[-500:]
            raise VideoEditorError(f"{step} failed: {tail}")
        if last < 1.0:
            on_progress(1.0)
    finally:
        if err_path:
            try:
                os.remove(err_path)
            except OSError:
                pass


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
# Subtitles tab; value = (font family name, regular file, bold file or None).
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

# Subtitle text colours as RGB tuples for the Pillow PNG subtitle
# renderer. The radio options come from these keys. Outline stays black.
# (The old ASS &HAABBGGRR table is gone with the libass fallback —
# Fix 5.)
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

    Prefers the Bold face when the font ships one (matches the
    renderer's Bold behaviour); otherwise the Regular face. The thick
    black stroke adds weight for Regular-only fonts.
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


def _split_subtitle_chunks(text: str, max_chars: int = 30) -> List[str]:
    """Split a long cue into short chunks (<= max_chars), greedy on spaces.

    A 5-line wall of text covering the speaker's face is unreadable, so
    long cues are shown as short chunks in turn, each fitting at most
    2 lines (see _split_chunk_to_pieces). Overlong single words are
    hard-split so no chunk exceeds max_chars.
    """
    words = text.split(" ")
    chunks: List[str] = []
    cur = ""
    for w in words:
        while len(w) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(w[:max_chars])
            w = w[max_chars:]
        cand = (cur + " " + w).strip()
        if cur and len(cand) > max_chars:
            chunks.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        chunks.append(cur)
    return chunks or [text]


def _split_chunk_to_pieces(chunk: str, font, max_width: int,
                           max_lines: int = 2) -> List[List[str]]:
    """Wrap one chunk; if it still exceeds max_lines, split its words in
    half and wrap each half (recursively) until every piece fits in
    max_lines. Returns a list of line-groups (one group per piece)."""
    lines = _wrap_subtitle_lines(chunk, font, max_width)
    words = chunk.split(" ")
    if len(lines) <= max_lines or len(words) <= 1:
        return [lines]
    mid = len(words) // 2
    out: List[List[str]] = []
    for half in (" ".join(words[:mid]), " ".join(words[mid:])):
        if half:
            out.extend(_split_chunk_to_pieces(half, font, max_width, max_lines))
    return out or [lines]


def _render_subtitle_pngs(srt_path: str, font_file: str, font_size: int,
                          out_w: int, out_h: int, pos_x: int, pos_y: int,
                          rgb: tuple, work_dir: str,
                          wrap_w_pct: Optional[float] = None) -> List[Dict]:
    """Render every SRT cue as full-size transparent PNGs (Pillow RAQM).

    RAQM (HarfBuzz) shapes Myanmar correctly: the e-vowel (ေ) reorders to
    the front and stacked consonants (ပါဌ်ဆင့်/ကင်းစီး) combine with no
    "+" sign — which the server's libass cannot do. Returns a list of
    {"path", "start", "end"} for the FFmpeg overlay chain. The full cue
    text is drawn as one continuous run (no tags inside a cluster).

    Chunking (Fix 2): each cue is split into short chunks (<= 30 chars),
    each wrapping to at most 2 lines, and the cue's time window is
    sliced equally across its chunks — so short chunks appear in turn
    with the speech instead of one tall 5-line block covering the face.
    ``wrap_w_pct`` (the canvas subtitle box width as a fraction of the
    frame width) caps the wrap width; without it the wrap falls back to
    92% of the frame width.
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
    try:
        max_width = int(min(0.98, max(0.20, float(wrap_w_pct))) * out_w) \
            if wrap_w_pct is not None else int(out_w * 0.92)
    except (TypeError, ValueError):
        max_width = int(out_w * 0.92)
    out: List[Dict] = []
    png_idx = 0
    for start, end, text in _parse_srt_cues(srt_path):
        pieces: List[List[str]] = []
        for chunk in _split_subtitle_chunks(text):
            pieces.extend(_split_chunk_to_pieces(chunk, font, max_width))
        n = len(pieces)
        dur = float(end) - float(start)
        for j, lines in enumerate(pieces):
            ps = float(start) + dur * j / n
            pe = float(start) + dur * (j + 1) / n
            img = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            # Stack lines upward from pos_y (last line's baseline sits
            # at pos_y).
            for li, line in enumerate(lines):
                y = pos_y - (len(lines) - 1 - li) * line_h
                draw.text((pos_x, y), line, font=font, fill=fill,
                          stroke_width=stroke_w, stroke_fill=stroke,
                          anchor="ms")
            png_path = os.path.join(work_dir, f"sub_{png_idx}.png")
            png_idx += 1
            img.save(png_path)
            out.append({"path": png_path, "start": ps, "end": pe})
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


def _atempo_chain(factor: float) -> str:
    """Build an atempo filter chain valid for any positive factor.

    A single atempo only accepts 0.5–100.0, so chain 0.5x steps below
    0.5 and 100x steps above 100 (e.g. 0.2 -> "atempo=0.5,atempo=0.4";
    250 -> "atempo=100,atempo=2.5").
    """
    if factor <= 0:
        raise VideoEditorError(f"Invalid speed factor: {factor}")
    parts: List[str] = []
    f = factor
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    while f > 100.0:
        parts.append("atempo=100")
        f /= 100.0
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


def _highlights_tile(highlights: List[Dict], src_dur: float,
                     tol: float = 0.05) -> bool:
    """True when highlights tile [0, src_dur] with no real gaps.

    Full-duration AI Recap (and Pro) guarantee this tiling, so the
    render can use the source video directly instead of re-encoding
    every highlight into a segment and concatenating them — that was
    N+1 full encodes for an N-highlight video (the main reason an
    8-min render took ~14 min). Tiny overlaps/gaps (<= tol, from
    2-decimal rounding) still count as tiling. Returns False for an
    empty list or any real gap, so partial-highlight renders keep the
    exact old trim+concat path.
    """
    if not highlights or not src_dur or src_dur <= 0:
        return False
    try:
        segs = sorted((float(h["start"]), float(h["end"]))
                      for h in highlights)
    except (KeyError, TypeError, ValueError):
        return False
    if segs[0][0] > tol:
        return False
    if segs[-1][1] < src_dur - tol:
        return False
    for (_, prev_end), (nxt_start, _) in zip(segs, segs[1:]):
        if nxt_start - prev_end > tol:
            return False
    return True


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


# --------------------------------------------------------------------------- #
# Step 15: aspect ratios, fit modes, copyright filters, Pro blocks
# --------------------------------------------------------------------------- #
# Fixed output sizes for the Source tab's aspect ratios. "original" keeps
# the source WxH (forced even for yuv420p).
ASPECT_TARGETS: Dict[str, Tuple[int, int]] = {
    "9:16": (720, 1280),
    "1:1": (720, 720),
    "4:5": (720, 900),
    "16:9": (1280, 720),
}

# Processing modes (Source tab): "ai_recap" = narration recap (audio is
# master, video stretches to fit it); "pro_ai_recap" = dialogue video,
# split into sequential blocks, each block speed-synced independently.
PROCESSING_MODES = ("ai_recap", "pro_ai_recap")


def _resolve_aspect_target(aspect_ratio: str, src_meta: Dict) -> Tuple[int, int]:
    """Map an aspect name to the (W, H) render target."""
    if aspect_ratio == "original":
        w = int(src_meta.get("width") or 0)
        h = int(src_meta.get("height") or 0)
        if w <= 0 or h <= 0:
            raise VideoEditorError(
                "Original aspect လိုအပ်သော်လည်း source အရွယ်အစား မသိရပါ။")
        return (w - (w % 2), h - (h % 2))
    if aspect_ratio in ASPECT_TARGETS:
        return ASPECT_TARGETS[aspect_ratio]
    raise ValueError(
        "aspect_ratio must be one of 'original', '9:16', '1:1', '4:5', '16:9'.")


def _copyright_filter_chain(
    base_w: int, base_h: int,
    mirror_horizontal: bool = False,
    copyright_zoom: float = 1.0,
    random_crop_offset: bool = False,
) -> Tuple[str, Dict]:
    """Source-frame copyright filters — FIRST in the chain, before fit/blur.

    Order (per the Source tab rule): hflip -> zoom (scale up + crop back
    to the base size) -> random crop offset (a 2-10px shift from the
    centred crop window). When zoom is 1.0 but random crop is on, the frame
    is first scaled 1.02x so there is slack to shift inside. Returns
    (comma-joined filter string, applied-dict with the exact crop window).
    Output dimensions are always (base_w, base_h).
    """
    parts: List[str] = []
    applied = {
        "mirror": bool(mirror_horizontal),
        "zoom": 1.0,
        "random_crop": bool(random_crop_offset),
        "crop_x": None,
        "crop_y": None,
        "shift_x": 0,
        "shift_y": 0,
    }
    if mirror_horizontal:
        parts.append("hflip")
    try:
        z = float(copyright_zoom or 1.0)
    except (TypeError, ValueError):
        z = 1.0
    z = min(1.25, max(1.0, z))
    if random_crop_offset and z <= 1.0:
        z = 1.02
    if z > 1.0:
        zw = int(round(base_w * z))
        zh = int(round(base_h * z))
        zw = max(base_w + 2, zw - (zw % 2))
        zh = max(base_h + 2, zh - (zh % 2))
        parts.append(f"scale={zw}:{zh}")
        slack_x = zw - base_w
        slack_y = zh - base_h
        cx, cy = slack_x // 2, slack_y // 2
        if random_crop_offset:
            rx = random.choice((-1, 1)) * random.randint(2, 10)
            ry = random.choice((-1, 1)) * random.randint(2, 10)
            cx = min(max(cx + rx, 0), slack_x)
            cy = min(max(cy + ry, 0), slack_y)
            applied["shift_x"] = cx - slack_x // 2
            applied["shift_y"] = cy - slack_y // 2
        parts.append(f"crop={base_w}:{base_h}:{cx}:{cy}")
        applied.update({"zoom": z, "crop_x": cx, "crop_y": cy})
    return (",".join(parts), applied)


def _fit_filter_steps(
    input_label: str, base_w: int, base_h: int,
    out_w: int, out_h: int, fit_mode: Optional[str],
    crop_align: str, inner_scale: float,
    vertical_pos: float, horizontal_pos: float,
    aspect_ratio: str,
) -> Tuple[List[str], str]:
    """Aspect/fit filters. Returns (filter_strings, output_label).

    fit_mode "crop": scale to fill the target, then crop to it; crop_align
    picks the vertical window (top y=0, center, bottom y=ih-oh).
    fit_mode "pad": blurred background (source scaled to fill the target +
    boxblur=25:5) with the foreground overlaid at inner_scale /
    vertical_pos / horizontal_pos (overflow auto-crops via overlay).
    fit_mode None = legacy behaviour (16:9 pads black, rest crop-fills),
    so existing callers that pass no fit_mode render exactly as before.
    """
    if fit_mode is None:
        if aspect_ratio == "16:9":
            return ([f"[{input_label}]scale={out_w}:{out_h}:"
                     f"force_original_aspect_ratio=decrease,"
                     f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                     f"setsar=1[vbase]"], "vbase")
        fit_mode = "crop"
    if fit_mode == "crop":
        if crop_align == "top":
            y_expr = "0"
        elif crop_align == "bottom":
            y_expr = "ih-oh"
        else:
            y_expr = "(ih-oh)/2"
        return ([f"[{input_label}]scale={out_w}:{out_h}:"
                 f"force_original_aspect_ratio=increase,"
                 f"crop={out_w}:{out_h}:x=(iw-ow)/2:y={y_expr},"
                 f"setsar=1[vbase]"], "vbase")
    if fit_mode == "pad":
        try:
            scale_f = float(inner_scale or 1.0)
        except (TypeError, ValueError):
            scale_f = 1.0
        scale_f = min(2.0, max(0.5, scale_f))
        try:
            vpos = min(50.0, max(-50.0, float(vertical_pos or 0.0)))
            hpos = min(50.0, max(-50.0, float(horizontal_pos or 0.0)))
        except (TypeError, ValueError):
            vpos, hpos = 0.0, 0.0
        # Foreground size: fit inside the target, then * inner_scale.
        # (Python-computed from the known base dims — exact, no expr.)
        base = min(out_w / base_w, out_h / base_h) if base_w and base_h else 1.0
        fg_w = max(2, int(round(base_w * base * scale_f)))
        fg_h = max(2, int(round(base_h * base * scale_f)))
        fg_w -= fg_w % 2
        fg_h -= fg_h % 2
        ox = (out_w - fg_w) // 2 + int(round(hpos / 100.0 * out_w))
        oy = (out_h - fg_h) // 2 + int(round(vpos / 100.0 * out_h))
        steps = [
            f"[{input_label}]split=2[fitbg][fitfg]",
            f"[fitbg]scale={out_w}:{out_h}:"
            f"force_original_aspect_ratio=increase,crop={out_w}:{out_h},"
            f"boxblur=25:5,setsar=1[bg]",
            f"[fitfg]scale={fg_w}:{fg_h},setsar=1[fg]",
            f"[bg][fg]overlay={ox}:{oy},setsar=1[vbase]",
        ]
        return (steps, "vbase")
    raise ValueError("fit_mode must be 'crop', 'pad', or None (legacy).")


def _resolve_subtitle_font(cfg: Dict, srt_path: Optional[str]) -> Optional[Dict]:
    """Resolve the subtitle font for a config (None when no SRT)."""
    if not srt_path:
        return None
    _fam, _bold_ok, _fonts_dir = get_subtitle_font(
        str(cfg.get("font_family", "Pyidaungsu")))
    if os.path.isdir(_fonts_dir):
        return {"family": _fam, "dir": _fonts_dir, "bold": _bold_ok,
                "file": _subtitle_font_file(
                    str(cfg.get("font_family", "Pyidaungsu")))}
    _sys = find_myanmar_font()
    if _sys:
        return {"family": _sys["family"], "dir": _sys["dir"],
                "bold": True, "file": _sys["path"]}
    return None


def _subtitle_step(cur_label: str, srt_path: Optional[str],
                   subtitle_config: Optional[Dict],
                   out_w: int, out_h: int, work_dir: str,
                   subtitle_input_idx: int = 2
                   ) -> Tuple[List[str], str, Optional[Dict], Optional[str]]:
    """Build the subtitle burn-in step (shared by both render paths).

    Returns (filter_steps, subtitle_mode, subtitle_position, concat_path).
    The steps always end with a [vout] label (null when nothing is burned).
    subtitle_input_idx is the FFmpeg input index of the subtitle concat
    video (2 in the recap render, 1 in the Pro render).
    """
    cfg = subtitle_config or {}
    cfg_enabled = cfg.get("enabled", True)
    font = _resolve_subtitle_font(cfg, srt_path)
    if not (srt_path and font and cfg_enabled):
        return ([f"[{cur_label}]null[vout]"], "none", None, None)
    try:
        fx = float(cfg.get("x_pct", 0.5))
        fy = float(cfg.get("y_pct", 0.82))
        # Prefer font_size_pct (fraction of output width) so the burned-in
        # size matches the canvas preview exactly at any output
        # resolution. Legacy font_size is px at the 400px reference.
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
    _rgb = SUBTITLE_COLOR_RGB.get(str(cfg.get("color", "Yellow")), (255, 255, 0))
    _sub_pos = {"x_pct": fx, "y_pct": fy, "font_size": fs,
                "font_size_pct": fpct, "pos_x": pos_x, "pos_y": pos_y,
                "font_family": font["family"],
                "color": str(cfg.get("color", "Yellow")),
                "w_pct": cfg.get("w_pct"),
                "max_lines": 2}
    if not _raqm_available():
        # No ASS/libass fallback anymore (Fix 5): the server's libass
        # cannot shape Myanmar (e-vowel stays at the back, stacked
        # consonants show "+"), so a degraded fallback would silently
        # produce WRONG subtitles. RAQM is required — it is present on
        # Streamlit Cloud (Pillow 12.3.0 with libraqm).
        raise VideoEditorError(
            "Pillow RAQM (libraqm) မရှိပါ — မြန်မာစာ ပုံဖော်မှု မရပါ။")
    subtitle_pngs = _render_subtitle_pngs(
        srt_path, font["file"], fs, out_w, out_h,
        pos_x, pos_y, _rgb, work_dir,
        wrap_w_pct=cfg.get("w_pct"))
    if not subtitle_pngs:
        raise VideoEditorError("SRT တွင် စာတန်းမတွေ့ပါ။")
    # One concat video input + ONE overlay (see _build_subtitle_concat
    # for why N overlays are too slow).
    subtitle_concat = _build_subtitle_concat(
        subtitle_pngs, out_w, out_h, work_dir)
    return ([f"[{cur_label}][{subtitle_input_idx}:v]"
             f"overlay=0:0:format=auto[vout]"],
            "burned_in",
            dict(_sub_pos, method="raqm_png", cues=len(subtitle_pngs)),
            subtitle_concat)


def _srt_timestamp(sec: float) -> str:
    """Format seconds as an SRT timestamp (HH:MM:SS,mmm)."""
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:  # rounding carry — cascade through s, m, h
        ms = 0
        s += 1
        if s == 60:
            s = 0
            m += 1
            if m == 60:
                m = 0
                h += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_pro_srt(blocks: List[Dict], srt_path: str) -> Tuple[str, int]:
    """Write subtitles for Pro blocks (one cue per block).

    Each cue uses the block's ORIGINAL dialogue window ("start"/"end"
    — the exact time the person speaks in the source video), because
    video speed is always 1.0 and the synced timeline IS the original
    timeline. Falls back to "synced_start"/"synced_end" for blocks
    that carry them instead. Returns (path, cue count).
    """
    with open(srt_path, "w", encoding="utf-8") as f:
        n = 0
        for i, b in enumerate(blocks):
            text = str(b.get("text", "")).strip()
            if not text:
                continue
            start = b.get("start", b.get("synced_start"))
            end = b.get("end", b.get("synced_end"))
            if start is None or end is None:
                continue
            n += 1
            f.write(
                f"{n}\n"
                f"{_srt_timestamp(start)} --> "
                f"{_srt_timestamp(end)}\n"
                f"{text}\n\n"
            )
    return srt_path, n


def _scale_srt_timestamps(srt_path: str, scale: float, max_dur: float,
                           out_path: str) -> str:
    """Rewrite an SRT with all cue times multiplied by ``scale``.

    Used when narration audio is atempo-fitted to the video duration:
    the word-boundary SRT from TTS is in original-audio time, so it must
    be scaled into video time. Cues starting at/after ``max_dur`` are
    dropped; ends are clamped to ``max_dur``. Returns out_path.
    """
    cues = _parse_srt_cues(srt_path)
    with open(out_path, "w", encoding="utf-8") as f:
        n = 0
        for start, end, text in cues:
            ns = start * scale
            ne = end * scale
            if ns >= max_dur - 1e-6:
                continue
            ne = min(ne, max_dur)
            if ne <= ns:
                continue
            n += 1
            f.write(f"{n}\n{_srt_timestamp(ns)} --> "
                    f"{_srt_timestamp(ne)}\n{text}\n\n")
    return out_path


def _offset_srt_timestamps(srt_path: str, offset: float,
                           out_path: str) -> str:
    """Rewrite an SRT with all cue times shifted by ``offset`` seconds.

    Used when a narration CONTINUATION's SRT is appended after the
    first narration: the continuation's cues are in continuation-audio
    time (starting at 0), so they must be shifted by the length of the
    first narration. Cue numbers are re-sequenced from 1. Returns
    out_path.
    """
    cues = _parse_srt_cues(srt_path)
    with open(out_path, "w", encoding="utf-8") as f:
        n = 0
        for start, end, text in cues:
            ns = start + offset
            ne = end + offset
            if ne <= ns:
                continue
            n += 1
            f.write(f"{n}\n{_srt_timestamp(ns)} --> "
                    f"{_srt_timestamp(ne)}\n{text}\n\n")
    return out_path


def fit_audio_to_duration(audio_path: str, srt_path: Optional[str],
                          target_dur: float,
                          work_dir: str) -> Tuple[str, Optional[str], Dict]:
    """Fit a voiceover to exactly ``target_dur`` seconds (video stays 1.0x).

    - Audio shorter than target: pad with silence (apad) to target.
      SRT is unchanged (cues stay in their original positions).
    - Audio longer than target: atempo to fit (factor up to
      PRO_TTS_FIT_MAX), SRT timestamps scaled by 1/factor. If the
      factor would exceed PRO_TTS_FIT_MAX, atempo at the max and trim
      the tail to target (last resort), SRT scaled by 1/max then cut.
    - Within 0.1s: return the originals untouched.

    Returns (fitted_audio_path, fitted_srt_path_or_None, fit_info).
    The fitted files live in ``work_dir``; the caller keeps the final
    video + SRT, so these intermediates are cleaned up with the run.
    """
    target = max(0.0, float(target_dur))
    if target <= 0:
        raise VideoEditorError("fit target duration must be > 0.")
    audio_len = _media_duration(audio_path)
    if abs(audio_len - target) < 0.1:
        return audio_path, srt_path, {"method": "none",
                                       "audio_len": round(audio_len, 3),
                                       "target": round(target, 3)}
    os.makedirs(work_dir, exist_ok=True)
    if audio_len < target:
        fitted = os.path.join(work_dir, "voiceover_fitted.mp3")
        _run(["ffmpeg", "-y", "-v", "error", "-i", audio_path,
              "-af", f"apad,atrim=0:{target:.3f}",
              "-c:a", "libmp3lame", "-q:a", "4", fitted],
             "pad voiceover to video duration")
        return fitted, srt_path, {"method": "pad",
                                   "audio_len": round(audio_len, 3),
                                   "target": round(target, 3)}
    # Longer: atempo (then trim if the factor would exceed the max).
    factor = audio_len / target
    if factor <= PRO_TTS_FIT_MAX:
        fitted = os.path.join(work_dir, "voiceover_fitted.mp3")
        _run(["ffmpeg", "-y", "-v", "error", "-i", audio_path,
              "-af", f"atempo={factor:.4f}",
              "-c:a", "libmp3lame", "-q:a", "4", fitted],
             "atempo voiceover to video duration")
        fitted_srt = srt_path
        if srt_path and os.path.isfile(srt_path):
            scaled = os.path.join(work_dir, "subtitles_fitted.srt")
            fitted_srt = _scale_srt_timestamps(srt_path, 1.0 / factor,
                                               target, scaled)
        return fitted, fitted_srt, {"method": "atempo",
                                     "factor": round(factor, 4),
                                     "audio_len": round(audio_len, 3),
                                     "target": round(target, 3)}
    fitted = os.path.join(work_dir, "voiceover_fitted.mp3")
    _run(["ffmpeg", "-y", "-v", "error", "-i", audio_path,
          "-af", f"atempo={PRO_TTS_FIT_MAX:.4f},atrim=0:{target:.3f}",
          "-c:a", "libmp3lame", "-q:a", "4", fitted],
         "atempo+trim voiceover to video duration")
    fitted_srt = srt_path
    if srt_path and os.path.isfile(srt_path):
        scaled = os.path.join(work_dir, "subtitles_fitted.srt")
        fitted_srt = _scale_srt_timestamps(srt_path, 1.0 / PRO_TTS_FIT_MAX,
                                           target, scaled)
    return fitted, fitted_srt, {"method": "atempo_trim",
                                 "factor": round(factor, 4),
                                 "audio_len": round(audio_len, 3),
                                 "target": round(target, 3)}


def _media_duration(path: str) -> float:
    """ffprobe duration (seconds) for any audio/video file."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise VideoEditorError(
            f"Could not measure duration: {proc.stderr.strip()[:200]}")
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise VideoEditorError("ffprobe returned an invalid duration.")


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
    bg_volume: float = 0.0,
    work_dir: Optional[str] = None,
    blur_boxes: Optional[List[Dict]] = None,
    subtitle_config: Optional[Dict] = None,
    # Step 15: fit mode + copyright filters (defaults preserve the old
    # behaviour exactly — fit_mode None = legacy 16:9 pad-black / crop).
    fit_mode: Optional[str] = None,
    crop_align: str = "center",
    inner_scale: float = 1.0,
    vertical_pos: float = 0.0,
    horizontal_pos: float = 0.0,
    mirror_horizontal: bool = False,
    copyright_zoom: float = 1.0,
    random_crop_offset: bool = False,
    # Progress callback for the render phase: called with a 0..1
    # fraction measured from ffmpeg's own -progress output
    # (out_time_ms / voiceover duration — the audio is master).
    on_progress: Optional[Callable[[float], None]] = None,
) -> Dict:
    """Render the final recap video. Returns a result dict.

    blur_boxes: [{"x_pct","y_pct","w_pct","h_pct","strength" 1..50}, ...] —
        each box is blurred onto the final frame at its exact position.
    subtitle_config: {"enabled": True, "x_pct": 0.5, "y_pct": 0.82,
        "font_size_pct": 0.055} — font size as a fraction of the output
        width, so the burned-in size matches the canvas preview exactly
        (legacy "font_size" px at the 400px reference still works).
        Subtitles are burned via Pillow RAQM PNG overlays so the Canvas
        preview matches the render 1:1.
    aspect_ratio: "original", "9:16", "1:1", "4:5" or "16:9".
    fit_mode: "crop" (scale-to-fill + crop_align top/center/bottom) or
        "pad" (blurred-bg pad + inner_scale / vertical_pos / horizontal_pos).
        None = legacy (16:9 pads black, the rest crop-fills).
    Copyright filters run FIRST (source-frame stage, before fit/blur):
        mirror_horizontal (hflip), copyright_zoom (1.0–1.25, scale + crop
        back), random_crop_offset (2–10px random shift; 1.02x when zoom
        is 1.0 so there is slack to shift inside).

    Result keys: video_path, subtitle_mode ("burned_in"|"soft"|"none"),
    aspect_ratio, video_speed_factor, duration, width, height, has_audio,
    blur_boxes_applied, subtitle_position, copyright_applied, fit_mode.
    """
    if aspect_ratio not in ("original", "16:9", "9:16", "1:1", "4:5"):
        raise ValueError(
            "aspect_ratio must be 'original', '16:9', '9:16', '1:1' or '4:5'.")
    if crop_align not in ("top", "center", "bottom"):
        raise ValueError("crop_align must be 'top', 'center' or 'bottom'.")
    if fit_mode is not None and fit_mode not in ("crop", "pad"):
        raise ValueError("fit_mode must be 'crop', 'pad', or None (legacy).")
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
        # 1) trim + concat — SKIPPED when highlights tile the whole
        # video (full-duration AI Recap): the source IS the concat,
        # so re-encoding every highlight first was pure waste
        # (N+1 encodes). Partial highlights keep the exact old path.
        if _highlights_tile(safe_highlights, src_dur):
            concat_path = source_video_path
        else:
            seg_paths = _trim_segments(source_video_path, safe_highlights,
                                       seg_dir)
            concat_path = _concat_segments(seg_paths, seg_dir)

        # 2) build the final filter graph
        out_w, out_h = _resolve_aspect_target(aspect_ratio, src_meta)
        src_w = int(src_meta.get("width") or out_w)
        src_h = int(src_meta.get("height") or out_h)
        base_w, base_h = src_w - (src_w % 2), src_h - (src_h % 2)
        # Copyright filters FIRST (source-frame stage, before fit/blur).
        copyright_str, copyright_applied = _copyright_filter_chain(
            base_w, base_h, mirror_horizontal, copyright_zoom,
            random_crop_offset)
        pre: List[str] = []
        if (base_w, base_h) != (src_w, src_h):
            pre.append(f"scale={base_w}:{base_h}")
        if copyright_str:
            pre.append(copyright_str)
        if abs(factor - 1.0) > 1e-6:
            pre.append(f"setpts=PTS/{factor:.4f}")
        vchain: List[str] = []
        fit_in = "0:v"
        if pre:
            vchain.append(f"[0:v]{','.join(pre)}[vpre]")
            fit_in = "vpre"
        fit_steps, cur_label = _fit_filter_steps(
            fit_in, base_w, base_h, out_w, out_h, fit_mode, crop_align,
            inner_scale, vertical_pos, horizontal_pos, aspect_ratio)
        vchain.extend(fit_steps)

        # 2a) multi blur boxes at exact output-resolution positions
        boxes = _normalize_blur_boxes(blur_boxes, out_w, out_h)
        if boxes:
            steps, cur_label = _blur_filter_steps(cur_label, boxes)
            vchain.extend(steps)

        # 2b) subtitles: exact position/size burn-in (RAQM PNG
        # overlay) or none — shared helper, also used by the Pro render.
        sub_steps, subtitle_mode, subtitle_position, subtitle_concat = \
            _subtitle_step(cur_label, srt_path, subtitle_config,
                           out_w, out_h, work_dir)
        vchain.extend(sub_steps)

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

        # Render progress comes from ffmpeg itself (out_time_ms /
        # voiceover duration — the audio is master, -shortest cuts
        # the output there).
        _run_ffmpeg_progress(
            cmd, "final render", _media_duration(voiceover_path),
            on_progress)
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
            "copyright_applied": copyright_applied,
            "fit_mode": fit_mode or ("pad_black_legacy"
                                     if aspect_ratio == "16:9" else "crop"),
            "crop_align": crop_align,
        }
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Step 15: Pro AI Recap render (per-block independent speed sync)
# --------------------------------------------------------------------------- #
def render_pro_recap_video(
    source_video_path: str,
    pro_blocks: List[Dict],
    output_path: Optional[str] = None,
    aspect_ratio: str = "9:16",
    fit_mode: Optional[str] = None,
    crop_align: str = "center",
    inner_scale: float = 1.0,
    vertical_pos: float = 0.0,
    horizontal_pos: float = 0.0,
    mirror_horizontal: bool = False,
    copyright_zoom: float = 1.0,
    random_crop_offset: bool = False,
    bg_volume: float = 0.0,
    work_dir: Optional[str] = None,
    blur_boxes: Optional[List[Dict]] = None,
    subtitle_config: Optional[Dict] = None,
    srt_path: Optional[str] = None,
    # Progress callback for the render phase (0..1): per-block sync
    # fills 0–0.40 (equal share per block), the final visual chain
    # fills 0.40–1.0 — both measured from ffmpeg's -progress output.
    on_progress: Optional[Callable[[float], None]] = None,
) -> Dict:
    """Render a Pro AI Recap video (dialogue dubbing, Step 15).

    pro_blocks: [{"start","end","text","audio_path"}, ...] — sequential
    blocks tiling the source. Video speed is ALWAYS 1.0 (No Cuts, no
    setpts, no slow-motion): each block's video segment keeps its
    ORIGINAL duration, and the block narration is fitted to that
    dialogue window (TTS rate ladder in tts_manager, then atempo up to
    PRO_TTS_FIT_MAX, then trim as a last resort; shorter narration is
    silence-padded). The synced timeline IS the original block timeline
    (synced_start = start, synced_end = end), so subtitles appear
    exactly when the person speaks. Blocks are concatenated, then the
    same visual chain as the recap render applies: copyright filters
    FIRST (source-frame stage), then aspect/fit, then blur boxes, then
    subtitles. srt_path is the path the rebuilt SRT is written to
    (default: work_dir/pro_subtitles.srt).

    Result keys: video_path, subtitle_mode, aspect_ratio,
    processing_mode ("pro_ai_recap"), blocks (with synced_start/end +
    speed 1.0), duration, width, height, has_audio, blur_boxes_applied,
    subtitle_position, copyright_applied, fit_mode, srt_path.
    """
    if aspect_ratio not in ("original", "16:9", "9:16", "1:1", "4:5"):
        raise ValueError(
            "aspect_ratio must be 'original', '16:9', '9:16', '1:1' or '4:5'.")
    if crop_align not in ("top", "center", "bottom"):
        raise ValueError("crop_align must be 'top', 'center' or 'bottom'.")
    if fit_mode is not None and fit_mode not in ("crop", "pad"):
        raise ValueError("fit_mode must be 'crop', 'pad', or None (legacy).")
    if not pro_blocks:
        raise ValueError("pro_blocks သည် အလွတ်ဖြစ်နေပါသည်။")
    if not os.path.isfile(str(source_video_path)):
        raise FileNotFoundError(f"source video not found: {source_video_path}")

    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix="video_recap_pro_")
    seg_dir = os.path.join(work_dir, "segments")
    block_dir = os.path.join(work_dir, "blocks")
    os.makedirs(block_dir, exist_ok=True)
    if output_path is None:
        output_path = os.path.join(
            work_dir, f"final_pro_{aspect_ratio.replace(':', 'x')}.mp4")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    if srt_path is None:
        srt_path = os.path.join(work_dir, "pro_subtitles.srt")

    src_meta = get_video_metadata(source_video_path)
    src_dur = src_meta["duration"] or 0

    # Normalize + clamp blocks (sorted, inside the source).
    blocks: List[Dict] = []
    for b in pro_blocks:
        try:
            s = max(0.0, float(b.get("start", 0)))
            e = float(b.get("end", 0))
            if src_dur:
                e = min(e, src_dur)
        except (TypeError, ValueError, AttributeError):
            continue
        ap = b.get("audio_path")
        if e <= s or not ap or not os.path.isfile(str(ap)):
            continue
        blocks.append({"start": s, "end": e,
                       "text": str(b.get("text", "")).strip(),
                       "audio_path": str(ap)})
    blocks.sort(key=lambda b: b["start"])
    if not blocks:
        raise VideoEditorError("No valid Pro blocks after clamping.")

    try:
        # 1) trim the block segments (re-encoded, like the recap path)
        seg_paths = _trim_segments(source_video_path, blocks, seg_dir)

        # 2) per-block audio mix — video speed is ALWAYS 1.0 (no
        # setpts, no slow-motion). Each block's video keeps its
        # ORIGINAL duration; the narration is fitted to that dialogue
        # window (TTS rate ladder already tried in tts_manager; here
        # atempo up to PRO_TTS_FIT_MAX, then trim, is the last resort;
        # shorter narration is silence-padded). The synced timeline
        # IS the original block timeline, so concat never drifts and
        # subtitles land exactly on the dialogue.
        synced: List[Dict] = []
        block_outs: List[str] = []
        cum = 0.0
        for i, b in enumerate(blocks):
            seg = seg_paths[i]
            seg_meta = get_video_metadata(seg)
            seg_dur = seg_meta["duration"] or (b["end"] - b["start"])
            audio_len = _media_duration(b["audio_path"])
            if audio_len <= 0:
                raise VideoEditorError(
                    f"Pro block {i} audio duration is 0.")
            out = os.path.join(block_dir, f"block_{i:03d}.mp4")
            speed = 1.0
            video_dur = seg_dur
            # Fit the block narration to the ORIGINAL dialogue window.
            if audio_len > video_dur + 1e-3:
                fit_factor = audio_len / video_dur
                if fit_factor <= PRO_TTS_FIT_MAX:
                    tts_fit = f"atempo={fit_factor:.4f}"
                else:
                    tts_fit = (
                        f"atempo={PRO_TTS_FIT_MAX:.4f},"
                        f"atrim=0:{video_dur:.3f}"
                    )
            elif audio_len < video_dur - 1e-3:
                tts_fit = f"apad,atrim=0:{video_dur:.3f}"
            else:
                tts_fit = "anull"
            if seg_meta["has_audio"]:
                fc = (
                    f"[0:v]null[v];"
                    f"[0:a]anull,volume={bg_volume}[bg];"
                    f"[1:a]{tts_fit},aresample=44100[tts];"
                    f"[bg][tts]amix=inputs=2:duration=shortest:"
                    f"dropout_transition=0:normalize=0,"
                    f"alimiter=limit=0.95,aresample=44100[a]"
                )
            else:
                fc = (f"[0:v]null[v];"
                      f"[1:a]{tts_fit},aresample=44100[a]")
            _n_blocks = len(blocks)

            def _block_cb(frac, _i=i, _n=_n_blocks):
                if on_progress is not None:
                    on_progress(0.40 * (_i + frac) / max(1, _n))

            _run_ffmpeg_progress(
                ["ffmpeg", "-y", "-v", "error",
                 "-i", seg, "-i", b["audio_path"],
                 "-filter_complex", fc,
                 "-map", "[v]", "-map", "[a]",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-r", "30",
                 "-c:a", "aac", "-b:a", "128k", out],
                f"pro block {i} sync",
                video_dur,
                _block_cb if on_progress is not None else None,
            )
            block_outs.append(out)
            # Synced timeline = ORIGINAL dialogue window (speed is 1.0,
            # so cumulative video durations equal the original starts
            # up to trim rounding — but subtitles must land EXACTLY on
            # the dialogue, so record the original start/end).
            synced.append({
                **b,
                "speed": round(speed, 4),
                "audio_len": round(audio_len, 3),
                "video_dur": round(video_dur, 3),
                "synced_start": round(b["start"], 3),
                "synced_end": round(b["end"], 3),
            })
            cum += video_dur

        # 3) concatenate the blocks (video + audio)
        concat_path = _concat_segments(block_outs, block_dir)

        # 4) rebuilt subtitles on the ORIGINAL dialogue timeline
        build_pro_srt(synced, srt_path)

        # 5) visual chain on the concatenated result
        out_w, out_h = _resolve_aspect_target(aspect_ratio, src_meta)
        src_w = int(src_meta.get("width") or out_w)
        src_h = int(src_meta.get("height") or out_h)
        base_w, base_h = src_w - (src_w % 2), src_h - (src_h % 2)
        copyright_str, copyright_applied = _copyright_filter_chain(
            base_w, base_h, mirror_horizontal, copyright_zoom,
            random_crop_offset)
        pre: List[str] = []
        if (base_w, base_h) != (src_w, src_h):
            pre.append(f"scale={base_w}:{base_h}")
        if copyright_str:
            pre.append(copyright_str)
        vchain: List[str] = []
        fit_in = "0:v"
        if pre:
            vchain.append(f"[0:v]{','.join(pre)}[vpre]")
            fit_in = "vpre"
        fit_steps, cur_label = _fit_filter_steps(
            fit_in, base_w, base_h, out_w, out_h, fit_mode, crop_align,
            inner_scale, vertical_pos, horizontal_pos, aspect_ratio)
        vchain.extend(fit_steps)
        boxes = _normalize_blur_boxes(blur_boxes, out_w, out_h)
        if boxes:
            steps, cur_label = _blur_filter_steps(cur_label, boxes)
            vchain.extend(steps)
        sub_steps, subtitle_mode, subtitle_position, subtitle_concat = \
            _subtitle_step(cur_label, srt_path, subtitle_config,
                           out_w, out_h, work_dir, subtitle_input_idx=1)
        vchain.extend(sub_steps)
        filtergraph = ";".join(vchain)

        cmd = ["ffmpeg", "-y", "-v", "error", "-i", concat_path]
        if subtitle_concat:
            cmd += ["-f", "concat", "-safe", "0", "-i", subtitle_concat]
        cmd += ["-filter_complex", filtergraph,
                "-map", "[vout]", "-map", "0:a",
                "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-r", "30",
                "-c:a", "copy",
                "-movflags", "+faststart"]
        cmd.append(output_path)
        _run_ffmpeg_progress(
            cmd, "pro final render", cum,
            (lambda f: on_progress(0.40 + 0.60 * f))
            if on_progress is not None else None)

        meta = get_video_metadata(output_path)
        return {
            "video_path": output_path,
            "subtitle_mode": subtitle_mode,
            "aspect_ratio": aspect_ratio,
            "processing_mode": "pro_ai_recap",
            "blocks": synced,
            "duration": meta["duration"],
            "width": meta["width"],
            "height": meta["height"],
            "has_audio": meta["has_audio"],
            "file_size_mb": meta["file_size_mb"],
            "srt_path": srt_path,
            "blur_boxes_applied": len(boxes),
            "subtitle_position": subtitle_position,
            "copyright_applied": copyright_applied,
            "fit_mode": fit_mode or ("pad_black_legacy"
                                     if aspect_ratio == "16:9" else "crop"),
            "crop_align": crop_align,
        }
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)
        shutil.rmtree(block_dir, ignore_errors=True)
