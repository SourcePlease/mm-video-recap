"""
app.py — Step 6: Multi-User BYOK Gradio Web UI.

100% Free Online Video Recap — every visitor uses their OWN free Gemini
API key (Bring-Your-Own-Key). No developer key exists anywhere in this app.

Multi-user safety: each request gets its own isolated working directory via
tempfile.mkdtemp(), so concurrent users never overwrite each other's files.
"""
from __future__ import annotations

import os
import tempfile
import traceback

import gradio as gr

from pipeline import run_full_recap_pipeline

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEMO_SAMPLE = os.path.join(APP_DIR, "sample_test.mp4")

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


def generate_recap(
    api_key,
    video_file,
    video_url,
    recap_length,
    voice_choice,
    aspect_choice,
    bg_volume,
    burn_subtitles,
    demo_mode,
):
    """Gradio handler: validate inputs, run one isolated pipeline session."""
    empty = (None, "", "", None, "")
    try:
        key = (api_key or "").strip()
        url = (video_url or "").strip()

        if not demo_mode and not key:
            msg = "ကျေးဇူးပြု၍ သင်၏ Free Gemini API Key ကို ထည့်သွင်းပေးပါ"
            return None, "", "", None, f"❌ {msg}"
        if not video_file and not url and not demo_mode:
            return (
                None, "", "", None,
                "❌ ဗီဒီယိုဖိုင် တင်ပေးပါ သို့မဟုတ် Video URL ထည့်ပေးပါ",
            )

        src_path = video_file or None
        src_url = url or None
        if demo_mode and not src_path and not src_url:
            if os.path.isfile(DEMO_SAMPLE):
                src_path = DEMO_SAMPLE
            else:
                return None, "", "", None, "❌ Demo video မရှိပါ"

        # Per-request isolated working directory (multi-user safety).
        session_dir = tempfile.mkdtemp(prefix="recap_session_")
        # Dev-VM only: explicit egress proxy for the keyless TTS service.
        # Production environments leave this as None.
        tts_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")

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
        return (
            result["final_video_path"],
            result["title"],
            result["script_text"],
            result["srt_path"],
            status,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the user in Myanmar UI
        traceback.print_exc()
        return None, "", "", None, f"❌ အမှား: {exc}"


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="100% Free Online Video Recap") as demo:
        gr.Markdown(HEADER)

        with gr.Row():
            api_key = gr.Textbox(
                label="🔑 သင်၏ Free Gemini API Key ထည့်ပါ",
                type="password",
                placeholder="AIza...",
            )
            demo_mode = gr.Checkbox(
                label="🧪 API Key မပါဘဲ Demo စမ်းသပ်မည် (Mock Mode)",
                value=False,
            )

        with gr.Row():
            video_file = gr.Video(label="📹 ဗီဒီယိုဖိုင် တင်ပါ")
            video_url = gr.Textbox(
                label="🔗 သို့မဟုတ် YouTube/Video URL ထည့်ပါ",
                placeholder="https://www.youtube.com/watch?v=...",
            )

        with gr.Row():
            recap_length = gr.Slider(
                15, 120, value=30, step=5,
                label="🎞️ Recap အရှည် (စက္ကန့်)",
            )
            voice_choice = gr.Radio(
                VOICE_CHOICES, value=VOICE_CHOICES[0],
                label="🎙️ မြန်မာ AI အသံ ရွေးပါ",
            )
            aspect_choice = gr.Radio(
                ASPECT_CHOICES, value=ASPECT_CHOICES[0],
                label="📐 Aspect Ratio ရွေးပါ",
            )

        with gr.Row():
            bg_volume = gr.Slider(
                0.0, 0.5, value=0.15, step=0.05,
                label="🔊 မူရင်းနောက်ခံအသံ အတိုးအကျယ်",
            )
            burn_subtitles = gr.Checkbox(
                label="💬 မြန်မာစာတန်းထိုး Burn-in ထည့်မည်",
                value=True,
            )

        run_btn = gr.Button("🎬 Recap ဗီဒီယို ထုတ်လုပ်မည်", variant="primary")
        status = gr.Textbox(label="📊 အခြေအနေ", interactive=False)

        with gr.Row():
            out_video = gr.Video(label="🎬 Recap Video")
        with gr.Row():
            out_title = gr.Textbox(label="📌 ခေါင်းစဉ်", interactive=False)
            out_srt = gr.File(label="📄 မြန်မာစာတန်းထိုး (.srt)")
        out_script = gr.Textbox(
            label="📝 ဇာတ်ကြောင်းပြော Script", lines=8, interactive=False
        )

        run_btn.click(
            fn=generate_recap,
            inputs=[
                api_key, video_file, video_url, recap_length, voice_choice,
                aspect_choice, bg_volume, burn_subtitles, demo_mode,
            ],
            outputs=[out_video, out_title, out_script, out_srt, status],
        )
    return demo


if __name__ == "__main__":
    build_ui().launch(server_name="0.0.0.0", server_port=7860)
