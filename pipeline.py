"""
pipeline.py — Step 5: Master Pipeline.

run_full_recap_pipeline(...) wires together video_processor, ai_analyzer,
tts_manager and video_editor into one end-to-end recap pipeline:

  ingest -> AI plan (or mock plan) -> Myanmar voiceover+SRT ->
  timing sync -> FFmpeg render -> cleanup

Returns dict with: final_video_path, srt_path, script_text, title, metadata,
subtitle_mode, sync_method, video_speed_factor.

No API keys are used or stored anywhere in this module; the user's key is
only passed through to ai_analyzer (Bring-Your-Own-Key).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
from typing import Callable, Dict, List, Optional, Union

from video_processor import get_video_metadata, prepare_video_source
from key_pool import GeminiKeyPool, QuotaExhaustedError
from ai_analyzer import (
    DEFAULT_STYLE,
    analyze_video_continuation,
    analyze_video_for_recap,
    analyze_video_pro_blocks,
    analyze_video_pro_fill,
    delete_gemini_file,
    generate_mock_pro_plan,
    generate_mock_recap_plan,
    generate_post_kit,
    validate_pro_blocks,
    validate_recap_plan,
)
from tts_manager import (
    generate_block_voiceovers,
    generate_burmese_voiceover,
    measure_audio_duration,
)
from video_editor import (
    QUALITY_PRESETS,
    SUBTITLE_COLOR_RGB,
    _audio_is_silent,
    _offset_srt_timestamps,
    _resolve_aspect_target,
    _subtitle_font_file,
    build_hybrid_filter_graph,
    build_pro_srt,
    fit_audio_to_duration,
    make_thumbnail,
    mix_pro_block_audio,
    prepare_overlay_images,
    render_pro_recap_video,
    render_recap_video,
    sanitize_filename,
)


class PipelineError(Exception):
    """Raised for pipeline failures."""


# Narration coverage: a plan is "full" when the Myanmar speech covers
# at least 90% of the video. Below that, the pipeline retries Gemini
# for the missing part (AI continuation / Pro fill).
NARRATION_COVERAGE_TARGET = 0.90
# Max extra Gemini narration rounds per Run (each costs one more
# analyze call + one more TTS pass). 2 was not enough: measured live
# 2026-10-17, first script + 2 continuations reached only 6:39/8:04
# (82%) — the loop ran out of rounds below the 90% target.
NARRATION_RETRY_MAX = 5
# A continuation round that adds less than this many seconds means
# Gemini has stalled on the tail — stop instead of burning calls.
NARRATION_MIN_ROUND_S = 5.0
# Max empty windows per Pro fill call: one giant call for all empty
# windows returns truncated / re-segmented JSON that matches nothing.
PRO_FILL_BATCH = 10


def _narration_coverage(covered_s: float, total_s: float) -> Dict[str, float]:
    """Coverage report shown in the success line (Fix 3).

    ``covered_s`` = seconds of real Myanmar speech (measured TTS
    audio length for AI; sum of non-empty block audio for Pro).
    """
    total = max(0.0, float(total_s or 0))
    covered = max(0.0, min(float(covered_s or 0), total)) if total else 0.0
    pct = (covered / total * 100.0) if total else 0.0
    return {"covered_s": round(covered, 1),
            "total_s": round(total, 1),
            "pct": round(pct, 1)}


def _fill_blocks_by_overlap(blocks, filled_blocks) -> int:
    """Fill empty Pro blocks from fill results by window overlap.

    Gemini may re-segment windows, so exact (start, end) equality
    misses. Each empty block takes the text of the filled block
    overlapping it most (same filled block may serve several empty
    blocks — duplication beats silence). Returns filled count.
    """
    _filled = 0
    for _b in blocks:
        if str(_b.get("text", "")).strip():
            continue
        try:
            _bs = float(_b.get("start", 0))
            _be = float(_b.get("end", 0))
        except (TypeError, ValueError):
            continue
        _best = None
        _best_ov = 0.0
        for _fb in filled_blocks or []:
            # Empty-text candidates must not win the overlap race:
            # they would beat a real candidate, then be rejected,
            # leaving the block unfilled.
            if not str(_fb.get("text", "")).strip():
                continue
            try:
                _fs = float(_fb.get("start", 0))
                _fe = float(_fb.get("end", 0))
            except (TypeError, ValueError, AttributeError):
                continue
            _ov = min(_be, _fe) - max(_bs, _fs)
            if _ov > _best_ov:
                _best_ov = _ov
                _best = _fb
        if _best is not None and _best_ov > 0:
            _txt = str(_best.get("text", "")).strip()
            if _txt:
                _b["text"] = _txt
                _filled += 1
    return _filled


def _find_underfilled_blocks(blocks, threshold: float = 0.60) -> List[tuple]:
    """Find Pro blocks whose audio is too short for their window.

    Coverage fix v3: a block with non-empty text can still render
    mostly silence if its text is too brief for its window (the old
    Pro prompt pushed Gemini toward very brief text). A block is
    under-filled when its measured audio is < ``threshold`` of its
    window duration. Empty-text blocks are NOT under-filled here —
    the empty-block fill handles them separately. Returns a list of
    (start, end) window tuples.
    """
    _under = []
    for _b in blocks:
        if not str(_b.get("text", "")).strip():
            continue
        try:
            _ws = float(_b.get("start", 0))
            _we = float(_b.get("end", 0))
            _al = float(_b.get("audio_len", 0))
        except (TypeError, ValueError):
            continue
        if (_we - _ws) > 0 and _al < threshold * (_we - _ws):
            _under.append((_ws, _we))
    return _under


def _renumber_srt(srt_path: str) -> str:
    """Re-sequence cue numbers 1..N in place.

    Used after appending a continuation SRT (whose cues restart at
    1) to the first SRT — duplicate numbers confuse some players.
    """
    import re

    with open(srt_path, encoding="utf-8") as f:
        text = f.read()
    blocks = re.split(r"\n\s*\n", text.strip())
    out = []
    n = 0
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) >= 2 and lines[0].strip().isdigit() \
                and "-->" in lines[1]:
            n += 1
            lines[0] = str(n)
            out.append("\n".join(lines))
        elif block.strip():
            out.append(block.strip())
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(out) + ("\n" if out else ""))
    return srt_path


def _concat_audios(paths: List[str], out_path: str) -> str:
    """Concatenate audio files end-to-end into one MP3 (ffmpeg concat).

    Used to append a narration CONTINUATION to the first narration.
    Re-encodes (MP3 concat with stream-copy can drift), so the result
    is a clean single file.
    """
    import subprocess

    if not paths:
        raise PipelineError("အသံဖိုင် ပေါင်းရန် ဖိုင် မရှိပါ။")
    if len(paths) == 1:
        shutil.copyfile(paths[0], out_path)
        return out_path
    inputs: List[str] = []
    for p in paths:
        inputs += ["-i", str(p)]
    n = len(paths)
    filt = "".join(f"[{i}:a]" for i in range(n)) + \
        f"concat=n={n}:v=0:a=1[a]"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *inputs,
         "-filter_complex", filt, "-map", "[a]",
         "-c:a", "libmp3lame", "-q:a", "4", out_path],
        capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise PipelineError(
            "အသံဖိုင် ပေါင်းစပ်မှု မအောင်မြင်ပါ: "
            f"{proc.stderr.strip()[:200]}")
    return out_path


def _clean_pack_dir(run_dir: str) -> None:
    """Delete everything in a Hybrid pack dir except the pack itself.

    Keeps pack.json + voiceover.mp3 (the device encode inputs) and
    drops all intermediates (block audio, merged/fitted temp files).
    job_status.json is written later by the caller — it is not here
    yet, so nothing else needs preserving.
    """
    if not run_dir or not os.path.isdir(str(run_dir)):
        return
    _keep = {"pack.json", "voiceover.mp3"}
    for _name in os.listdir(run_dir):
        if _name in _keep:
            continue
        _p = os.path.join(run_dir, _name)
        try:
            if os.path.isfile(_p):
                os.remove(_p)
            elif os.path.isdir(_p):
                shutil.rmtree(_p, ignore_errors=True)
        except OSError:
            pass


def _build_hybrid_pack(
    *,
    run_dir: str,
    processing_mode: str,
    title: str,
    script_text: str,
    sync_method: str,
    narration_coverage: Dict,
    aspect_ratio: str,
    video_duration: float,
    src_w: int,
    src_h: int,
    fit_mode,
    crop_align: str,
    inner_scale: float,
    vertical_pos: float,
    horizontal_pos: float,
    mirror_horizontal: bool,
    copyright_zoom: float,
    random_crop_offset: bool,
    bg_volume: float,
    blur_boxes,
    subtitle_config: Optional[Dict],
    burn_subtitles: bool,
    voiceover_path: str,
    srt_path: Optional[str],
    extra: Optional[Dict] = None,
    # Feature pack v1.13.
    quality: str = "fast",
    fps: int = 30,
    output_name: str = "recap.mp4",
    watermark: Optional[Dict] = None,
    logo: Optional[Dict] = None,
    postkit: Optional[Dict] = None,
) -> Dict:
    """Build the Hybrid pack: everything the device encode needs.

    Writes voiceover.mp3 + pack.json into ``run_dir`` (the persistence
    for a session reset: the pack is re-sent from these files) and
    returns the pack dict (no audio bytes — the voiceover travels as
    the separate voiceover.mp3 file, re-read when the pack is sent).
    The pack is small by design: voiceover (~5-10MB) + SRT (~35KB) +
    plan JSON + font (~1MB) — no per-cue PNGs, no video bytes.
    """
    os.makedirs(run_dir, exist_ok=True)
    if not os.path.isfile(str(voiceover_path)):
        raise PipelineError("Hybrid pack: voiceover ဖိုင် မတွေ့ပါ။")
    _pack_voice = os.path.join(run_dir, "voiceover.mp3")
    shutil.copyfile(str(voiceover_path), _pack_voice)

    _srt_text = ""
    if srt_path and os.path.isfile(str(srt_path)):
        with open(str(srt_path), encoding="utf-8") as _f:
            _srt_text = _f.read()
    # Subtitles are burned only when enabled AND the SRT has cues.
    _sub_enabled = bool(burn_subtitles) and ("-->" in _srt_text)

    _cfg = dict(subtitle_config or {})
    # v1.13: overlay input indices. The browser adds -i args in pack
    # order (subtitle concat, watermark PNG, logo PNG); the graph is
    # built with the same indices. Output dims are resolved first so
    # the overlay PNGs can be rendered at the right size.
    _out_w, _out_h = _resolve_aspect_target(
        aspect_ratio,
        {"width": int(src_w or 0), "height": int(src_h or 0)})
    _prepared_ovl = prepare_overlay_images(watermark, logo, _out_w,
                                           _out_h, run_dir)
    _ovl_inputs: List[Dict] = []
    _idx = 2
    _sub_idx = _idx
    if _sub_enabled:
        _ovl_inputs.append({"kind": "subtitle", "idx": _idx})
        _idx += 1
    _wm_spec = None
    _wm_prep = _prepared_ovl.get("watermark")
    if _wm_prep:
        with open(_wm_prep["png_path"], "rb") as _f:
            _wm_b64 = base64.b64encode(_f.read()).decode()
        _wm_spec = {"input_idx": _idx, "x": int(_wm_prep["x"]),
                    "y": int(_wm_prep["y"])}
        _ovl_inputs.append({"kind": "watermark", "idx": _idx,
                            "png_b64": _wm_b64,
                            "x": int(_wm_prep["x"]),
                            "y": int(_wm_prep["y"])})
        _idx += 1
    _lg_spec = None
    _lg_prep = _prepared_ovl.get("logo")
    if _lg_prep:
        with open(_lg_prep["png_path"], "rb") as _f:
            _lg_b64 = base64.b64encode(_f.read()).decode()
        _lg_spec = {"input_idx": _idx, "x": int(_lg_prep["x"]),
                    "y": int(_lg_prep["y"])}
        _ovl_inputs.append({"kind": "logo", "idx": _idx,
                            "png_b64": _lg_b64,
                            "x": int(_lg_prep["x"]),
                            "y": int(_lg_prep["y"])})
        _idx += 1
    _graphs = build_hybrid_filter_graph(
        src_w=int(src_w or 0), src_h=int(src_h or 0),
        aspect_ratio=aspect_ratio, fit_mode=fit_mode,
        crop_align=crop_align, inner_scale=inner_scale,
        vertical_pos=vertical_pos, horizontal_pos=horizontal_pos,
        mirror_horizontal=mirror_horizontal,
        copyright_zoom=copyright_zoom,
        random_crop_offset=random_crop_offset,
        blur_boxes=blur_boxes, subtitle_enabled=_sub_enabled,
        bg_volume=bg_volume, subtitle_input_idx=_sub_idx,
        watermark=_wm_spec, logo=_lg_spec,
        loudnorm=not _audio_is_silent(str(voiceover_path)))
    _out_w, _out_h = _graphs["out_w"], _graphs["out_h"]

    # Subtitle geometry — same normalization as the server's
    # _subtitle_step (clamped fpct, pixel pos from fractions).
    try:
        _fx = min(1.0, max(0.0, float(_cfg.get("x_pct", 0.5))))
        _fy = min(1.0, max(0.0, float(_cfg.get("y_pct", 0.82))))
        if _cfg.get("font_size_pct") is not None:
            _fpct = float(_cfg["font_size_pct"])
        else:
            _fpct = float(_cfg.get("font_size", 22)) / 400.0
    except (TypeError, ValueError):
        _fx, _fy, _fpct = 0.5, 0.82, 0.055
    _fpct = min(0.30, max(0.02, _fpct))
    _fs = min(200, max(8, int(round(_fpct * _out_w))))
    _font_family = str(_cfg.get("font_family", "Pyidaungsu"))
    _color_name = str(_cfg.get("color", "Yellow"))
    _rgb = SUBTITLE_COLOR_RGB.get(_color_name, (255, 255, 0))
    _font_b64 = None
    _font_bold = False
    try:
        _font_path = _subtitle_font_file(_font_family)
        # The server renders with the Bold face when one ships (same
        # helper) — tell the device so its canvas weight matches.
        _font_bold = "bold" in os.path.basename(_font_path).lower()
        with open(_font_path, "rb") as _ff:
            _font_b64 = base64.b64encode(_ff.read()).decode()
    except (OSError, ValueError):
        _font_b64 = None

    _q = QUALITY_PRESETS.get(str(quality or "fast").lower(),
                            QUALITY_PRESETS["fast"])
    _pack = {
        "pack_id": os.path.basename(str(run_dir)),
        "pack_version": 2,
        "quality": {"name": str(quality or "fast"),
                    "preset": str(_q["preset"]),
                    "crf": _q["crf"]},
        "fps": int(fps or 30),
        "output_name": str(output_name or "recap.mp4"),
        "overlay_inputs": _ovl_inputs,
        "postkit": postkit,
        "processing_mode": processing_mode,
        "title": title,
        "script_text": script_text,
        "sync_method": sync_method,
        "narration_coverage": narration_coverage,
        "aspect_ratio": aspect_ratio,
        "video": {
            "duration": round(float(video_duration or 0), 3),
            "src_w": int(src_w or 0),
            "src_h": int(src_h or 0),
            "out_w": _out_w,
            "out_h": _out_h,
        },
        "fit_mode": fit_mode,
        "crop_align": crop_align,
        "copyright_applied": _graphs["copyright_applied"],
        "blur_boxes": _graphs["blur_boxes"],
        "subtitle": {
            "enabled": _sub_enabled,
            "x_pct": _fx,
            "y_pct": _fy,
            "font_size_pct": _fpct,
            "font_size": _fs,
            "pos_x": int(round(_fx * _out_w)),
            "pos_y": int(round(_fy * _out_h)),
            "w_pct": _cfg.get("w_pct"),
            "font_family": _font_family,
            "color": _color_name,
            "color_rgb": [_rgb[0], _rgb[1], _rgb[2]],
            "font_b64": _font_b64,
            "font_bold": _font_bold,
            "words_per_caption": _cfg.get("words_per_caption"),
        },
        "filter_graph": {
            "with_audio": _graphs["with_audio"],
            "no_audio": _graphs["no_audio"],
        },
        "subtitle_input": _graphs["subtitle_input"],
        "bg_volume": bg_volume,
        "srt_text": _srt_text,
        "voiceover_file": "voiceover.mp3",
    }
    if extra:
        _pack["diagnostics"] = dict(extra)
    with open(os.path.join(run_dir, "pack.json"), "w",
              encoding="utf-8") as _f:
        json.dump(_pack, _f)
    _clean_pack_dir(run_dir)
    return _pack


def _postkit_for_run(title, script_text, gemini_pool, groq_key, model,
                     use_mock_plan, extra_hashtags, append_fyp):
    """Post Kit caption + hashtags (feature pack v1.13; Keys v2).

    Best-effort: returns None on any failure (mock/demo mode, no key,
    API error) — the Post Kit is a bonus and never fails the run.
    Text task routed: Gemini keys (rotated) -> Groq fallback.
    """
    try:
        if use_mock_plan:
            return None
        return generate_post_kit(
            title, script_text, gemini_pool, groq_key=groq_key, model=model,
            extra_hashtags=extra_hashtags, append_fyp=append_fyp)
    except Exception:  # noqa: BLE001 - Post Kit is best-effort
        return None


def _enrich_server_result(result, work_dir, gemini_pool, groq_key, model,
                          use_mock_plan, extra_hashtags, append_fyp):
    """Add Post Kit + thumbnail to a server-mode result (best-effort).

    Never raises — both are bonuses on top of the finished render.
    """
    try:
        result["postkit"] = _postkit_for_run(
            result.get("title"), result.get("script_text"),
            gemini_pool, groq_key, model, use_mock_plan,
            extra_hashtags, append_fyp)
    except Exception:  # noqa: BLE001 - best-effort
        result["postkit"] = None
    try:
        _vp = result.get("final_video_path")
        if _vp and os.path.isfile(str(_vp)):
            _tp = os.path.join(work_dir, "thumbnail.png")
            make_thumbnail(str(_vp), str(result.get("title") or ""), _tp)
            result["thumbnail_path"] = _tp
        else:
            result["thumbnail_path"] = None
    except Exception:  # noqa: BLE001 - best-effort
        result["thumbnail_path"] = None
    return result


def run_full_recap_pipeline(
    video_path: Optional[str] = None,
    video_url: Optional[str] = None,
    user_api_key: Union[str, List[str], None] = None,
    groq_api_key: Optional[str] = None,
    on_quota_warning: Optional[Callable[[str], None]] = None,
    target_duration: int = 30,
    style: str = DEFAULT_STYLE,
    voice_name: str = "my-MM-ThihaNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
    aspect_ratio: str = "16:9",
    bg_volume: float = 0.0,
    work_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_name: str = "final_recap.mp4",
    use_mock_plan: bool = False,
    plan: Optional[Dict] = None,
    voiceover_path: Optional[str] = None,
    srt_path: Optional[str] = None,
    burn_subtitles: bool = True,
    tts_proxy: Optional[str] = None,
    model: Optional[str] = None,
    blur_boxes: Optional[List[Dict]] = None,
    subtitle_config: Optional[Dict] = None,
    # Step 15: processing mode + Source-tab engine options.
    processing_mode: str = "ai_recap",
    fit_mode: Optional[str] = None,
    crop_align: str = "center",
    inner_scale: float = 1.0,
    vertical_pos: float = 0.0,
    horizontal_pos: float = 0.0,
    mirror_horizontal: bool = False,
    copyright_zoom: float = 1.0,
    random_crop_offset: bool = False,
    # Progress callback: on_progress(pct, phase, detail) with pct in
    # [0, 100]. Phase weights — analyze 0–30, TTS 30–55, render
    # 55–100. Analyze / single-voiceover TTS are single calls with no
    # sub-progress, so they report phase start/end only (the bar
    # holds at the phase start while they run — that is honest, not
    # a faked creeping bar). Pro TTS and both renders report real
    # measured sub-progress (per-block completion, ffmpeg -progress).
    on_progress: Optional[Callable[[float, str, str], None]] = None,
    # Hybrid mode (device-side encode): the server does Gemini analysis
    # + TTS + pack building only — NO FFmpeg render. The browser holds
    # the source video (never uploaded to Streamlit) and encodes with
    # FFmpeg.wasm from the returned pack. gemini_file_name is the
    # already-uploaded Gemini file (browser-uploaded); video_duration /
    # src_width / src_height are the browser's video metadata (there is
    # no local file to ffprobe). Server mode ignores all of these.
    hybrid_pack: bool = False,
    gemini_file_name: Optional[str] = None,
    video_duration: Optional[float] = None,
    src_width: Optional[int] = None,
    src_height: Optional[int] = None,
    # Feature pack v1.13: render quality (fast/normal/high), output
    # fps, burned overlays, Post Kit options. watermark =
    # {"text","position","opacity"}; logo =
    # {"upload_path","position","scale","opacity"}.
    quality: str = "fast",
    fps: int = 30,
    watermark: Optional[Dict] = None,
    logo: Optional[Dict] = None,
    postkit_extra_hashtags: str = "",
    postkit_append_fyp: bool = True,
) -> Dict:
    """Run the full recap pipeline end to end.

    Set use_mock_plan=True (or pass plan=) to skip the live Gemini call —
    useful for offline testing. Pass voiceover_path=/srt_path= to reuse
    prebuilt voiceover assets instead of re-running TTS.
    tts_proxy is dev-VM only; production leaves it as None.

    processing_mode: "ai_recap" (default, Narration Master — FULL
    original duration, narration over the full footage, video speed
    always 1.0, audio fitted to the video) or "pro_ai_recap" (dialogue
    dubbing — sequential blocks, video speed always 1.0, each block's
    narration fitted to its own dialogue window; voiceover_path/srt_path
    are ignored for Pro because every block carries its own audio; pass
    a plan whose blocks already have "audio_path" to reuse prebuilt
    block audio).
    ``target_duration`` is DEPRECATED and IGNORED — AI Recap is always
    the full video duration, never a short cut.
    fit_mode / crop_align / inner_scale / vertical_pos / horizontal_pos /
    mirror_horizontal / copyright_zoom / random_crop_offset are the
    Source-tab engine options (Step 15); defaults preserve the old output.
    """
    if processing_mode not in ("ai_recap", "pro_ai_recap"):
        raise PipelineError(
            "processing_mode must be 'ai_recap' or 'pro_ai_recap'.")
    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix="video_recap_pipe_")
    os.makedirs(work_dir, exist_ok=True)
    if output_dir is None:
        output_dir = work_dir
    os.makedirs(output_dir, exist_ok=True)

    # Server-restart guard (part 1): fail FAST with a clear message
    # when the disk is essentially full — a render that runs the disk
    # dry can OOM/crash the container (Streamlit Cloud restarts it,
    # and the job is unrecoverable). The precise per-video estimate
    # runs after ingest (part 2), once the duration is known. A guard
    # failure itself must never block a run.
    try:
        _free_mb = shutil.disk_usage(work_dir).free / (1024 * 1024)
        if _free_mb < 50:
            raise PipelineError(
                "Server မှာ နေရာလွတ် မလောက်တော့ပါ "
                f"({_free_mb:.0f}MB သာ ကျန်သည်) — "
                "ခဏစောင့်ပြီး Run ကို ထပ်နှိပ်ပေးပါ။"
            )
    except PipelineError:
        raise
    except Exception:
        pass

    # Progress reporting: phase weights as (base, span) — analyze
    # 0–30, TTS 30–55, render 55–100 (Hybrid: pack 55–100, no render).
    # Single calls (Gemini analyze, one-shot TTS) report only phase
    # start/end — no faked creep.
    _PHASES = {"analyze": (0.0, 30.0), "tts": (30.0, 25.0),
               "render": (55.0, 45.0), "pack": (55.0, 45.0)}

    def _report(phase: str, frac: float, detail: str) -> None:
        if on_progress is None:
            return
        base, span = _PHASES[phase]
        pct = base + span * min(1.0, max(0.0, frac))
        on_progress(round(pct, 1), phase, detail)

    # API Keys v2: rotating Gemini key pool (one run shares exhaustion
    # state across analyze / continuation / fill / post-kit calls).
    # Warnings are collected here; the caller surfaces them via
    # on_quota_warning (the UI shows a non-fatal banner per rotation).
    _quota_warnings: List[str] = []

    def _on_key_exhausted(key_num: int) -> None:
        _msg = (f"Key {key_num} quota ကုန်သွားလို့ "
                f"Key {key_num + 1} နဲ့ ဆက်လုပ်နေပါတယ်")
        _quota_warnings.append(_msg)
        if on_quota_warning is not None:
            try:
                on_quota_warning(_msg)
            except Exception:
                pass

    _key_pool = GeminiKeyPool(user_api_key, on_exhausted=_on_key_exhausted)

    def _gcall(fn, *args, **kwargs):
        """Run a Gemini analyzer fn through the rotating key pool."""
        return _key_pool.run(fn, *args, **kwargs)

    # 1) ingest
    _report("analyze", 0.0, "ဗီဒီယို ရယူနေသည်…")
    if hybrid_pack:
        # No server-side video: the browser holds the source (it is
        # NEVER uploaded to Streamlit in Hybrid mode). Metadata comes
        # from the browser (local file) or the demo sample (mock runs).
        if video_duration is None or float(video_duration) <= 0:
            raise PipelineError("Hybrid: video_duration လိုအပ်ပါသည်။")
        local_path = None
        metadata = {
            "duration": float(video_duration),
            "width": int(src_width or 0),
            "height": int(src_height or 0),
            "fps": None,
            # The device probes the real value before encoding.
            "has_audio": True,
            "file_size_mb": 0.0,
        }
    else:
        local_path, metadata = prepare_video_source(
            uploaded_file_path=video_path, video_url=video_url,
            work_dir=work_dir,
        )

    # Disk guard (part 2): estimate what THIS run will newly write —
    # output (~0.4 MB/s at ~3.2 Mbps, crf 20) + intermediates + margin.
    # A fixed threshold misfires (a 20s test needs ~150MB, an 8-min
    # video ~350MB), so scale by duration. Fail fast, not mid-render.
    # Hybrid skips it: the server writes only the pack (~15MB), never
    # a full render.
    if not hybrid_pack:
        try:
            _dur = float(metadata.get("duration") or 0)
            _need_mb = _dur * 0.4 + 150.0
            _free2_mb = shutil.disk_usage(work_dir).free / (1024 * 1024)
            if _free2_mb < _need_mb:
                raise PipelineError(
                    "Server မှာ နေရာလွတ် မလောက်တော့ပါ "
                    f"({_free2_mb:.0f}MB သာ ကျန်သည်၊ "
                    f"ဒီ video အတွက် ~{_need_mb:.0f}MB လိုသည်) — "
                    "ခဏစောင့်ပြီး Run ကို ထပ်နှိပ်ပေးပါ။"
                )
        except PipelineError:
            raise
        except Exception:
            pass

    # 2) recap plan (live AI or mock / prebuilt) — mode-aware.
    # Plans are re-validated here so even a directly-passed plan gets
    # the tiling guarantee (highlights/blocks cover [0, duration]).
    _report("analyze", 0.0, "AI က ဗီဒီယို စိစစ်နေသည်…")
    _video_dur = metadata["duration"] or 0
    # True only when the plan came from live Gemini (not mock, not a
    # caller-passed prebuilt plan) — only then may the pipeline call
    # Gemini again for the Pro fill.
    _live_ai_plan = (plan is None and not use_mock_plan)
    if plan is None:
        if processing_mode == "pro_ai_recap":
            if use_mock_plan:
                plan = generate_mock_pro_plan(_video_dur)
            else:
                plan = _gcall(
                    analyze_video_pro_blocks,
                    local_path, model=model,
                    gemini_file_name=gemini_file_name,
                    video_duration=_video_dur,
                    delete_remote=False)
        else:
            if use_mock_plan:
                plan = generate_mock_recap_plan(_video_dur)
            else:
                plan = _gcall(
                    analyze_video_for_recap,
                    local_path,
                    target_duration=target_duration, style=style,
                    model=model,
                    gemini_file_name=gemini_file_name,
                    video_duration=_video_dur,
                    delete_remote=False,
                )
    if processing_mode == "pro_ai_recap":
        plan = validate_pro_blocks(plan, _video_dur)
    else:
        plan = validate_recap_plan(plan, _video_dur)
    _report("analyze", 1.0, "AI စိစစ်မှု ပြီးပါပြီ")
    title = plan.get("title", "")

    if processing_mode == "pro_ai_recap":
        # 3-4) Pro: per-block TTS (or measure prebuilt block audio)
        blocks = plan.get("blocks") or []
        if not blocks:
            raise PipelineError("Pro blocks သည် အလွတ်ဖြစ်နေပါသည်။")
        # 2b) Fill empty blocks (Fix 2): blocks whose text came back
        # empty would render as silence — Myanmar speech "comes and
        # goes". Ask Gemini once for narration for exactly those
        # windows (dubbing, or a brief action narration when nobody
        # speaks), then re-validate so the tiling holds.
        if _live_ai_plan:
            _empty = [(float(b.get("start", 0)), float(b.get("end", 0)))
                      for b in blocks
                      if not str(b.get("text", "")).strip()]
            if _empty:
                try:
                    # Batched fill: one giant call for ALL empty windows
                    # returns truncated / re-segmented JSON that
                    # matches nothing (measured live 2026-10-17:
                    # 2:37/8:04). Small batches return complete JSON.
                    _filled_blocks = []
                    for _i in range(0, len(_empty), PRO_FILL_BATCH):
                        try:
                            _filled = _gcall(
                                analyze_video_pro_fill,
                                local_path,
                                _empty[_i:_i + PRO_FILL_BATCH], model=model,
                                gemini_file_name=gemini_file_name,
                                delete_remote=False)
                            _filled_blocks.extend(
                                _filled.get("blocks", []) or [])
                        except Exception:
                            continue
                    _fill_blocks_by_overlap(blocks, _filled_blocks)
                    plan = validate_pro_blocks(
                        {"title": title, "blocks": blocks}, _video_dur)
                    blocks = plan.get("blocks") or []
                except Exception:
                    pass
        blocks = generate_block_voiceovers(
            blocks, os.path.join(work_dir, "block_audio"),
            voice_name=voice_name, rate=rate, proxy=tts_proxy,
            pitch=pitch,
            on_progress=lambda f: _report(
                "tts", f, "မြန်မာအသံ ထုတ်နေသည်…"))
        # Coverage fix v3 — Pro under-filled detection: a block can
        # have non-empty text yet still render mostly silence, because
        # the Pro prompt used to push Gemini toward very brief text
        # ("အလွန်တိုတို"). After the first TTS pass, flag blocks
        # whose measured audio is <60% of their window as
        # under-filled, ask Gemini once for expanded narration for
        # exactly those windows, and re-synthesize only the updated
        # blocks. The empty-block fill above handles blocks with NO
        # text; this handles blocks with TOO-SHORT text.
        _pro_fill_status = {"underfilled_found": 0, "filled": 0,
                            "unmatched": 0, "error": None}
        if _live_ai_plan:
            _under = _find_underfilled_blocks(blocks, 0.60)
            _pro_fill_status["underfilled_found"] = len(_under)
            if _under:
                try:
                    _exp_blocks = []
                    for _i in range(0, len(_under), PRO_FILL_BATCH):
                        try:
                            _exp = _gcall(
                                analyze_video_pro_fill,
                                local_path,
                                _under[_i:_i + PRO_FILL_BATCH],
                                model=model,
                                gemini_file_name=gemini_file_name,
                                delete_remote=False)
                            _exp_blocks.extend(
                                _exp.get("blocks", []) or [])
                        except Exception:
                            continue
                    # Snapshot text before overlap fill, so we know
                    # which blocks actually got expanded text.
                    _before = [str(_b.get("text", "")) for _b in blocks]
                    _n_filled = _fill_blocks_by_overlap(
                        blocks, _exp_blocks)
                    _pro_fill_status["filled"] = _n_filled
                    _pro_fill_status["unmatched"] = max(
                        0, len(_under) - _n_filled)
                    if _n_filled > 0:
                        # Clear audio_path for blocks whose text
                        # changed, so the second TTS pass re-synthesizes
                        # only those (blocks with an existing audio_path
                        # are measured, not re-synthesized).
                        for _j, _b in enumerate(blocks):
                            if str(_b.get("text", "")) != _before[_j]:
                                _b.pop("audio_path", None)
                                _b.pop("audio_len", None)
                        blocks = generate_block_voiceovers(
                            blocks,
                            os.path.join(work_dir, "block_audio"),
                            voice_name=voice_name, rate=rate,
                            proxy=tts_proxy, pitch=pitch,
                            on_progress=lambda f: _report(
                                "tts", f,
                                "မြန်မာအသံ ထပ်ထုတ်နေသည်…"))
                except Exception as _e:
                    _pro_fill_status["error"] = str(_e)[:200]
        script_text = plan.get("burmese_script", "") or " ".join(
            str(b.get("text", "")) for b in blocks).strip()
        sync = None
        audio_duration = sum(b["audio_len"] for b in blocks)
        # Coverage counts only blocks with real narration text — the
        # 0.5s silence placeholders for empty blocks are not speech.
        _pro_covered = sum(
            b["audio_len"] for b in blocks
            if str(b.get("text", "")).strip())

        # Hybrid: no server render — pre-mix the block audios into ONE
        # track (speech on the dialogue windows, silence elsewhere),
        # rebuild the SRT on the original timeline, build the pack.
        if hybrid_pack:
            _report("pack", 0.0, "Hybrid pack ထုတ်နေသည်…")
            _postkit = _postkit_for_run(
                title, script_text, _key_pool, groq_api_key, model,
                use_mock_plan,
                postkit_extra_hashtags, postkit_append_fyp)
            _mixed = mix_pro_block_audio(
                blocks, work_dir,
                os.path.join(work_dir, "voiceover_mixed.mp3"))
            _pro_srt_path = os.path.join(work_dir, "pro_subtitles.srt")
            build_pro_srt(blocks, _pro_srt_path)
            _pack = _build_hybrid_pack(
                run_dir=work_dir, processing_mode="pro_ai_recap",
                title=title, script_text=script_text,
                sync_method="pro_blocks",
                narration_coverage=_narration_coverage(
                    _pro_covered, _video_dur),
                aspect_ratio=aspect_ratio,
                video_duration=_video_dur,
                src_w=metadata.get("width") or 0,
                src_h=metadata.get("height") or 0,
                fit_mode=fit_mode, crop_align=crop_align,
                inner_scale=inner_scale, vertical_pos=vertical_pos,
                horizontal_pos=horizontal_pos,
                mirror_horizontal=mirror_horizontal,
                copyright_zoom=copyright_zoom,
                random_crop_offset=random_crop_offset,
                bg_volume=bg_volume, blur_boxes=blur_boxes,
                subtitle_config=subtitle_config,
                burn_subtitles=burn_subtitles,
                voiceover_path=_mixed, srt_path=_pro_srt_path,
                extra={"pro_fill_status": _pro_fill_status,
                       "pitch": pitch},
                quality=quality, fps=fps, output_name=output_name,
                watermark=watermark, logo=logo, postkit=_postkit,
            )
            _report("pack", 1.0, "Hybrid pack အဆင်သင့်")
            if gemini_file_name:
                try:
                    delete_gemini_file(_key_pool.first_key(),
                                       gemini_file_name)
                except Exception:
                    pass
            return {
                "hybrid_pack": True,
                "pack_dir": work_dir,
                "pack": _pack,
                "title": title,
                "script_text": script_text,
                "subtitle_mode": ("burned_in" if burn_subtitles
                                  else "soft"),
                "sync_method": "pro_blocks",
                "narration_coverage": _narration_coverage(
                    _pro_covered, _video_dur),
                "quota_warnings": list(_quota_warnings),
                "video_speed_factor": None,
                "aspect_ratio": aspect_ratio,
                "processing_mode": "pro_ai_recap",
                "pro_fill_status": _pro_fill_status,
            }

        # 5) Pro render (blocks define the timeline + synced subtitles)
        final_path = os.path.join(output_dir, output_name)
        scfg = dict(subtitle_config or {})
        scfg["enabled"] = bool(burn_subtitles)
        _report("render", 0.0, "ဗီဒီယို ပေါင်းနေသည်…")
        render = render_pro_recap_video(
            local_path, blocks,
            output_path=final_path,
            aspect_ratio=aspect_ratio,
            fit_mode=fit_mode,
            crop_align=crop_align,
            inner_scale=inner_scale,
            vertical_pos=vertical_pos,
            horizontal_pos=horizontal_pos,
            mirror_horizontal=mirror_horizontal,
            copyright_zoom=copyright_zoom,
            random_crop_offset=random_crop_offset,
            bg_volume=bg_volume,
            work_dir=work_dir,
            blur_boxes=blur_boxes,
            subtitle_config=scfg,
            srt_path=os.path.join(output_dir, "subtitles.srt"),
            on_progress=lambda f: _report(
                "render", f, "ဗီဒီယို ပေါင်းနေသည်…"),
            quality=quality, fps=fps,
            watermark=watermark, logo=logo,
        )
        _report("render", 1.0, "ဗီဒီယို ပေါင်းတာ ပြီးပါပြီ")
        final_srt = render.get("srt_path")
        result = {
            "final_video_path": render["video_path"],
            "srt_path": final_srt,
            "script_text": script_text,
            "title": title,
            "metadata": {
                "duration": render["duration"],
                "width": render["width"],
                "height": render["height"],
                "has_audio": render["has_audio"],
                "file_size_mb": render["file_size_mb"],
            },
            "subtitle_mode": render["subtitle_mode"],
            "sync_method": "pro_blocks",
            "narration_coverage": _narration_coverage(
                _pro_covered, _video_dur),
            "quota_warnings": list(_quota_warnings),
            "video_speed_factor": None,
            "aspect_ratio": aspect_ratio,
            "processing_mode": "pro_ai_recap",
            "pro_fill_status": _pro_fill_status,
            "blocks": render.get("blocks"),
            "blur_boxes_applied": render.get("blur_boxes_applied", 0),
            "subtitle_position": render.get("subtitle_position"),
            "copyright_applied": render.get("copyright_applied"),
            "fit_mode": render.get("fit_mode"),
        }
        result = _enrich_server_result(
            result, work_dir, _key_pool, groq_api_key, model, use_mock_plan,
            postkit_extra_hashtags, postkit_append_fyp)
    else:
        # AI Recap = Narration Master, FULL duration. The narration
        # covers the whole video; the video keeps its ORIGINAL timing
        # (speed 1.0, no cuts); the AUDIO is fitted to the video.
        script_text = plan.get("burmese_script", "")
        if not script_text.strip():
            raise PipelineError("burmese_script သည် အလွတ်ဖြစ်နေပါသည်။")
        highlights = plan.get("highlights", []) or []
        if not highlights:
            highlights = [{"start": 0.0, "end": float(_video_dur),
                           "description": ""}]

        # 3) voiceover + subtitles. TTS rate ladder (same idea as Pro):
        # if the narration is longer than the video, retry faster
        # (+10%, then +20%) so the video never has to wait/slow.
        # Single-voiceover TTS reports phase start/end only, and only
        # when it actually synthesizes (prebuilt voiceover skips TTS,
        # preserving the ["analyze", "render"] phase contract).
        _tts_rate_used = rate
        _auto_assets = voiceover_path is None
        if _auto_assets:
            _report("tts", 0.0, "မြန်မာအသံ ထုတ်နေသည်…")
            voiceover_path = os.path.join(output_dir, "voiceover.mp3")
            auto_srt = os.path.join(output_dir, "subtitles.srt")
            _ladder = []
            for _r in (rate, "+10%", "+20%"):
                try:
                    _v = int(str(_r).strip().rstrip("%"))
                    _base = int(str(rate).strip().rstrip("%"))
                except ValueError:
                    _v, _base = 0, 0
                if _v >= _base and _r not in _ladder:
                    _ladder.append(_r)
            audio_duration = 0.0
            for _try_rate in _ladder:
                audio_duration = generate_burmese_voiceover(
                    script_text, voiceover_path, auto_srt,
                    voice_name=voice_name, rate=_try_rate,
                    proxy=tts_proxy, pitch=pitch,
                )
                _tts_rate_used = _try_rate
                if _video_dur <= 0 or audio_duration <= _video_dur:
                    break
            srt_path = auto_srt
        else:
            if not os.path.isfile(voiceover_path):
                raise FileNotFoundError(
                    f"voiceover not found: {voiceover_path}")
            audio_duration = measure_audio_duration(voiceover_path)
            if audio_duration <= 0:
                raise PipelineError(
                    "voiceover အသံဖိုင် ကြာချိန် တိုင်းတာ၍ မရပါ။")
        if _auto_assets:
            _report("tts", 1.0, "အသံ ထုတ်တာ ပြီးပါပြီ")

        # 3b) Narration coverage (Fix 1): the first script sometimes
        # ends far before the video ends (measured: 8:04 video,
        # narration ended at 1:43) — the old code padded the rest with
        # silence and built subtitles only for the short script, so
        # speech + subtitles never reached the end. If the measured
        # narration covers < 90% of the video, ask Gemini for a
        # CONTINUATION covering the uncovered tail, synthesize it, and
        # append its audio + subtitles (offset). Up to
        # NARRATION_RETRY_MAX rounds. Skipped for mock plans (test
        # fixtures) and for prebuilt voiceovers (the caller owns them).
        #
        # Coverage fix v3: after each continuation, count words — if
        # Gemini delivered <50% of the requested words, re-ask once
        # with an explicit "too short, expand" prompt. Record
        # rounds_used + cap_hit + a per-round log (words, added
        # seconds, coverage after) so a run that ends below 90% shows
        # WHY (cap hit vs stall vs call failure) instead of silently
        # passing as a normal success.
        _cont_log = []
        _cont_cap_hit = False
        _cont_error = None
        if _auto_assets and not use_mock_plan:
            for _cont in range(NARRATION_RETRY_MAX):
                if _video_dur <= 0 or \
                        audio_duration >= NARRATION_COVERAGE_TARGET * _video_dur:
                    break
                try:
                    _tail = _gcall(
                        analyze_video_continuation,
                        local_path,
                        from_time=audio_duration,
                        video_duration=_video_dur,
                        style=style, model=model,
                        gemini_file_name=gemini_file_name,
                        delete_remote=False)
                    _cont_text = str(
                        _tail.get("burmese_script", "")).strip()
                except Exception as _e:
                    _cont_error = str(_e)[:200]
                    break
                if not _cont_text:
                    _cont_error = "empty continuation text"
                    break
                # Word-count check: if Gemini delivered <50% of the
                # requested words for this tail, re-ask once with the
                # expand prompt. Use the longer of the two texts.
                _tail_dur = max(0.0, _video_dur - audio_duration)
                _expected_words = int(_tail_dur * 2.2)
                _delivered_words = len(_cont_text.split())
                _reasked = False
                if _expected_words > 0 and \
                        _delivered_words < 0.5 * _expected_words:
                    try:
                        _tail2 = _gcall(
                            analyze_video_continuation,
                            local_path,
                            from_time=audio_duration,
                            video_duration=_video_dur,
                            style=style, model=model,
                            delivered_words=_delivered_words,
                            gemini_file_name=gemini_file_name,
                            delete_remote=False)
                        _cont_text2 = str(
                            _tail2.get("burmese_script", "")).strip()
                        _reasked = True
                        if len(_cont_text2.split()) > _delivered_words:
                            _cont_text = _cont_text2
                            _delivered_words = len(
                                _cont_text.split())
                    except Exception:
                        # Re-ask failed — keep the first text.
                        pass
                _cont_audio = os.path.join(
                    work_dir, f"voiceover_cont_{_cont}.mp3")
                _cont_srt = os.path.join(
                    work_dir, f"voiceover_cont_{_cont}.srt")
                _cont_len = generate_burmese_voiceover(
                    _cont_text, _cont_audio, _cont_srt,
                    voice_name=voice_name, rate=_tts_rate_used,
                    proxy=tts_proxy, pitch=pitch)
                if _cont_len <= 0 or not os.path.isfile(_cont_audio):
                    _cont_error = "continuation TTS produced no audio"
                    break
                _merged_audio = os.path.join(
                    work_dir, f"voiceover_merged_{_cont}.mp3")
                _concat_audios([voiceover_path, _cont_audio], _merged_audio)
                _merged_srt = os.path.join(
                    work_dir, f"subtitles_merged_{_cont}.srt")
                _offset_srt_timestamps(
                    _cont_srt, audio_duration, _merged_srt)
                # Append the continuation cues after the first SRT's.
                with open(srt_path, "a", encoding="utf-8") as _f:
                    _f.write(open(_merged_srt, encoding="utf-8").read())
                # Renumber cue indices across the merged file (the
                # appended block restarts at 1).
                _renumber_srt(srt_path)
                voiceover_path = _merged_audio
                script_text = (script_text + " " + _cont_text).strip()
                audio_duration = audio_duration + _cont_len
                _cont_log.append({
                    "round": _cont,
                    "words": _delivered_words,
                    "expected_words": _expected_words,
                    "reasked": _reasked,
                    "added_s": round(_cont_len, 1),
                    "coverage_after_s": round(audio_duration, 1),
                })
                # A round that adds almost nothing means Gemini has
                # stalled on the tail — stop instead of burning calls.
                if _cont_len < NARRATION_MIN_ROUND_S:
                    break
            # Cap hit: the loop ran all NARRATION_RETRY_MAX rounds
            # without reaching the 90% target — the run ends below
            # target because the round budget ran out, not because
            # Gemini stalled or a call failed.
            if len(_cont_log) >= NARRATION_RETRY_MAX and _video_dur > 0 \
                    and audio_duration < \
                    NARRATION_COVERAGE_TARGET * _video_dur:
                _cont_cap_hit = True

        # 4) fit the AUDIO to the full video duration (video stays 1.0x):
        # pad silence if shorter; atempo (+ scaled SRT) if longer.
        fitted_audio, fitted_srt, audio_fit = fit_audio_to_duration(
            voiceover_path, srt_path, _video_dur, work_dir)

        # Hybrid: no server render — build the pack and return. The
        # device encodes from the pack (source + voiceover + SRT +
        # filter graph). The shared Gemini file is deleted once here,
        # after ALL analysis calls for this run.
        if hybrid_pack:
            _report("pack", 0.0, "Hybrid pack ထုတ်နေသည်…")
            _postkit = _postkit_for_run(
                title, script_text, _key_pool, groq_api_key, model,
                use_mock_plan,
                postkit_extra_hashtags, postkit_append_fyp)
            _pack = _build_hybrid_pack(
                run_dir=work_dir, processing_mode="ai_recap",
                title=title, script_text=script_text,
                sync_method="full_narration",
                narration_coverage=_narration_coverage(
                    audio_duration, _video_dur),
                aspect_ratio=aspect_ratio,
                video_duration=_video_dur,
                src_w=metadata.get("width") or 0,
                src_h=metadata.get("height") or 0,
                fit_mode=fit_mode, crop_align=crop_align,
                inner_scale=inner_scale, vertical_pos=vertical_pos,
                horizontal_pos=horizontal_pos,
                mirror_horizontal=mirror_horizontal,
                copyright_zoom=copyright_zoom,
                random_crop_offset=random_crop_offset,
                bg_volume=bg_volume, blur_boxes=blur_boxes,
                subtitle_config=subtitle_config,
                burn_subtitles=burn_subtitles,
                voiceover_path=fitted_audio, srt_path=fitted_srt,
                extra={
                    "tts_rate": _tts_rate_used,
                    "pitch": pitch,
                    "audio_fit": audio_fit,
                    "continuation_rounds": len(_cont_log),
                    "continuation_cap_hit": _cont_cap_hit,
                    "continuation_log": _cont_log,
                    "continuation_error": _cont_error,
                },
                quality=quality, fps=fps, output_name=output_name,
                watermark=watermark, logo=logo, postkit=_postkit,
            )
            _report("pack", 1.0, "Hybrid pack အဆင်သင့်")
            if gemini_file_name:
                try:
                    delete_gemini_file(_key_pool.first_key(),
                                       gemini_file_name)
                except Exception:
                    pass
            return {
                "hybrid_pack": True,
                "pack_dir": work_dir,
                "pack": _pack,
                "title": title,
                "script_text": script_text,
                "subtitle_mode": ("burned_in" if burn_subtitles
                                  else "soft"),
                "sync_method": "full_narration",
                "narration_coverage": _narration_coverage(
                    audio_duration, _video_dur),
            "quota_warnings": list(_quota_warnings),
                "video_speed_factor": 1.0,
                "aspect_ratio": aspect_ratio,
                "processing_mode": "ai_recap",
                "tts_rate": _tts_rate_used,
                "audio_fit": audio_fit,
                "continuation_rounds": len(_cont_log),
                "continuation_cap_hit": _cont_cap_hit,
                "continuation_log": _cont_log,
                "continuation_error": _cont_error,
            }

        # 5) render — full footage (highlights tile the whole video),
        # video speed exactly 1.0, narration as master.
        final_path = os.path.join(output_dir, output_name)
        _report("render", 0.0, "ဗီဒီယို ပေါင်းနေသည်…")
        render = render_recap_video(
            local_path,
            highlights,
            fitted_audio,
            srt_path=fitted_srt if burn_subtitles else None,
            output_path=final_path,
            aspect_ratio=aspect_ratio,
            video_speed_factor=1.0,
            bg_volume=bg_volume,
            work_dir=work_dir,
            blur_boxes=blur_boxes,
            subtitle_config=subtitle_config,
            fit_mode=fit_mode,
            crop_align=crop_align,
            inner_scale=inner_scale,
            vertical_pos=vertical_pos,
            horizontal_pos=horizontal_pos,
            mirror_horizontal=mirror_horizontal,
            copyright_zoom=copyright_zoom,
            random_crop_offset=random_crop_offset,
            on_progress=lambda f: _report(
                "render", f, "ဗီဒီယို ပေါင်းနေသည်…"),
            quality=quality, fps=fps,
            watermark=watermark, logo=logo,
        )
        _report("render", 1.0, "ဗီဒီယို ပေါင်းတာ ပြီးပါပြီ")
        final_srt = render.get("sidecar_srt") or fitted_srt
        result = {
            "final_video_path": render["video_path"],
            "srt_path": final_srt,
            "script_text": script_text,
            "title": title,
            "metadata": {
                "duration": render["duration"],
                "width": render["width"],
                "height": render["height"],
                "has_audio": render["has_audio"],
                "file_size_mb": render["file_size_mb"],
            },
            "subtitle_mode": render["subtitle_mode"],
            "sync_method": "full_narration",
            "narration_coverage": _narration_coverage(
                audio_duration, _video_dur),
            "quota_warnings": list(_quota_warnings),
            "video_speed_factor": 1.0,
            "aspect_ratio": aspect_ratio,
            "processing_mode": "ai_recap",
            "blur_boxes_applied": render.get("blur_boxes_applied", 0),
            "subtitle_position": render.get("subtitle_position"),
            "copyright_applied": render.get("copyright_applied"),
            "fit_mode": render.get("fit_mode"),
            "tts_rate": _tts_rate_used,
            "audio_fit": audio_fit,
            # Coverage fix v3 diagnostics: how many continuation
            # rounds ran, whether the round budget ran out below
            # target, per-round words/coverage, and any error that
            # ended the loop early.
            "continuation_rounds": len(_cont_log),
            "continuation_cap_hit": _cont_cap_hit,
            "continuation_log": _cont_log,
            "continuation_error": _cont_error,
        }
        result = _enrich_server_result(
            result, work_dir, _key_pool, groq_api_key, model, use_mock_plan,
            postkit_extra_hashtags, postkit_append_fyp)

    # 6) cleanup temp fragments (keep final video + srt + voiceover)
    for name in ("segments",):
        shutil.rmtree(os.path.join(work_dir, name), ignore_errors=True)
    try:
        if local_path and os.path.abspath(local_path).startswith(
                os.path.abspath(work_dir)):
            os.remove(local_path)
    except OSError:
        pass

    return result
