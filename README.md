---
title: MM Video Recap
emoji: 🎬
colorFrom: blue
colorTo: indigo
sdk: gradio
sdk_version: 6.30.0
app_file: app.py
pinned: false
---

# 🎬 100% Free Online Video Recap

ဗီဒီယိုအရှည်ကြီးတွေကို **မြန်မာ AI အသံ** ဖြင့် အကျဉ်းချုပ်ပေးတဲ့ အခမဲ့စနစ်။

## 🔑 Free Gemini API Key အခမဲ့ယူနည်း (၁ မိနစ်အတွင်း၊ ကတ်မလို)

1. 👉 **[https://aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)** ကို နှိပ်ပါ
2. **"Create API Key"** → **"Create API key in new project"** ကို ရွေးပါ
3. ရလာတဲ့ Key ကို copy ကူးပြီး App ထဲက 🔑 အကွက်မှာ paste ထည့်ပါ

> Key ကို server မှာ သိမ်းထားခြင်း **လုံးဝမရှိပါ** — သင့်ဘရောက်ဇာကနေ
> တိုက်ရိုက်ပေးပို့ပြီး အသုံးပြုပြီးတိုင်း မေ့ပစ်ပါတယ် (Bring-Your-Own-Key)။

## 📹 အသုံးပြုနည်း

1. 🔑 အကွက်မှာ သင်၏ Free Gemini API Key ထည့်ပါ
2. 📹 ဗီဒီယိုဖိုင် တင်ပါ (သို့မဟုတ် 🔗 YouTube/Video URL ထည့်ပါ)
3. ⚙️ Recap အရှည်၊ မြန်မာအသံ (ကျား/မ)၊ Aspect Ratio (9:16 / 16:9) ရွေးပါ
4. 🎬 **"Recap ဗီဒီယို ထုတ်လုပ်မည်"** ကို နှိပ်ပါ
5. 🎬 ရလာတဲ့ ဗီဒီယိုကို ကြည့်ပြီး 📄 `.srt` စာတန်းထိုးဖိုင်ကို ဒေါင်းလုဒ်ဆွဲပါ

🧪 **Demo Mode**: API Key မရှိသေးရင် *"API Key မပါဘဲ Demo စမ်းသပ်မည်"* ကို
အမှန်ခြစ်ပြီး စနစ်အလုပ်လုပ်ပုံကို နမူနာ စမ်းကြည့်နိုင်ပါတယ်။

## ☁️ Deploy (Streamlit Community Cloud — အခမဲ့)

1. ဒီ repo ကို GitHub မှာ public အဖြစ် တင်ပါ
2. [share.streamlit.io](https://share.streamlit.io) မှာ GitHub နဲ့ ဝင်ပြီး
   `streamlit_app.py` ကို deploy လုပ်ပါ
3. `packages.txt` (`ffmpeg`, `fonts-noto-core`) ကို Streamlit Cloud က
   အလိုအလျောက် သွင်းပေးပါတယ် — `requirements.txt` ထဲက Python packages တွေလည်း
   အလိုအလျောက် install လုပ်ပေးပါတယ်
4. ရလာတဲ့ `https://*.streamlit.app` URL က အမြဲတမ်း သုံးနိုင်တဲ့ Free Domain ပါ

## ⚙️ နည်းပညာ အချက်အလက်

- **AI Video Analysis**: Google Gemini (`gemini-2.5-flash` → auto-fallback) —
  သုံးစွဲသူရဲ့ Key နဲ့သာ ခေါ်ပါတယ်
- **မြန်မာ Voiceover**: Microsoft Edge TTS (`my-MM-ThihaNeural` / `my-MM-NilarNeural`)
- **Video**: FFmpeg (highlight trim/concat, setpts speed-sync, audio ducking,
  libass မြန်မာစာတန်းထိုး burn-in)
- **Multi-user**: request တစ်ခုချင်းစီ သီးခြား temp directory နဲ့ အလုပ်လုပ်ပါတယ်
