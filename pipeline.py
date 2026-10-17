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
    analyze_video_continuation,
    analyze_video_for_recap,
    analyze_video_pro_blocks,
    analyze_video_pro_fill,
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
    _offset_srt_timestamps,
    fit_audio_to_duration,
    render_pro_recap_video,
    render_recap_video,
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


def run_full_recap_pipeline(
    video_path: Optional[str] = None,
    video_url: Optional[str] = None,
    user_api_key: Optional[str] = None,
    target_duration: int = 30,
    style: str = DEFAULT_STYLE,
    voice_name: str = "my-MM-ThihaNeural",
    rate: str = "+0%",
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
    # True only when the plan came from live Gemini (not mock, not a
    # caller-passed prebuilt plan) — only then may the pipeline call
    # Gemini again for the Pro fill.
    _live_ai_plan = (plan is None and not use_mock_plan)
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
                            _filled = analyze_video_pro_fill(
                                local_path, user_api_key,
                                _empty[_i:_i + PRO_FILL_BATCH], model=model)
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
                            _exp = analyze_video_pro_fill(
                                local_path, user_api_key,
                                _under[_i:_i + PRO_FILL_BATCH],
                                model=model)
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
                            proxy=tts_proxy,
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
            "narration_coverage": _narration_coverage(
                _pro_covered, _video_dur),
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
                    _tail = analyze_video_continuation(
                        local_path, user_api_key,
                        from_time=audio_duration,
                        video_duration=_video_dur,
                        style=style, model=model)
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
                        _tail2 = analyze_video_continuation(
                            local_path, user_api_key,
                            from_time=audio_duration,
                            video_duration=_video_dur,
                            style=style, model=model,
                            delivered_words=_delivered_words)
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
                    proxy=tts_proxy)
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
            "narration_coverage": _narration_coverage(
                audio_duration, _video_dur),
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

    # 6) cleanup temp fragments (keep final video + srt + voiceover)
    for name in ("segments",):
        shutil.rmtree(os.path.join(work_dir, name), ignore_errors=True)
    try:
        if os.path.abspath(local_path).startswith(os.path.abspath(work_dir)):
            os.remove(local_path)
    except OSError:
        pass

    return result
