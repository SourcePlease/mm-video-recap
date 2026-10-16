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

import os
import shutil
import tempfile
from typing import Callable, Dict, List, Optional

from video_processor import get_video_metadata, prepare_video_source
from ai_analyzer import (
    DEFAULT_STYLE,
    analyze_video_for_recap,
    analyze_video_pro_blocks,
    generate_mock_pro_plan,
    generate_mock_recap_plan,
    validate_pro_blocks,
    validate_recap_plan,
)
from tts_manager import (
    generate_block_voiceovers,
    generate_burmese_voiceover,
    measure_audio_duration,
)
from video_editor import (
    fit_audio_to_duration,
    render_pro_recap_video,
    render_recap_video,
)


class PipelineError(Exception):
    """Raised for pipeline failures."""


def run_full_recap_pipeline(
    video_path: Optional[str] = None,
    video_url: Optional[str] = None,
    user_api_key: Optional[str] = None,
    target_duration: int = 30,
    style: str = DEFAULT_STYLE,
    voice_name: str = "my-MM-ThihaNeural",
    rate: str = "+0%",
    aspect_ratio: str = "16:9",
    bg_volume: float = 0.15,
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
    # 0–30, TTS 30–55, render 55–100. Single calls (Gemini analyze,
    # one-shot TTS) report only phase start/end — no faked creep.
    _PHASES = {"analyze": (0.0, 30.0), "tts": (30.0, 25.0),
               "render": (55.0, 45.0)}

    def _report(phase: str, frac: float, detail: str) -> None:
        if on_progress is None:
            return
        base, span = _PHASES[phase]
        pct = base + span * min(1.0, max(0.0, frac))
        on_progress(round(pct, 1), phase, detail)

    # 1) ingest
    _report("analyze", 0.0, "ဗီဒီယို ရယူနေသည်…")
    local_path, metadata = prepare_video_source(
        uploaded_file_path=video_path, video_url=video_url, work_dir=work_dir
    )

    # Disk guard (part 2): estimate what THIS run will newly write —
    # output (~0.4 MB/s at ~3.2 Mbps, crf 20) + intermediates + margin.
    # A fixed threshold misfires (a 20s test needs ~150MB, an 8-min
    # video ~350MB), so scale by duration. Fail fast, not mid-render.
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
    if plan is None:
        if processing_mode == "pro_ai_recap":
            if use_mock_plan:
                plan = generate_mock_pro_plan(_video_dur)
            else:
                plan = analyze_video_pro_blocks(
                    local_path, user_api_key, model=model)
        else:
            if use_mock_plan:
                plan = generate_mock_recap_plan(_video_dur)
            else:
                plan = analyze_video_for_recap(
                    local_path, user_api_key,
                    target_duration=target_duration, style=style,
                    model=model,
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
        blocks = generate_block_voiceovers(
            blocks, os.path.join(work_dir, "block_audio"),
            voice_name=voice_name, rate=rate, proxy=tts_proxy,
            on_progress=lambda f: _report(
                "tts", f, "မြန်မာအသံ ထုတ်နေသည်…"))
        script_text = plan.get("burmese_script", "") or " ".join(
            str(b.get("text", "")) for b in blocks).strip()
        sync = None
        audio_duration = sum(b["audio_len"] for b in blocks)

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
            "video_speed_factor": None,
            "aspect_ratio": aspect_ratio,
            "processing_mode": "pro_ai_recap",
            "blocks": render.get("blocks"),
            "blur_boxes_applied": render.get("blur_boxes_applied", 0),
            "subtitle_position": render.get("subtitle_position"),
            "copyright_applied": render.get("copyright_applied"),
            "fit_mode": render.get("fit_mode"),
        }
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
                    voice_name=voice_name, rate=_try_rate, proxy=tts_proxy,
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

        # 4) fit the AUDIO to the full video duration (video stays 1.0x):
        # pad silence if shorter; atempo (+ scaled SRT) if longer.
        fitted_audio, fitted_srt, audio_fit = fit_audio_to_duration(
            voiceover_path, srt_path, _video_dur, work_dir)

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
            "video_speed_factor": 1.0,
            "aspect_ratio": aspect_ratio,
            "processing_mode": "ai_recap",
            "blur_boxes_applied": render.get("blur_boxes_applied", 0),
            "subtitle_position": render.get("subtitle_position"),
            "copyright_applied": render.get("copyright_applied"),
            "fit_mode": render.get("fit_mode"),
            "tts_rate": _tts_rate_used,
            "audio_fit": audio_fit,
        }

    # 6) cleanup temp fragments (keep final video + srt + voiceover)
    for name in ("segments",):
        shutil.rmtree(os.path.join(work_dir, name), ignore_errors=True)
    try:
        if os.path.abspath(local_path).startswith(os.path.abspath(work_dir)):
            os.remove(local_path)
    except OSError:
        pass

    return result
