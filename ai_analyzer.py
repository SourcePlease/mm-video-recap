"""
ai_analyzer.py — Step 3: BYOK AI Video Analyzer & Myanmar Script Module.

Bring-Your-Own-Key design
-------------------------
The caller MUST pass the end user's own free Gemini API key as
``user_api_key``. No developer key is stored anywhere in this module —
no literal key string appears in this file by design.

Public functions
----------------
- analyze_video_for_recap(video_path, user_api_key, target_duration=30,
    style="စိတ်ဝင်စားဖွယ် ဇာတ်လမ်းအကျဉ်းချုပ်", model=None)
    Uploads the video to the user's own Gemini Files storage, waits until it
    is ACTIVE, asks Gemini for a FULL-DURATION narration plan as strict
    JSON (highlights tile the whole video, burmese_script fills the full
    duration — target_duration is IGNORED), validates the timestamps
    against the real video duration, then ALWAYS deletes the remote file
    in a ``finally:`` block so the user's free cloud storage does not fill
    up. ``model`` optionally pins one model ID (tried first); otherwise
    the free-tier MODEL_CANDIDATES are tried in order, then any other
    flash model the key can access (live discovery).
- validate_recap_plan(plan_dict, video_duration)
    Clamps every highlight's start/end into [0, video_duration], drops
    invalid segments, sorts by start time.
- generate_mock_recap_plan(video_duration)
    Offline mock plan (no API key needed) for testing the pipeline stages.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types

from video_processor import get_video_metadata

# Shown to the user when they forget to paste their own free key.
MISSING_KEY_MSG = "ကျေးဇူးပြု၍ သင်၏ Free Gemini API Key ကို ထည့်သွင်းပေးပါ"

DEFAULT_STYLE = "စိတ်ဝင်စားဖွယ် ဇာတ်လမ်းအကျဉ်းချုပ်"

# Preferred models first (verified free-tier, Oct 2026); auto-fallback if a
# model is unavailable for the key. Sources: Google Gemini Developer API
# pricing page snapshots (Oct 2026) — gemini-3.8-flash, gemini-3.6-flash,
# gemini-3.5-flash-lite, gemini-3.1-flash-lite, gemini-3-flash-preview are
# "Free of charge"; gemini-2.5-* remain only for keys that used them before.
# "gemini-flash-latest" is a last-resort alias; harmless if it 404s (skipped).
MODEL_CANDIDATES = [
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
]

# Model-name fragments that must NOT be auto-picked by dynamic discovery
# (image/audio/live/TTS/transcribe/embedding variants can't do this job).
_NON_TEXT_FLASH_HINTS = (
    "image", "audio", "live", "tts", "transcribe", "embedding", "aqa",
)

# Strict JSON schema for the recap plan.
RECAP_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "description": {"type": "string"},
                },
                "required": ["start", "end", "description"],
            },
        },
        "burmese_script": {"type": "string"},
    },
    "required": ["title", "highlights", "burmese_script"],
}

# Strict JSON schema for the Pro AI Recap block plan (Step 15): the whole
# video tiled into sequential dialogue blocks, each with Myanmar text.
PRO_BLOCKS_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "text": {"type": "string"},
                },
                "required": ["start", "end", "text"],
            },
        },
    },
    "required": ["title", "blocks"],
}

# Strict JSON schema for an AI narration CONTINUATION (Fix 1): only the
# burmese_script for the uncovered tail [from_time, video_duration].
CONTINUATION_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "burmese_script": {"type": "string"},
    },
    "required": ["burmese_script"],
}

# Strict JSON schema for the Pro FILL call (Fix 2): Myanmar narration
# text for the specific windows whose blocks came back empty.
PRO_FILL_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "text": {"type": "string"},
                },
                "required": ["start", "end", "text"],
            },
        },
    },
    "required": ["blocks"],
}


class AnalyzerError(Exception):
    """Analyzer failure carrying a user-facing Myanmar message."""


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #
def _require_key(user_api_key: Any) -> str:
    """Return the stripped key or raise a clear Myanmar error."""
    key = (user_api_key or "").strip() if isinstance(user_api_key, str) else ""
    if not key:
        raise AnalyzerError(MISSING_KEY_MSG)
    return key


def _looks_like_model_error(exc: Exception) -> bool:
    """True if another model in the fallback chain is worth trying.

    Covers: model doesn't exist for this key (404/not-found), the key's
    quota for this model is exhausted (429/rate-limit), and the model is
    temporarily overloaded (503/high-demand). A 400-class bad request
    (bad video, bad schema) is NOT matched — it raises immediately.
    """
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "404", "429", "503",
            "not_found", "not found", "unknown model",
            "model is not supported", "invalid model", "not available",
            "rate limit", "rate_limit", "high demand", "overloaded",
            "unavailable",
        )
    )


def _discover_flash_models(client: genai.Client,
                           already_tried: List[str]) -> List[str]:
    """Dynamically find usable flash models via the API's own model list.

    Returns short model IDs (e.g. "gemini-3.8-flash") containing "flash"
    that this key can see, excluding already-tried ones and non-text
    variants (image/audio/live/TTS/...). Never raises — returns [] if the
    list call itself fails.
    """
    try:
        found: List[str] = []
        for m in client.models.list():
            full = getattr(m, "name", "") or ""
            short = full.split("/")[-1]
            low = short.lower()
            if "flash" not in low:
                continue
            if short in already_tried or short in found:
                continue
            if any(h in low for h in _NON_TEXT_FLASH_HINTS):
                continue
            found.append(short)
        return found
    except Exception:
        return []


def _resolve_uploaded(client: genai.Client, path: str,
                      gemini_file_name: Optional[str] = None):
    """Return the Gemini File object for analysis.

    Default (server mode): upload the local file. When
    ``gemini_file_name`` is given (Hybrid mode — the browser uploaded
    the video directly to the Gemini Files API), fetch the
    already-uploaded file instead of uploading again.
    """
    if gemini_file_name:
        return client.files.get(name=str(gemini_file_name))
    return client.files.upload(file=path)


def delete_gemini_file(user_api_key: str, gemini_file_name: str) -> None:
    """Best-effort delete of a Gemini remote file (Hybrid cleanup).

    Never raises — a failed cleanup must not fail the run.
    """
    try:
        _k = _require_key(user_api_key)
        if not gemini_file_name:
            return
        _client = genai.Client(**{"api_key": _k})
        _client.files.delete(name=str(gemini_file_name))
    except Exception:
        pass


def _wait_for_active(client: genai.Client, file_name: str,
                     timeout_s: int = 600, interval_s: int = 5):
    """Poll the uploaded file until its state becomes ACTIVE."""
    deadline = time.time() + timeout_s
    while True:
        remote = client.files.get(name=file_name)
        state = getattr(remote.state, "name", str(remote.state))
        if state == "ACTIVE":
            return remote
        if state == "FAILED":
            raise AnalyzerError(
                "ဗီဒီယိုဖိုင်ကို ပြင်ဆင်ရာတွင် အမှားဖြစ်ပေါ်နေပါသည်။"
            )
        if time.time() > deadline:
            raise AnalyzerError(
                "ဗီဒီယိုဖိုင် အသင့်ဖြစ်ရန် အချိန်ကြာလွန်းနေပါသည်။ "
                "ထပ်မံကြိုးစားကြည့်ပါ။"
            )
        time.sleep(interval_s)


def _build_prompt(video_duration: float, style: str) -> str:
    # AI Recap = Narration Master, FULL duration (never a short cut).
    # The narration must fill the ENTIRE original video: Burmese TTS
    # speaks ~2.2 words/sec, so the script needs ~2.2 words per second
    # of video (e.g. an 8-min video needs ~1000 words). A too-short
    # script was the old 17s-output bug — the output was cut to the
    # audio length. Now the output is always the full video duration
    # with the narration as master over the full footage.
    _dur = max(0.0, float(video_duration or 0))
    _lo, _hi = int(_dur * 2.0), int(_dur * 2.4)
    _word_count = f"{_lo}–{_hi}" if _dur > 0 else "ဗီဒီယို ကြာချိန်နှင့် လိုက်ဖက်သော"
    _dur_txt = f"{_dur:.0f} စက္ကန့်" if _dur > 0 else "ဗီဒီယို အပြည့်"
    return (
        "သင်သည် မြန်မာဗီဒီယို ဇာတ်ကြောင်းပြော (narration) ကျွမ်းကျင်သူဖြစ်သည်။\n"
        "ပေးထားသော ဗီဒီယိုကို သေချာကြည့်ပြီး အောက်ပါအတိုင်း မြန်မာလို "
        "ဇာတ်ကြောင်းပြော အစီအစဉ် အပြည့်အစုံ ရေးပေးပါ။\n\n"
        f"- ဇာတ်ကြောင်းပြော စတိုင်: {style}\n"
        f"- မူရင်းဗီဒီယို ကြာချိန်: {_dur_txt} — ဗီဒီယိုကို တိုတိုလေး "
        "ဖြတ်မချရပါ။ မူရင်း footage အပြည့်အစုံကို အသုံးပြုရမည်။ "
        "Output သည် မူရင်းဗီဒီယို ကြာချိန်အတိုင်း အပြည့်အဝ ဖြစ်ရမည်။\n"
        "- highlights တစ်ခုချင်းစီအတွက် start/end ကို စက္ကန့်ဖြင့် (ဒသမကိန်းရ) "
        "ပေးပါ။\n"
        "- highlights များသည် ဗီဒီယိုတစ်ခုလုံးကို အစမှအဆုံး ဆက်တိုက် "
        "ဖုံးရမည်။ ပထမ highlight က 0.0 တွင် စရမည်၊ နောက်ဆုံး highlight က "
        "ဗီဒီယို၏ အဆုံးတွင် ဆုံးရမည်။ highlights များအကြား လစ်ဟာမှု (gap) "
        "သို့မဟုတ် ထပ်မှု (overlap) မရှိရပါ။ highlight တစ်ခုချင်းစီ၏ "
        "ကြာချိန်သည် ၁၀–၃၀ စက္ကန့် ဖြစ်သင့်သည် (၁၅ မိနစ်ထက် ရှည်သော "
        "ဗီဒီယိုများအတွက် ၆၀ စက္ကန့်အထိ ရသည်)။ highlights တစ်ခုချင်းစီ၏ "
        "ကြာချိန်သည် စက္ကန့်အတိုင်း မှန်ရမည် (start နှင့် end ကွာခြားချက်သည် "
        "ထိုအခန်း၏ တကယ့် ကြာချိန်နှင့် ကိုက်ရမည်)။\n"
        "- title သည် ဆွဲဆောင်မှုရှိသော မြန်မာ ခေါင်းစဉ်ဖြစ်ရမည်။\n"
        "- description များနှင့် burmese_script သည် သဘာဝကျပြီး နားထောင်ကောင်းသော "
        "မြန်မာစကားပြော ဖြစ်ရမည်။ description တစ်ခုချင်းစီသည် ထို "
        "highlight အပိုင်း၏ အကြောင်းအရာကို ဖော်ပြရမည်။\n"
        "- မြန်မာစာလုံးပေါင်း အတိအကျ မှန်ရမည်။ မြန်မာစာအဖွဲ့၏ စံစာလုံးပေါင်း "
        "(standard Myanmar orthography) ကိုသာ သုံးပါ။ စကားလုံးတစ်လုံးချင်းစီ၏ "
        "စာလုံးပေါင်းကို ထုတ်ပေးခြင်းမပြုမီ ပြန်စစ်ပါ။ အသတ် (်)၊ ဝိသဇ္ဇနာ (း)၊ "
        "အက္ခရာတွဲများ (stacked consonants) ကို မှန်ကန်စွာ သုံးပါ။ "
        "စံမဟုတ်သော၊ တီထွင်ထားသော စာလုံးပေါင်းများ လုံးဝ မသုံးပါနှင့်။ "
        "နိုင်ငံခြားစကားလုံးများကိုလည်း စံမြန်မာအသံထွက် စာလုံးပေါင်းဖြင့် "
        "ရေးပါ။ စာပုဒ်ဖြတ်နှင့် စကားလုံးခြားခြင်းကိုလည်း စံအတိုင်း ထားပါ။\n"
        "- title, description နှင့် burmese_script အားလုံးသည် Unicode မြန်မာစာ "
        "(Unicode Myanmar encoding) ဖြင့်သာ ရေးရမည်။ Zawgyi encoding လုံးဝ "
        "မသုံးပါနှင့်။\n"
        "- burmese_script သည် ဗီဒီယိုတစ်ခုလုံး၏ အစ / အလယ် / အဆုံး "
        "အကြောင်းအရာ စုံလင်သော ဇာတ်ကြောင်းပြော အပြည့်အစုံ ဖြစ်ရမည်။\n"
        f"burmese_script ၏ စကားလုံးအရေအတွက်သည် {_word_count} လုံး ဖြစ်ရမည် "
        "(မြန်မာအသံ တစ်စက္ကန့်လျှင် ~၂.၂ လုံးနှုန်းဖြင့် ဗီဒီယို "
        "ကြာချိန်အပြည့် ဖြည့်ရန်) — စာသား တိုလျှင် အသံက စောပြီး "
        "ပြီးသွားမည်။\n\n"
        "STRICT JSON ONLY ဖြင့် ပြန်ပေးပါ။ JSON မှလွဲ၍ အခြားစာသား မထည့်ပါနှင့်။\n"
        'ပုံစံ: {"title": "...", "highlights": '
        '[{"start": 0.0, "end": 20.0, "description": "..."}], '
        '"burmese_script": "..."}'
    )


def _build_continuation_prompt(from_time: float, video_duration: float,
                              style: str) -> str:
    """Prompt for an AI narration CONTINUATION (Fix 1).

    The first narration only covered up to ``from_time``; this asks
    Gemini to narrate ONLY what happens in [from_time, video_duration]
    — no repetition of the earlier part — so the pipeline can append
    it and fill the video to the end. Word count scales with the tail
    length at the same ~2.2 words/sec speaking rate.
    """
    tail = max(0.0, float(video_duration) - float(from_time))
    _words = int(tail * 2.2)
    _lo = int(_words * 0.9)
    _hi = int(_words * 1.1)
    return (
        "သင်သည် မြန်မာဗီဒီယို ဇာတ်ကြောင်းပြော (narration) ကျွမ်းကျင်သူဖြစ်သည်။\n"
        "ပေးထားသော ဗီဒီယို၏ အစောပိုင်းကို ဇာတ်ကြောင်းပြောပြီးပါပြီ။ "
        f"အခု {from_time:.0f} စက္ကန့်မှ ဗီဒီယို၏ အဆုံး ({video_duration:.0f} စက္ကန့်) "
        "အထိ ဖြစ်ပျက်သည်များကိုသာ ဆက်လက် ဇာတ်ကြောင်းပြောပေးပါ။\n\n"
        f"- ဤအပိုင်း၏ ကြာချိန်သည် {tail:.0f} စက္ကန့် ဖြစ်သည်။ "
        f"burmese_script ၏ စကားလုံးအရေအတွက်သည် {_lo}–{_hi} လုံး ဖြစ်ရမည် "
        "(အစောပိုင်းကို ထပ်မပြောရ၊ အဆုံးထိ ရောက်အောင် ပြောရမည်)။\n"
        "- ဇာတ်ကြောင်းပြောသည် သဘာဝကျပြီး နားထောင်ကောင်းသော မြန်မာစကားပြော "
        "ဖြစ်ရမည်။ မြန်မာစာလုံးပေါင်း အတိအကျ မှန်ရမည်။\n"
        "- burmese_script သည် Unicode မြန်မာစာဖြင့်သာ ရေးရမည်။ "
        "Zawgyi encoding လုံးဝ မသုံးပါနှင့်။\n\n"
        "STRICT JSON ONLY ဖြင့် ပြန်ပေးပါ။ JSON မှလွဲ၍ အခြားစာသား မထည့်ပါနှင့်။\n"
        "'{\"burmese_script\": \"...\"}'"
    )


def _build_continuation_expand_prompt(from_time: float, video_duration: float,
                                     style: str, delivered_words: int) -> str:
    """Prompt for a continuation RE-ASK (coverage fix v3).

    The first continuation for [from_time, video_duration] came back
    too short (``delivered_words`` vs the expected tail × 2.2). This
    asks Gemini explicitly to EXPAND — more detail, more sentences —
    so the narration actually fills the tail instead of leaving
    silence. Same word-count target as the normal continuation.
    """
    tail = max(0.0, float(video_duration) - float(from_time))
    _words = int(tail * 2.2)
    _lo = int(_words * 0.9)
    _hi = int(_words * 1.1)
    return (
        "သင်သည် မြန်မာဗီဒီယို ဇာတ်ကြောင်းပြော (narration) ကျွမ်းကျင်သူဖြစ်သည်။\n"
        f"အခု {from_time:.0f} စက္ကန့်မှ ဗီဒီယို၏ အဆုံး ({video_duration:.0f} စက္ကန့်) "
        "အထိ ဇာတ်ကြောင်းပြောရန် အရင်အဖြေက တိုလွန်းပါသည် "
        f"(စကားလုံး {delivered_words} လုံးသာ ပါခဲ့သည်)။\n\n"
        f"ဤအပိုင်းကို ပို၍ အသေးစိတ် ချဲ့ပြီး ထပ်ပြောပေးပါ။ "
        f"burmese_script ၏ စကားလုံးအရေအတွက်သည် {_lo}–{_hi} လုံး ဖြစ်ရမည် "
        "(အစောပိုင်းကို ထပ်မပြောရ၊ အဆုံးထိ ရောက်အောင် ပြောရမည်)။\n"
        "- ဖြစ်ပျက်သည်များကို အသေးစိတ် ဖော်ပြပါ — လူများ ဘာပြောသည်၊ "
        "ဘာလုပ်သည်၊ ဘယ်လို တုံ့ပြန်သည်ကို ပိုထည့်ပါ။\n"
        "- ဇာတ်ကြောင်းပြောသည် သဘာဝကျပြီး နားထောင်ကောင်းသော မြန်မာစကားပြော "
        "ဖြစ်ရမည်။ မြန်မာစာလုံးပေါင်း အတိအကျ မှန်ရမည်။\n"
        "- burmese_script သည် Unicode မြန်မာစာဖြင့်သာ ရေးရမည်။ "
        "Zawgyi encoding လုံးဝ မသုံးပါနှင့်။\n\n"
        "STRICT JSON ONLY ဖြင့် ပြန်ပေးပါ။ JSON မှလွဲ၍ အခြားစာသား မထည့်ပါနှင့်။\n"
        "'{\"burmese_script\": \"...\"}'"
    )


def _build_pro_fill_prompt(windows) -> str:
    """Prompt for the Pro FILL call (Fix 2): narration for empty blocks.

    ``windows`` is a list of (start, end) pairs whose blocks came back
    with empty text. Gemini must return non-empty Myanmar narration
    for EACH window — dubbing of what is said, or a brief narration of
    the action when nobody speaks — so Myanmar speech never drops out.
    """
    _list = ", ".join(f"[{s:.1f}–{e:.1f}s]" for s, e in windows)
    return (
        "သင်သည် မြန်မာ Dialogue ဗီဒီယို ပြန်ဆိုသူ (dubbing) ကျွမ်းကျင်သူဖြစ်သည်။\n"
        "ပေးထားသော ဗီဒီယိုတွင် အောက်ပါ အချိန်အပိုင်းများ၏ မြန်မာစာသား "
        "လွတ်နေပါသည်။ အပိုင်းတစ်ခုချင်းစီအတွက် မြန်မာလို ပြန်ဆိုထားသော "
        f"စာသားကို ပေးပါ: {_list}\n\n"
        "- Block တစ်ခုချင်းစီအတွက် start/end ကို ပေးထားသည့်အတိုင်း အတိအကျ "
        "ပြန်ပေးပါ။\n"
        "- text သည် ထို Block ၏ စကားပြောကို မြန်မာလို ပြန်ဆိုထားသော "
        "စာသား ဖြစ်ရမည်။ စကားပြော မရှိသော Block တွင်လည်း ထိုအခိုက်အတန့်တွင် "
        "ဖြစ်ပျက်နေသည်ကို တိုတိုရှင်းရှင်း ဇာတ်ကြောင်းပြောအဖြစ် ရေးပေးပါ။\n"
        "- text အလွတ် (empty) လုံးဝ မရှိစေရ။ Block တစ်ခုချင်းစီတွင် "
        "text အနည်းဆုံး စကားလုံး ၃ လုံး ရှိရမည်။\n"
        "- မြန်မာစာလုံးပေါင်း အတိအကျ မှန်ရမည်။ Unicode မြန်မာစာဖြင့်သာ "
        "ရေးရမည်။ Zawgyi encoding လုံးဝ မသုံးပါနှင့်။\n\n"
        "STRICT JSON ONLY ဖြင့် ပြန်ပေးပါ။ JSON မှလွဲ၍ အခြားစာသား မထည့်ပါနှင့်။\n"
        "'{\"blocks\": [{\"start\": 0.0, \"end\": 5.0, \"text\": \"...\"}]}'"
    )


def _build_pro_prompt() -> str:
    """Prompt for Pro AI Recap (Step 15): sequential dialogue blocks."""
    return (
        "သင်သည် မြန်မာ Dialogue ဗီဒီယို ပြန်ဆိုသူ (dubbing) ကျွမ်းကျင်သူဖြစ်သည်။\n"
        "ပေးထားသော ဗီဒီယိုကို အစမှအဆုံး ကြည့်ပြီး စကားပြော (dialogue) အလိုက် "
        "Sequential Blocks များ အဖြစ် ခွဲပေးပါ။\n\n"
        "- Block တစ်ခုချင်းစီသည် ဗီဒီယို၏ ဆက်တိုက် အပိုင်းဖြစ်ရမည်။ "
        "ပထမ Block က 0.0 တွင် စရမည်၊ နောက်ဆုံး Block က ဗီဒီယို၏ အဆုံးတွင် "
        "ဆုံးရမည်။ Blocks များအကြား လစ်ဟာမှု (gap) သို့မဟုတ် ထပ်မှု "
        "(overlap) မရှိရပါ။\n"
        "- Block တစ်ခုချင်းစီအတွက် start/end ကို စက္ကန့်ဖြင့် (ဒသမ) ပေးပါ။ "
        "Block တစ်ခု၏ ကြာချိန်သည် များသောအားဖြင့် ၃–၈ စက္ကန့် ဖြစ်သင့်သည်။\n"
        "- text သည် ထို Block ၏ စကားပြောကို မြန်မာလို ပြန်ဆိုထားသော "
        "(သို့မဟုတ် ဇာတ်ကြောင်းပြော) စာသားဖြစ်ရမည်။ Block တိုင်းတွင် "
        "text အလွတ် (empty) လုံးဝ မရှိစေရ — စကားပြော မရှိသော Block "
        "တွင်လည်း ထိုအခိုက်အတန့်တွင် ဖြစ်ပျက်နေသည်ကို တိုတိုရှင်းရှင်း "
        "ဇာတ်ကြောင်းပြောအဖြစ် ရေးပေးပါ (မြန်မာအသံ ဘယ်တော့မှ ပြတ်မသွားစေရန်)။\n"
        "- ဗီဒီယိုကို ဘာမှ မဖြတ်ပါ (No Cuts) — ဗီဒီယို အမြန်နှုန်းသည် "
        "အမြဲ 1.0 (ပုံမှန်) ဖြစ်ရမည်။ ဗီဒီယိုကို နှေးသွားအောင် (slow-motion) "
        "လုံးဝ မလုပ်ရပါ။ အသံက Block အတွင်း မဝင်လျှင် ဗီဒီယိုကို "
        "မနှေးစေဘဲ အသံကိုသာ မြန်အောင် ညှိမည် — ထို့ကြောင့် text သည် "
        "Block အတွင်း ကွက်တိ ဝင်ဆံ့ရမည်။\n"
        "- text သည် Block ၏ ကြာချိန်၏ ၇၀–၉၀% လောက် ပြောလောက်အောင် "
        "ရှည်ရမည် (ခန့်မှန်း စကားလုံးအရေအတွက် ≈ Block ကြာချိန် "
        "(စက္ကန့်) × ၂.၀)။ တစ်စက္ကန့်လျှင် အများဆုံး ၃ စကားလုံး နှုန်းကို "
        "အထက်ကန့်သတ် (upper bound) အဖြစ်သာ သုံးပါ — ပစ်မှတ် (target) "
        "အဖြစ် မသုံးပါနှင့်။ (ဥပမာ ၅ စက္ကန့် Block ဆိုလျှင် စာလုံးရေ "
        "၇–၁၀ လုံးခန့် ထည့်ပါ)။ တိုလွန်းသော စာသားသည် Block အတွင်း "
        "တိတ်ဆိတ်ဖြစ်စေသည်။ ရှည်လျားသော ဝါကျများ၊ အပိုစကားများ "
        "လုံးဝ မထည့်ပါနှင့်။\n"
        "- မြန်မာစာလုံးပေါင်း အတိအကျ မှန်ရမည်။ မြန်မာစာအဖွဲ့၏ စံစာလုံးပေါင်း "
        "(standard Myanmar orthography) ကိုသာ သုံးပါ။ အသတ် (်)၊ ဝိသဇ္ဇနာ (း)၊ "
        "အက္ခရာတွဲများ (stacked consonants) ကို မှန်ကန်စွာ သုံးပါ။ "
        "စံမဟုတ်သော၊ တီထွင်ထားသော စာလုံးပေါင်းများ လုံးဝ မသုံးပါနှင့်။\n"
        "- title နှင့် text အားလုံးသည် Unicode မြန်မာစာ (Unicode Myanmar "
        "encoding) ဖြင့်သာ ရေးရမည်။ Zawgyi encoding လုံးဝ မသုံးပါနှင့်။\n\n"
        "STRICT JSON ONLY ဖြင့် ပြန်ပေးပါ။ JSON မှလွဲ၍ အခြားစာသား မထည့်ပါနှင့်။\n"
        'ပုံစံ: {"title": "...", "blocks": '
        '[{"start": 0.0, "end": 5.0, "text": "..."}, '
        '{"start": 5.0, "end": 10.0, "text": "..."}]}'
    )


def _generate_with_fallback(client: genai.Client, uploaded, prompt: str,
                            schema: Dict[str, Any],
                            pinned: Optional[str]) -> Dict[str, Any]:
    """Generate strict JSON from an uploaded video, trying models in order.

    Candidate order: explicit user pick -> static MODEL_CANDIDATES -> live
    discovery of other flash models this key can access. Returns the parsed
    JSON dict, or raises AnalyzerError when no model answers.
    """
    candidates: List[str] = []
    pinned = (pinned or "").strip()
    if pinned and pinned.lower() != "auto":
        candidates.append(pinned)
    for m in MODEL_CANDIDATES:
        if m not in candidates:
            candidates.append(m)

    plan: Optional[Dict[str, Any]] = None
    last_err: Optional[Exception] = None
    tried: List[str] = []

    def _try_generate(mid: str) -> bool:
        nonlocal plan, last_err
        tried.append(mid)
        try:
            resp = client.models.generate_content(
                model=mid,
                contents=[uploaded, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=schema,
                ),
            )
            plan = json.loads(resp.text)
            return True
        except Exception as exc:  # noqa: BLE001 - inspected below
            if _looks_like_model_error(exc):
                last_err = exc
                return False
            raise

    for mid in candidates:
        if _try_generate(mid):
            break
    if plan is None:
        # Dynamic fallback: ask the API which flash models this key
        # can actually use, and try each of them.
        for mid in _discover_flash_models(client, tried):
            if _try_generate(mid):
                break
    if plan is None:
        raise AnalyzerError(
            "AI model နှင့် ချိတ်ဆက်၍ မရပါ။ "
            f"({last_err if last_err else 'unknown error'})"
        )
    return plan


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def analyze_video_for_recap(
    video_path,
    user_api_key: str,
    target_duration: int = 30,
    style: str = DEFAULT_STYLE,
    model: Optional[str] = None,
    gemini_file_name: Optional[str] = None,
    video_duration: Optional[float] = None,
    delete_remote: bool = True,
) -> Dict[str, Any]:
    """Analyze a video with the USER's own Gemini key; return a recap plan.

    AI Recap is ALWAYS full-duration (Narration Master): the prompt is
    built from the REAL video duration, not ``target_duration``.
    ``target_duration`` is kept only for backward compatibility and is
    IGNORED — an 8-min video always gets a full ~8-min narration plan,
    never a 30s cut. ``model`` optionally pins one model ID (tried
    first). Otherwise the static MODEL_CANDIDATES are tried in order,
    then any other flash model the key can access (discovered live via
    ``client.models.list()``).

    The uploaded remote file is always deleted in a ``finally:`` block.
    """
    api_key = _require_key(user_api_key)

    path = str(video_path) if video_path else ""
    if gemini_file_name:
        # Hybrid mode: the browser uploaded the video to Gemini
        # directly — there is no local file to ffprobe, so the
        # duration comes from the browser's video metadata.
        if not video_duration:
            raise AnalyzerError(
                "Hybrid: video_duration လိုအပ်ပါသည်။")
        duration = float(video_duration)
    else:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Video file not found: {path}")
        # Real duration FIRST — the full-duration prompt needs it.
        metadata = get_video_metadata(path)
        duration = metadata.get("duration") or 0

    client = genai.Client(api_key=api_key)
    uploaded = _resolve_uploaded(client, path, gemini_file_name)
    try:
        _wait_for_active(client, uploaded.name)
        prompt = _build_prompt(duration, style)
        plan = _generate_with_fallback(
            client, uploaded, prompt, RECAP_JSON_SCHEMA, model)
        return validate_recap_plan(plan, duration)
    finally:
        # Keep the user's free cloud storage clean, even on failure.
        # Hybrid: the shared file is deleted once by the pipeline
        # after ALL analysis calls (continuation/fill reuse it).
        if delete_remote:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass


def analyze_video_pro_blocks(
    video_path,
    user_api_key: str,
    model: Optional[str] = None,
    gemini_file_name: Optional[str] = None,
    video_duration: Optional[float] = None,
    delete_remote: bool = True,
) -> Dict[str, Any]:
    """Analyze a dialogue video with the USER's own Gemini key (Step 15).

    Returns a Pro block plan: {"title", "blocks": [{start, end, text}...],
    "burmese_script"} — the blocks tile the whole video [0, duration] with
    no gaps/overlaps (see validate_pro_blocks). ``model`` optionally pins
    one model ID (tried first); otherwise the same fallback chain as the
    recap analyzer runs. The uploaded remote file is always deleted in a
    ``finally:`` block.
    """
    api_key = _require_key(user_api_key)

    path = str(video_path) if video_path else ""
    if not gemini_file_name and not os.path.isfile(path):
        raise FileNotFoundError(f"Video file not found: {path}")

    client = genai.Client(api_key=api_key)
    uploaded = _resolve_uploaded(client, path, gemini_file_name)
    try:
        _wait_for_active(client, uploaded.name)
        prompt = _build_pro_prompt()
        plan = _generate_with_fallback(
            client, uploaded, prompt, PRO_BLOCKS_JSON_SCHEMA, model)
        if gemini_file_name:
            if not video_duration:
                raise AnalyzerError(
                    "Hybrid: video_duration လိုအပ်ပါသည်။")
            _dur = float(video_duration)
        else:
            metadata = get_video_metadata(path)
            _dur = metadata.get("duration") or 0
        return validate_pro_blocks(plan, _dur)
    finally:
        if delete_remote:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass


def analyze_video_continuation(
    video_path,
    user_api_key: str,
    from_time: float,
    video_duration: float,
    style: str = DEFAULT_STYLE,
    model: Optional[str] = None,
    delivered_words: Optional[int] = None,
    gemini_file_name: Optional[str] = None,
    delete_remote: bool = True,
) -> Dict[str, Any]:
    """Ask Gemini for narration covering ONLY [from_time, video_duration].

    Fix 1: the first AI narration sometimes ends far before the video
    ends (measured: 8:04 video, narration ended at 1:43). The pipeline
    appends this continuation's audio + subtitles so the narration
    reaches the end. Returns {"burmese_script": ...} (non-empty, or
    raises AnalyzerError). The uploaded remote file is always deleted.

    Coverage fix v3: when ``delivered_words`` is given, the previous
    continuation for this tail came back too short — use the expand
    prompt, which explicitly asks Gemini to add detail and reach the
    full word-count target.
    """
    api_key = _require_key(user_api_key)

    path = str(video_path) if video_path else ""
    if not gemini_file_name and not os.path.isfile(path):
        raise FileNotFoundError(f"Video file not found: {path}")
    if float(video_duration) <= float(from_time):
        raise AnalyzerError("ဆက်လက် ဇာတ်ကြောင်းပြောရန် အပိုင်း မကျန်ပါ။")

    client = genai.Client(api_key=api_key)
    uploaded = _resolve_uploaded(client, path, gemini_file_name)
    try:
        _wait_for_active(client, uploaded.name)
        if delivered_words is not None:
            prompt = _build_continuation_expand_prompt(
                from_time, video_duration, style, delivered_words)
        else:
            prompt = _build_continuation_prompt(
                from_time, video_duration, style)
        plan = _generate_with_fallback(
            client, uploaded, prompt, CONTINUATION_JSON_SCHEMA, model)
        script = str((plan or {}).get("burmese_script", "")).strip()
        if not script:
            raise AnalyzerError(
                "AI က ဆက်လက် ဇာတ်ကြောင်းပြော ပြန်မပေးနိုင်ပါ။")
        return {"burmese_script": script}
    finally:
        if delete_remote:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass


def analyze_video_pro_fill(
    video_path,
    user_api_key: str,
    windows,
    model: Optional[str] = None,
    gemini_file_name: Optional[str] = None,
    delete_remote: bool = True,
) -> Dict[str, Any]:
    """Ask Gemini for Myanmar narration for the given empty windows.

    Fix 2: Pro blocks whose text came back empty would render as
    silence (Myanmar speech "comes and goes"). The pipeline fills
    those blocks' text from this call, then re-synthesizes them.
    ``windows`` is a list of (start, end) pairs. Returns
    {"blocks": [{"start","end","text"}...]}. The uploaded remote file
    is always deleted.
    """
    api_key = _require_key(user_api_key)

    path = str(video_path) if video_path else ""
    if not gemini_file_name and not os.path.isfile(path):
        raise FileNotFoundError(f"Video file not found: {path}")
    windows = [(float(s), float(e)) for s, e in (windows or []) if e > s]
    if not windows:
        return {"blocks": []}

    client = genai.Client(api_key=api_key)
    uploaded = _resolve_uploaded(client, path, gemini_file_name)
    try:
        _wait_for_active(client, uploaded.name)
        prompt = _build_pro_fill_prompt(windows)
        plan = _generate_with_fallback(
            client, uploaded, prompt, PRO_FILL_JSON_SCHEMA, model)
        blocks = []
        for b in (plan or {}).get("blocks", []) or []:
            try:
                s, e = float(b.get("start", 0)), float(b.get("end", 0))
            except (TypeError, ValueError, AttributeError):
                continue
            text = str(b.get("text", "")).strip()
            if e > s and text:
                blocks.append({"start": s, "end": e, "text": text})
        return {"blocks": blocks}
    finally:
        if delete_remote:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass


def validate_recap_plan(
    plan_dict: Dict[str, Any], video_duration: float
) -> Dict[str, Any]:
    """Normalize highlights into a clean tiling of [0, video_duration].

    AI Recap is full-duration: highlights must tile the whole video
    (same tiling rule as validate_pro_blocks). Sorts by start, clamps
    into range, drops empty segments, fills gaps by extending the
    previous segment's end, cuts overlaps, forces the first segment to
    start at 0.0 and extends the last segment to the full duration. If
    no valid highlight survives, falls back to one [0, duration]
    segment so the render still covers the full video.
    """
    if not isinstance(plan_dict, dict):
        raise AnalyzerError("AI မှ ပြန်လာသော ရလဒ် ပုံစံမမှန်ပါ။")
    duration = max(0.0, float(video_duration or 0))

    raw: List[Dict[str, Any]] = []
    for h in plan_dict.get("highlights", []) or []:
        try:
            start = float(h.get("start", 0))
            end = float(h.get("end", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        # Preserve extra keys — only start/end/description normalize.
        _copy = dict(h) if isinstance(h, dict) else {}
        _copy["start"] = start
        _copy["end"] = end
        _copy["description"] = str(h.get("description", "")).strip()
        raw.append(_copy)
    raw.sort(key=lambda h: h["start"])

    cleaned: List[Dict[str, Any]] = []
    for h in raw:
        s = max(0.0, min(h["start"], duration))
        e = max(0.0, min(h["end"], duration))
        if not cleaned:
            s = 0.0  # the first segment always starts at 0
        else:
            prev = cleaned[-1]
            if s > prev["end"]:
                prev["end"] = round(s, 2)  # fill the gap: extend previous
            s = prev["end"]  # cut any overlap
        if e <= s:
            continue
        _out = dict(h)
        _out["start"] = round(s, 2)
        _out["end"] = round(e, 2)
        cleaned.append(_out)
    if cleaned and cleaned[-1]["end"] < duration:
        cleaned[-1]["end"] = round(duration, 2)
    if not cleaned and duration > 0:
        cleaned = [{"start": 0.0, "end": round(duration, 2),
                    "description": ""}]
    return {
        "title": str(plan_dict.get("title", "")).strip(),
        "highlights": cleaned,
        "burmese_script": str(plan_dict.get("burmese_script", "")).strip(),
    }


def validate_pro_blocks(
    plan_dict: Dict[str, Any], video_duration: float
) -> Dict[str, Any]:
    """Normalize Pro blocks into a clean tiling of [0, video_duration].

    Sorts by start, clamps into range, drops empty blocks, fills gaps by
    extending the previous block's end (so no video is dropped), cuts
    overlaps at the previous block's end, forces the first block to start
    at 0.0 and extends the last block to the full duration. Returns
    {"title", "blocks", "burmese_script"} (script = joined block texts).
    """
    if not isinstance(plan_dict, dict):
        raise AnalyzerError("AI မှ ပြန်လာသော ရလဒ် ပုံစံမမှန်ပါ။")
    duration = max(0.0, float(video_duration or 0))

    raw: List[Dict[str, Any]] = []
    for b in plan_dict.get("blocks", []) or []:
        try:
            start = float(b.get("start", 0))
            end = float(b.get("end", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        # Preserve every extra key (audio_path / audio_len / tts_rate
        # from prebuilt block audio) — only start/end/text normalize.
        _copy = dict(b) if isinstance(b, dict) else {}
        _copy["start"] = start
        _copy["end"] = end
        _copy["text"] = str(b.get("text", "")).strip()
        raw.append(_copy)
    raw.sort(key=lambda b: b["start"])

    blocks: List[Dict[str, Any]] = []
    for b in raw:
        s = max(0.0, min(b["start"], duration))
        e = max(0.0, min(b["end"], duration))
        if not blocks:
            s = 0.0  # the first block always starts at 0
        else:
            prev = blocks[-1]
            if s > prev["end"]:
                prev["end"] = round(s, 2)  # fill the gap: extend previous
            s = prev["end"]  # cut any overlap
        if e <= s:
            continue
        _out = dict(b)
        _out["start"] = round(s, 2)
        _out["end"] = round(e, 2)
        blocks.append(_out)
    if blocks and blocks[-1]["end"] < duration:
        blocks[-1]["end"] = round(duration, 2)
    return {
        "title": str(plan_dict.get("title", "")).strip(),
        "blocks": blocks,
        "burmese_script": " ".join(b["text"] for b in blocks).strip(),
    }


def generate_mock_pro_plan(video_duration: float) -> Dict[str, Any]:
    """Offline mock Pro block plan (no API key needed) for pipeline testing."""
    duration = max(0.0, float(video_duration or 0))
    blocks: List[Dict[str, Any]] = []
    seg_len = 5.0
    t = 0.0
    idx = 1
    while t < duration:
        end = min(t + seg_len, duration)
        blocks.append(
            {
                "start": round(t, 2),
                "end": round(end, 2),
                "text": f"အခန်း {idx}: ဗီဒီယို၏ စကားပြော အပိုင်း "
                        f"({t:.0f}–{end:.0f} စက္ကန့်)",
            }
        )
        t = end
        idx += 1
    plan = {
        "title": "စမ်းသပ် Pro ဗီဒီယို",
        "blocks": blocks,
        "burmese_script": " ".join(b["text"] for b in blocks),
    }
    return validate_pro_blocks(plan, duration)


def generate_mock_recap_plan(video_duration: float) -> Dict[str, Any]:
    """Offline mock recap plan (no API key needed) for pipeline testing."""
    duration = max(0.0, float(video_duration or 0))
    highlights: List[Dict[str, Any]] = []
    seg_len = 5.0
    t = 0.0
    idx = 1
    while t < duration:
        end = min(t + seg_len, duration)
        highlights.append(
            {
                "start": round(t, 2),
                "end": round(end, 2),
                "description": (
                    f"အခန်း {idx}: ဗီဒီယို၏ အဓိက အစိတ်အပိုင်း "
                    f"({t:.0f}–{end:.0f} စက္ကန့်)"
                ),
            }
        )
        t = end
        idx += 1
    script = (
        "မင်္ဂလာပါ။ ဒီဗီဒီယိုရဲ့ အဓိက အခန်းတွေကို အကျဉ်းချုပ် ပြောပြပေးပါမယ်။ "
        + " ".join(
            f"အခန်း {i + 1} မှာတော့ {h['description']} ပါဝင်ပါတယ်။"
            for i, h in enumerate(highlights)
        )
        + " ဒါပါပဲ၊ အကျဉ်းချုပ် ပြီးဆုံးပါပြီ။"
    )
    plan = {
        "title": "စမ်းသပ် ဗီဒီယို အကျဉ်းချုပ်",
        "highlights": highlights,
        "burmese_script": script,
    }
    return validate_recap_plan(plan, duration)


# ---------------------------------------------------------------------------
# Post Kit (feature pack v1.13)
# ---------------------------------------------------------------------------

POST_KIT_SCHEMA = {
    "type": "object",
    "properties": {
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["caption", "hashtags"],
}


def generate_post_kit(title, script_text, user_api_key, model=None,
                      extra_hashtags="", append_fyp=True):
    """Post Kit: Gemini writes a Burmese TikTok caption + hashtags.

    Uses the caller's own BYOK key (same key as the recap analysis).
    Returns {"caption": str, "hashtags": [str]} with the user's extra
    hashtags appended and #fyp appended when the toggle is on.
    Raises AnalyzerError on failure — callers catch it and skip the
    Post Kit gracefully (it is a bonus, never fatal to the run).
    """
    key = _require_key(user_api_key)
    client = genai.Client(**{"api_key": key})
    script_excerpt = str(script_text or "")[:2000]
    prompt = (
        "You are writing a TikTok/Facebook post for a Myanmar video recap.\n"
        "Video title: " + str(title or "") + "\n"
        "Recap script excerpt: " + script_excerpt + "\n\n"
        "Write in Burmese (Unicode Myanmar, never Zawgyi):\n"
        "1. \"caption\": a catchy 1-2 line caption for the post.\n"
        "2. \"hashtags\": 8-12 relevant hashtags (Myanmar and English mix, "
        "each starting with #). Do NOT include #myanmartiktok.\n"
        "Return ONLY strict JSON matching the schema."
    )
    candidates = []
    if model:
        candidates.append(model)
    for m in MODEL_CANDIDATES:
        if m not in candidates:
            candidates.append(m)
    last_err = None
    data = None
    for mid in candidates:
        try:
            resp = client.models.generate_content(
                model=mid,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=POST_KIT_SCHEMA,
                ),
            )
            data = json.loads(resp.text)
            break
        except Exception as exc:  # noqa: BLE001 - try next model
            if _looks_like_model_error(exc):
                last_err = exc
                continue
            raise
    if data is None:
        raise AnalyzerError(
            "Post Kit ဖန်တီးမရပါ။ "
            f"({last_err if last_err else 'unknown error'})"
        )
    caption = str(data.get("caption") or "").strip()
    tags = []
    for h in data.get("hashtags") or []:
        h = str(h).strip()
        if not h:
            continue
        if not h.startswith("#"):
            h = "#" + h
        if h not in tags:
            tags.append(h)
    # User's custom hashtags.
    for h in str(extra_hashtags or "").replace(",", " ").split():
        h = h.strip()
        if not h:
            continue
        if not h.startswith("#"):
            h = "#" + h
        if h not in tags:
            tags.append(h)
    if append_fyp and "#fyp" not in tags:
        tags.append("#fyp")
    return {"caption": caption, "hashtags": tags}
