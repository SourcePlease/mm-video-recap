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

import json
import os
import tempfile
import traceback

import streamlit as st
from streamlit_js_eval import streamlit_js_eval

from pipeline import run_full_recap_pipeline

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEMO_SAMPLE = os.path.join(APP_DIR, "sample_test.mp4")

st.set_page_config(
    page_title="100% Free Online Video Recap",
    page_icon="🎬",
    layout="centered",
)

HEADER = """\
# 🎬 100% Free Online Video Recap

ဗီဒီယိုအရှည်ကြီးတွေကို **မြန်မာ AI အသံ** ဖြင့် အကျဉ်းချုပ်ပေးတဲ့ အခမဲ့စနစ်။
သင့်ရဲ့ **ကိုယ်ပိုင် Free Gemini API Key** နဲ့သာ အလုပ်လုပ်ပါတယ် —
Key ကို server မှာ သိမ်းထားခြင်း **လုံးဝမရှိပါ**။

### 🔑 Free Gemini API Key အခမဲ့ယူနည်း (၁ မိနစ်အတွင်း၊ ကတ်မလို)
1. 👉 **[https://aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)** ကို နှိပ်ပါ
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


def verify_gemini_api_key(user_api_key: str):
    """Lightweight live check that a Gemini API key actually works.

    Returns (ok, message). Makes exactly one tiny API call (first page of
    the model list) — no video is uploaded and (almost) no tokens are spent.
    """
    key = (user_api_key or "").strip()
    if not key:
        return False, "❌ API Key ထည့်သွင်းပေးပါ"
    try:
        from google import genai

        client = genai.Client(api_key=key)
        # Force exactly one small HTTP request; any auth/network failure
        # raises here, which means the key is not usable.
        next(iter(client.models.list()), None)
        return True, "✅ API Key မှန်ကန်ပါသည်"
    except Exception:  # noqa: BLE001 - any failure means the key can't be used
        return (
            False,
            "❌ API Key မှားယွင်းနေပါသည် သို့မဟုတ် အလုပ်မလုပ်ပါ။ "
            "Key အမှန်ကို ပြန်စစ်ပေးပါ",
        )


# Browser-local key storage (per-user, never touches our server).
LS_KEY = "mmr_gemini_api_key"

if "api_key" not in st.session_state:
    st.session_state["api_key"] = ""
if "key_restored" not in st.session_state:
    st.session_state["key_restored"] = False

# Ask this browser for a previously saved key (cached after first answer).
browser_saved = streamlit_js_eval(
    js_expressions=f"localStorage.getItem('{LS_KEY}')",
    key="mmr_key_load",
)
if browser_saved and not st.session_state["key_restored"]:
    st.session_state["api_key"] = browser_saved  # before widget creation
    st.session_state["key_restored"] = True

st.markdown(HEADER)

key_input = st.text_input(
    "🔑 သင်၏ Free Gemini API Key ထည့်ပါ",
    type="password",
    value=st.session_state["api_key"],
    placeholder="AIza...",
    key="api_key_box",
)
# Plain source of truth (the widget above owns "api_key_box").
st.session_state["api_key"] = key_input

if browser_saved and st.session_state["api_key"] == browser_saved:
    st.info("🟢 သိမ်းဆည်းထားသော API Key အသင့်ရှိနေပါသည်")

key_col1, key_col2 = st.columns(2)
with key_col1:
    verify_save_btn = st.button("✅ API Key စစ်ဆေးပြီး Browser မှာ သိမ်းမည်")
with key_col2:
    forget_btn = st.button("🗑️ သိမ်းထားသော Key ဖျက်မည်")

if verify_save_btn:
    _k = (st.session_state["api_key"] or "").strip()
    _ok, _msg = verify_gemini_api_key(_k)
    if _ok:
        streamlit_js_eval(
            js_expressions=f"localStorage.setItem('{LS_KEY}',{json.dumps(_k)})",
            key="mmr_key_save",
        )
        st.success(
            "✅ API Key မှန်ကန်ကြောင်း စစ်ဆေးပြီး "
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
demo_mode = st.checkbox(
    "🧪 API Key မပါဘဲ Demo စမ်းသပ်မည် (Mock Mode)",
    value=False,
)

video_file = st.file_uploader(
    "📹 ဗီဒီယိုဖိုင် တင်ပါ",
    type=["mp4", "mov", "mkv", "avi", "webm"],
)
video_url = st.text_input(
    "🔗 သို့မဟုတ် YouTube/Video URL ထည့်ပါ",
    placeholder="https://www.youtube.com/watch?v=...",
)

recap_length = st.slider("🎞️ Recap အရှည် (စက္ကန့်)", 15, 120, 30, 5)
voice_choice = st.radio("🎙️ မြန်မာ AI အသံ ရွေးပါ", VOICE_CHOICES, index=0)
aspect_choice = st.radio("📐 Aspect Ratio ရွေးပါ", ASPECT_CHOICES, index=0)
bg_volume = st.slider("🔊 မူရင်းနောက်ခံအသံ အတိုးအကျယ်", 0.0, 0.5, 0.15, 0.05)
burn_subtitles = st.checkbox("💬 မြန်မာစာတန်းထိုး Burn-in ထည့်မည်", value=True)

run_btn = st.button("🎬 Recap ဗီဒီယို ထုတ်လုပ်မည်", type="primary")

if run_btn:
    key = (st.session_state.get("api_key") or "").strip()
    url = (video_url or "").strip()

    if not demo_mode and not key:
        st.error("❌ ကျေးဇူးပြု၍ သင်၏ Free Gemini API Key ကို ထည့်သွင်းပေးပါ")
        st.stop()
    if not video_file and not url and not demo_mode:
        st.error("❌ ဗီဒီယိုဖိုင် တင်ပေးပါ သို့မဟုတ် Video URL ထည့်ပေးပါ")
        st.stop()

    # Per-run isolated working directory (multi-user safety).
    session_dir = tempfile.mkdtemp(prefix="recap_session_")

    src_path = None
    src_url = url or None
    if video_file is not None:
        src_path = os.path.join(session_dir, "input.mp4")
        with open(src_path, "wb") as f:
            f.write(video_file.getbuffer())
    elif demo_mode and not src_url:
        if os.path.isfile(DEMO_SAMPLE):
            src_path = DEMO_SAMPLE
        else:
            st.error("❌ Demo video မရှိပါ")
            st.stop()

    # Streamlit Cloud has no egress proxy; the env lookup is harmless there
    # and keeps local/dev parity with app.py.
    tts_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")

    try:
        with st.spinner("⏳ Recap ဗီဒီယို ထုတ်လုပ်နေပါတယ်… ခဏစောင့်ပေးပါ"):
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
                use_mock_plan=bool(demo_mode),
                tts_proxy=tts_proxy,
            )
        status = (
            f"✅ ပြီးဆုံးပါပြီ! | {result['aspect_ratio']} | "
            f"စာတန်း: {result['subtitle_mode']} | "
            f"sync: {result['sync_method']} ({result['video_speed_factor']})"
        )
        st.success(status)
        st.video(result["final_video_path"])
        st.text_input("📌 ခေါင်းစဉ်", value=result["title"], disabled=True)
        st.text_area(
            "📝 ဇာတ်ကြောင်းပြော Script",
            value=result["script_text"],
            height=200,
            disabled=True,
        )
        with open(result["final_video_path"], "rb") as f:
            st.download_button(
                "⬇️ Recap ဗီဒီယို Download (.mp4)",
                data=f.read(),
                file_name="recap.mp4",
                mime="video/mp4",
            )
        with open(result["srt_path"], "rb") as f:
            st.download_button(
                "⬇️ မြန်မာစာတန်းထိုး Download (.srt)",
                data=f.read(),
                file_name="subtitles.srt",
                mime="text/plain",
            )
    except Exception as exc:  # noqa: BLE001 - surfaced to the user in Myanmar UI
        traceback.print_exc()
        st.error(f"❌ အမှား: {exc}")
