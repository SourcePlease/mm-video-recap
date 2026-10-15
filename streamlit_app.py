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
import tempfile
import traceback

import streamlit as st
from streamlit_js_eval import streamlit_js_eval

from canvas_component import CANVAS_DEFAULT, interactive_canvas
from pipeline import run_full_recap_pipeline
from video_editor import (
    SUBTITLE_COLOR_RGB,
    SUBTITLE_FONTS,
    _subtitle_font_file,
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
LS_KEY = "mmr_gemini_api_key"

if "api_key" not in st.session_state:
    st.session_state["api_key"] = ""
if "key_restored" not in st.session_state:
    st.session_state["key_restored"] = False
if "forget_pending" not in st.session_state:
    st.session_state["forget_pending"] = False
if "forgotten_key" not in st.session_state:
    st.session_state["forgotten_key"] = ""
if "key_load_nonce" not in st.session_state:
    st.session_state["key_load_nonce"] = 0
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
browser_saved = streamlit_js_eval(
    js_expressions=f"localStorage.getItem('{LS_KEY}')",
    key=f"mmr_key_load_{st.session_state['key_load_nonce']}",
)
# Restore on EVERY run while the key box is empty (not just once per
# session): a server session reset must not force the user to re-enter
# the key. A typed-but-unverified key in the box is never overwritten.
# After Forget, block restore until the browser confirms removal
# (browser_saved is None) so the just-deleted key can't come back;
# forgotten_key also guards against a stale cached read of it.
# NOTE: the forget guards are NEVER cleared by a None read — a None
# can be transient (the component hasn't answered yet), and clearing
# on it would let a stale OLD value re-restore the deleted key. The
# guards clear only on a fresh Verify-save or a fresh session.
if (
    browser_saved
    and browser_saved != st.session_state.get("forgotten_key")
    and not st.session_state.get("api_key_box")
    and not st.session_state.get("forget_pending")
):
    st.session_state["api_key"] = browser_saved
    st.session_state["key_restored"] = True

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
    "aspect_ratio": str(st.session_state.get("aspect_ratio", "9:16")),
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
# ("ဒီနေရာမှာ စာတန်းထိုး ပေါ်မယ်") instead of only showing in the
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
    if _action == "remove_video":
        # Canvas ✕: drop the current video and reset the canvas.
        st.session_state["src_path"] = None
        st.session_state["src_url"] = None
        st.session_state["src_video_id"] = None
        st.session_state["src_local_picked"] = False
        st.session_state["canvas_aspect"] = "9 / 16"
        st.session_state["demo_suppressed"] = True
        _reset_canvas_state()
        st.session_state["canvas_vid"] = None
        st.session_state["canvas_vid_sent"] = None
        # Deferred: the refresh rerun happens at the end of the script
        # (see bottom) so a Run click coalesced with this action is
        # not swallowed by an early st.rerun().
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
            _reset_canvas_state()
        except Exception:  # noqa: BLE001 - bad payload: keep previous state
            st.error("✕ ဗီဒီယိုဖိုင် ဖတ်လို့ မရပါ။ ထပ်ကြိုးစားပေးပါ")
        st.session_state["_canvas_rerun_needed"] = True
    elif _action == "video_url":
        _u = (_val.get("video_url") or "").strip()
        if _u:
            st.session_state["src_url"] = _u
            st.session_state["src_path"] = None
            st.session_state["src_video_id"] = None
            st.session_state["src_local_picked"] = False
            st.session_state["canvas_aspect"] = "9 / 16"
            _reset_canvas_state()
        st.session_state["_canvas_rerun_needed"] = True
    elif _action == "video_meta":
        # URL video's real dimensions -> canvas follows its ratio.
        if (
            _val.get("video_id") == st.session_state.get("canvas_vid")
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
                    step: float, fmt: str) -> None:
    """A slider flanked by − / + buttons (Step 16 reference layout)."""
    c1, c2, c3 = st.columns([1, 6, 1])
    with c1:
        st.button("−", key=f"{key}_minus", on_click=_bump,
                  args=(key, -step, lo, hi))
    with c2:
        st.slider(label, lo, hi, key=key, step=step, format=fmt,
                  label_visibility="collapsed")
    with c3:
        st.button("+", key=f"{key}_plus", on_click=_bump,
                  args=(key, step, lo, hi))


with tab_src:
    # Current video source (picked inside the canvas, or demo / URL).
    _src_name = "—"
    if st.session_state.get("src_path"):
        _src_name = os.path.basename(st.session_state["src_path"])
    elif (st.session_state.get("src_url") or "").strip():
        _src_name = st.session_state["src_url"]
    elif demo_active:
        _src_name = "Demo ဗီဒီယို (sample_test.mp4)"
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

    # ---- Step 16: Aspect Ratio (5 pills) + Fit Mode (2 pills) ----
    st.markdown("### Aspect Ratio")
    st.pills(
        "Aspect Ratio",
        options=["Original", "9:16", "1:1", "4:5", "16:9"],
        key="aspect_ratio",
        label_visibility="collapsed",
    )
    st.markdown("### Fit Mode")
    st.pills(
        "Fit Mode",
        options=["crop", "pad"],
        format_func=lambda m: (
            "Crop (fill)" if m == "crop" else "Pad (blur bg)"),
        key="fit_mode",
        label_visibility="collapsed",
    )
    if st.session_state.get("fit_mode", "crop") == "crop":
        st.pills(
            "Crop alignment",
            options=["top", "center", "bottom"],
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
                        "%.2f×")
        st.markdown(
            f"**Vertical position** — "
            f"{float(st.session_state.get('vertical_pos', 0.0)):+.0f}%"
        )
        _stepper_slider("Vertical position", "vertical_pos", -50.0, 50.0,
                        1.0, "%+.0f%%")
        st.markdown(
            f"**Horizontal position** — "
            f"{float(st.session_state.get('horizontal_pos', 0.0)):+.0f}%"
        )
        _stepper_slider("Horizontal position", "horizontal_pos", -50.0,
                        50.0, 1.0, "%+.0f%%")
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
                    0.01, "%.2f×")
    st.toggle("Small random crop offset", key="random_crop_offset")
    st.caption(
        "Mirror ကို bounding box/pad position တွေထက် အရင်ချမှတ်ပါတယ် — "
        "box တွေနဲ့ pad position ဟာ မှန်ရိပ်ပြီးတဲ့ frame အတိုင်းသာ "
        "အလုပ်လုပ်ပါတယ်။ Random crop က render တစ်ခါစီ pixel အနည်းငယ် "
        "ကွဲပြားစေတယ်။"
    )

    model_choice = st.selectbox(" AI Model ရွေးပါ", MODEL_OPTIONS, index=0)

with tab_voice:
    voice_choice = st.radio(" မြန်မာ AI အသံ ရွေးပါ", VOICE_CHOICES, index=0)
    bg_volume = st.slider(
        " မူရင်းနောက်ခံအသံ အတိုးအကျယ်", 0.0, 0.5, 0.15, 0.05
    )

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

    def _key_changed() -> None:
        # Fires ONLY when the user actually edits the key box. Browser
        # autofill does not trigger this, so a stale autofill ghost can
        # never clobber the restored key. The restored key lives in
        # "api_key" (a plain key); the box itself always starts empty.
        st.session_state["api_key"] = st.session_state.get("api_key_box", "")

    key_saved = bool(
        browser_saved
        and st.session_state.get("api_key") == browser_saved
        # After Forget, a stale cached read can still equal a re-typed
        # key — forget_pending blocks the note until a fresh save.
        and not st.session_state.get("forget_pending")
    )
    key_input = st.text_input(
        " သင်၏ Free Gemini API Key ထည့်ပါ",
        type="password",
        # Always start empty: the restored key is shown via the green
        # "သိမ်းပြီး" note, never echoed back into the box (and an
        # autofill ghost in the box must not become the app's key).
        # Once saved, the box is disabled so it is obvious no re-entry
        # is needed; Forget re-enables it for a new key.
        value="",
        key="api_key_box",
        placeholder="••••••••" if key_saved else "AIza...",
        disabled=key_saved,
        on_change=_key_changed,
    )
    # NOTE: do NOT copy key_input into api_key here. The box is empty on
    # every fresh load; copying it would wipe the localStorage-restored
    # key (the "enter the key every time" bug). api_key is set only by
    # the restore above and by _key_changed (a real user edit).

    if key_saved:
        _saved_note()

    def _forget_key() -> None:
        # on_click callback: runs BEFORE the script body on the click
        # run, so the tab UI below renders the cleared state
        # immediately (no stale green note / disabled box).
        _old = st.session_state.get("api_key") or ""
        st.session_state["forgotten_key"] = _old
        st.session_state["api_key"] = ""
        st.session_state["key_restored"] = True
        st.session_state["forget_pending"] = True
        st.session_state["key_load_nonce"] += 1
        st.session_state["forget_just_clicked"] = True
        if "api_key_box" in st.session_state:
            del st.session_state["api_key_box"]

    key_col1, key_col2 = st.columns(2)
    with key_col1:
        verify_save_btn = st.button("✓ API Key စစ်ဆေးပြီး Browser မှာ သိမ်းမည်")
    with key_col2:
        forget_btn = st.button(" သိမ်းထားသော Key ဖျက်မည်", on_click=_forget_key)

    if verify_save_btn:
        _k = (st.session_state["api_key"] or "").strip()
        _ok, _msg = verify_gemini_api_key(_k)
        if _ok:
            # A fresh save supersedes any earlier Forget; force a fresh
            # localStorage read so the green note shows immediately.
            st.session_state["forgotten_key"] = ""
            st.session_state["forget_pending"] = False
            st.session_state["key_load_nonce"] += 1
            streamlit_js_eval(
                js_expressions=f"localStorage.setItem('{LS_KEY}',{json.dumps(_k)})",
                key=f"mmr_key_save_{st.session_state['key_load_nonce']}",
            )
            _saved_note()
        else:
            # Invalid key: show the error and do NOT save anything.
            st.error(_msg)

    if st.session_state.get("forget_just_clicked"):
        # Execute the removal exactly once. The eval key includes the
        # nonce (bumped in the callback), so the component re-executes
        # even if the user forgets again later (the frontend only runs
        # an expression when it changes). NO st.rerun() here: it would
        # discard this run's render and the removal would never reach
        # the browser. The removal result triggers a rerun on its own.
        streamlit_js_eval(
            js_expressions=f"localStorage.removeItem('{LS_KEY}')",
            key=f"mmr_key_del_{st.session_state['key_load_nonce']}",
        )
        st.session_state["forget_just_clicked"] = False

run_btn = st.button(" Recap ဗီဒီယို ထုတ်လုပ်မည်", type="primary")

if run_btn:
    # Fall back to the browser-saved key if session_state lost it
    # (e.g. a session reset) but the browser still has it. A key the
    # user just deleted (Forget) is never used, even if a stale cached
    # read still returns it.
    _saved = browser_saved or ""
    if _saved == st.session_state.get("forgotten_key"):
        _saved = ""
    key = (st.session_state.get("api_key") or _saved or "").strip()

    if not demo_active and not key:
        st.error("✕ ကျေးဇူးပြု၍ သင်၏ Free Gemini API Key ကို ထည့်သွင်းပေးပါ")
        st.stop()

    # Source precedence matches the canvas display: picked file > demo > URL.
    src_path = st.session_state.get("src_path") or None
    src_url = (st.session_state.get("src_url") or "").strip() or None
    if src_path:
        src_url = None
    elif demo_active and not src_url:
        if os.path.isfile(DEMO_SAMPLE):
            src_path = DEMO_SAMPLE
        else:
            st.error("✕ Demo video မရှိပါ")
            st.stop()
    if not src_path and not src_url:
        st.error("✕ Canvas ထဲမှာ ဗီဒီယို ရွေးပေးပါ သို့မဟုတ် Video URL ထည့်ပေးပါ")
        st.stop()

    # Per-run isolated working directory (multi-user safety).
    session_dir = tempfile.mkdtemp(prefix="recap_session_")

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
    }
    _blur_boxes = st.session_state.get("canvas_blur") or None

    # A new Run invalidates the previous result immediately, so a failed
    # run never shows a stale result.
    st.session_state["recap_result"] = None
    try:
        with st.spinner(" Recap ဗီဒီယို ထုတ်လုပ်နေပါတယ်… ခဏစောင့်ပေးပါ"):
            result = run_full_recap_pipeline(
                video_path=src_path,
                video_url=src_url,
                user_api_key=key or None,
                # Recap length slider removed (user order): AI Recap +
                # Pro AI Recap both fully work, so the length is fixed
                # at 30s (AI Recap narration target; Pro ignores it —
                # it uses the full video).
                target_duration=30,
                voice_name=_parse_voice(voice_choice),
                # Step 16: all Source-tab settings flow into the Step 15
                # backend exactly as chosen (aspect is now the Source
                # tab's 5-way pill selector, not the old Advanced radio).
                processing_mode=str(
                    st.session_state.get("processing_mode", "ai_recap")),
                aspect_ratio=str(
                    st.session_state.get("aspect_ratio", "9:16")),
                fit_mode=str(st.session_state.get("fit_mode", "crop")),
                crop_align=str(
                    st.session_state.get("crop_align", "center")),
                inner_scale=float(
                    st.session_state.get("inner_scale", 1.22)),
                vertical_pos=float(
                    st.session_state.get("vertical_pos", 0.0)),
                horizontal_pos=float(
                    st.session_state.get("horizontal_pos", 0.0)),
                mirror_horizontal=bool(
                    st.session_state.get("mirror_horizontal", False)),
                copyright_zoom=float(
                    st.session_state.get("copyright_zoom", 1.07)),
                random_crop_offset=bool(
                    st.session_state.get("random_crop_offset", False)),
                bg_volume=float(bg_volume),
                burn_subtitles=bool(burn_subtitles),
                work_dir=session_dir,
                output_dir=session_dir,
                output_name="recap.mp4",
                use_mock_plan=bool(demo_active),
                tts_proxy=tts_proxy,
                model=_parse_model(model_choice),
                blur_boxes=_blur_boxes,
                subtitle_config=_subtitle_config,
            )
        # Pro runs have no single video_speed_factor (per-block speeds)
        # — don't show "(None)".
        _sync_note = str(result["sync_method"])
        if result.get("video_speed_factor") is not None:
            _sync_note += f" ({result['video_speed_factor']})"
        st.session_state["recap_result"] = {
            "status": (
                f"✓ ပြီးဆုံးပါပြီ! | {result['aspect_ratio']} | "
                f"စာတန်း: {result['subtitle_mode']} | "
                f"sync: {_sync_note}"
            ),
            "video_path": result["final_video_path"],
            "srt_path": result["srt_path"],
            "title": result["title"],
            "script_text": result["script_text"],
        }
    except Exception as exc:  # noqa: BLE001 - surfaced to the user in Myanmar UI
        traceback.print_exc()
        st.error(f"✕ အမှား: {exc}")

# Render the last successful result on EVERY run — not only the run in
# which Run was clicked — so a download click or any other rerun does
# not wipe the output (the previous bug: the result lived only in a
# local variable, so the first download click cleared the whole section).
_saved = st.session_state.get("recap_result")
if _saved and os.path.isfile(_saved.get("video_path", "")):
    st.success(_saved["status"])
    st.video(_saved["video_path"])
    st.text_input(" ခေါင်းစဉ်", value=_saved["title"], disabled=True)
    st.text_area(
        " ဇာတ်ကြောင်းပြော Script",
        value=_saved["script_text"],
        height=200,
        disabled=True,
    )
    with open(_saved["video_path"], "rb") as f:
        st.download_button(
            " Recap ဗီဒီယို Download (.mp4)",
            data=f.read(),
            file_name="recap.mp4",
            mime="video/mp4",
        )
    if os.path.isfile(_saved.get("srt_path", "")):
        with open(_saved["srt_path"], "rb") as f:
            st.download_button(
                " မြန်မာစာတန်းထိုး Download (.srt)",
                data=f.read(),
                file_name="subtitles.srt",
                mime="text/plain",
            )
elif _saved:
    # Output files are gone (session temp cleaned): ask for a fresh run.
    st.info("ထုတ်လုပ်ထားတဲ့ ဗီဒီယိုဖိုင် သက်တမ်းကုန်သွားပါပြီ။ Run ကို ထပ်နှိပ်ပေးပါ။")
    st.session_state["recap_result"] = None

# A canvas action asked for a refresh. Do it now — after the Run
# handler has had its chance — so a Run click that arrived in the same
# run as a canvas action (e.g. picking a video, then hitting Run
# immediately) is not swallowed by an early st.rerun(). When Run did
# run, skip the refresh: the recap result must stay on screen.
if st.session_state.pop("_canvas_rerun_needed", False) and not run_btn:
    st.rerun()
