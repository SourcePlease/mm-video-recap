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
ASPECT_CHOICES = [
    "9:16 (TikTok / Reels / Shorts)",
    "16:9 (YouTube / Landscape)",
]


def _parse_voice(choice: str) -> str:
    return (choice or "").split(" — ")[0].strip() or "my-MM-ThihaNeural"


def _parse_aspect(choice: str) -> str:
    return "9:16" if (choice or "").startswith("9:16") else "16:9"


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
# Persistent per-session dir for the canvas-picked source file.
if "session_dir" not in st.session_state:
    st.session_state["session_dir"] = tempfile.mkdtemp(prefix="recap_session_")

# Ask this browser for a previously saved key (cached after first answer).
browser_saved = streamlit_js_eval(
    js_expressions=f"localStorage.getItem('{LS_KEY}')",
    key="mmr_key_load",
)
if browser_saved and not st.session_state["key_restored"]:
    st.session_state["api_key"] = browser_saved  # before widget creation
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
    key="mmr_canvas",
)
if isinstance(_val, dict):
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
            st.session_state["canvas_sub"] = {
                "x_pct": float(_s.get("x_pct", 0.5)),
                "y_pct": float(_s.get("y_pct", 0.82)),
                "font_size_pct": float(_fpct),
            }
        except (TypeError, ValueError):
            pass  # keep previous canvas state on malformed values

# ---- Step 12: Segmented Settings Tabs (directly below the canvas) ----
tab_src, tab_voice, tab_sub, tab_adv = st.tabs(
    [" Source", " Voice", " Subtitles", " Advanced"]
)

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
    model_choice = st.selectbox(" AI Model ရွေးပါ", MODEL_OPTIONS, index=0)
    recap_length = st.slider(" Recap အရှည် (စက္ကန့်)", 15, 120, 30, 5)

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
    )

with tab_adv:
    st.markdown(KEY_GUIDE)

    def _key_changed() -> None:
        # Fires ONLY when the user actually edits the key box. Browser
        # autofill does not trigger this, so a stale autofill ghost can
        # never clobber the restored key. The restored key lives in
        # "api_key" (a plain key); the box itself always starts empty.
        st.session_state["api_key"] = st.session_state.get("api_key_box", "")

    key_input = st.text_input(
        " သင်၏ Free Gemini API Key ထည့်ပါ",
        type="password",
        # Always start empty: the restored key is shown via the blue note,
        # never echoed back into the box (and an autofill ghost in the box
        # must not become the app's key).
        value="",
        key="api_key_box",
        placeholder="AIza...",
        on_change=_key_changed,
    )
    # NOTE: do NOT copy key_input into api_key here. The box is empty on
    # every fresh load; copying it would wipe the localStorage-restored
    # key (the "enter the key every time" bug). api_key is set only by
    # the restore above and by _key_changed (a real user edit).

    if browser_saved and st.session_state["api_key"] == browser_saved:
        st.info("✓ သိမ်းဆည်းထားသော API Key အသင့်ရှိနေပါသည်")

    key_col1, key_col2 = st.columns(2)
    with key_col1:
        verify_save_btn = st.button("✓ API Key စစ်ဆေးပြီး Browser မှာ သိမ်းမည်")
    with key_col2:
        forget_btn = st.button(" သိမ်းထားသော Key ဖျက်မည်")

    if verify_save_btn:
        _k = (st.session_state["api_key"] or "").strip()
        _ok, _msg = verify_gemini_api_key(_k)
        if _ok:
            streamlit_js_eval(
                js_expressions=f"localStorage.setItem('{LS_KEY}',{json.dumps(_k)})",
                key="mmr_key_save",
            )
            st.success(
                "✓ API Key မှန်ကန်ကြောင်း စစ်ဆေးပြီး "
                "သင့် Browser တွင် သိမ်းဆည်းပြီးပါပြီ!"
            )
        else:
            # Invalid key: show the error and do NOT save anything.
            st.error(_msg)

    if forget_btn:
        streamlit_js_eval(
            js_expressions=f"localStorage.removeItem('{LS_KEY}')",
            key="mmr_key_del",
        )
        st.session_state["api_key"] = ""
        # Don't re-restore the (now stale) cached browser value on rerun.
        st.session_state["key_restored"] = True
        if "api_key_box" in st.session_state:
            del st.session_state["api_key_box"]
        st.rerun()

    aspect_choice = st.radio(" Aspect Ratio ရွေးပါ", ASPECT_CHOICES, index=0)

run_btn = st.button(" Recap ဗီဒီယို ထုတ်လုပ်မည်", type="primary")

if run_btn:
    key = (st.session_state.get("api_key") or "").strip()

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
    }
    _blur_boxes = st.session_state.get("canvas_blur") or None

    try:
        with st.spinner(" Recap ဗီဒီယို ထုတ်လုပ်နေပါတယ်… ခဏစောင့်ပေးပါ"):
            result = run_full_recap_pipeline(
                video_path=src_path,
                video_url=src_url,
                user_api_key=key or None,
                target_duration=int(recap_length),
                voice_name=_parse_voice(voice_choice),
                aspect_ratio=_parse_aspect(aspect_choice),
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
        status = (
            f"✓ ပြီးဆုံးပါပြီ! | {result['aspect_ratio']} | "
            f"စာတန်း: {result['subtitle_mode']} | "
            f"sync: {result['sync_method']} ({result['video_speed_factor']})"
        )
        st.success(status)
        st.video(result["final_video_path"])
        st.text_input(" ခေါင်းစဉ်", value=result["title"], disabled=True)
        st.text_area(
            " ဇာတ်ကြောင်းပြော Script",
            value=result["script_text"],
            height=200,
            disabled=True,
        )
        with open(result["final_video_path"], "rb") as f:
            st.download_button(
                " Recap ဗီဒီယို Download (.mp4)",
                data=f.read(),
                file_name="recap.mp4",
                mime="video/mp4",
            )
        with open(result["srt_path"], "rb") as f:
            st.download_button(
                " မြန်မာစာတန်းထိုး Download (.srt)",
                data=f.read(),
                file_name="subtitles.srt",
                mime="text/plain",
            )
    except Exception as exc:  # noqa: BLE001 - surfaced to the user in Myanmar UI
        traceback.print_exc()
        st.error(f"✕ အမှား: {exc}")

# A canvas action asked for a refresh. Do it now — after the Run
# handler has had its chance — so a Run click that arrived in the same
# run as a canvas action (e.g. picking a video, then hitting Run
# immediately) is not swallowed by an early st.rerun(). When Run did
# run, skip the refresh: the recap result must stay on screen.
if st.session_state.pop("_canvas_rerun_needed", False) and not run_btn:
    st.rerun()
