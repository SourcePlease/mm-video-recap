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
    style="စိတ်ဝင်စားဖွယ် ဇာတ်လမ်းအကျဉ်းချုပ်")
    Uploads the video to the user's own Gemini Files storage, waits until it
    is ACTIVE, asks Gemini for a recap plan as strict JSON, validates the
    timestamps against the real video duration, then ALWAYS deletes the
    remote file in a ``finally:`` block so the user's free cloud storage
    does not fill up.
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

# Preferred model first; auto-fallback if a model is unavailable for the key.
MODEL_CANDIDATES = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]

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
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "404", "not_found", "not found", "unknown model",
            "model is not supported", "invalid model", "not available",
        )
    )


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


def _build_prompt(target_duration: int, style: str) -> str:
    return (
        "သင်သည် မြန်မာဗီဒီယို အကျဉ်းချုပ် (video recap) ကျွမ်းကျင်သူဖြစ်သည်။\n"
        "ပေးထားသော ဗီဒီယိုကို သေချာကြည့်ပြီး အောက်ပါအတိုင်း မြန်မာလို အကျဉ်းချုပ် "
        "အစီအစဉ် ရေးပေးပါ။\n\n"
        f"- အကျဉ်းချုပ် စတိုင်: {style}\n"
        f"- ရွေးချယ်ရမည့် အဓိကအခန်းများ စုစုပေါင်း ကြာချိန်: ခန့်မှန်းခြေ {target_duration} စက္ကန့်\n"
        "- highlights တစ်ခုချင်းစီအတွက် start/end ကို စက္ကန့်ဖြင့် (ဒသမကိန်းရ) "
        "ပေးပါ။\n"
        "- title သည် ဆွဲဆောင်မှုရှိသော မြန်မာ ခေါင်းစဉ်ဖြစ်ရမည်။\n"
        "- description များနှင့် burmese_script သည် သဘာဝကျပြီး နားထောင်ကောင်းသော "
        "မြန်မာစကားပြော ဖြစ်ရမည်။\n"
        "- burmese_script သည် ရွေးထားသော highlights များကို ဆက်စပ်ပြီး ပြောပြသော "
        f"ဇာတ်ကြောင်းပြော စာသား ဖြစ်ရမည် (ခန့်မှန်း {target_duration} စက္ကန့်စာ အသံထွက်)။\n\n"
        "STRICT JSON ONLY ဖြင့် ပြန်ပေးပါ။ JSON မှလွဲ၍ အခြားစာသား မထည့်ပါနှင့်။\n"
        'ပုံစံ: {"title": "...", "highlights": '
        '[{"start": 0.0, "end": 5.0, "description": "..."}], '
        '"burmese_script": "..."}'
    )


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def analyze_video_for_recap(
    video_path,
    user_api_key: str,
    target_duration: int = 30,
    style: str = DEFAULT_STYLE,
) -> Dict[str, Any]:
    """Analyze a video with the USER's own Gemini key; return a recap plan.

    The uploaded remote file is always deleted in a ``finally:`` block.
    """
    api_key = _require_key(user_api_key)

    path = str(video_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Video file not found: {path}")

    client = genai.Client(api_key=api_key)
    uploaded = client.files.upload(file=path)
    try:
        _wait_for_active(client, uploaded.name)
        prompt = _build_prompt(target_duration, style)

        plan: Optional[Dict[str, Any]] = None
        last_err: Optional[Exception] = None
        for model in MODEL_CANDIDATES:
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=[uploaded, prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_json_schema=RECAP_JSON_SCHEMA,
                    ),
                )
                plan = json.loads(resp.text)
                break
            except Exception as exc:  # noqa: BLE001 - inspected below
                if _looks_like_model_error(exc):
                    last_err = exc
                    continue
                raise
        if plan is None:
            raise AnalyzerError(
                "AI model နှင့် ချိတ်ဆက်၍ မရပါ။ "
                f"({last_err if last_err else 'unknown error'})"
            )

        metadata = get_video_metadata(path)
        return validate_recap_plan(plan, metadata.get("duration") or 0)
    finally:
        # Keep the user's free cloud storage clean, even on failure.
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass


def validate_recap_plan(
    plan_dict: Dict[str, Any], video_duration: float
) -> Dict[str, Any]:
    """Clamp highlight timestamps into [0, video_duration]; drop invalid ones."""
    if not isinstance(plan_dict, dict):
        raise AnalyzerError("AI မှ ပြန်လာသော ရလဒ် ပုံစံမမှန်ပါ။")
    duration = max(0.0, float(video_duration or 0))

    cleaned: List[Dict[str, Any]] = []
    for h in plan_dict.get("highlights", []) or []:
        try:
            start = float(h.get("start", 0))
            end = float(h.get("end", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))
        if end <= start:
            continue
        cleaned.append(
            {
                "start": round(start, 2),
                "end": round(end, 2),
                "description": str(h.get("description", "")).strip(),
            }
        )
    cleaned.sort(key=lambda h: h["start"])
    return {
        "title": str(plan_dict.get("title", "")).strip(),
        "highlights": cleaned,
        "burmese_script": str(plan_dict.get("burmese_script", "")).strip(),
    }


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
