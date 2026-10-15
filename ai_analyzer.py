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
    is ACTIVE, asks Gemini for a recap plan as strict JSON, validates the
    timestamps against the real video duration, then ALWAYS deletes the
    remote file in a ``finally:`` block so the user's free cloud storage
    does not fill up. ``model`` optionally pins one model ID (tried first);
    otherwise the free-tier MODEL_CANDIDATES are tried in order, then any
    other flash model the key can access (live discovery).
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
        "- burmese_script သည် ရွေးထားသော highlights များကို ဆက်စပ်ပြီး ပြောပြသော "
        f"ဇာတ်ကြောင်းပြော စာသား ဖြစ်ရမည် (ခန့်မှန်း {target_duration} စက္ကန့်စာ အသံထွက်)။\n\n"
        "STRICT JSON ONLY ဖြင့် ပြန်ပေးပါ။ JSON မှလွဲ၍ အခြားစာသား မထည့်ပါနှင့်။\n"
        'ပုံစံ: {"title": "...", "highlights": '
        '[{"start": 0.0, "end": 5.0, "description": "..."}], '
        '"burmese_script": "..."}'
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
        "(သို့မဟုတ် ဇာတ်ကြောင်းပြော) စာသားဖြစ်ရမည်။\n"
        "- text သည် အလွန်တိုတိုနှင့် အနှစ်ချုပ်သာ ဖြစ်ရမည်။ မြန်မာစကားပြော "
        "နှုန်း (တစ်စက္ကန့်လျှင် အများဆုံး ၃ စကားလုံးနှုန်း) အရ Block "
        "ကြာချိန်နှင့် လိုက်ဖက်သော စကားလုံးအရေအတွက်သာ ထည့်ပါ "
        "(ဥပမာ ၃ စက္ကန့် Block ဆိုလျှင် စာလုံးရေ ၁၀ လုံးထက် မကျော်စေရန်၊ "
        "၅ စက္ကန့် Block ဆိုလျှင် ၁၅ လုံးထက် မကျော်စေရန်)။ ရှည်လျားသော "
        "ဝါကျများ၊ အပိုစကားများ လုံးဝ မထည့်ပါနှင့်။ စာသားရှည်လျှင် "
        "အသံက လေးလံပြီး ဗီဒီယိုက slow-motion ဖြစ်သွားမည်။\n"
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
) -> Dict[str, Any]:
    """Analyze a video with the USER's own Gemini key; return a recap plan.

    ``model`` optionally pins one model ID (tried first). Otherwise the
    static MODEL_CANDIDATES are tried in order, then any other flash model
    the key can access (discovered live via ``client.models.list()``).

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
        plan = _generate_with_fallback(
            client, uploaded, prompt, RECAP_JSON_SCHEMA, model)
        metadata = get_video_metadata(path)
        return validate_recap_plan(plan, metadata.get("duration") or 0)
    finally:
        # Keep the user's free cloud storage clean, even on failure.
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass


def analyze_video_pro_blocks(
    video_path,
    user_api_key: str,
    model: Optional[str] = None,
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

    path = str(video_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Video file not found: {path}")

    client = genai.Client(api_key=api_key)
    uploaded = client.files.upload(file=path)
    try:
        _wait_for_active(client, uploaded.name)
        prompt = _build_pro_prompt()
        plan = _generate_with_fallback(
            client, uploaded, prompt, PRO_BLOCKS_JSON_SCHEMA, model)
        metadata = get_video_metadata(path)
        return validate_pro_blocks(plan, metadata.get("duration") or 0)
    finally:
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
        raw.append({"start": start, "end": end,
                    "text": str(b.get("text", "")).strip()})
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
        blocks.append({"start": round(s, 2), "end": round(e, 2),
                       "text": b["text"]})
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
