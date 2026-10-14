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
from typing import Dict, List, Optional

from video_processor import get_video_metadata, prepare_video_source
from ai_analyzer import (
    DEFAULT_STYLE,
    analyze_video_for_recap,
    generate_mock_recap_plan,
)
from tts_manager import (
    generate_burmese_voiceover,
    measure_audio_duration,
    sync_highlights_with_audio,
)
from video_editor import render_recap_video


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
) -> Dict:
    """Run the full recap pipeline end to end.

    Set use_mock_plan=True (or pass plan=) to skip the live Gemini call —
    useful for offline testing. Pass voiceover_path=/srt_path= to reuse
    prebuilt voiceover assets instead of re-running TTS.
    tts_proxy is dev-VM only; production leaves it as None.
    """
    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix="video_recap_pipe_")
    os.makedirs(work_dir, exist_ok=True)
    if output_dir is None:
        output_dir = work_dir
    os.makedirs(output_dir, exist_ok=True)

    # 1) ingest
    local_path, metadata = prepare_video_source(
        uploaded_file_path=video_path, video_url=video_url, work_dir=work_dir
    )

    # 2) recap plan (live AI or mock / prebuilt)
    if plan is None:
        if use_mock_plan:
            plan = generate_mock_recap_plan(metadata["duration"] or 0)
        else:
            plan = analyze_video_for_recap(
                local_path, user_api_key,
                target_duration=target_duration, style=style,
                model=model,
            )
    script_text = plan.get("burmese_script", "")
    title = plan.get("title", "")
    if not script_text.strip():
        raise PipelineError("burmese_script သည် အလွတ်ဖြစ်နေပါသည်။")

    # 3) voiceover + subtitles
    auto_assets = False
    if voiceover_path is None:
        auto_assets = True
        voiceover_path = os.path.join(output_dir, "voiceover.mp3")
        auto_srt = os.path.join(output_dir, "subtitles.srt")
        audio_duration = generate_burmese_voiceover(
            script_text, voiceover_path, auto_srt,
            voice_name=voice_name, rate=rate, proxy=tts_proxy,
        )
        srt_path = auto_srt
    else:
        if not os.path.isfile(voiceover_path):
            raise FileNotFoundError(f"voiceover not found: {voiceover_path}")
        audio_duration = measure_audio_duration(voiceover_path)
        if audio_duration <= 0:
            raise PipelineError("voiceover အသံဖိုင် ကြာချိန် တိုင်းတာ၍ မရပါ။")

    # 4) timing sync
    sync = sync_highlights_with_audio(
        plan.get("highlights", []), audio_duration, metadata["duration"] or 0
    )

    # 5) render
    final_path = os.path.join(output_dir, output_name)
    render = render_recap_video(
        local_path,
        sync["highlights"],
        voiceover_path,
        srt_path=srt_path if burn_subtitles else None,
        output_path=final_path,
        aspect_ratio=aspect_ratio,
        video_speed_factor=sync["video_speed_factor"],
        bg_volume=bg_volume,
        work_dir=work_dir,
        blur_boxes=blur_boxes,
        subtitle_config=subtitle_config,
    )

    # 6) cleanup temp fragments (keep final video + srt + voiceover)
    for name in ("segments",):
        shutil.rmtree(os.path.join(work_dir, name), ignore_errors=True)
    try:
        if os.path.abspath(local_path).startswith(os.path.abspath(work_dir)):
            os.remove(local_path)
    except OSError:
        pass

    final_srt = render.get("sidecar_srt") or srt_path
    return {
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
        "sync_method": sync["method"],
        "video_speed_factor": sync["video_speed_factor"],
        "aspect_ratio": aspect_ratio,
        "blur_boxes_applied": render.get("blur_boxes_applied", 0),
        "subtitle_position": render.get("subtitle_position"),
    }
