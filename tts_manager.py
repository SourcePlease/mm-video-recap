"""
tts_manager.py — Step 4: Myanmar Voiceover, Auto-Subtitles & Timing Sync Module.

- generate_burmese_voiceover(script_text, output_audio_path,
    output_srt_path=None, voice_name="my-MM-ThihaNeural", rate="+0%", proxy=None)
    Uses keyless Edge-TTS to render Myanmar speech (MP3) and, in the same
    pass, word-timed subtitles (SRT) via SubMaker. Returns the real audio
    duration in seconds (measured with ffprobe).
    The optional ``proxy`` parameter exists only so this dev VM (which routes
    outbound traffic through an egress proxy) can reach the TTS service;
    production callers leave it as None.
- sync_highlights_with_audio(highlights, audio_duration, source_video_duration)
    Aligns highlight clips with the voiceover length: first tries to stretch/
    shrink clip ends proportionally inside the source video; otherwise returns
    a video_speed_factor for the FFmpeg stage.

No API keys are used or stored anywhere in this module (Edge-TTS is keyless).
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from typing import Callable, Dict, List, Optional

import edge_tts
from edge_tts import SubMaker

# The two Myanmar voices offered to the user.
MYANMAR_VOICES = {
    "male": "my-MM-ThihaNeural",
    "female": "my-MM-NilarNeural",
}

_RATE_RE = re.compile(r"^[+-]?\d+%$")
_PITCH_RE = re.compile(r"^[+-]?\d+Hz$")


def format_pitch(pitch_hz: int) -> str:
    """Format a slider value (-10..+10) as an Edge-TTS pitch string.

    0 -> "+0Hz" (flat), +5 -> "+5Hz", -5 -> "-5Hz".
    """
    return f"{int(pitch_hz):+d}Hz"


class TTSManagerError(Exception):
    """Raised for voiceover / subtitle generation failures."""


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #
def measure_audio_duration(audio_path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise TTSManagerError(
            f"Could not measure audio duration: {proc.stderr.strip()[:200]}"
        )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise TTSManagerError("ffprobe returned an invalid duration.")


async def _synthesize(
    text: str,
    audio_path: str,
    srt_path: Optional[str],
    voice_name: str,
    rate: str,
    proxy: Optional[str],
    pitch: str = "+0Hz",
) -> None:
    communicate = edge_tts.Communicate(
        text, voice_name, rate=rate, pitch=pitch,
        boundary="WordBoundary", proxy=proxy
    )
    submaker = SubMaker()
    out_dir = os.path.dirname(os.path.abspath(audio_path))
    os.makedirs(out_dir, exist_ok=True)

    with open(audio_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            ctype = chunk["type"]
            if ctype == "audio":
                audio_file.write(chunk["data"])
            elif ctype in ("WordBoundary", "SentenceBoundary") and srt_path:
                submaker.feed(chunk)

    if srt_path:
        srt_text = submaker.get_srt()
        if not srt_text.strip():
            raise TTSManagerError(
                "စာတန်းထိုး (SRT) ထုတ်လုပ်ရာတွင် အမှားဖြစ်ပေါ်နေပါသည်။"
            )
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_text)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def generate_burmese_voiceover(
    script_text: str,
    output_audio_path: str,
    output_srt_path: Optional[str] = None,
    voice_name: str = "my-MM-ThihaNeural",
    rate: str = "+0%",
    proxy: Optional[str] = None,
    pitch: str = "+0Hz",
) -> float:
    """Render Myanmar voiceover MP3 (+ optional SRT); return audio seconds.

    Args:
        script_text: Myanmar narration script (required, non-empty).
        output_audio_path: where to write the MP3.
        output_srt_path: where to write the SRT (skipped if None).
        voice_name: "my-MM-ThihaNeural" (male) or "my-MM-NilarNeural" (female).
        rate: speech rate like "+0%", "+20%", "-10%".
        proxy: optional proxy URL (dev-VM use only; default None).
        pitch: voice tone like "+0Hz" (flat), "+5Hz" (higher),
            "-5Hz" (lower). Independent of rate.
    """
    text = (script_text or "").strip()
    if not text:
        raise ValueError("script_text သည် အလွတ်ဖြစ်နေပါသည်။")
    if not _RATE_RE.match(rate or ""):
        raise ValueError(
            f"rate ပုံစံမမှန်ပါ: {rate!r} (ဥပမာ '+0%', '+20%', '-10%')"
        )
    if not _PITCH_RE.match(pitch or ""):
        raise ValueError(
            f"pitch ပုံစံမမှန်ပါ: {pitch!r} (ဥပမာ '+0Hz', '+5Hz', '-5Hz')"
        )

    asyncio.run(
        _synthesize(text, output_audio_path, output_srt_path,
                    voice_name, rate, proxy, pitch)
    )

    if not os.path.isfile(output_audio_path) or os.path.getsize(output_audio_path) == 0:
        raise TTSManagerError("အသံဖိုင် ထုတ်လုပ်မှု မအောင်မြင်ပါ။")
    return measure_audio_duration(output_audio_path)


def generate_block_voiceovers(
    blocks: List[Dict],
    out_dir: str,
    voice_name: str = "my-MM-ThihaNeural",
    rate: str = "+0%",
    proxy: Optional[str] = None,
    pitch: str = "+0Hz",
    # Progress callback: called with 0..1 after each block finishes.
    on_progress: Optional[Callable[[float], None]] = None,
) -> List[Dict]:
    """Synthesize one Myanmar voiceover per Pro block (Step 15).

    Each input block is {"start","end","text", optional "audio_path"}.
    A block that already has an existing "audio_path" is only measured
    (not re-synthesized) — this is how offline tests inject prebuilt
    audio. A block with empty text gets 0.5s of silence so it still has
    an audio length for the sync math.

    Rate ladder (Pro dubbing concept): the VIDEO is never slowed —
    speed is always 1.0 — so the narration must fit its own dialogue
    window. If synthesis at the base ``rate`` runs longer than the
    block, retry faster in steps (+10%, then +20%) and keep the first
    audio that fits the window. Only rates faster than (or equal to)
    the base rate are tried. Returns the blocks with "audio_path" +
    "audio_len" (seconds) + "tts_rate" (the rate actually used) added.
    """
    os.makedirs(out_dir, exist_ok=True)

    def _rate_value(r: str) -> int:
        try:
            return int(str(r).strip().rstrip("%"))
        except ValueError:
            return 0

    _base_val = _rate_value(rate)
    _ladder: List[str] = []
    for _r in (rate, "+10%", "+20%"):
        if _rate_value(_r) >= _base_val and _r not in _ladder:
            _ladder.append(_r)

    out: List[Dict] = []
    for i, b in enumerate(blocks):
        ap = b.get("audio_path")
        used_rate = rate
        if ap and os.path.isfile(str(ap)):
            audio_len = measure_audio_duration(str(ap))
        else:
            ap = os.path.join(out_dir, f"block_{i:03d}.mp3")
            text = str(b.get("text", "")).strip()
            if text:
                try:
                    block_dur = float(b.get("end", 0)) - float(b.get("start", 0))
                except (TypeError, ValueError):
                    block_dur = 0.0
                audio_len = 0.0
                for _try_rate in _ladder:
                    audio_len = generate_burmese_voiceover(
                        text, ap, None,
                        voice_name=voice_name, rate=_try_rate,
                        proxy=proxy, pitch=pitch,
                    )
                    used_rate = _try_rate
                    # Fits the dialogue window — stop, keep this audio.
                    if block_dur <= 0 or audio_len <= block_dur:
                        break
                    # Too long: loop tries the next faster rate. The
                    # render's atempo fit is the last resort if even
                    # +20% does not fit (prompt word-cap should prevent
                    # this for well-formed blocks).
            else:
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-v", "error",
                     "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                     "-t", "0.5", "-c:a", "libmp3lame", "-q:a", "4", ap],
                    capture_output=True, text=True, timeout=60)
                if proc.returncode != 0:
                    raise TTSManagerError(
                        "Silent block audio failed: "
                        f"{proc.stderr.strip()[:200]}")
                audio_len = measure_audio_duration(ap)
        out.append({**b, "audio_path": str(ap),
                    "audio_len": float(audio_len),
                    "tts_rate": used_rate})
        if on_progress is not None:
            on_progress((i + 1) / max(1, len(blocks)))
    return out


def sync_highlights_with_audio(
    highlights: List[Dict],
    audio_duration: float,
    source_video_duration: float,
) -> Dict:
    """Align highlight clips with the voiceover duration.

    LEGACY — the pipeline no longer calls this: AI Recap (full narration)
    and Pro (dialogue dubbing) both keep video speed at exactly 1.0 and
    fit the AUDIO to the video (TTS rate ladder + atempo/pad), never the
    video to the audio. Kept as a public utility for callers that still
    need clip/audio alignment.

    Strategy (က): proportionally stretch/shrink each clip's ``end`` (keeping
    ``start`` fixed) so the clips' total duration matches ``audio_duration`` —
    but only if every adjusted clip still lies inside the source video and
    clips do not overlap.
    Strategy (ခ): otherwise keep the original clips and return a
    ``video_speed_factor`` (= total_clip_duration / audio_duration) for the
    FFmpeg stage (< 1 slows the video down, > 1 speeds it up).

    Returns dict with keys: highlights, total_clip_duration, audio_duration,
    video_speed_factor, method ("adjust_clips" or "speed_adjust").
    """
    if not highlights:
        raise ValueError("highlights သည် အလွတ်ဖြစ်နေပါသည်။")
    audio_duration = float(audio_duration)
    if audio_duration <= 0:
        raise ValueError("audio_duration သည် 0 ထက် ကြီးရမည်။")
    source_video_duration = float(source_video_duration)

    clips = [
        {
            "start": float(h["start"]),
            "end": float(h["end"]),
            "description": str(h.get("description", "")),
        }
        for h in highlights
    ]
    total_clip = sum(c["end"] - c["start"] for c in clips)
    if total_clip <= 0:
        raise ValueError("highlight ကြာချိန်များ မမှန်ကန်ပါ။")

    scale = audio_duration / total_clip

    # --- strategy (က): proportional end adjustment ------------------------ #
    adjusted: List[Dict] = []
    feasible = True
    for i, c in enumerate(clips):
        new_end = c["start"] + (c["end"] - c["start"]) * scale
        new_end = round(new_end, 3)
        if not (c["start"] < new_end <= source_video_duration + 1e-6):
            feasible = False
            break
        if i + 1 < len(clips) and new_end > clips[i + 1]["start"] + 1e-6:
            feasible = False  # would overlap the next clip
            break
        adjusted.append(
            {
                "start": round(c["start"], 3),
                "end": min(new_end, round(source_video_duration, 3)),
                "description": c["description"],
            }
        )

    if feasible:
        total = round(sum(c["end"] - c["start"] for c in adjusted), 3)
        return {
            "highlights": adjusted,
            "total_clip_duration": total,
            "audio_duration": round(audio_duration, 3),
            "video_speed_factor": 1.0,
            "method": "adjust_clips",
        }

    # --- strategy (ခ): keep clips, let FFmpeg change video speed ----------- #
    original = [
        {
            "start": round(c["start"], 3),
            "end": round(c["end"], 3),
            "description": c["description"],
        }
        for c in clips
    ]
    return {
        "highlights": original,
        "total_clip_duration": round(total_clip, 3),
        "audio_duration": round(audio_duration, 3),
        "video_speed_factor": round(total_clip / audio_duration, 4),
        "method": "speed_adjust",
    }
