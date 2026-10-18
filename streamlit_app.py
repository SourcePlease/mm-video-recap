"""
streamlit_app.py — Step 8: Streamlit Community Cloud entry point.

100% Free Online Video Recap — every visitor uses their OWN free Gemini
API key (Bring-Your-Own-Key). No developer key exists anywhere in this app.

Calls run_full_recap_pipeline() from pipeline.py — the same battle-tested
pipeline the Gradio UI (app.py) uses. Deploy target: Streamlit Community
Cloud (https://share.streamlit.io) -> permanent https://*.streamlit.app URL.

Multi-user safety: each run gets its own isolated working directory via
tempfile.mkdtemp(), so concurrent users never overwrite each other's files.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import traceback
import uuid

import streamlit as st
from streamlit_js_eval import streamlit_js_eval

from canvas_component import CANVAS_DEFAULT, interactive_canvas
from pipeline import run_full_recap_pipeline

# Build marker: bump on every push so a live page check can prove
# which code the running app has.
BUILD_VERSION = "2026-10-18 webcodecs v1.1"
# Server boot time: set ONCE when this module is imported (i.e. when
# the app process starts). A server restart re-imports the module, so
# BOOT_TIME jumps forward. The re-attach logic compares it with a
# remembered job's saved_at: if the server booted AFTER the job was
# saved, the job died with the old process (its /tmp status file is
# gone) — that is a server restart, not a stalled worker, and the user
# deserves the exact message instead of a silent idle page.
BOOT_TIME = time.time()
from video_editor import (
    SUBTITLE_COLOR_RGB,
    SUBTITLE_FONTS,
    _subtitle_font_file,
    sanitize_filename,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEMO_SAMPLE = os.path.join(APP_DIR, "sample_test.mp4")

st.set_page_config(
    page_title="100% Free Online Video Recap",
    page_icon="⬛",
    layout="centered",
)

# ---- Step 12: iOS 27 Liquid Glass Design System ----
LIQUID_GLASS_CSS = """
<style>
/* Page: deep dark backdrop so the glass cards read as glass. */
.stApp {
  background:
    radial-gradient(1200px 600px at 85% -10%, rgba(255, 255, 255, 0.07), transparent 60%),
    radial-gradient(900px 500px at -10% 20%, rgba(255, 255, 255, 0.05), transparent 55%),
    linear-gradient(160deg, #000000 0%, #111111 45%, #000000 100%);
  color: #f5f5f5;
}
.stApp > header[data-testid="stHeader"] {
  background: transparent;
}
.stApp h1, .stApp h2, .stApp h3, .stApp h4,
.stApp p, .stApp li, .stApp label, .stApp span {
  color: #f5f5f5;
}
.stApp [data-testid="stCaptionContainer"] {
  color: #a3a3a3;
}

/* Segmented Tab Bar: black bar (matches the reference image). */
div[data-testid="stTabs"] div[role="tablist"] {
  background: #000000;
  border-radius: 20px;
  padding: 6px;
  gap: 6px;
  border: 1px solid rgba(255, 255, 255, 0.08);
}
div[data-testid="stTabs"] div[role="tab"] {
  background: transparent;
  color: #a3a3a3;
  border-radius: 18px;
  font-weight: 500;
  border: none;
  padding: 10px 14px;
  cursor: pointer;
}
div[data-testid="stTabs"] div[role="tab"] p {
  color: #a3a3a3;
}
div[data-testid="stTabs"] div[role="tab"]:hover {
  color: #ffffff;
}
div[data-testid="stTabs"] div[role="tab"]:hover p {
  color: #ffffff;
}
div[data-testid="stTabs"] div[role="tab"][aria-selected="true"] {
  background: #ffffff;
  color: #000000;
  border-radius: 18px;
  font-weight: 600;
  box-shadow: 0 4px 15px rgba(0, 0, 0, 0.25);
}
div[data-testid="stTabs"] div[role="tab"][aria-selected="true"] p {
  color: #000000;
}
/* Hide the default underline indicator — the white pill is the indicator. */
div[data-testid="stTabs"] .react-aria-SelectionIndicator {
  display: none;
}

/* Tab content: Dark Liquid Glass Card. */
div[role="tabpanel"] {
  background: rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(25px) saturate(180%);
  -webkit-backdrop-filter: blur(25px) saturate(180%);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 24px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.35);
  padding: 20px;
  margin-top: 12px;
}

/* Inputs on dark glass: keep them dark so they don't glow white. */
.stApp [data-testid="stTextInput"] input,
.stApp [data-testid="stSelectbox"] > div > div {
  background: rgba(255, 255, 255, 0.08);
  color: #f5f5f5;
  border: 1px solid rgba(255, 255, 255, 0.14);
}
.stApp [data-testid="stTextInput"] input::placeholder {
  color: #737373;
}

/* Alert boxes (success / error / info): monochrome, no green/red/blue. */
.stApp [data-testid="stAlert"] {
  background: #ffffff;
  color: #000000;
  border: 1px solid #000000;
}
.stApp [data-testid="stAlert"] p,
.stApp [data-testid="stAlert"] span {
  color: #000000;
}

/* Action Button: solid white, black text (no gradient colour). */
.stApp button[data-testid="stBaseButton-primary"] {
  background: #ffffff;
  color: #000000;
  border: none;
  border-radius: 18px;
  font-weight: 700;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.5),
              0 0 0 1px rgba(255, 255, 255, 0.3) inset;
  padding: 14px 22px;
}
.stApp button[data-testid="stBaseButton-primary"]:hover {
  background: #e5e5e5;
  box-shadow: 0 10px 34px rgba(0, 0, 0, 0.6),
              0 0 0 1px rgba(255, 255, 255, 0.4) inset;
}
/* The label inside a primary button is a nested span: Streamlit's
   own primary style colours that span white, which beats the
   inherited black above and leaves white text on a white button
   (invisible). Force the label — and the disabled state — black. */
.stApp button[data-testid="stBaseButton-primary"] *,
.stApp button[data-testid="stBaseButton-primary"]:disabled,
.stApp button[data-testid="stBaseButton-primary"]:disabled * {
  color: #000000 !important;
}
.stApp button[data-testid="stBaseButton-primary"]:disabled {
  background: #e5e5e5;
  opacity: 1;
}

/* ---- Step 16: Source tab controls (blue accents, per the reference) ---- */
/* Processing Mode cards: the radio whose option labels contain a bold
   title (<strong>) is the mode radio — its options render as cards;
   the checked card gets a light-blue border + tint. The Voice/Color
   radios have plain-text options, so they are never matched. */
.stApp [data-testid="stRadio"]:has(label strong)
  label:not([data-testid="stWidgetLabel"]) {
  display: block;
  border: 1px solid rgba(255, 255, 255, 0.16);
  border-radius: 16px;
  padding: 14px 16px;
  margin-bottom: 10px;
  background: rgba(255, 255, 255, 0.04);
  cursor: pointer;
}
.stApp [data-testid="stRadio"]:has(label strong)
  label:not([data-testid="stWidgetLabel"]):has(input[type="radio"]:checked) {
  border: 2px solid #5b9dff;
  background: rgba(91, 157, 255, 0.14);
}
.stApp [data-testid="stRadio"]:has(label strong)
  label:not([data-testid="stWidgetLabel"]) p {
  margin: 0;
}
.stApp [data-testid="stRadio"]:has(label strong)
  label:not([data-testid="stWidgetLabel"]) p + p {
  margin-top: 4px;
  color: #a3a3a3;
  font-size: 14px;
}
/* Hide the radio circle inside the mode cards (the card IS the control). */
.stApp [data-testid="stRadio"]:has(label strong)
  label:not([data-testid="stWidgetLabel"]) > span:first-child {
  display: none;
}
/* Pills (aspect / fit / align): unselected = dark chip, selected = blue.
   In Streamlit 1.65 pills render as stButtonGroup buttons with
   data-variant="pills" and data-selected on the chosen one. */
.stApp [data-testid="stButtonGroup"] button[data-variant="pills"] {
  border-radius: 999px;
  border: 1px solid rgba(255, 255, 255, 0.16);
  background: rgba(255, 255, 255, 0.06);
  color: #d4d4d4;
  font-weight: 500;
}
.stApp [data-testid="stButtonGroup"] button[data-variant="pills"][data-selected="true"] {
  background: #3b82f6;
  border-color: #3b82f6;
  color: #ffffff;
  font-weight: 600;
}
.stApp [data-testid="stButtonGroup"] button[data-variant="pills"]:hover {
  border-color: #3b82f6;
  color: #ffffff;
}
/* Toggles (Mirror / random crop): blue track when ON. st.toggle renders
   as stCheckbox with an input[role="switch"]; the track is the label's
   first div. Regular checkboxes (role="checkbox") are never matched. */
.stApp [data-testid="stCheckbox"] label:has(input[role="switch"]:checked) > div:nth-of-type(1) {
  background-color: #3b82f6;
}
/* Sliders in the Source tab: blue thumb. The thumb is the only div
   positioned with translate(-50%, -50%) inside the slider. */
.stApp [data-testid="stSlider"] div[style*="translate(-50%, -50%)"] {
  background-color: #3b82f6;
}

/* ---- Responsive: automatic desktop / mobile layout (CSS only) ----
   No widget-tree change: same controls, same order, same labels,
   same B&W + Step 16 blue accents. Breakpoint 768px — phones keep
   the phone-first stacked layout; wider screens get a wider content
   column + a larger (capped) canvas. No new colours in this block:
   layout / sizing / spacing only. */
@media (max-width: 768px) {
  /* Mobile: tight padding so canvas + controls use the full width. */
  .stApp [data-testid="stMainBlockContainer"] {
    padding-left: 12px;
    padding-right: 12px;
    padding-top: 0.75rem;
    max-width: 100%;
  }
  /* Title fits a narrow screen. */
  .stApp h1 {
    font-size: 1.6rem;
    line-height: 1.25;
  }
  /* Run button: full width, easy thumb target. The button alone
     is not enough: Streamlit shrink-wraps the stButton /
     stElementContainer wrappers to the button's intrinsic width,
     so the wrappers must stretch too. */
  .stApp [data-testid="stElementContainer"]:has(> div[data-testid="stButton"]) {
    width: 100%;
  }
  .stApp [data-testid="stButton"] {
    width: 100%;
  }
  .stApp button[data-testid="stBaseButton-primary"] {
    width: 100%;
  }
  /* Tabs: compact + horizontally scrollable instead of clipped. */
  div[data-testid="stTabs"] div[role="tablist"] {
    flex-wrap: nowrap;
    overflow-x: auto;
    scrollbar-width: none;
  }
  div[data-testid="stTabs"] div[role="tab"] {
    padding: 8px 10px;
    font-size: 14px;
    white-space: nowrap;
  }
  /* Pills: wrap to the next line instead of clipping off-screen. */
  .stApp [data-testid="stButtonGroup"] {
    flex-wrap: wrap;
  }
  .stApp [data-testid="stButtonGroup"] button[data-variant="pills"] {
    padding: 6px 12px;
    font-size: 14px;
  }
  /* Guard: a squeezed slider/stepper row must never cause page scroll. */
  .stApp {
    overflow-x: hidden;
  }
}
@media (min-width: 769px) {
  /* Desktop: wider content column, comfortable reading width. */
  .stApp [data-testid="stMainBlockContainer"] {
    max-width: 980px;
    padding-left: 2rem;
    padding-right: 2rem;
  }
  /* Run button: prominent, not stretched across the whole column. */
  .stApp button[data-testid="stBaseButton-primary"] {
    min-width: 320px;
    padding: 16px 28px;
    font-size: 17px;
  }
  /* Pills: comfortable wrap. */
  .stApp [data-testid="stButtonGroup"] {
    flex-wrap: wrap;
  }
}
</style>
"""
st.markdown(LIQUID_GLASS_CSS, unsafe_allow_html=True)

HEADER_TOP = """\
#  100% Free Online Video Recap

ဗီဒီယိုအရှည်ကြီးတွေကို **မြန်မာ AI အသံ** ဖြင့် အကျဉ်းချုပ်ပေးတဲ့ အခမဲ့စနစ်။
သင့်ရဲ့ **ကိုယ်ပိုင် Free Gemini API Key** နဲ့သာ အလုပ်လုပ်ပါတယ် —
Key ကို server မှာ သိမ်းထားခြင်း **လုံးဝမရှိပါ**။
"""

KEY_GUIDE = """\
###  Free Gemini API Key အခမဲ့ယူနည်း (၁ မိနစ်အတွင်း၊ ကတ်မလို)
1.  **[https://aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)** ကို နှိပ်ပါ
2. **"Create API Key"** → **"Create API key in new project"** ကို ရွေးပါ
3. ရလာတဲ့ Key ကို copy ကူးပြီး အောက်က အကွက်မှာ paste ထည့်ပါ
"""

VOICE_CHOICES = [
    "my-MM-ThihaNeural — ကျားအသံ",
    "my-MM-NilarNeural — မအသံ",
]


def _parse_voice(choice: str) -> str:
    return (choice or "").split(" — ")[0].strip() or "my-MM-ThihaNeural"


def _format_rate_pct(value) -> str:
    """Voice-tab speed slider value (-50..+50) -> Edge-TTS rate string."""
    try:
        return f"{int(value):+d}%"
    except (TypeError, ValueError):
        return "+0%"


def _format_pitch_hz(value) -> str:
    """Voice-tab slider value (-10..+10) -> Edge-TTS pitch string."""
    try:
        return f"{int(value):+d}Hz"
    except (TypeError, ValueError):
        return "+0Hz"


MODEL_OPTIONS = [
    "Auto (အလိုအလျောက် အကောင်းဆုံး Free Model ရွေးမည်)",
    "gemini-3.1-flash-lite (Gemini 3.1 Flash-Lite)",
    "gemini-3.8-flash (Gemini 3.8 Flash)",
    "gemini-3-flash-preview (Gemini 3 Flash)",
    "gemini-3.6-flash (Gemini 3.6 Flash)",
]


def _parse_model(choice: str):
    """Return a model ID, or None for Auto."""
    if not choice or choice.startswith("Auto"):
        return None
    return choice.split(" (")[0].strip() or None


def verify_gemini_api_key(user_api_key: str):
    """Lightweight live check that a Gemini API key actually works.

    Returns (ok, message). Makes exactly one tiny API call (first page of
    the model list) — no video is uploaded and (almost) no tokens are spent.
    """
    key = (user_api_key or "").strip()
    if not key:
        return False, "✕ API Key ထည့်သွင်းပေးပါ"
    try:
        from google import genai

        client = genai.Client(api_key=key)
        # Force exactly one small HTTP request; any auth/network failure
        # raises here, which means the key is not usable.
        next(iter(client.models.list()), None)
        return True, "✓ API Key မှန်ကန်ပါသည်"
    except Exception:  # noqa: BLE001 - any failure means the key can't be used
        return (
            False,
            "✕ API Key မှားယွင်းနေပါသည် သို့မဟုတ် အလုပ်မလုပ်ပါ။ "
            "Key အမှန်ကို ပြန်စစ်ပေးပါ",
        )


# Browser-local key storage (per-user, never touches our server).
# API Keys v2: Gemini keys are a JSON list (up to 5); the old single-key
# entry is migrated on first load. Groq key is a separate single entry.
LS_KEY = "mmr_gemini_api_key"  # legacy single key (migrated, then unused)
LS_KEYS = "mmr_gemini_api_keys"  # JSON array of Gemini keys
LS_GROQ_KEY = "mmr_groq_api_key"
# Browser-local remembered Run (job persistence; see helpers below).
LS_JOB = "mmr_job_id"

if "gemini_keys" not in st.session_state:
    st.session_state["gemini_keys"] = []
if "gemini_key_uids" not in st.session_state:
    # Stable per-row ids, parallel to gemini_keys: widget keys use the
    # uid (not the positional index) so add/remove never shuffles
    # typed text between rows.
    st.session_state["gemini_key_uids"] = []
if "gemini_key_slots" not in st.session_state:
    st.session_state["gemini_key_slots"] = 1
if "keys_bootstrapped" not in st.session_state:
    # True once the key rows were adopted from the browser (or the user
    # saved/added/removed). After bootstrap, session_state is the
    # authority — the browser read never overwrites it again. This is
    # what stops "+ Key" from being clobbered and a removed key from
    # resurrecting via the stale cached read.
    st.session_state["keys_bootstrapped"] = False
if "_key_save_pending" not in st.session_state:
    # Browser write queued by the Remove on_click callback; the tab
    # body flushes it (callbacks cannot emit elements themselves).
    st.session_state["_key_save_pending"] = None
if "groq_api_key" not in st.session_state:
    st.session_state["groq_api_key"] = ""
if "quota_warn" not in st.session_state:
    st.session_state["quota_warn"] = True
if "key_restored" not in st.session_state:
    st.session_state["key_restored"] = False
if "forget_pending" not in st.session_state:
    st.session_state["forget_pending"] = False
if "forgotten_key" not in st.session_state:
    st.session_state["forgotten_key"] = ""
if "forgotten_keys" not in st.session_state:
    # Every Gemini key the user removed this session: never resurrect
    # one for runs, even if a stale browser read still lists it.
    st.session_state["forgotten_keys"] = []
if "groq_forgotten_key" not in st.session_state:
    st.session_state["groq_forgotten_key"] = ""
if "key_load_nonce" not in st.session_state:
    st.session_state["key_load_nonce"] = 0
if "job_load_nonce" not in st.session_state:
    st.session_state["job_load_nonce"] = 0
if "job_save_nonce" not in st.session_state:
    st.session_state["job_save_nonce"] = 0
if "job_clear_pending" not in st.session_state:
    st.session_state["job_clear_pending"] = False
if "job_clear_nonce" not in st.session_state:
    st.session_state["job_clear_nonce"] = 0
if "forget_just_clicked" not in st.session_state:
    st.session_state["forget_just_clicked"] = False

# Step 11 canvas state (survives reruns; reset when the video changes).
if "canvas_blur" not in st.session_state:
    st.session_state["canvas_blur"] = []
if "canvas_sub" not in st.session_state:
    st.session_state["canvas_sub"] = dict(CANVAS_DEFAULT["subtitle"])
if "canvas_vid" not in st.session_state:
    st.session_state["canvas_vid"] = None
if "canvas_vid_sent" not in st.session_state:
    st.session_state["canvas_vid_sent"] = None
# Video source picked INSIDE the canvas (file) or entered there (URL).
if "src_path" not in st.session_state:
    st.session_state["src_path"] = None
if "src_url" not in st.session_state:
    st.session_state["src_url"] = None
if "src_video_id" not in st.session_state:
    st.session_state["src_video_id"] = None
if "src_local_picked" not in st.session_state:
    st.session_state["src_local_picked"] = False
# The canvas phone follows the SOURCE video's own ratio ("1280 / 720").
if "canvas_aspect" not in st.session_state:
    st.session_state["canvas_aspect"] = "9 / 16"
# Demo can be suppressed by the canvas ✕ (a plain flag — widget keys
# like "demo_mode" cannot be written after the widget is created).
if "demo_suppressed" not in st.session_state:
    st.session_state["demo_suppressed"] = False
# Step 16: Source-tab engine defaults (set here, before the canvas call,
# so the canvas preview sees them on the very first run too).
_STEP16_DEFAULTS = {
    "processing_mode": "ai_recap",
    "render_location": "hybrid",
    "aspect_ratio": "9:16",
    "fit_mode": "crop",
    "crop_align": "center",
    "inner_scale": 1.22,
    "vertical_pos": 0.0,
    "horizontal_pos": 0.0,
    "mirror_horizontal": False,
    "copyright_zoom": 1.07,
    "random_crop_offset": False,
}
for _k, _v in _STEP16_DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v
# Persistent per-session dir for the canvas-picked source file.
if "session_dir" not in st.session_state:
    st.session_state["session_dir"] = tempfile.mkdtemp(prefix="recap_session_")

# Ask this browser for a previously saved key (cached after first answer).
# The component key includes a nonce: bumping the nonce forces a FRESH
# read. Without it the component keeps returning its first (stale) value
# even after localStorage changes (Verify-save / Forget).
# API Keys v2: the Gemini keys are a JSON list. The legacy single-key
# entry is migrated into the list on first load.
browser_saved_keys_raw = streamlit_js_eval(
    js_expressions=f"localStorage.getItem('{LS_KEYS}')",
    key=f"mmr_keys_load_{st.session_state['key_load_nonce']}",
)
browser_saved_legacy = streamlit_js_eval(
    js_expressions=f"localStorage.getItem('{LS_KEY}')",
    key=f"mmr_key_legacy_load_{st.session_state['key_load_nonce']}",
)
browser_saved_groq = streamlit_js_eval(
    js_expressions=f"localStorage.getItem('{LS_GROQ_KEY}')",
    key=f"mmr_groq_load_{st.session_state['key_load_nonce']}",
)


def _parse_saved_keys(raw):
    """Parse the saved Gemini key list (JSON array)."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, list):
        return [k for k in data if isinstance(k, str) and k.strip()]
    return []


browser_saved_keys = _parse_saved_keys(browser_saved_keys_raw)
# Migrate the legacy single key once.
if not browser_saved_keys and browser_saved_legacy:
    _legacy = browser_saved_legacy.strip()
    if _legacy and _legacy not in (st.session_state.get("forgotten_keys") or []):
        browser_saved_keys = [_legacy]


def _new_key_uid():
    """A stable, unique id for one key-list row (widget keys use the
    uid, never the positional index, so add/remove can't shuffle
    typed text between rows)."""
    return uuid.uuid4().hex[:8]


def _add_key_row():
    """on_click for "+ Key": bumps the row count. Runs BEFORE the
    script body, so the new row renders in the same run (an inline
    handler after the rows would need another rerun to show it)."""
    _slots = max(1, min(5, int(st.session_state.get("gemini_key_slots") or 1)))
    st.session_state["gemini_key_slots"] = min(5, _slots + 1)


def _remove_key_row(uid):
    """on_click for a row's Remove: deletes the row from session state
    BEFORE the script body, so the row disappears in the same run.
    Queues the browser write via _key_save_pending — a callback cannot
    emit the js-eval iframe itself, and the tab body flushes it below
    with no st.rerun() after (st.rerun() would discard the iframe and
    the key would resurrect from the stale cached read)."""
    _keys = list(st.session_state.get("gemini_keys") or [])
    _uids = list(st.session_state.get("gemini_key_uids") or [])
    try:
        _idx = _uids.index(uid)
    except ValueError:
        return  # stale double-click; nothing to do
    _removed = (_keys[_idx] or "").strip() if _idx < len(_keys) else ""
    _new_keys = [k for j, k in enumerate(_keys) if j != _idx]
    _new_uids = [u for j, u in enumerate(_uids) if j != _idx]
    if _removed:
        _fg = list(st.session_state.get("forgotten_keys") or [])
        if _removed not in _fg:
            _fg.append(_removed)
        st.session_state["forgotten_keys"] = _fg
    # Never leave zero rows: keep one empty Key 1 row.
    if not _new_keys:
        _new_keys = [""]
        _new_uids = [_new_key_uid()]
    st.session_state["gemini_keys"] = _new_keys
    st.session_state["gemini_key_uids"] = _new_uids
    st.session_state["gemini_key_slots"] = max(1, len(_new_keys))
    # Drop only the removed row's widget state: stable uids keep the
    # other rows' typed text intact.
    _box_key = f"api_key_box_{uid}"
    if _box_key in st.session_state:
        del st.session_state[_box_key]
    st.session_state["_key_save_pending"] = [k for k in _new_keys if k]


# Bootstrap the key rows from the browser ONCE per session. After
# bootstrap (or after the user saves a key), session_state is the
# authority and the browser read never overwrites it again.
#
# The old "restore on EVERY run" caused both live bugs:
#  (1) "+ Key" taps were clobbered — slots were recomputed from the
#      saved list on the rerun, wiping the +1 before it rendered;
#  (2) removed keys resurrected — the Remove handler's st.rerun()
#      discarded the localStorage.setItem iframe, so the write never
#      executed and the next run's stale cached read re-populated
#      the list (green pill and all).
# A server session reset wipes session_state, so the bootstrap simply
# runs again — the user never has to re-enter keys.
if not st.session_state.get("keys_bootstrapped"):
    _adopt = [k for k in (browser_saved_keys or [])
              if k not in (st.session_state.get("forgotten_keys") or [])]
    _typed_now = any(
        st.session_state.get(f"api_key_box_{_u}")
        for _u in (st.session_state.get("gemini_key_uids") or [])
    )
    # A typed-but-unverified key in a box is never overwritten.
    if _adopt and not _typed_now:
        _n = max(1, min(5, len(_adopt)))
        st.session_state["gemini_keys"] = list(_adopt[:5])
        st.session_state["gemini_key_uids"] = [
            _new_key_uid() for _ in range(_n)]
        # Never shrink the row count on adopt: keep extra empty rows
        # the user added before the read resolved.
        st.session_state["gemini_key_slots"] = max(
            _n, int(st.session_state.get("gemini_key_slots") or 1))
        st.session_state["key_restored"] = True
        st.session_state["keys_bootstrapped"] = True

if (
    browser_saved_groq
    and browser_saved_groq != st.session_state.get("groq_forgotten_key")
    and not st.session_state.get("groq_api_key_box")
    and not st.session_state.get("groq_forget_pending")
):
    st.session_state["groq_api_key"] = browser_saved_groq


def _get_active_gemini_keys():
    """The run's Gemini key list (API Keys v2).

    Session-state keys first. Before the key UI bootstraps (very first
    runs, browser read not yet resolved), fall back to the browser-saved
    list. After bootstrap the session is authoritative: an empty session
    means the user removed every key, and falling back here would
    resurrect removed keys for runs. A key the user removed this
    session is never used, even if a stale cached read still lists it.
    """
    _keys = [k for k in (st.session_state.get("gemini_keys") or [])
             if isinstance(k, str) and k.strip()]
    if not _keys and not st.session_state.get("keys_bootstrapped"):
        _forgotten = set(st.session_state.get("forgotten_keys") or [])
        _saved = [k for k in (browser_saved_keys or [])
                  if k not in _forgotten]
        _keys = _saved
    # Dedupe, preserve order.
    _seen = set()
    _out = []
    for _k in _keys:
        if _k not in _seen:
            _seen.add(_k)
            _out.append(_k)
    return _out


def _get_active_groq_key():
    """The Groq key (single, optional)."""
    _g = (st.session_state.get("groq_api_key") or "").strip()
    if not _g:
        _g = (browser_saved_groq or "").strip()
        if _g == st.session_state.get("groq_forgotten_key"):
            _g = ""
    return _g

# Ask this browser for a remembered Run (job persistence): the
# component key includes a nonce, bumped on every Run save, so the
# read stays fresh (same staleness trap as the key load).
_browser_job = streamlit_js_eval(
    js_expressions=f"localStorage.getItem('{LS_JOB}')",
    key=f"mmr_job_load_{st.session_state['job_load_nonce']}",
)
# Parsed stored job (if any): {"run_dir": ..., "saved_at": ...}.
# Kept in session_state so the progress fragment (which re-runs
# without the main script) can re-attach to a job from a dead session.
try:
    _parsed_job = json.loads(_browser_job) if _browser_job else None
except ValueError:
    _parsed_job = None
if isinstance(_parsed_job, dict) and _parsed_job.get("run_dir"):
    st.session_state["stored_job"] = _parsed_job
elif _parsed_job is not None:
    st.session_state["stored_job"] = None
# Consume a stale remembered job (v1.7): the fragment schedules this
# when a remembered job is older than the stale window — the entry
# is removed from localStorage so the lost message shows ONCE and
# never nags on later loads. Runs in the main script (not the
# fragment): a js-eval placed before the fragment's st.rerun() would
# never render, because st.rerun() discards the current run's output.
if st.session_state.get("job_clear_pending"):
    st.session_state["job_clear_pending"] = False
    st.session_state["job_clear_nonce"] = (
        int(st.session_state.get("job_clear_nonce") or 0) + 1)
    streamlit_js_eval(
        js_expressions=f"localStorage.removeItem('{LS_JOB}')",
        key=f"mmr_job_clear_{st.session_state['job_clear_nonce']}",
    )

st.markdown(HEADER_TOP)

def _demo_toggled() -> None:
    # Re-arming the Demo checkbox clears a suppression set by canvas ✕.
    st.session_state["demo_suppressed"] = False


# Demo state is read from session_state here (the checkbox itself lives
# in the Source tab, below the canvas). Streamlit sets widget state from
# the frontend before the script runs, so this already reflects the
# current checkbox value.
demo_active = bool(st.session_state.get("demo_mode", False)) and not st.session_state.get("demo_suppressed")

# ---- Step 11: Interactive Main Canvas (TOP; video input lives inside it) ----
_sub_default = dict(CANVAS_DEFAULT["subtitle"])


def _reset_canvas_state() -> None:
    st.session_state["canvas_blur"] = []
    st.session_state["canvas_sub"] = dict(_sub_default)


def _delete_quietly(path) -> None:
    """Delete one file, ignoring missing files / errors."""
    try:
        if path and os.path.isfile(str(path)):
            os.remove(str(path))
    except OSError:
        pass


def _delete_input_file() -> None:
    """Delete the currently uploaded picked video (if any).

    Only a locally-picked upload is deleted — never the bundled demo
    sample. Called when the input is superseded (new pick / remove /
    new URL) or after a successful result (auto-delete). While a job
    is running, the file itself is left alone (the worker is still
    reading it) — the fragment deletes the job's own input path on
    completion — but the session pointer is still cleared.
    """
    _p = st.session_state.get("src_path")
    if _p and st.session_state.get("src_local_picked"):
        if not st.session_state.get("run_job"):
            _delete_quietly(_p)
    st.session_state["src_path"] = None
    st.session_state["src_local_picked"] = False
    st.session_state["src_video_id"] = None


def _delete_result_files() -> None:
    """Delete the previous run's output files (video + SRT + thumbnail),
    if any — except files still referenced by the in-session library."""
    _lib_paths = set()
    for _e in (st.session_state.get("recap_library") or []):
        for _k in ("video_path", "srt_path", "thumbnail_path"):
            if _e.get(_k):
                try:
                    _lib_paths.add(os.path.abspath(str(_e[_k])))
                except OSError:
                    pass
    _r = st.session_state.get("recap_result") or {}
    for _k in ("video_path", "srt_path", "thumbnail_path"):
        _p = _r.get(_k)
        if _p:
            try:
                if os.path.abspath(str(_p)) not in _lib_paths:
                    _delete_quietly(_p)
            except OSError:
                _delete_quietly(_p)
    st.session_state["recap_result"] = None


def _cleanup_run_workdir(run_dir, keep_paths) -> None:
    """Delete everything in a run's working dir except keep_paths.

    After a successful result the final video + SRT stay (the user
    still needs to download them); the voiceover, subtitle PNGs,
    concat lists and any other intermediates are deleted.
    """
    if not run_dir or not os.path.isdir(str(run_dir)):
        return
    _keep = {os.path.abspath(str(p)) for p in keep_paths if p}
    try:
        for _name in os.listdir(run_dir):
            _p = os.path.join(run_dir, _name)
            try:
                if os.path.abspath(_p) in _keep:
                    continue
                if os.path.isfile(_p):
                    os.remove(_p)
                elif os.path.isdir(_p):
                    shutil.rmtree(_p, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


# ---- Job persistence: a Run's status lives in a file, not only in
# session memory, so a session reset mid-run (Streamlit reconnect /
# container hiccup under CPU throttle) no longer makes the job vanish
# silently. The browser keeps the run_dir in localStorage (mmr_job_id);
# a fresh session re-attaches to the file: running -> live progress
# from the file, done -> result restored, error -> error restored,
# file gone shortly after the Run -> a "lost" error instead of idle.
_JOB_LOST_MSG = (
    "ထုတ်လုပ်မှု အလယ်မှာ ပြတ်သွားပါပြီ — app က ပြန်စသွားလို့ "
    "ဒီ Run ရဲ့ ရလဒ် ပျောက်သွားပါပြီ။ Run ကို ထပ်နှိပ်ပြီး ပြန်စပေးပါ။"
)
# Distinct message when the SERVER itself restarted mid-run (the job
# died with the old process; its /tmp status file is gone). The user
# deserves the exact cause, not a vague "lost".
_JOB_RESTART_MSG = (
    "Server ပြန်စသွားလို့ ဒီ Run ရဲ့ အလုပ် ပျောက်သွားပါပြီ — "
    "Server restart ဖြစ်သွားတာပါ။ Run ကို ထပ်နှိပ်ပြီး ပြန်စပေးပါ။"
)
_JOB_STALE_RUNNING_S = 15 * 60  # file untouched this long -> worker dead
_JOB_LOST_MIN_S = 60  # file missing sooner -> worker may not have started yet
# Hybrid: the browser→Gemini upload alone takes ~45s, and a throttled
# server is slow to launch the worker — give a Hybrid Run longer
# before its missing status file counts as lost.
_JOB_LOST_MIN_S_HYBRID = 180
_JOB_LOST_MAX_S = 30 * 60  # file missing later -> stale entry, ignore
# Distinct message when the Run never started on the server (run dir
# exists but no status file was ever written — the handoff dropped
# the Run, e.g. the gemini_uploaded action never reached the
# starter). This is NOT a server restart: do not blame one.
_JOB_NEVER_STARTED_MSG = (
    "Run က server မှာ မစတင်ခဲ့ပါ — အလုပ် server ကို မရောက်ခဲ့ပါ။ "
    "Run ကို ထပ်နှိပ်ပြီး ပြန်စပေးပါ။"
)


def _lost_message(saved_at, run_dir, last_detail="") -> str:
    """Honest lost-job message for a missing status file.

    The restart claim rests on the boot-time check (the process
    booted after the save) — that holds even when the run dir
    survived (a restart does not always wipe /tmp). The NEW
    distinction is the never-started case: the process did NOT
    boot after the save, yet no status file exists even though
    the run dir does — the Run never started on the server
    (the handoff dropped it), so say that instead of blaming a
    restart. Appends the last known stage when it is known.
    """
    if _classify_lost_job(saved_at) == "server_restart":
        _msg = _JOB_RESTART_MSG
    elif os.path.isdir(str(run_dir)):
        _msg = _JOB_NEVER_STARTED_MSG
    else:
        _msg = _JOB_LOST_MSG
    if last_detail:
        _msg = f"{_msg} (နောက်ဆုံး အဆင့်: {last_detail})"
    return _msg


def _classify_lost_job(saved_at, boot_time=BOOT_TIME) -> str:
    """Classify a remembered job whose status file is gone / stale.

    Returns "server_restart" when the server process booted AFTER the
    job was saved (the job died with the old process), else "lost"
    (worker died without a restart, or the file was never written).
    Pure function — unit-testable without Streamlit.
    """
    try:
        _saved = float(saved_at or 0)
    except (TypeError, ValueError):
        _saved = 0.0
    if _saved and float(boot_time) > _saved:
        return "server_restart"
    return "lost"


def _job_status_path(run_dir) -> str:
    return os.path.join(str(run_dir), "job_status.json")


def _write_job_status(run_dir, payload) -> None:
    """Atomic best-effort write; a failed write must never break the run."""
    try:
        _tmp = os.path.join(str(run_dir), ".job_status.tmp")
        with open(_tmp, "w", encoding="utf-8") as _f:
            json.dump(payload, _f)
        os.replace(_tmp, _job_status_path(run_dir))
    except OSError:
        pass


def _read_job_status(run_dir):
    try:
        with open(_job_status_path(run_dir), encoding="utf-8") as _f:
            _data = json.load(_f)
        return _data if isinstance(_data, dict) else None
    except (OSError, ValueError):
        return None


def _job_file_snapshot(snap) -> Dict:
    return {
        "run_dir": snap.get("run_dir"),
        "input_path": snap.get("input_path"),
        "src_local_picked_at_start": bool(snap.get("src_local_picked_at_start")),
    }


def _job_file_result(result) -> Dict:
    # Hybrid: the pack itself lives on disk (pack.json + voiceover.mp3
    # in pack_dir) — the status file keeps only the summary + pack_dir
    # so a session reset can re-attach and re-send the pack. The
    # voiceover bytes / font payload must NEVER land in this file.
    if result.get("hybrid_pack"):
        return {
            "hybrid_pack": True,
            "pack_dir": result.get("pack_dir"),
            "pack_id": (result.get("pack") or {}).get("pack_id"),
            "aspect_ratio": result.get("aspect_ratio"),
            "subtitle_mode": result.get("subtitle_mode"),
            "sync_method": result.get("sync_method"),
            "video_speed_factor": result.get("video_speed_factor"),
            "title": result.get("title"),
            "script_text": result.get("script_text"),
            "narration_coverage": result.get("narration_coverage"),
            "processing_mode": result.get("processing_mode"),
            "continuation_rounds": result.get("continuation_rounds"),
            "continuation_cap_hit": result.get("continuation_cap_hit"),
            "continuation_log": result.get("continuation_log"),
            "continuation_error": result.get("continuation_error"),
            "pro_fill_status": result.get("pro_fill_status"),
        }
    return {
        "aspect_ratio": result.get("aspect_ratio"),
        "subtitle_mode": result.get("subtitle_mode"),
        "sync_method": result.get("sync_method"),
        "video_speed_factor": result.get("video_speed_factor"),
        "final_video_path": result.get("final_video_path"),
        "srt_path": result.get("srt_path"),
        "title": result.get("title"),
        "script_text": result.get("script_text"),
        "narration_coverage": result.get("narration_coverage"),
        # Coverage fix v3 diagnostics (AI continuation + Pro fill).
        "continuation_rounds": result.get("continuation_rounds"),
        "continuation_cap_hit": result.get("continuation_cap_hit"),
        "continuation_log": result.get("continuation_log"),
        "continuation_error": result.get("continuation_error"),
        "pro_fill_status": result.get("pro_fill_status"),
    }


def _success_status_line(result) -> str:
    """Build the exact success line from a result dict (server + hybrid).

    Shared by _apply_job_result (server finalize) and the Hybrid
    encode-done handler — both must show the same info, including
    the narration coverage ("အသံ: X/Y").
    """
    _sync_note = str(result.get("sync_method"))
    if result.get("video_speed_factor") is not None:
        _sync_note += f" ({result['video_speed_factor']})"
    # Narration coverage (Fix 3): "အသံ: 1:43/8:04" — how much of
    # the video actually has Myanmar speech.
    _cov_note = ""
    _cov = result.get("narration_coverage") or {}
    if _cov.get("total_s"):
        def _mmss(_s):
            _s = max(0, int(round(float(_s))))
            return f"{_s // 60}:{_s % 60:02d}"
        _cov_note = (
            f" | အသံ: {_mmss(_cov.get('covered_s', 0))}"
            f"/{_mmss(_cov.get('total_s', 0))}"
        )
        # Coverage fix v3: if the continuation loop hit its round
        # budget below the 90% target, say so.
        if result.get("continuation_cap_hit"):
            _cov_note += " — continuation cap reached"
    return (
        f"✓ ပြီးဆုံးပါပြီ! | {result.get('aspect_ratio')} | "
        f"စာတန်း: {result.get('subtitle_mode')} | "
        f"sync: {_sync_note}{_cov_note}"
    )


def _apply_job_result(result, snap) -> Dict:
    """Build recap_result + cleanup + auto-delete input (idempotent).

    Server mode only (Hybrid results never reach here — the pack
    lives on disk and the device encodes). Runs on the script thread
    only (never the worker): it touches session_state and deletes
    files, so both the in-memory finalize path and the file re-attach
    path share it.
    """
    _recap = {
        "status": _success_status_line(result),
        "video_path": result.get("final_video_path"),
        "srt_path": result.get("srt_path"),
        "title": result.get("title"),
        "script_text": result.get("script_text"),
        # v1.13: Post Kit + thumbnail + output naming + library entry.
        "postkit": result.get("postkit"),
        "thumbnail_path": result.get("thumbnail_path"),
        "output_name": os.path.basename(
            str(result.get("final_video_path") or "recap.mp4")),
        "processing_mode": result.get("processing_mode"),
        "duration": (result.get("metadata") or {}).get("duration"),
        # API Keys v2: quota-rotation warnings (non-fatal).
        "quota_warnings": list(result.get("quota_warnings") or []),
    }
    _cleanup_run_workdir(
        snap.get("run_dir"),
        [result.get("final_video_path"), result.get("srt_path"),
         result.get("thumbnail_path"),
         _job_status_path(snap.get("run_dir"))])
    # Library (v1.13): in-session list of completed runs. Streamlit
    # Cloud disk is ephemeral — this list lives only for this session.
    _lib = st.session_state.get("recap_library") or []
    _lib = [_e for _e in _lib
            if _e.get("video_path") != _recap["video_path"]]
    _lib.insert(0, dict(_recap))
    # Cap at 5 (disk): each entry keeps its MP4 on the ephemeral disk.
    _old = _lib[5:]
    st.session_state["recap_library"] = _lib[:5]
    for _e in _old:
        for _k in ("video_path", "srt_path", "thumbnail_path"):
            _delete_quietly(_e.get(_k))
    # Auto-delete the uploaded input (the result no longer needs
    # it; a re-Run re-uploads from the canvas if asked).
    if snap.get("src_local_picked_at_start"):
        _delete_quietly(snap.get("input_path"))
        if st.session_state.get("src_path") == snap.get("input_path"):
            st.session_state["src_path"] = None
            st.session_state["src_local_picked"] = False
            st.session_state["src_video_id"] = None
    return _recap


# ---- Async Run: the pipeline runs in a worker thread so the UI can
# show live progress (percentage + elapsed + estimated remaining).
# The worker NEVER touches st.session_state — it only mutates the
# plain job dict (under its lock). The progress fragment (script
# thread) reads the dict, and on completion stores the result in
# session_state and reruns.
_RUN_PHASE_LABELS = {
    "uploading": "ဗီဒီယို တင်နေသည်…",
    "analyze": "AI က ဗီဒီယို စိစစ်နေသည်…",
    "tts": "မြန်မာအသံ ထုတ်နေသည်…",
    "render": "ဗီဒီယို ပေါင်းနေသည်…",
    "pack": "Hybrid pack ထုတ်နေသည်…",
    "done": "ပြီးဆုံးပါပြီ",
}


def _run_pipeline_job(job: Dict) -> None:
    """Worker-thread body: run the pipeline, report progress into job."""
    snap = job["snapshot"]
    lock = job["lock"]

    def _cb(pct, phase, detail):
        with lock:
            job["pct"] = float(pct)
            job["phase"] = phase
            job["detail"] = detail
        # Mirror progress to the status file so a fresh session can
        # re-attach if this session dies mid-run.
        _write_job_status(snap["run_dir"], {
            "status": "running",
            "pct": float(pct),
            "phase": phase,
            "detail": detail,
            "started_at": job.get("started_at", time.time()),
            "snapshot": _job_file_snapshot(snap),
        })

    def _record_quota_warning(msg: str) -> None:
        # API Keys v2: a key exhausted mid-run and rotation happened.
        # Recorded on the job (thread-safe); the UI shows it as a
        # non-fatal warning banner when the run finishes (and the
        # result also carries quota_warnings).
        with lock:
            _wl = job.setdefault("quota_warnings", [])
            if msg not in _wl:
                _wl.append(msg)

    try:
        if snap.get("render_location") == "hybrid":
            # Hybrid: the browser holds the source video (never
            # uploaded to Streamlit). The server does Gemini analysis
            # (from the browser-uploaded Gemini file) + TTS + pack
            # building only — no FFmpeg render. The device encodes.
            result = run_full_recap_pipeline(
                video_path=None,
                video_url=None,
                user_api_key=snap.get("gemini_keys") or [],
                groq_api_key=snap.get("groq_api_key") or "",
                on_quota_warning=(_record_quota_warning
                                  if snap.get("quota_warn", True)
                                  else None),
                voice_name=snap["voice_name"],
                pitch=_format_pitch_hz(snap.get("voice_pitch_hz", 0)),
                processing_mode=snap["processing_mode"],
                aspect_ratio=snap["aspect_ratio"],
                fit_mode=snap["fit_mode"],
                crop_align=snap["crop_align"],
                inner_scale=snap["inner_scale"],
                vertical_pos=snap["vertical_pos"],
                horizontal_pos=snap["horizontal_pos"],
                mirror_horizontal=snap["mirror_horizontal"],
                copyright_zoom=snap["copyright_zoom"],
                random_crop_offset=snap["random_crop_offset"],
                bg_volume=snap["bg_volume"],
                burn_subtitles=snap["burn_subtitles"],
                work_dir=snap["run_dir"],
                output_dir=snap["run_dir"],
                output_name=(
                    sanitize_filename(
                        snap.get("output_filename") or "recap") + ".mp4"),
                use_mock_plan=snap["demo_active"],
                tts_proxy=snap["tts_proxy"],
                model=snap["model"],
                blur_boxes=snap["blur_boxes"],
                subtitle_config=snap["subtitle_config"],
                on_progress=_cb,
                rate=_format_rate_pct(snap.get("voice_speed_pct", 0)),
                quality=str(snap.get("render_quality", "fast")),
                fps=int(snap.get("output_fps", 30)),
                watermark=snap.get("watermark"),
                logo=snap.get("logo"),
                postkit_extra_hashtags=snap.get("postkit_hashtags", ""),
                postkit_append_fyp=bool(snap.get("postkit_fyp", True)),
                hybrid_pack=True,
                gemini_file_name=snap.get("gemini_file_name"),
                video_duration=snap.get("video_duration"),
                src_width=snap.get("src_width"),
                src_height=snap.get("src_height"),
            )
        else:
            # Server mode: today's EXACT flow, untouched.
            result = run_full_recap_pipeline(
                video_path=snap["src_path"],
                video_url=snap["src_url"],
                user_api_key=snap.get("gemini_keys") or [],
                groq_api_key=snap.get("groq_api_key") or "",
                on_quota_warning=(_record_quota_warning
                                  if snap.get("quota_warn", True)
                                  else None),
                # No target_duration: AI Recap is always full-duration
                # (pipeline ignores target_duration; see pipeline.py).
                voice_name=snap["voice_name"],
                pitch=_format_pitch_hz(snap.get("voice_pitch_hz", 0)),
                processing_mode=snap["processing_mode"],
                aspect_ratio=snap["aspect_ratio"],
                fit_mode=snap["fit_mode"],
                crop_align=snap["crop_align"],
                inner_scale=snap["inner_scale"],
                vertical_pos=snap["vertical_pos"],
                horizontal_pos=snap["horizontal_pos"],
                mirror_horizontal=snap["mirror_horizontal"],
                copyright_zoom=snap["copyright_zoom"],
                random_crop_offset=snap["random_crop_offset"],
                bg_volume=snap["bg_volume"],
                burn_subtitles=snap["burn_subtitles"],
                work_dir=snap["run_dir"],
                output_dir=snap["run_dir"],
                output_name=(
                    sanitize_filename(
                        snap.get("output_filename") or "recap") + ".mp4"),
                use_mock_plan=snap["demo_active"],
                tts_proxy=snap["tts_proxy"],
                model=snap["model"],
                rate=_format_rate_pct(snap.get("voice_speed_pct", 0)),
                quality=str(snap.get("render_quality", "fast")),
                fps=int(snap.get("output_fps", 30)),
                watermark=snap.get("watermark"),
                logo=snap.get("logo"),
                postkit_extra_hashtags=snap.get("postkit_hashtags", ""),
                postkit_append_fyp=bool(snap.get("postkit_fyp", True)),
                blur_boxes=snap["blur_boxes"],
                subtitle_config=snap["subtitle_config"],
                on_progress=_cb,
            )
        with lock:
            job["status"] = "done"
            job["pct"] = 100.0
            job["phase"] = "done"
            job["detail"] = "ပြီးဆုံးပါပြီ"
            job["result"] = result
        _write_job_status(snap["run_dir"], {
            "status": "done",
            "pct": 100.0,
            "phase": "done",
            "detail": "ပြီးဆုံးပါပြီ",
            "started_at": job.get("started_at", time.time()),
            "snapshot": _job_file_snapshot(snap),
            "result": _job_file_result(result),
        })
    except Exception as exc:  # noqa: BLE001 - surfaced in the fragment
        traceback.print_exc()
        with lock:
            job["status"] = "error"
            job["error"] = str(exc)
        _write_job_status(snap["run_dir"], {
            "status": "error",
            "pct": job.get("pct", 0.0),
            "phase": job.get("phase", "analyze"),
            "detail": str(exc),
            "started_at": job.get("started_at", time.time()),
            "snapshot": _job_file_snapshot(snap),
            "error": str(exc),
        })


# Phone-hang fix: poll every 5s, not every 1s (was 3s). A 1s interval
# meant ~360 fragment reruns over a 6-min Pro run — constant network +
# CPU on the phone for a progress bar that only changes in bursts
# anyway. 5s is still live (progress + elapsed tick every 5s) with
# 5x less load than 1s; renders take minutes, so a 5s tick is plenty.
@st.fragment(run_every=5)
def _render_run_progress() -> None:
    """Poll the async job and render live progress / finalize it.

    While a Run is pending upload (video not on the server yet) the
    bar shows the upload wait; while the pipeline runs it shows the
    measured percentage + elapsed + estimated remaining. On
    completion (script thread): auto-delete the uploaded input +
    run intermediates (keep final video + SRT for download), store
    the result, clear the job, and full-rerun to show it.
    """
    job = st.session_state.get("run_job")
    pending = st.session_state.get("pending_job")
    if job is None and pending is not None and pending.get("needs_upload"):
        elapsed = max(0.0, time.time() - float(pending.get("clicked_at", time.time())))
        st.progress(0.0,
                    text=f"ဗီဒီယို တင်နေသည်… — 0% | {elapsed:.0f}s ကြာပြီး")
        st.caption("ဗီဒီယို server ကို တင်နေသည်… Run နှိပ်ပြီးမှ တင်တာပါ။")
        return
    if job is None and pending is not None and pending.get("needs_gemini_upload"):
        # Hybrid: the browser uploads the video DIRECTLY to Gemini
        # (never to Streamlit) — show that upload's progress.
        # v1.8: the canvas reports its TRUE sub-stage (metadata /
        # FFmpeg / upload) — the old code showed "Gemini upload"
        # from Run press on, so a hang in readiness/probe looked
        # like an upload hang. The stage text makes a future hang
        # locatable from the UI alone.
        _gup = st.session_state.get("gemini_upload") or {}
        try:
            _gpct = float(_gup.get("pct", 0.0))
        except (TypeError, ValueError):
            _gpct = 0.0
        elapsed = max(0.0, time.time() - float(pending.get("clicked_at", time.time())))
        _gstage = st.session_state.get("gemini_upload_stage") or "upload"
        if _gstage == "ffmpeg":
            st.progress(0.0,
                        text=f"FFmpeg ဖွင့်နေသည်… — 0% | {elapsed:.0f}s ကြာပြီး")
            st.caption("ဖုန်းထဲက FFmpeg ကို ဖွင့်နေသည်…")
        elif _gstage == "metadata":
            st.progress(0.0,
                        text=f"Metadata စစ်နေသည်… — 0% | {elapsed:.0f}s ကြာပြီး")
            st.caption("ဗီဒီယိုရဲ့ Metadata ကို စစ်နေသည်…")
        else:
            st.progress(min(1.0, _gpct / 100.0),
                        text=f"Gemini ကို ဗီဒီယို တင်နေသည်… — {_gpct:.0f}% | {elapsed:.0f}s ကြာပြီး")
            st.caption("ဗီဒီယိုကို Gemini ကို တိုက်ရိုက် တင်နေသည်… server ကို မတင်ပါ။")
        return
    if not job:
        # No in-memory job: re-attach to a job from a dead session, if
        # the browser remembers one (stored_job) and its status file
        # exists. Without this, a session reset mid-run looks like the
        # page silently going idle (the observed Pro failure).
        _stored = st.session_state.get("stored_job") or {}
        _fdir = _stored.get("run_dir")
        # DIAGNOSTIC: show whether the browser remembers a Run, so a
        # lost job can be told apart from a job that was never saved.
        # Shown only while the remembered job has NOT been restored
        # yet (after a success it would be stale noise).
        if _fdir and st.session_state.get("restored_job_dir") != _fdir:
            _dsaved = float(_stored.get("saved_at") or 0)
            _dage = max(0.0, time.time() - _dsaved) if _dsaved else 0.0
            st.caption(
                f"🔎 မှတ်ထားတဲ့ Run ရှိသည်: "
                f"{os.path.basename(str(_fdir))} | "
                f"{_dage:.0f}s အလို သိမ်းထားသည်")
        if _fdir and st.session_state.get("restored_job_dir") != _fdir:
            _fstatus = _read_job_status(_fdir)
            if _fstatus is not None:
                _fst = _fstatus.get("status")
                _fsnap = _fstatus.get("snapshot") or {}
                if _fst == "done" and _fstatus.get("result"):
                    _fres = _fstatus["result"]
                    if _fres.get("hybrid_pack"):
                        # Hybrid re-attach: the pack persists on disk
                        # (pack.json + voiceover.mp3) — re-send it to
                        # the device instead of rebuilding anything.
                        st.session_state["hybrid_pack_dir"] = (
                            _fres.get("pack_dir"))
                        st.session_state["hybrid_pack_meta"] = {
                            k: _fres.get(k) for k in (
                                "title", "script_text", "sync_method",
                                "subtitle_mode", "aspect_ratio",
                                "narration_coverage", "processing_mode",
                                "pack_id", "continuation_cap_hit")}
                        st.session_state["hybrid_pack_acked_id"] = None
                        st.session_state["hybrid_encode"] = {
                            "status": "waiting", "pct": 0.0, "detail": ""}
                        st.session_state["hybrid_result"] = None
                        st.session_state["restored_job_dir"] = _fdir
                        st.rerun(scope="app")
                    st.session_state["recap_result"] = _apply_job_result(
                        _fres, _fsnap)
                    st.session_state["restored_job_dir"] = _fdir
                    st.rerun(scope="app")
                if _fst == "error":
                    _ferr = _fstatus.get("error") or "အမည်မသိ အမှား"
                    if st.session_state.get("run_error") != _ferr:
                        st.session_state["run_error"] = _ferr
                        st.rerun(scope="app")
                if _fst == "running":
                    # Worker dead but file survived? A stale file means
                    # the worker is gone — report it instead of a
                    # frozen bar. (A live worker updates the file on
                    # every progress callback.)
                    try:
                        _fage = time.time() - os.path.getmtime(
                            _job_status_path(_fdir))
                    except OSError:
                        _fage = 0.0
                    if _fage > _JOB_STALE_RUNNING_S:
                        # A terminal error already recorded (e.g. the
                        # worker's real error, or an upload error) is
                        # NEVER overwritten by the generic lost /
                        # restart message — the real cause wins.
                        if not st.session_state.get("run_error"):
                            _msg = (
                                _JOB_RESTART_MSG
                                if _classify_lost_job(
                                    _fstatus.get("started_at")
                                    or _stored.get("saved_at")) == "server_restart"
                                else _JOB_LOST_MSG)
                            # Honest lost message: include the job's
                            # last known phase/detail from its status
                            # file instead of a bare generic claim.
                            _flast = (
                                f"{_fstatus.get('phase', '')} — "
                                f"{_fstatus.get('detail', '')}"
                            ).strip(" —")
                            if _flast:
                                _msg = (
                                    f"{_msg} "
                                    f"(နောက်ဆုံး အဆင့်: {_flast})")
                            st.session_state["run_error"] = _msg
                            st.rerun(scope="app")
                    else:
                        _fpct = float(_fstatus.get("pct", 0.0))
                        _fstarted = float(
                            _fstatus.get("started_at", time.time()))
                        _felapsed = max(0.0, time.time() - _fstarted)
                        _feta = ""
                        if _fpct >= 10.0:
                            _fremain = _felapsed * (100.0 - _fpct) / _fpct
                            _feta = f" | ခန့်မှန်း ကျန်ချိန် ~{_fremain:.0f}s"
                        _flabel = _RUN_PHASE_LABELS.get(
                            _fstatus.get("phase", "analyze"),
                            _fstatus.get("detail") or "")
                        st.progress(
                            min(1.0, _fpct / 100.0),
                            text=(f"{_flabel} — {_fpct:.0f}% | "
                                  f"{_felapsed:.0f}s ကြာပြီး{_feta}"))
                        st.caption(_fstatus.get("detail") or "")
                        return
            else:
                # No status file: the Run was lost before it could write
                # one (or the container wiped /tmp). Only call it lost
                # inside a window — too soon and the worker may not
                # have started yet; too late and it's a stale entry.
                # Hybrid gets the longer grace window (its Gemini
                # upload alone takes ~45s on top of throttled start).
                _saved_at = float(_stored.get("saved_at") or 0)
                _fage = time.time() - _saved_at if _saved_at else 0.0
                _flost_min = (
                    _JOB_LOST_MIN_S_HYBRID
                    if _stored.get("render_location") == "hybrid"
                    else _JOB_LOST_MIN_S)
                if _flost_min < _fage < _JOB_LOST_MAX_S:
                    # A terminal error already recorded is NEVER
                    # overwritten by the lost / restart message.
                    if not st.session_state.get("run_error"):
                        st.session_state["run_error"] = _lost_message(
                            _stored.get("saved_at"), _fdir)
                        st.rerun(scope="app")
                elif _fage <= _flost_min:
                    # The worker may not have written its status file
                    # yet — say the job is remembered instead of
                    # showing a silently idle page.
                    st.caption("ထုတ်လုပ်မှု မှတ်ထားသည် — စတင်နေသည်…")
                else:
                    # Ancient entry (older than the stale window):
                    # previously ignored silently, so a Run lost to
                    # a restart after a long hang showed a fresh
                    # form with no explanation. Show the lost
                    # message ONCE, then consume the entry (the main
                    # script removes it from localStorage) so it
                    # never nags on later loads.
                    if not st.session_state.get("run_error"):
                        st.session_state["run_error"] = _lost_message(
                            _stored.get("saved_at"), _fdir)
                        st.session_state["restored_job_dir"] = _fdir
                        st.session_state["job_clear_pending"] = True
                        st.rerun(scope="app")
        return
    with job["lock"]:
        status = job["status"]
        pct = float(job.get("pct", 0.0))
        phase = job.get("phase", "analyze")
        detail = job.get("detail", "")
        started = float(job.get("started_at", time.time()))
        result = job.get("result")
        error = job.get("error")
    if status == "running":
        elapsed = max(0.0, time.time() - started)
        # ETA: linear projection from the measured fraction; only
        # shown once there is real progress (pct >= 10) — before
        # that it would be a guess, not a measurement.
        eta = ""
        if pct >= 10.0:
            remain = elapsed * (100.0 - pct) / pct
            eta = f" | ခန့်မှန်း ကျန်ချိန် ~{remain:.0f}s"
        label = _RUN_PHASE_LABELS.get(phase, detail or phase)
        st.progress(min(1.0, pct / 100.0),
                    text=f"{label} — {pct:.0f}% | {elapsed:.0f}s ကြာပြီး{eta}")
        st.caption(detail or "")
        # DIAGNOSTIC: confirm the browser-side job save happened for
        # THIS run — if this line is missing during a Run, the save
        # never ran and a later reload cannot re-attach.
        _dstored = st.session_state.get("stored_job") or {}
        if (_dstored.get("run_dir")
                and _dstored.get("run_dir")
                == (job.get("snapshot") or {}).get("run_dir")):
            st.caption("🔎 Job မှတ်ထားပြီ ✓ — session ပြတ်လည်း ပြန်ချိတ်လို့ရသည်")
        return
    if status == "done" and result is not None:
        if (job.get("snapshot") or {}).get("render_location") == "hybrid":
            # Hybrid finalize: the pack is on disk (pack.json +
            # voiceover.mp3); the device encodes next. No server
            # result to apply, no input to auto-delete (there is
            # none — the video never left the browser).
            st.session_state["hybrid_pack_dir"] = (
                job["snapshot"].get("run_dir"))
            st.session_state["hybrid_pack_meta"] = {
                "title": result.get("title"),
                "script_text": result.get("script_text"),
                "sync_method": result.get("sync_method"),
                "subtitle_mode": result.get("subtitle_mode"),
                "aspect_ratio": result.get("aspect_ratio"),
                "narration_coverage": result.get("narration_coverage"),
                "processing_mode": result.get("processing_mode"),
                "pack_id": (result.get("pack") or {}).get("pack_id"),
                "continuation_cap_hit": result.get(
                    "continuation_cap_hit"),
            }
            st.session_state["hybrid_pack_acked_id"] = None
            st.session_state["hybrid_encode"] = {
                "status": "waiting", "pct": 0.0, "detail": ""}
            st.session_state["hybrid_result"] = None
            st.session_state["restored_job_dir"] = (
                job["snapshot"].get("run_dir"))
            st.session_state["run_job"] = None
            st.rerun(scope="app")
        st.session_state["recap_result"] = _apply_job_result(
            result, job["snapshot"])
        st.session_state["restored_job_dir"] = job["snapshot"].get("run_dir")
        st.session_state["run_job"] = None
        # Full-app rerun: a fragment-scoped rerun would NOT re-execute
        # the main script, so the result below would never render.
        st.rerun(scope="app")
    if status == "error":
        # Persist the error in session state and let the MAIN script
        # render it: rendering it here + a fragment-scoped rerun would
        # flash it for <1s and then wipe it (the fragment would
        # re-execute with run_job=None and render nothing).
        st.session_state["run_error"] = error or "အမည်မသိ အမှား"
        st.session_state["run_job"] = None
        st.rerun(scope="app")


# Source precedence matches the canvas display: picked file > demo > URL.
_canvas_video_id = None
_canvas_data_url = None
_canvas_local_picked = False
if st.session_state.get("src_path"):
    _canvas_video_id = st.session_state.get("src_video_id")
    _canvas_local_picked = True  # JS already holds this video; never re-send
elif demo_active and os.path.isfile(DEMO_SAMPLE):
    _canvas_video_id = "demo"
    if st.session_state.get("canvas_vid_sent") != _canvas_video_id:
        with open(DEMO_SAMPLE, "rb") as _f:
            _canvas_data_url = (
                "data:video/mp4;base64," + base64.b64encode(_f.read()).decode()
            )
elif st.session_state.get("url_hybrid_path"):
    # v1.13: Hybrid + URL — the server downloaded the video; the
    # browser fetches the file ONCE via a one-shot data URL, then the
    # Hybrid flow continues as a local file.
    _uhp = str(st.session_state["url_hybrid_path"])
    _canvas_video_id = "urlhybrid_" + hashlib.md5(
        _uhp.encode()).hexdigest()[:12]
    if (st.session_state.get("canvas_vid_sent") != _canvas_video_id
            and os.path.isfile(_uhp)):
        with open(_uhp, "rb") as _f:
            _canvas_data_url = (
                "data:video/mp4;base64,"
                + base64.b64encode(_f.read()).decode()
            )
elif (st.session_state.get("src_url") or "").strip():
    _u = (st.session_state.get("src_url") or "").strip()
    _canvas_video_id = "url_" + hashlib.md5(_u.encode()).hexdigest()[:12]
    _canvas_data_url = _u  # direct URL as <video> src (best effort)

# Demo video is 1280x720 by construction; other sources set the aspect
# when they arrive (ffprobe for files, video_meta for URL videos).
if _canvas_video_id == "demo":
    st.session_state["canvas_aspect"] = "1280 / 720"

if _canvas_video_id is not None:
    if st.session_state.get("canvas_vid") != _canvas_video_id:
        # A new video means a fresh canvas.
        st.session_state["canvas_vid"] = _canvas_video_id
        _reset_canvas_state()
    if _canvas_data_url is not None:
        st.session_state["canvas_vid_sent"] = _canvas_video_id
else:
    st.session_state["canvas_vid"] = None

st.markdown("###  Main Canvas — ဗီဒီယို ပြင်ဆင်ရန်")
st.caption(
    " ဗီဒီယို မရှိသေးရင် Canvas ထဲမှာပဲ ရွေးပါ / URL ထည့်ပါ။ "
    " Edit ကို နှိပ်ပြီး Blur ထည့်ခြင်း / စာတန်းနေရာ ရွှေ့ခြင်း တို့ကို "
    "လုပ်နိုင်ပါတယ်။"
)
# Step 16: live canvas preview of the Source settings (read from
# session_state — the widgets below update it on every rerun).
_preview = {
    "aspect_ratio": str(
        st.session_state.get("aspect_ratio", "9:16")).strip().lower(),
    "fit_mode": str(st.session_state.get("fit_mode", "crop")),
    "crop_align": str(st.session_state.get("crop_align", "center")),
    "inner_scale": float(st.session_state.get("inner_scale", 1.22)),
    "vertical_pos": float(st.session_state.get("vertical_pos", 0.0)),
    "horizontal_pos": float(st.session_state.get("horizontal_pos", 0.0)),
    "mirror_horizontal": bool(
        st.session_state.get("mirror_horizontal", False)),
    "copyright_zoom": float(st.session_state.get("copyright_zoom", 1.07)),
}
# Fix 4: reactive subtitle style for the canvas preview — the
# Subtitles-tab colour + font flow into the canvas live, so changing
# the dropdown/radio immediately recolours / re-fonts the placeholder
# ("ဒီနေရာတွင် စာတန်းထိုးထည့်ပေးပါမည်") instead of only showing in the
# rendered video. The font file is sent as a data URL until the canvas
# acks the load (tracked in _canvas_font_loaded from the component
# value's loaded_font field); otherwise font_data is None and the
# canvas keeps its loaded font.
_sub_font = str(st.session_state.get("subtitle_font", "Pyidaungsu"))
_sub_rgb = SUBTITLE_COLOR_RGB.get(
    str(st.session_state.get("subtitle_color", "Yellow")), (255, 255, 0))
_sub_font_data = None
if st.session_state.get("_canvas_font_loaded") != _sub_font:
    try:
        with open(_subtitle_font_file(_sub_font), "rb") as _ff:
            _sub_font_data = (
                "data:font/ttf;base64,"
                + base64.b64encode(_ff.read()).decode()
            )
    except (OSError, ValueError):
        _sub_font_data = None
_subtitle_style = {
    "font_family": _sub_font,
    "color": "#%02x%02x%02x" % _sub_rgb,
    # Stroke has no picker in the UI: always black, like the renderer.
    "stroke": "#000000",
    "font_data": _sub_font_data,
}
# ---- Hybrid: pack transport + Gemini upload credentials ----
# The Hybrid pack (pack.json + voiceover.mp3 on disk) is re-assembled
# into ONE component arg on every render until the device acks it
# (hybrid_pack_received). Re-assembly from disk keeps one code path
# for the fresh run AND the session-reset re-attach. The voiceover
# bytes are the only large part (~5-10MB base64, one-time).
_hybrid_pack_arg = None
if st.session_state.get("hybrid_pack_dir"):
    _pdir = str(st.session_state["hybrid_pack_dir"])
    _pjson = os.path.join(_pdir, "pack.json")
    _pvoice = os.path.join(_pdir, "voiceover.mp3")
    if os.path.isfile(_pjson) and os.path.isfile(_pvoice):
        try:
            with open(_pjson, encoding="utf-8") as _pf:
                _hybrid_pack_arg = json.load(_pf)
            with open(_pvoice, "rb") as _vf:
                _hybrid_pack_arg["voiceover_b64"] = base64.b64encode(
                    _vf.read()).decode()
            # v1.15: server pre-rendered final audio (bg_volume == 0) —
            # the device muxes it with a pure stream copy. Kept out of
            # pack.json (audio bytes travel only in this component arg,
            # like voiceover_b64). Absent => old wasm-mix path.
            _pfinal = os.path.join(_pdir, "audio.m4a")
            if (_hybrid_pack_arg.get("audio_final_file")
                    and os.path.isfile(_pfinal)):
                with open(_pfinal, "rb") as _af:
                    _hybrid_pack_arg["audio_final_b64"] = base64.b64encode(
                        _af.read()).decode()
            if (st.session_state.get("hybrid_pack_acked_id")
                    == _hybrid_pack_arg.get("pack_id")):
                _hybrid_pack_arg = None
        except (OSError, ValueError):
            _hybrid_pack_arg = None
# The Gemini upload credential goes to the canvas ONLY while a Hybrid
# Run is waiting for the browser→Gemini upload (pending_job with
# needs_gemini_upload). It is never logged, never rendered, and clears
# the moment the upload completes (pending_job is consumed). The key
# already travels browser<->server for the server-side Gemini calls,
# so this adds no new trust boundary. API Keys v2: the canvas gets the
# full KEY LIST; the browser rotates on 429/quota errors.
_gemini_auth = None
if (st.session_state.get("pending_job") or {}).get("needs_gemini_upload"):
    _gemini_auth = _get_active_gemini_keys() or None
_val = interactive_canvas(
    video_id=_canvas_video_id,
    video_data_url=_canvas_data_url,
    blur_boxes=st.session_state.get("canvas_blur") or [],
    subtitle=st.session_state.get("canvas_sub") or dict(_sub_default),
    subtitles_enabled=bool(st.session_state.get("burn_subtitles", True)),
    canvas_aspect=st.session_state.get("canvas_aspect") or "9 / 16",
    local_picked=_canvas_local_picked,
    # Ack: the server has saved this picked file (lets the frontend
    # drop the base64 payload so it is not re-sent every rerun).
    video_ack=(
        st.session_state.get("src_video_id")
        if st.session_state.get("src_path")
        else None
    ),
    preview=_preview,
    subtitle_style=_subtitle_style,
    # Deferred upload: a fresh nonce when a Run click needs the
    # picked video (None = no upload requested).
    upload_request=st.session_state.get("upload_nonce"),
    # Pick re-announce: tell the canvas which pick Python knows, so
    # it can re-announce a local pick lost to a session reset.
    pending_ack=st.session_state.get("pending_video_id"),
    # Fix 3: pause the canvas preview while a Run is in progress —
    # decoding during a minutes-long render burns phone resources.
    paused=bool(st.session_state.get("run_job")
                or st.session_state.get("pending_job")),
    # Hybrid: browser→Gemini upload trigger + credential (transient,
    # see above) + the pack for the device encode (acked once).
    gemini_auth=_gemini_auth,
    gemini_upload_request=st.session_state.get("gemini_nonce"),
    # Ack for the browser→Gemini upload result: lets the canvas stop
    # re-sending gemini_uploaded / gemini_upload_error (sticky until
    # acked, so a later sendValue can never silently lose them).
    gemini_ack=st.session_state.get("gemini_ack"),
    hybrid_pack=_hybrid_pack_arg,
    # Tell the canvas the current Render Location so it gates the
    # browser-side FFmpeg header probe to Hybrid mode only.
    render_location=str(
        st.session_state.get("render_location", "hybrid")),
    # v1.14 (WebCodecs): prefer hardware encode on the device.
    hw_encode=bool(st.session_state.get("hw_encode", True)),
    key="mmr_canvas",
)
if isinstance(_val, dict):
    # Fix 4 ack: the canvas reports which subtitle font it actually
    # loaded. Python keeps sending font_data until the canvas acks
    # this font.
    _loaded_font = _val.get("loaded_font")
    if isinstance(_loaded_font, str) and _loaded_font:
        st.session_state["_canvas_font_loaded"] = _loaded_font
    _action = _val.get("action")
    # Actions are sticky: the component's last value is re-returned on
    # every rerun, so consume each nonce exactly once.
    _nonce = _val.get("nonce")
    if _action and _nonce is not None:
        if _nonce == st.session_state.get("last_canvas_nonce"):
            _action = None  # already processed
        else:
            st.session_state["last_canvas_nonce"] = _nonce
    _fresh_action = _action is not None
    # ---- Hybrid actions (device encode / Gemini upload) ----
    if _action == "gemini_uploaded":
        # The browser uploaded the video directly to Gemini — the
        # server never sees the bytes. Record the file + metadata so
        # the starter can launch the Hybrid worker. Ack it (the
        # canvas re-sends until acked); re-processing the same file
        # is idempotent. The pre-upload stage is done — clear it.
        st.session_state["gemini_upload_stage"] = None
        _gf_name = _val.get("file_name")
        if _gf_name:
            st.session_state["gemini_ack"] = _gf_name
        if _gf_name:
            try:
                st.session_state["gemini_file"] = {
                    "file_name": _gf_name,
                    "duration": float(_val.get("duration") or 0),
                    "width": int(_val.get("width") or 0),
                    "height": int(_val.get("height") or 0),
                }
            except (TypeError, ValueError):
                st.session_state["gemini_file"] = {
                    "file_name": _gf_name, "duration": 0,
                    "width": 0, "height": 0}
            st.session_state["_canvas_rerun_needed"] = True
    elif _action == "gemini_upload_progress":
        try:
            st.session_state["gemini_upload"] = {
                "pct": float(_val.get("pct", 0.0))}
        except (TypeError, ValueError):
            pass
    elif _action == "gemini_upload_stage":
        # v1.8: the canvas reports its true pre-upload sub-stage
        # (metadata / ffmpeg / upload) so the fragment shows honest
        # progress instead of "Gemini upload" throughout.
        _stage = str(_val.get("stage") or "")
        if _stage in ("metadata", "ffmpeg", "upload"):
            st.session_state["gemini_upload_stage"] = _stage
    elif _action == "gemini_upload_error":
        # Ack the error too (the canvas re-sends until acked).
        st.session_state["gemini_ack"] = "error"
        st.session_state["run_error"] = (
            _val.get("error") or "Gemini တင်မှု မအောင်မြင်ပါ")
        st.session_state["pending_job"] = None
        st.session_state["gemini_upload"] = None
        st.session_state["gemini_upload_stage"] = None
    elif _action == "hybrid_pack_received":
        # The device got the pack — stop re-sending it.
        if _val.get("pack_id"):
            st.session_state["hybrid_pack_acked_id"] = _val.get("pack_id")
    elif _action == "hybrid_encode_progress":
        try:
            st.session_state["hybrid_encode"] = {
                "status": "encoding",
                "pct": float(_val.get("pct", 0.0)),
                "detail": str(_val.get("detail") or ""),
            }
        except (TypeError, ValueError):
            pass
    elif _action == "hybrid_encode_done":
        # The device finished — build the SAME success line as Server
        # mode from the pack meta (title / coverage / sync).
        _hmeta = dict(st.session_state.get("hybrid_pack_meta") or {})
        _hmeta.setdefault("sync_method", "full_narration")
        _hmeta.setdefault("aspect_ratio", "")
        _hmeta.setdefault("subtitle_mode", "burned_in")
        st.session_state["hybrid_result"] = {
            "status": _success_status_line(_hmeta),
            "title": _hmeta.get("title", ""),
            "script_text": _hmeta.get("script_text", ""),
        }
        st.session_state["hybrid_encode"] = {
            "status": "done", "pct": 100.0, "detail": ""}
    elif _action == "hybrid_encode_error":
        _herr = _val.get("error") or "Hybrid encode မအောင်မြင်ပါ"
        st.session_state["hybrid_encode"] = {
            "status": "error", "pct": 0.0, "detail": "", "error": _herr}
        # A Hybrid encode failure must NOT corrupt Server-mode state:
        # it is only an error note — the Server toggle still works.
        st.session_state["run_error"] = _herr
    elif _action == "remove_video":
        # Canvas ✕: drop the current video and reset the canvas.
        # The picked upload + any previous result are superseded.
        _delete_input_file()
        _delete_result_files()
        # A removed video invalidates any Hybrid pack / encode state.
        st.session_state["hybrid_pack_dir"] = None
        st.session_state["hybrid_pack_meta"] = None
        st.session_state["hybrid_result"] = None
        st.session_state["hybrid_encode"] = None
        st.session_state["gemini_file"] = None
        st.session_state["src_url"] = None
        st.session_state["pending_video_id"] = None
        st.session_state["pending_video_name"] = None
        st.session_state["pending_job"] = None
        st.session_state["upload_nonce"] = None
        st.session_state["canvas_aspect"] = "9 / 16"
        st.session_state["demo_suppressed"] = True
        _reset_canvas_state()
        st.session_state["canvas_vid"] = None
        st.session_state["canvas_vid_sent"] = None
        # Deferred: the refresh rerun happens at the end of the script
        # (see bottom) so a Run click coalesced with this action is
        # not swallowed by an early st.rerun().
        st.session_state["_canvas_rerun_needed"] = True
    elif _action == "video_picked":
        # A file picked inside the canvas: it stays LOCAL in the
        # browser (deferred upload — no video_data is sent yet).
        # Python only records that a local video is waiting; the
        # payload is requested when the user clicks Run.
        _vid = _val.get("video_id")
        if _vid and _vid != st.session_state.get("pending_video_id"):
            # A NEW local video supersedes any previous upload /
            # result / pending run.
            _delete_input_file()
            _delete_result_files()
            # A new video invalidates any Hybrid pack / encode state.
            st.session_state["hybrid_pack_dir"] = None
            st.session_state["hybrid_pack_meta"] = None
            st.session_state["hybrid_result"] = None
            st.session_state["hybrid_encode"] = None
            st.session_state["gemini_file"] = None
            st.session_state["src_url"] = None
            st.session_state["pending_video_id"] = _vid
            st.session_state["pending_video_name"] = (
                _val.get("video_name") or "video")
            st.session_state["pending_job"] = None
            st.session_state["upload_nonce"] = None
            _reset_canvas_state()
            st.session_state["_canvas_rerun_needed"] = True
    elif _action == "video_selected":
        # A file picked inside the canvas: save it, ffprobe its ratio.
        try:
            _data = _val.get("video_data") or ""
            _mime, _, _b64 = _data.partition(";base64,")
            _raw = base64.b64decode(_b64)
            _ext = {
                "data:video/mp4": "mp4",
                "data:video/quicktime": "mov",
                "data:video/webm": "webm",
                "data:video/x-matroska": "mkv",
                "data:video/avi": "avi",
                "data:video/x-m4v": "m4v",
            }.get(_mime, "mp4")
            _dst = os.path.join(
                st.session_state["session_dir"], f"input.{_ext}"
            )
            with open(_dst, "wb") as _f:
                _f.write(_raw)
            st.session_state["src_path"] = _dst
            st.session_state["src_url"] = None
            st.session_state["url_hybrid_path"] = None
            st.session_state["src_video_id"] = _val.get("video_id")
            st.session_state["src_local_picked"] = True
            st.session_state["canvas_vid_sent"] = _val.get("video_id")
            try:
                from video_processor import get_video_metadata

                _meta = get_video_metadata(_dst)
                if _meta.get("width") and _meta.get("height"):
                    st.session_state["canvas_aspect"] = (
                        f"{_meta['width']} / {_meta['height']}"
                    )
            except Exception:  # noqa: BLE001 - aspect stays at the default
                pass
            # NOTE: no _reset_canvas_state() here — the upload landing
            # is NOT a new video (the user may already have edited
            # blur/subtitle after picking). The reset for a new video
            # happens in the video_picked handler.
        except Exception:  # noqa: BLE001 - bad payload: keep previous state
            st.error("✕ ဗီဒီယိုဖိုင် ဖတ်လို့ မရပါ။ ထပ်ကြိုးစားပေးပါ")
        st.session_state["_canvas_rerun_needed"] = True
    elif _action == "video_url":
        _u = (_val.get("video_url") or "").strip()
        if _u:
            # A URL supersedes any picked upload / previous result.
            _delete_input_file()
            _delete_result_files()
            st.session_state["src_url"] = _u
            st.session_state["url_hybrid_path"] = None
            st.session_state["pending_video_id"] = None
            st.session_state["pending_video_name"] = None
            st.session_state["pending_job"] = None
            st.session_state["upload_nonce"] = None
            st.session_state["canvas_aspect"] = "9 / 16"
            _reset_canvas_state()
        st.session_state["_canvas_rerun_needed"] = True
    elif _action == "video_meta":
        # URL video's real dimensions — or a locally-picked video's
        # (deferred upload: local files are no longer ffprobed at
        # pick, so the canvas reports the ratio via loadedmetadata).
        if (
            (_val.get("video_id") == st.session_state.get("canvas_vid")
             or _val.get("video_id")
             == st.session_state.get("pending_video_id"))
            and _val.get("video_w")
            and _val.get("video_h")
        ):
            st.session_state["canvas_aspect"] = (
                f"{int(_val['video_w'])} / {int(_val['video_h'])}"
            )
            st.session_state["_canvas_rerun_needed"] = True
    # Adopt the canvas's current edit state — but NOT when a fresh
    # action was just consumed (its handler already reset/applied the
    # canvas state; adopting the action value's stale boxes would
    # undo that).
    if not _fresh_action:
        try:
            st.session_state["canvas_blur"] = list(_val.get("blur_boxes") or [])
            _s = _val.get("subtitle") or {}
            # Prefer font_size_pct (fraction of width); legacy font_size
            # (px at the 400px reference) converts.
            _fpct = _s.get("font_size_pct")
            if _fpct is None:
                _fpct = float(_s.get("font_size", 22)) / 400.0
            _wpct = _s.get("w_pct")
            st.session_state["canvas_sub"] = {
                "x_pct": float(_s.get("x_pct", 0.5)),
                "y_pct": float(_s.get("y_pct", 0.82)),
                "font_size_pct": float(_fpct),
                # Subtitle box width (Fix 3): None = auto width.
                "w_pct": (None if _wpct is None
                          else float(_wpct)),
            }
        except (TypeError, ValueError):
            pass  # keep previous canvas state on malformed values

# v1.13.1: heal a lost pick (defense-in-depth for the Run check).
# If Python lost pending_video_id (e.g. a restart race the pick
# re-announce hasn't healed yet) but the canvas still holds a local
# video, adopt the canvas's live video_id so Run sees the video.
# The canvas supplies the bytes on the upload/Gemini nonce, so the
# Run proceeds exactly like a normally-announced pick. Guarded by
# `not _fresh_action`: a just-processed remove_video / new pick /
# URL must never be overridden by a stale component value.
if (not _fresh_action
        and not st.session_state.get("pending_video_id")
        and not st.session_state.get("src_path")
        and not (st.session_state.get("src_url") or "").strip()
        and isinstance(_val, dict)
        and _val.get("has_video")
        and _val.get("video_id")):
    st.session_state["pending_video_id"] = str(_val.get("video_id"))
    st.session_state["pending_video_name"] = (
        str(_val.get("video_name") or "video"))

# ---- Step 12: Segmented Settings Tabs (directly below the canvas) ----
tab_src, tab_voice, tab_sub, tab_adv = st.tabs(
    [" Source", " Voice", " Subtitles", " Advanced"]
)


def _mode_label(mode: str) -> str:
    """Processing-mode card label: bold title + subtitle (markdown)."""
    if mode == "pro_ai_recap":
        return (
            "**Pro AI Recap**\n\nDialogue ဗီဒီယိုအတွက် — ဘာမှ မဖြတ်ဘူး။ "
            "block တစ်ခုစီရဲ့ ဗီဒီယို အပိုင်းကို အသံ length နဲ့ အတိအကျ "
            "ကိုက်အောင် independent speed နဲ့ ချိန်တယ်။"
        )
    return (
        "**AI Recap**\n\nNarration / recap ဗီဒီယို — အသံက master ဖြစ်ပြီး "
        "ဗီဒီယိုကို အသံနဲ့ကိုက်အောင် ဆွဲချိန်တယ်။"
    )


def _bump(key: str, delta: float, lo: float, hi: float) -> None:
    """Step 16 −/+ buttons: nudge a slider's session_state value."""
    v = float(st.session_state.get(key, 0.0))
    st.session_state[key] = round(min(hi, max(lo, v + delta)), 4)


def _stepper_slider(label: str, key: str, lo: float, hi: float,
                    step: float, fmt: str, default: float = None) -> None:
    """A slider flanked by − / + buttons (Step 16 reference layout).

    `default` is the slider's FIRST-render value and MUST be passed:
    without an explicit value=, Streamlit initializes a keyed slider
    to lo (min). The _STEP16_DEFAULTS seeding block already sets the
    intended values in session_state before the widgets render (so the
    Run snapshot was never actually 1.00x), but passing value= makes
    the widget agree with the seeded value explicitly instead of
    depending on seeding alone — belt and braces, so the widget,
    the readout, and the Run snapshot can never disagree.
    """
    c1, c2, c3 = st.columns([1, 6, 1])
    with c1:
        st.button("−", key=f"{key}_minus", on_click=_bump,
                  args=(key, -step, lo, hi))
    with c2:
        if default is None:
            default = lo
        st.slider(label, lo, hi, key=key, step=step, format=fmt,
                  label_visibility="collapsed", value=default)
    with c3:
        st.button("+", key=f"{key}_plus", on_click=_bump,
                  args=(key, step, lo, hi))


def _resolve_video_source(src_path, src_url, pending_video_id,
                          demo_active, demo_exists):
    """v1.13.1: pure Run video-source check (unit-testable).

    Precedence matches the canvas display: picked file > demo > URL.
    Returns (ok, error_message) — error_message is "" when ok.
    """
    _src_url = (src_url or "").strip() or None
    _has_pending = bool(pending_video_id)
    if src_path:
        return True, ""
    if demo_active and not _src_url:
        if not demo_exists:
            return False, "✕ Demo video မရှိပါ"
        return True, ""
    if not _src_url and not _has_pending:
        return False, (
            "✕ Canvas ထဲမှာ ဗီဒီယို ရွေးပေးပါ "
            "သို့မဟုတ် Video URL ထည့်ပေးပါ")
    return True, ""


with tab_src:
    # Current video source (picked inside the canvas, or demo / URL).
    # A picked-but-not-yet-uploaded video (deferred upload) has no
    # src_path/src_url yet — show its pending name, not "—".
    _src_name = "—"
    if st.session_state.get("src_path"):
        _src_name = os.path.basename(st.session_state["src_path"])
    elif (st.session_state.get("src_url") or "").strip():
        _src_name = st.session_state["src_url"]
    elif demo_active:
        _src_name = "Demo ဗီဒီယို (sample_test.mp4)"
    elif st.session_state.get("pending_video_id"):
        _src_name = (
            f"{st.session_state.get('pending_video_name') or 'ဗီဒီယို'}"
            " (ဖုန်းထဲမှာ — Run နှိပ်ရင် တင်မည်)"
        )
    st.caption(f" လက်ရှိ ဗီဒီယို: {_src_name}")
    demo_mode = st.checkbox(
        " API Key မပါဘဲ Demo စမ်းသပ်မည် (Mock Mode)",
        value=False,
        key="demo_mode",
        on_change=_demo_toggled,
    )

    # ---- Step 16: Processing Mode (2 cards) ----
    st.markdown("### Processing Mode")
    st.radio(
        "Processing Mode",
        options=["ai_recap", "pro_ai_recap"],
        format_func=_mode_label,
        key="processing_mode",
        label_visibility="collapsed",
    )

    # ---- Hybrid: Render Location (device encode vs server encode) ----
    st.markdown("### Render Location")
    # Explicit default= on every pills call: Streamlit's implicit
    # first-option selection is not a contract — a fresh load must
    # deterministically land on the intended defaults (Hybrid, 9:16,
    # Crop, Center). default= only applies when the key is absent,
    # so it never overrides a user's existing choice.
    st.pills(
        "Render Location",
        options=["hybrid", "server"],
        default="hybrid",
        format_func=lambda m: (
            "Hybrid — ဖုန်းမှာ ပေါင်းမည်" if m == "hybrid"
            else "Server — server မှာ ပေါင်းမည်"),
        key="render_location",
        label_visibility="collapsed",
    )
    st.caption(
        "Hybrid: ဗီဒီယိုကို server ကို မတင်ဘူး — Gemini စိစစ်မှု + "
        "အသံထုတ်မှုပဲ server က လုပ်ပြီး ပေါင်းတာကို ဖုန်းမှာ လုပ်မယ်။ "
        "Server: အရင်အတိုင်း အကုန် server မှာ လုပ်မယ်။"
    )

    # NOTE: No duration selector — AI Recap is ALWAYS the full original
    # duration (Narration Master concept: never cut an 8-min video to
    # 30s). The old 30/60/90/180 pills were removed because they
    # contradicted the full-duration spec. Pro is full-duration too.

    # ---- Feature pack v1.13: Quality + FPS ----
    st.markdown("### Quality")
    st.pills(
        "Quality",
        options=["fast", "normal", "high"],
        default="fast",
        format_func=lambda q: (
            "Fast — အမြန်ဆုံး" if q == "fast"
            else "Normal — ပုံမှန်" if q == "normal"
            else "High — အကောင်းဆုံး (နှေးမည်)"),
        key="render_quality",
        label_visibility="collapsed",
    )
    st.caption(
        "Fast = ultrafast (v1.12 အတိုင်း, file ကြီးမည်) | "
        "Normal = veryfast crf 23 | High = medium crf 20 (နှေးမည်)။"
    )
    # ---- v1.14 (WebCodecs): hardware encode toggle ----
    st.checkbox(
        "Hardware encode (မြန်)",
        value=True,
        key="hw_encode",
        help="စက်ရဲ့ GPU နဲ့ မြန်မြန် encode လုပ်မယ် — မရရင် အလိုအလျောက် ပုံမှန်နည်းနဲ့ ပြောင်းမယ်။",
    )
    st.caption(
        "ဖွင့်ထားရင်: canvas → စက်ရဲ့ GPU (VideoEncoder) → FFmpeg နဲ့ အသံပေါင်း "
        "(~1-2x video အရှည်)။ ပိတ်ထားရင် / မရရင်: FFmpeg.wasm ပုံမှန်နည်း။"
    )
    st.markdown("### FPS")
    st.pills(
        "FPS",
        options=[24, 30, 60],
        default=30,
        format_func=lambda f: f"{f} fps",
        key="output_fps",
        label_visibility="collapsed",
    )

    # ---- Step 16: Aspect Ratio (5 pills) + Fit Mode (2 pills) ----
    st.markdown("### Aspect Ratio")
    st.pills(
        "Aspect Ratio",
        options=["Original", "9:16", "1:1", "4:5", "16:9"],
        default="9:16",
        key="aspect_ratio",
        label_visibility="collapsed",
    )
    st.markdown("### Fit Mode")
    st.pills(
        "Fit Mode",
        options=["crop", "pad"],
        default="crop",
        format_func=lambda m: (
            "Crop (fill)" if m == "crop" else "Pad (blur bg)"),
        key="fit_mode",
        label_visibility="collapsed",
    )
    if st.session_state.get("fit_mode", "crop") == "crop":
        st.pills(
            "Crop alignment",
            options=["top", "center", "bottom"],
            default="center",
            format_func=lambda a: a.capitalize(),
            key="crop_align",
            label_visibility="collapsed",
        )
    else:  # Pad (blur bg): inner scale + vertical/horizontal position
        st.markdown(
            f"**Inner scale** — "
            f"{float(st.session_state.get('inner_scale', 1.22)):.2f}×"
        )
        _stepper_slider("Inner scale", "inner_scale", 0.5, 2.0, 0.01,
                        "%.2f×", 1.22)
        st.markdown(
            f"**Vertical position** — "
            f"{float(st.session_state.get('vertical_pos', 0.0)):+.0f}%"
        )
        _stepper_slider("Vertical position", "vertical_pos", -50.0, 50.0,
                        1.0, "%+.0f%%", 0.0)
        st.markdown(
            f"**Horizontal position** — "
            f"{float(st.session_state.get('horizontal_pos', 0.0)):+.0f}%"
        )
        _stepper_slider("Horizontal position", "horizontal_pos", -50.0,
                        50.0, 1.0, "%+.0f%%", 0.0)
        st.caption(
            "Source ကို pad frame ထဲမှာ ချဲ့/ရွှေ့ချင်ရင် — မြင်ပပျောက်တဲ့ "
            "အပိုင်းကို crop လုပ်ပါတယ်။ Preview မှာ တိုက်ရိုက် မြင်ရပါတယ်။"
        )

    # ---- Step 16: Copyright ----
    st.markdown("### Copyright")
    st.toggle("Mirror horizontally", key="mirror_horizontal")
    st.markdown(
        f"**Zoom (copyright nudge)** — "
        f"{float(st.session_state.get('copyright_zoom', 1.07)):.2f}×"
    )
    _stepper_slider("Zoom (copyright nudge)", "copyright_zoom", 1.0, 1.25,
                    0.01, "%.2f×", 1.07)
    st.toggle("Small random crop offset", key="random_crop_offset")
    st.caption(
        "Mirror ကို bounding box/pad position တွေထက် အရင်ချမှတ်ပါတယ် — "
        "box တွေနဲ့ pad position ဟာ မှန်ရိပ်ပြီးတဲ့ frame အတိုင်းသာ "
        "အလုပ်လုပ်ပါတယ်။ Random crop က render တစ်ခါစီ pixel အနည်းငယ် "
        "ကွဲပြားစေတယ်။"
    )

    # ---- Feature pack v1.13: Watermark (text) ----
    st.markdown("### Watermark (စာသား)")
    watermark_text = st.text_input(
        "Watermark စာသား (မထည့်ချင်ရင် ဗလာထားပါ)",
        key="watermark_text",
    )
    if (watermark_text or "").strip():
        st.pills(
            "Watermark နေရာ",
            options=["top-left", "top-right", "bottom-left",
                     "bottom-right", "center"],
            default="bottom-right",
            format_func=lambda p: {
                "top-left": "အပေါ်-ဘယ်", "top-right": "အပေါ်-ညာ",
                "bottom-left": "အောက်-ဘယ်", "bottom-right": "အောက်-ညာ",
                "center": "အလယ်"}[p],
            key="watermark_pos",
            label_visibility="collapsed",
        )
        st.slider("Watermark အထင်အရှား (%)", 10, 100, 50, 5,
                  key="watermark_opacity")

    # ---- Feature pack v1.13: Logo (image) ----
    st.markdown("### Logo (ပုံ)")
    logo_file = st.file_uploader(
        "Logo PNG တင်ပါ (မထည့်ချင်ရင် ကျော်ပါ)",
        type=["png"],
        key="logo_upload",
    )
    if logo_file is not None:
        st.pills(
            "Logo နေရာ",
            options=["top-left", "top-right", "bottom-left",
                     "bottom-right"],
            default="top-right",
            format_func=lambda p: {
                "top-left": "အပေါ်-ဘယ်", "top-right": "အပေါ်-ညာ",
                "bottom-left": "အောက်-ဘယ်",
                "bottom-right": "အောက်-ညာ"}[p],
            key="logo_pos",
            label_visibility="collapsed",
        )
        st.slider("Logo အရွယ်အစား (% of width)", 5, 30, 12, 1,
                  key="logo_scale")
        st.slider("Logo အထင်အရှား (%)", 10, 100, 85, 5,
                  key="logo_opacity")

    # ---- Feature pack v1.13: caption blur band (one-tap preset) ----
    st.markdown("### Blur")
    _has_band = any(
        isinstance(_b, dict) and _b.get("band")
        for _b in (st.session_state.get("canvas_blur") or []))
    if st.button(
            "စာတန်းဖုံး blur band ဖြုတ်မယ်" if _has_band
            else "စာတန်းဖုံး blur band ထည့်မယ်",
            key="blur_band_btn"):
        _boxes = [dict(_b) for _b in
                  (st.session_state.get("canvas_blur") or [])]
        if _has_band:
            _boxes = [_b for _b in _boxes if not _b.get("band")]
        else:
            # Full-width band over the current subtitle area (covers
            # original burned-in subtitles).
            _sub = st.session_state.get("canvas_sub") or {}
            _y = float(_sub.get("y_pct", 0.82))
            _boxes.append({
                "x_pct": 0.0, "y_pct": max(0.0, _y - 0.07),
                "w_pct": 1.0, "h_pct": 0.14,
                "strength": 25, "band": True,
            })
        st.session_state["canvas_blur"] = _boxes
        st.session_state["_canvas_rerun_needed"] = True
    st.caption(
        "မူရင်း video မှာ ကပ်ထားတဲ့ စာတန်းတွေ ဖုံးဖို့ "
        "အလျားလိုက် blur band တစ်ခု ထည့်ပေးပါတယ်။"
    )

    model_choice = st.selectbox(" AI Model ရွေးပါ", MODEL_OPTIONS, index=0)

with tab_voice:
    voice_choice = st.radio(" မြန်မာ AI အသံ ရွေးပါ", VOICE_CHOICES, index=0)
    bg_volume = st.slider(
        " မူရင်းနောက်ခံအသံ အတိုးအကျယ်", 0.0, 0.5, 0.0, 0.05
    )
    pitch_hz = st.slider(" အသံ Tone (အနိမ့်–အမြင့်)", -10, 10, 0, 1)
    st.caption(
        "လက်ရှိ Tone: "
        + ("0 (မူရင်း)" if pitch_hz == 0 else f"{pitch_hz:+d}Hz")
    )
    voice_speed_pct = st.slider(
        " အသံ အမြန်နှုန်း", -50, 50, 0, 5,
        key="voice_speed_pct",
    )
    st.caption(
        "လက်ရှိ အမြန်နှုန်း: "
        + ("0% (မူရင်း)" if voice_speed_pct == 0
           else f"{voice_speed_pct:+d}%")
    )
    # Voice preview (v1.13): a short sample with the CURRENT voice +
    # speed + tone — no full Run needed. Edge-TTS needs no API key.
    if st.button("▶ အသံ နားထောင်ကြည့်မယ်", key="voice_preview_btn"):
        _pv_voice = _parse_voice(voice_choice)
        _pv_text = "မင်္ဂလာပါ။ ဒါက အသံ နမူနာ ဖြစ်ပါတယ်။"
        try:
            with st.spinner("အသံ နမူနာ ထုတ်နေသည်…"):
                from tts_manager import generate_burmese_voiceover as _pv_tts
                _pv_proxy = (os.environ.get("HTTPS_PROXY")
                             or os.environ.get("https_proxy"))
                # NOTE: generate_burmese_voiceover returns the audio
                # DURATION (float), not the path — open the known path.
                _pv_path = os.path.join(
                    tempfile.gettempdir(), "voice_preview.mp3")
                _pv_tts(
                    _pv_text, _pv_path,
                    voice_name=_pv_voice,
                    rate=_format_rate_pct(voice_speed_pct),
                    pitch=_format_pitch_hz(pitch_hz),
                    proxy=_pv_proxy,
                )
                with open(_pv_path, "rb") as _pf:
                    st.session_state["voice_preview_audio"] = _pf.read()
        except Exception as _pv_exc:  # noqa: BLE001 - show, don't crash
            st.error(f"✕ အသံ နမူနာ ထုတ်မရပါ: {_pv_exc}")
            st.session_state["voice_preview_audio"] = None
    if st.session_state.get("voice_preview_audio"):
        st.audio(st.session_state["voice_preview_audio"], format="audio/mp3")

with tab_sub:
    burn_subtitles = st.checkbox(
        " မြန်မာစာတန်းထိုး Burn-in ထည့်မည်",
        value=True,
        key="burn_subtitles",
    )
    subtitle_font = st.selectbox(
        " စာတန်းထိုး Font ရွေးပါ",
        options=list(SUBTITLE_FONTS.keys()),
        index=list(SUBTITLE_FONTS.keys()).index("Pyidaungsu"),
        key="subtitle_font",
    )
    subtitle_color = st.radio(
        " စာတန်းထိုး အရောင် ရွေးပါ",
        options=list(SUBTITLE_COLOR_RGB.keys()),
        index=list(SUBTITLE_COLOR_RGB.keys()).index("Yellow"),
        key="subtitle_color",
    )
    # v1.13: subtitle position (Bottom/Middle) + bottom margin. Writes
    # straight into the canvas subtitle state so the preview mirrors it;
    # a canvas Edit drag overwrites the same state afterwards.
    subtitle_pos = st.pills(
        " စာတန်းထိုး နေရာ",
        options=["အောက်ခြေ", "အလယ်"],
        default="အောက်ခြေ",
        key="subtitle_pos",
    )
    subtitle_margin = st.slider(
        " အောက်ခြေ အကွာအဝေး (%)", 5, 25, 12, 1,
        key="subtitle_margin",
        disabled=(subtitle_pos != "အောက်ခြေ"),
    )
    _sub_y = 0.5 if subtitle_pos == "အလယ်" else (1.0 - subtitle_margin / 100.0)
    # Only push the pill value into the canvas state when the pills
    # themselves changed — a canvas Edit drag writes the same state and
    # must not be overwritten on the next rerun.
    _pos_key = (subtitle_pos, subtitle_margin)
    if st.session_state.get("_subtitle_pos_applied") != _pos_key:
        st.session_state["_subtitle_pos_applied"] = _pos_key
        _cs = dict(st.session_state.get("canvas_sub")
                   or dict(CANVAS_DEFAULT["subtitle"]))
        _cs["y_pct"] = _sub_y
        st.session_state["canvas_sub"] = _cs
    # v1.13: words per caption (DeepRecap-style). The ≤2-line invariant
    # is enforced by the renderer regardless of this value.
    words_per_caption = st.slider(
        " စာတန်း တစ်ကွက်မှာ စာလုံး အများဆုံး", 2, 12, 6, 1,
        key="words_per_caption",
    )
    st.caption(
        "စာတန်းတစ်ကွက်ကို အများဆုံး ၂ ကြောင်းထက် မပိုစေပါ။"
    )
    _sub_info = st.session_state.get("canvas_sub") or dict(
        CANVAS_DEFAULT["subtitle"]
    )
    st.caption(
        " စာတန်းရဲ့ နေရာ / အရွယ်အစားကို Canvas မှာ Edit နှိပ်ပြီး "
        "စာတန်းသေတ္တာကို ဆွဲ / အရွယ်ချဲ့ ချိန်နိုင်ပါတယ်။ "
        f"လက်ရှိ: x={float(_sub_info.get('x_pct', 0.5)):.2f}, "
        f"y={float(_sub_info.get('y_pct', 0.82)):.2f}, "
        f"font={float(_sub_info.get('font_size_pct', 0.055)) * 100:.1f}% "
        f"(အကျယ်၏)"
        + (
            f", width={float(_sub_info['w_pct']) * 100:.0f}%"
            if _sub_info.get("w_pct") is not None
            else ""
        )
    )


def _saved_note() -> None:
    # Small green pill (not a Streamlit alert: the B&W theme forces all
    # alerts white, but the user asked for this one note to be green).
    st.markdown(
        '<div style="display:inline-block;background:#16a34a;color:#ffffff;'
        "font-weight:700;padding:6px 14px;border-radius:999px;\">"
        "✓ သိမ်းပြီး</div>",
        unsafe_allow_html=True,
    )


with tab_adv:
    st.markdown(KEY_GUIDE)

    # ---- API Keys v2: Gemini key list (up to 5, auto-rotating) ----
    st.markdown("### Gemini API Keys")

    def _save_gemini_keys_to_browser(keys):
        # Persist the JSON key list (nonce-keyed so the eval re-runs).
        # Keys live ONLY in the user's browser localStorage — never on
        # our server, never in logs.
        # NOTE: the list must go through JSON.stringify — embedding the
        # raw json.dumps output would pass a JS *array* to setItem,
        # which coerces it to "k1,k2" (and [] to ""), corrupting the
        # stored value so the pill/restore logic can't parse it back.
        st.session_state["key_load_nonce"] += 1
        streamlit_js_eval(
            js_expressions=(
                f"localStorage.setItem('{LS_KEYS}',"
                f"JSON.stringify({json.dumps(keys)}))"),
            key=f"mmr_keys_save_{st.session_state['key_load_nonce']}",
        )

    _slots = int(st.session_state.get("gemini_key_slots") or 1)
    _slots = max(1, min(5, _slots))
    _keys = list(st.session_state.get("gemini_keys") or [])
    _uids = list(st.session_state.get("gemini_key_uids") or [])
    # Stable per-row uids (parallel to _keys): widget keys use the uid,
    # never the positional index, so add/remove can't shuffle typed
    # text between rows.
    while len(_uids) < _slots:
        _uids.append(_new_key_uid())
    _uids = _uids[:_slots]
    while len(_keys) < _slots:
        _keys.append("")
    _keys = _keys[:_slots]
    st.session_state["gemini_keys"] = _keys
    st.session_state["gemini_key_uids"] = _uids
    st.session_state["gemini_key_slots"] = _slots
    _saved_keys = list(browser_saved_keys or [])

    for _i in range(_slots):
        _uid = _uids[_i]
        _box_key = f"api_key_box_{_uid}"
        _is_saved = bool(
            _i < len(_keys) and _keys[_i]
            and _keys[_i] in _saved_keys
            and not st.session_state.get("forget_pending")
        )
        _kc1, _kc2, _kc3 = st.columns([3, 1, 1])
        with _kc1:
            st.text_input(
                f"Key {_i + 1}",
                type="password",
                # Always start empty: the saved key is shown via the
                # green pill, never echoed back into the box.
                value="",
                key=_box_key,
                placeholder="••••••••" if _is_saved else "AIza...",
                disabled=_is_saved,
            )
        with _kc2:
            _test_btn = st.button("Test", key=f"key_test_{_uid}")
        with _kc3:
            st.button("Remove", key=f"key_remove_{_uid}",
                      on_click=_remove_key_row, args=(_uid,))
        if _is_saved:
            _saved_note()
        if _test_btn:
            _k = (st.session_state.get(_box_key) or "").strip()
            if not _k and _i < len(_keys):
                _k = (_keys[_i] or "").strip()
            _ok, _msg = verify_gemini_api_key(_k)
            if _ok:
                while len(_keys) <= _i:
                    _keys.append("")
                _keys[_i] = _k
                st.session_state["gemini_keys"] = _keys
                st.session_state["forgotten_key"] = ""
                _fg = [k for k in (st.session_state.get("forgotten_keys")
                                   or []) if k != _k]
                st.session_state["forgotten_keys"] = _fg
                st.session_state["forget_pending"] = False
                st.session_state["keys_bootstrapped"] = True
                _save_gemini_keys_to_browser([k for k in _keys if k])
                _saved_note()
            else:
                st.error(_msg)
    # Flush a key-list save queued by the Remove on_click callback (a
    # callback cannot emit the js-eval iframe itself). Emitting it here
    # — in the same run, with no st.rerun() after — guarantees the
    # localStorage write executes, so the removed key can't resurrect.
    if st.session_state.get("_key_save_pending") is not None:
        _save_gemini_keys_to_browser(st.session_state["_key_save_pending"])
        st.session_state["_key_save_pending"] = None

    if _slots < 5:
        # on_click runs BEFORE the script body, so the new row renders
        # in the same run. (An inline handler after the rows would need
        # another rerun to show it.)
        st.button("＋ Key ထည့်မယ်", key="gemini_add_key", on_click=_add_key_row)

    st.caption(
        "Key တစ်ခုချင်းစီကို တခြား Google account တစ်ခုစီမှ ယူပါ — "
        "account တစ်ခုစီမှာ free quota သီးခြား ရမယ်။ (Google AI Studio)"
    )

    # Quota-exhausted warning toggle (default ON).
    st.checkbox(
        "Quota ကုန်ရင် သတိပေးမယ်",
        value=True,
        key="quota_warn",
    )

    # ---- Groq API key (optional, text tasks only) ----
    st.markdown("### Groq API Key (optional)")
    _groq_saved = bool(
        browser_saved_groq
        and st.session_state.get("groq_api_key") == browser_saved_groq
        and not st.session_state.get("groq_forget_pending")
    )
    st.text_input(
        "Groq API Key",
        type="password",
        value="",
        key="groq_api_key_box",
        placeholder="••••••••" if _groq_saved else "gsk_...",
        disabled=_groq_saved,
    )
    if _groq_saved:
        _saved_note()
    _gq1, _gq2 = st.columns(2)
    with _gq1:
        _groq_save_btn = st.button("✓ Groq Key သိမ်းမည်")
    with _gq2:
        _groq_forget_btn = st.button("Groq Key ဖျက်မည်")
    if _groq_save_btn:
        _gk = (st.session_state.get("groq_api_key_box") or "").strip()
        if _gk:
            st.session_state["groq_api_key"] = _gk
            st.session_state["groq_forgotten_key"] = ""
            st.session_state["groq_forget_pending"] = False
            st.session_state["key_load_nonce"] += 1
            streamlit_js_eval(
                js_expressions=(
                    "localStorage.setItem("
                    f"'{LS_GROQ_KEY}',JSON.stringify({json.dumps(_gk)}))"),
                key=f"mmr_groq_save_{st.session_state['key_load_nonce']}",
            )
            _saved_note()
        else:
            st.error("✕ Groq API Key ထည့်သွင်းပေးပါ")
    if _groq_forget_btn:
        st.session_state["groq_forgotten_key"] = (
            st.session_state.get("groq_api_key") or "")
        st.session_state["groq_api_key"] = ""
        st.session_state["groq_forget_pending"] = True
        st.session_state["key_load_nonce"] += 1
        if "groq_api_key_box" in st.session_state:
            del st.session_state["groq_api_key_box"]
        streamlit_js_eval(
            js_expressions=f"localStorage.removeItem('{LS_GROQ_KEY}')",
            key=f"mmr_groq_del_{st.session_state['key_load_nonce']}",
        )
    st.caption(
        "Video analysis (ဗီဒီယို ကြည့်တာ) က Gemini ပဲ ရမယ် — "
        "Groq က စာသား task တွေ (Post Kit caption) အတွက်သုံး"
    )

    # ---- Feature pack v1.13: output filename + Post Kit options ----
    st.markdown("### Output")
    st.text_input(
        "ဖိုင်နာမည် (ဗလာထားရင် recap)",
        key="output_filename",
    )
    st.caption("MP4 / SRT download တွေမှာ ဒီနာမည်ကို သုံးမယ်။")
    st.markdown("### Post Kit")
    st.text_input(
        "ကိုယ်ပိုင် hashtags (space ခြား၊ # မပါလည်း ရပါတယ်)",
        key="postkit_hashtags",
    )
    st.checkbox(
        "အဆုံးမှာ #fyp ထည့်မယ်",
        value=True,
        key="postkit_fyp",
    )
    st.caption(
        "Run ပြီးတိုင်း Gemini က Burmese caption + hashtags နဲ့ "
        "3:4 thumbnail ထုတ်ပေးမယ်။"
    )

# A locally-picked video waits on the phone until Run is clicked
# (deferred upload) — say so, so the user knows nothing has been
# sent yet. Hidden once a result exists / a run is pending / active.
if (
    st.session_state.get("pending_video_id")
    and not st.session_state.get("src_path")
    and not st.session_state.get("run_job")
    and not st.session_state.get("pending_job")
    and not st.session_state.get("recap_result")
):
    st.caption("ဗီဒီယို ရွေးပြီး — Run နှိပ်ရင် server ကို တင်မည်")

run_btn = st.button(" Recap ဗီဒီယို ထုတ်လုပ်မည်", type="primary")

# A Run already in progress (or just finished, not yet finalized by
# the fragment) blocks a new Run — the fragment clears the job
# within ~1s, then Run works again.
_run_blocked = bool(
    (st.session_state.get("run_job") or {}).get("status")
    in ("running", "done", "error")
)
if run_btn and _run_blocked:
    st.info(" ထုတ်လုပ်နေဆဲပါ — ခဏစောင့်ပေးပါ")

if run_btn and not _run_blocked:
    # A new Run supersedes any previous run's error.
    st.session_state["run_error"] = None
    # Fall back to the browser-saved key if session_state lost it
    # (e.g. a session reset) but the browser still has it. A key the
    # user just deleted (Forget) is never used, even if a stale cached
    # read still returns it.
    gemini_keys = _get_active_gemini_keys()
    groq_key = _get_active_groq_key()

    if not demo_active and not gemini_keys:
        st.error("✕ ကျေးဇူးပြု၍ သင်၏ Free Gemini API Key ကို ထည့်သွင်းပေးပါ")
        st.stop()

    # Source precedence matches the canvas display: picked file > demo > URL.
    # A picked file may still be LOCAL (deferred upload) — then the
    # Run only queues the job and requests the upload; the starter
    # below launches the pipeline once the video lands.
    src_path = st.session_state.get("src_path") or None
    src_url = (st.session_state.get("src_url") or "").strip() or None
    _has_pending_video = bool(st.session_state.get("pending_video_id"))
    if src_path:
        src_url = None
    _src_ok, _src_err = _resolve_video_source(
        src_path, src_url, st.session_state.get("pending_video_id"),
        demo_active, os.path.isfile(DEMO_SAMPLE))
    if not _src_ok:
        st.error(_src_err)
        st.stop()

    # Snapshot ALL settings at click time — the worker runs async,
    # so later widget changes must not leak into this run.
    _prev_pending = st.session_state.get("pending_job") or {}
    _run_dir = (
        (_prev_pending.get("snapshot") or {}).get("run_dir")
        or tempfile.mkdtemp(prefix="recap_session_")
    )
    # Streamlit Cloud has no egress proxy; the env lookup is harmless there
    # and keeps local/dev parity with app.py.
    tts_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")

    # Step 10/11: canvas edits flow into the render engine.
    _sub = st.session_state.get("canvas_sub") or dict(CANVAS_DEFAULT["subtitle"])
    _subtitle_config = {
        "enabled": bool(burn_subtitles),
        "x_pct": float(_sub.get("x_pct", 0.5)),
        "y_pct": float(_sub.get("y_pct", 0.82)),
        # Fraction of the output width — the burned-in size matches the
        # canvas preview exactly at any output resolution.
        "font_size_pct": float(
            _sub.get("font_size_pct",
                     float(_sub.get("font_size", 22)) / 400.0)
        ),
        # Canvas subtitle box width (Fix 3): the renderer wraps to
        # this width; None = auto (92% of the frame width).
        "w_pct": _sub.get("w_pct"),
        # Subtitles-tab choices: bundled Unicode font + text colour.
        # Defaults are Step 13: Pyidaungsu + Yellow.
        "font_family": str(st.session_state.get("subtitle_font", "Pyidaungsu")),
        "color": str(st.session_state.get("subtitle_color", "Yellow")),
        # v1.13: words per caption (DeepRecap-style chunking). The
        # renderer keeps the ≤2-line invariant regardless.
        "words_per_caption": int(
            st.session_state.get("words_per_caption", 6)),
    }
    _blur_boxes = st.session_state.get("canvas_blur") or None

    # v1.13: logo upload — persist the PNG into the run dir so the
    # worker (thread) can use it after this rerun ends.
    _logo_snap = None
    _logo_up = st.session_state.get("logo_upload")
    if _logo_up is not None:
        try:
            _logo_path = os.path.join(_run_dir, "logo_upload.png")
            with open(_logo_path, "wb") as _lf:
                _lf.write(_logo_up.getvalue())
            _logo_snap = {
                "upload_path": _logo_path,
                "position": str(
                    st.session_state.get("logo_pos", "top-right")),
                "scale": float(st.session_state.get("logo_scale", 12)),
                "opacity": float(
                    st.session_state.get("logo_opacity", 85)) / 100.0,
            }
        except OSError:
            _logo_snap = None

    # Hybrid: a URL video cannot be uploaded to Gemini from the
    # browser (it is not a local file) — fall back to Server mode for
    # this Run and say so. Local files + demo stay Hybrid.
    _render_location = str(
        st.session_state.get("render_location", "hybrid"))
    st.session_state["hybrid_fell_back"] = False
    # v1.13: Hybrid + URL — the server downloads via yt-dlp and uploads
    # to Gemini server-side; the browser fetches the file ONCE (one-shot
    # data URL) and the Hybrid flow continues as a local file. Fail fast
    # with a clear message if the download or upload fails — never
    # silently. (The old behavior fell back to Server mode.)
    st.session_state["url_hybrid_path"] = None
    if _render_location == "hybrid" and src_url and not demo_active:
        try:
            with st.spinner("URL ကနေ ဗီဒီယို ဒေါင်းလုဒ်လုပ်နေသည်…"):
                from video_processor import (
                    download_video_from_url as _uh_dl,
                    get_video_metadata as _uh_gvm)
                _uh_path = _uh_dl(src_url, _run_dir)
            _uh_meta = _uh_gvm(_uh_path)
            with st.spinner("Gemini ကို ဗီဒီယို တင်နေသည်…"):
                from google import genai as _uh_genai
                from key_pool import GeminiKeyPool as _UHKeyPool
                # API Keys v2: rotate the upload across keys on quota.
                _uh_pool = _UHKeyPool(gemini_keys)
                _uh_up = _uh_pool.run(
                    lambda _k: _uh_genai.Client(api_key=_k).files.upload(
                        file=_uh_path))
                _uh_client = _uh_genai.Client(api_key=_uh_pool.first_key())
                _uh_name = _uh_up.name
                for _ in range(72):
                    _uh_f = _uh_client.files.get(name=_uh_name)
                    _uh_state = str(
                        getattr(getattr(_uh_f, "state", None),
                                "name", "") or "").upper()
                    if _uh_state and _uh_state != "PROCESSING":
                        break
                    time.sleep(5)
            st.session_state["gemini_file"] = {
                "file_name": _uh_name,
                "duration": float(_uh_meta.get("duration") or 0),
                "width": int(_uh_meta.get("width") or 0),
                "height": int(_uh_meta.get("height") or 0),
            }
            # Stash (not gemini_file directly): the "new Run invalidates
            # previous state" block below clears gemini_file; it is
            # restored after that block.
            st.session_state["_url_hybrid_gemini"] = st.session_state.pop(
                "gemini_file")
            st.session_state["url_hybrid_path"] = _uh_path
        except Exception as _uh_exc:  # noqa: BLE001 - fail fast, loudly
            st.error(f"✕ URL ဗီဒီယို ရယူမရပါ: {_uh_exc}")
            st.stop()
    # Demo Hybrid: the demo sample is on the server — its metadata
    # comes from ffprobe here (no Gemini upload, mock plan).
    _demo_dur = None
    _demo_w = None
    _demo_h = None
    if _render_location == "hybrid" and demo_active:
        try:
            from video_processor import get_video_metadata as _gvm

            _dm = _gvm(DEMO_SAMPLE)
            _demo_dur = _dm.get("duration")
            _demo_w = _dm.get("width")
            _demo_h = _dm.get("height")
        except Exception:  # noqa: BLE001 - fall back to Server below
            _render_location = "server"
            st.session_state["hybrid_fell_back"] = True

    _snap = {
        # API Keys v2: the run carries the full Gemini key LIST (the
        # pipeline rotates on quota exhaustion), the optional Groq key,
        # and the quota-warning toggle.
        "gemini_keys": gemini_keys,
        "groq_api_key": groq_key,
        "quota_warn": bool(st.session_state.get("quota_warn", True)),
        "demo_active": bool(demo_active),
        "render_location": _render_location,
        # Hybrid: filled in by the gemini_uploaded handler (local
        # file) or the demo ffprobe above. None until then.
        "gemini_file_name": None,
        "video_duration": _demo_dur,
        "src_width": _demo_w,
        "src_height": _demo_h,
        "src_url": src_url,
        "voice_name": _parse_voice(voice_choice),
        # Step 16: all Source-tab settings flow into the Step 15
        # backend exactly as chosen (aspect is now the Source
        # tab's 5-way pill selector, not the old Advanced radio).
        "processing_mode": str(
            st.session_state.get("processing_mode", "ai_recap")),
        # Normalize: the pills offer "Original" (capital O) but the
        # render contract is lowercase "original" — without this,
        # picking Original fails the Run with a ValueError.
        "aspect_ratio": str(
            st.session_state.get("aspect_ratio", "9:16")).strip().lower(),
        "fit_mode": str(st.session_state.get("fit_mode", "crop")),
        "crop_align": str(
            st.session_state.get("crop_align", "center")),
        "inner_scale": float(
            st.session_state.get("inner_scale", 1.22)),
        "vertical_pos": float(
            st.session_state.get("vertical_pos", 0.0)),
        "horizontal_pos": float(
            st.session_state.get("horizontal_pos", 0.0)),
        "mirror_horizontal": bool(
            st.session_state.get("mirror_horizontal", False)),
        "copyright_zoom": float(
            st.session_state.get("copyright_zoom", 1.07)),
        "random_crop_offset": bool(
            st.session_state.get("random_crop_offset", False)),
        "bg_volume": float(bg_volume),
        "voice_pitch_hz": int(pitch_hz),
        "voice_speed_pct": int(st.session_state.get("voice_speed_pct", 0)),
        "burn_subtitles": bool(burn_subtitles),
        "tts_proxy": tts_proxy,
        "model": _parse_model(model_choice),
        "blur_boxes": _blur_boxes,
        "subtitle_config": _subtitle_config,
        # v1.13 feature pack.
        "render_quality": str(
            st.session_state.get("render_quality", "fast")),
        "output_fps": int(st.session_state.get("output_fps", 30)),
        "output_filename": str(
            st.session_state.get("output_filename", "") or ""),
        "watermark": (
            {"text": (st.session_state.get("watermark_text") or "").strip(),
             "position": str(
                 st.session_state.get("watermark_pos", "bottom-right")),
             "opacity": float(
                 st.session_state.get("watermark_opacity", 50)) / 100.0}
            if (st.session_state.get("watermark_text") or "").strip()
            else None
        ),
        "logo": _logo_snap,
        "postkit_hashtags": str(
            st.session_state.get("postkit_hashtags", "") or ""),
        "postkit_fyp": bool(st.session_state.get("postkit_fyp", True)),
        "run_dir": _run_dir,
        # src_path is resolved at worker start (the upload may land
        # in a later rerun than the click).
        "src_path": None,
    }
    _needs_upload = bool(
        _render_location == "server"
        and _has_pending_video and not src_path)
    # Hybrid: the browser uploads the video directly to Gemini —
    # never to Streamlit. A previously-uploaded server file does not
    # count: the browser still holds the File (or must re-pick it).
    _needs_gemini_upload = bool(
        _render_location == "hybrid" and not demo_active
        and not src_url and (_has_pending_video or src_path))
    st.session_state["pending_job"] = {
        "snapshot": _snap,
        "needs_upload": _needs_upload,
        "needs_gemini_upload": _needs_gemini_upload,
        "clicked_at": time.time(),
    }
    # A new Run invalidates any previous Hybrid pack / encode state.
    st.session_state["hybrid_pack_dir"] = None
    st.session_state["hybrid_pack_meta"] = None
    st.session_state["hybrid_result"] = None
    st.session_state["hybrid_encode"] = None
    st.session_state["gemini_file"] = None
    st.session_state["gemini_upload"] = None
    st.session_state["gemini_upload_stage"] = None
    # A fresh Run supersedes any previous Gemini-upload ack.
    st.session_state["gemini_ack"] = None
    # v1.13 URL-hybrid: restore the server-side Gemini upload info
    # (cleared above with the previous run's state).
    if st.session_state.get("_url_hybrid_gemini"):
        st.session_state["gemini_file"] = st.session_state.pop(
            "_url_hybrid_gemini")
    # Remember this Run in the browser: if the session dies mid-run,
    # a fresh session re-attaches to the job's status file instead of
    # going silently idle. The load nonce bump forces a fresh read.
    # render_location rides along so the fragment's lost-job window
    # can use the longer Hybrid grace after a reload.
    _job_saved_at = time.time()
    st.session_state["stored_job"] = {
        "run_dir": _run_dir, "saved_at": _job_saved_at,
        "render_location": _render_location}
    st.session_state["restored_job_dir"] = None
    st.session_state["job_save_nonce"] = (
        int(st.session_state.get("job_save_nonce") or 0) + 1)
    streamlit_js_eval(
        js_expressions=(
            f"localStorage.setItem('{LS_JOB}',"
            f"{json.dumps(json.dumps({'run_dir': _run_dir, 'saved_at': _job_saved_at, 'render_location': _render_location}))})"
        ),
        key=f"mmr_job_save_{st.session_state['job_save_nonce']}",
    )
    st.session_state["job_load_nonce"] = (
        int(st.session_state.get("job_load_nonce") or 0) + 1)
    if _needs_upload:
        # Fresh nonce -> the canvas sends the stored video_data once.
        st.session_state["upload_nonce"] = (
            int(st.session_state.get("upload_nonce") or 0) + 1)
        # The canvas call already ran with the old nonce this rerun,
        # so ask for one more rerun to pass the fresh nonce to it.
        st.session_state["_rerun_needed"] = True
    if _needs_gemini_upload:
        # Fresh nonce -> the canvas uploads the File to Gemini once
        # (with the transient credential from the next render).
        st.session_state["gemini_nonce"] = (
            int(st.session_state.get("gemini_nonce") or 0) + 1)
        st.session_state["_rerun_needed"] = True
    # A new Run invalidates the previous result immediately, so a failed
    # run never shows a stale result — and its files are deleted (they
    # are superseded; the new run's outputs replace them).
    _delete_result_files()

# Starter: launch a pending Run once its source is ready. For a
# deferred upload the video arrives in a later rerun than the Run
# click (the canvas sends it when it sees the upload nonce); for an
# already-uploaded / URL / demo source it starts in the same rerun.
# Hybrid: the worker starts once the browser→Gemini upload lands
# (gemini_uploaded) — or immediately for demo (mock plan, no upload).
_pending = st.session_state.get("pending_job")
if _pending and not st.session_state.get("run_job"):
    _snap0 = dict(_pending.get("snapshot") or {})
    _snap = None
    if _snap0.get("render_location") == "hybrid":
        _gemini = st.session_state.get("gemini_file") or {}
        if _snap0.get("demo_active") or _gemini.get("file_name"):
            _snap = dict(_snap0)
            if _gemini.get("file_name"):
                _snap["gemini_file_name"] = _gemini.get("file_name")
                _snap["video_duration"] = _gemini.get("duration")
                _snap["src_width"] = _gemini.get("width")
                _snap["src_height"] = _gemini.get("height")
            # No server-side video in Hybrid mode — ever.
            _snap["src_path"] = None
            _snap["src_url"] = None
            _snap["input_path"] = None
            _snap["src_local_picked_at_start"] = False
        elif (not st.session_state.get("pending_video_id")
                and not st.session_state.get("src_path")):
            # No source and no local video waiting to upload: the
            # video vanished (e.g. removed) — drop the pending run.
            st.session_state["pending_job"] = None
            st.error("✕ ဗီဒီယို မရှိတော့ပါ။ Canvas ထဲမှာ ဗီဒီယို ပြန်ရွေးပေးပါ")
        # else: still waiting for the browser→Gemini upload — the
        # gemini_uploaded action will re-trigger this starter.
    else:
        _src = st.session_state.get("src_path")
        _url = (st.session_state.get("src_url") or "").strip() or None
        _demo = bool(_snap0.get("demo_active"))
        if _src or _url or _demo:
            _snap = dict(_snap0)
            _snap["src_path"] = (
                _src if _src else (DEMO_SAMPLE if _demo else None))
            _snap["src_url"] = _url if not _src else None
            _snap["input_path"] = _src
            _snap["src_local_picked_at_start"] = bool(
                st.session_state.get("src_local_picked"))
        elif not st.session_state.get("pending_video_id"):
            # No source and no local video waiting to upload: the video
            # vanished (e.g. removed) — drop the pending run.
            st.session_state["pending_job"] = None
            st.error("✕ ဗီဒီယို မရှိတော့ပါ။ Canvas ထဲမှာ ဗီဒီယို ပြန်ရွေးပေးပါ")
    if _snap is not None:
        job = {
            "lock": threading.Lock(),
            "status": "running",
            "phase": "analyze",
            "pct": 0.0,
            "detail": "AI က ဗီဒီယို စိစစ်နေသည်…",
            "started_at": time.time(),
            "result": None,
            "error": None,
            "snapshot": _snap,
        }
        st.session_state["run_job"] = job
        st.session_state["pending_job"] = None
        # The status file exists from the moment the worker launches
        # (before the thread even starts): a started worker can never
        # look "missing" to the fragment's lost-job check, so a
        # missing file now honestly means the worker never launched.
        _write_job_status(_snap.get("run_dir"), {
            "status": "running",
            "pct": 0.0,
            "phase": "analyze",
            "detail": "စတင်နေသည်…",
            "started_at": job["started_at"],
            "snapshot": _job_file_snapshot(_snap),
        })
        threading.Thread(
            target=_run_pipeline_job, args=(job,), daemon=True).start()

# Live progress (upload wait / pipeline % + elapsed + ETA) and
# completion finalizing (auto-delete + result + rerun).
_render_run_progress()

# A failed run's error persists (rendered by the main script) until
# the next Run — the fragment stores it, it does not render it.
if st.session_state.get("run_error"):
    st.error(f"✕ အမှား: {st.session_state['run_error']}")

# ---- Feature pack v1.13: Post Kit + Library rendering ----
def _render_postkit(postkit, thumbnail_path=None, key_prefix="pk"):
    """Render the Post Kit (caption + hashtags + 3:4 thumbnail)."""
    _pk = postkit or {}
    _caption = str(_pk.get("caption") or "").strip()
    _tags = [str(_t) for _t in (_pk.get("hashtags") or []) if str(_t).strip()]
    if not _caption and not _tags and not thumbnail_path:
        return
    st.markdown("### Post Kit")
    if _caption:
        st.text_area("Caption (copy ယူနိုင်သည်)",
                     value=_caption, height=80,
                     key=f"{key_prefix}_caption")
    if _tags:
        st.code(" ".join(_tags), language=None)
    if thumbnail_path and os.path.isfile(str(thumbnail_path)):
        st.image(str(thumbnail_path), caption="Thumbnail 3:4 (1080×1440)")
        with open(str(thumbnail_path), "rb") as _tf:
            st.download_button(
                " Thumbnail Download (.png)",
                data=_tf.read(),
                file_name="thumbnail.png",
                mime="image/png",
                key=f"{key_prefix}_thumb_dl",
            )


def _render_library():
    """In-session library of completed runs (v1.13)."""
    _lib = st.session_state.get("recap_library") or []
    _lib = [_e for _e in _lib
            if _e.get("video_path")
            and os.path.isfile(str(_e["video_path"]))]
    if not _lib:
        return
    st.markdown("### Library")
    st.caption(
        "ဒီ session မှာပဲ သိမ်းထားသည် — စာမျက်နှာ ပိတ်ရင် / "
        "server restart ဖြစ်ရင် ပျောက်မယ်။"
    )
    for _i, _e in enumerate(_lib):
        _mode = ("AI Recap" if str(_e.get("processing_mode"))
                 .startswith("ai") else "Pro")
        _dur = _e.get("duration")
        _dur_s = (f"{float(_dur):.0f}s" if _dur else "—")
        with st.expander(
                f"{_e.get('title') or 'Recap'} | {_mode} | {_dur_s}",
                expanded=False):
            st.caption(f"Mode: {_mode} | Duration: {_dur_s}")
            _base = os.path.splitext(
                str(_e.get("output_name") or "recap.mp4"))[0]
            with open(str(_e["video_path"]), "rb") as _vf:
                st.download_button(
                    " MP4 Download",
                    data=_vf.read(),
                    file_name=f"{_base}.mp4",
                    mime="video/mp4",
                    key=f"lib_mp4_{_i}",
                )
            if _e.get("srt_path") and os.path.isfile(str(_e["srt_path"])):
                with open(str(_e["srt_path"]), "rb") as _sf:
                    st.download_button(
                        " SRT Download",
                        data=_sf.read(),
                        file_name=f"{_base}.srt",
                        mime="text/plain",
                        key=f"lib_srt_{_i}",
                    )
            _render_postkit(_e.get("postkit"), _e.get("thumbnail_path"),
                            key_prefix=f"libpk_{_i}")


# Render the last successful result on EVERY run — not only the run in
# which Run was clicked — so a download click or any other rerun does
# not wipe the output (the previous bug: the result lived only in a
# local variable, so the first download click cleared the whole section).
_saved = st.session_state.get("recap_result")
if _saved and os.path.isfile(_saved.get("video_path", "")):
    st.success(_saved["status"])
    # API Keys v2: non-fatal quota-rotation warnings.
    for _qw in (_saved.get("quota_warnings") or []):
        st.warning(f"⚠ {_qw}")
    st.video(_saved["video_path"])
    st.text_input(" ခေါင်းစဉ်", value=_saved["title"], disabled=True)
    st.text_area(
        " ဇာတ်ကြောင်းပြော Script",
        value=_saved["script_text"],
        height=200,
        disabled=True,
    )
    _dl_base = os.path.splitext(
        str(_saved.get("output_name") or "recap.mp4"))[0]
    with open(_saved["video_path"], "rb") as f:
        st.download_button(
            " Recap ဗီဒီယို Download (.mp4)",
            data=f.read(),
            file_name=f"{_dl_base}.mp4",
            mime="video/mp4",
        )
    if os.path.isfile(_saved.get("srt_path", "")):
        with open(_saved["srt_path"], "rb") as f:
            st.download_button(
                " မြန်မာစာတန်းထိုး Download (.srt)",
                data=f.read(),
                file_name=f"{_dl_base}.srt",
                mime="text/plain",
            )
    _render_postkit(_saved.get("postkit"), _saved.get("thumbnail_path"),
                    key_prefix="result")
elif _saved:
    # Output files are gone (session temp cleaned): ask for a fresh run.
    st.info("ထုတ်လုပ်ထားတဲ့ ဗီဒီယိုဖိုင် သက်တမ်းကုန်သွားပါပြီ။ Run ကို ထပ်နှိပ်ပေးပါ။")
    st.session_state["recap_result"] = None

# ---- Hybrid result: the video lives in the browser (device encode),
# so there is no server video_path / download button here. The pack
# arrival shows the encode progress; the encode-done action shows
# the success line + title + script. The video itself plays in the
# Canvas (top of the page) with MP4 + SRT download links.
_hybrid_encode = st.session_state.get("hybrid_encode") or {}
_hybrid_result = st.session_state.get("hybrid_result")
if st.session_state.get("hybrid_pack_dir") and not _hybrid_result:
    if _hybrid_encode.get("status") == "encoding":
        try:
            _hepct = float(_hybrid_encode.get("pct", 0.0))
        except (TypeError, ValueError):
            _hepct = 0.0
        st.progress(min(1.0, _hepct / 100.0),
                    text=(f"ဖုန်းမှာ ဗီဒီယို ပေါင်းနေသည်… — "
                          f"{_hepct:.0f}%"))
        st.caption(_hybrid_encode.get("detail") or "")
    elif _hybrid_encode.get("status") == "waiting":
        st.progress(0.0, text="ဖုန်းမှာ ဗီဒီယို ပေါင်းရန် အဆင်သင့်…")
        st.caption("Hybrid pack ရပြီ — ဖုန်းက encode စတင်နေသည်…")
if _hybrid_result:
    st.success(_hybrid_result["status"])
    st.text_input(" ခေါင်းစဉ်", value=_hybrid_result["title"],
                  disabled=True)
    st.text_area(
        " ဇာတ်ကြောင်းပြော Script",
        value=_hybrid_result["script_text"],
        height=200,
        disabled=True,
    )
    st.info("ဗီဒီယိုက Canvas ထဲမှာ ရှိနေသည် — အဲဒီမှာ ကြည့်ရှု / Download လုပ်နိုင်သည်။")
    # v1.13: Post Kit from the pack (caption + hashtags; the 3:4
    # thumbnail is captured device-side from the result video).
    try:
        _hpp = os.path.join(
            str(st.session_state.get("hybrid_pack_dir") or ""),
            "pack.json")
        _hp = {}
        if os.path.isfile(_hpp):
            with open(_hpp, encoding="utf-8") as _hpf:
                _hp = json.load(_hpf)
        _render_postkit((_hp or {}).get("postkit"), None,
                        key_prefix="hybrid")
    except (OSError, ValueError):
        pass

# v1.13: in-session library (server renders; Hybrid entries live in
# the Canvas result bar — browser blobs stay on the device).
_render_library()

# (v1.13: URL videos now stay Hybrid — the server downloads them, so
# the old Server-fallback caption is gone.)

# A canvas action asked for a refresh. Do it now — after the Run
# handler has had its chance — so a Run click that arrived in the same
# run as a canvas action (e.g. picking a video, then hitting Run
# immediately) is not swallowed by an early st.rerun(). When Run did
# run, skip the refresh: the recap result must stay on screen.
if st.session_state.pop("_canvas_rerun_needed", False) and not run_btn:
    st.rerun()
# A Run click that needs an upload asked for one more rerun so the
# canvas call above re-renders with the fresh upload nonce (the
# canvas call already ran with the old nonce this rerun).
if st.session_state.pop("_rerun_needed", False):
    st.rerun()

# Build marker: proves which code the running app has (a browser
# check can read it; it also makes "is the fix live?" answerable
# without guessing from the repo commit).
st.caption(f"build {BUILD_VERSION}")
