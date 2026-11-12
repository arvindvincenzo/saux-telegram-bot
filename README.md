# 24/7 Multi-Purpose Agentic Telegram Bot (PydanticAI)

An autonomous, decision-making Telegram bot powered by **PydanticAI** and **aiogram 3**.

---

## ⚡ Features
- **Intelligent Decision-Maker (PydanticAI):** Evaluates user intent, infers missing parameters, and autonomously selects the appropriate tool.
- **Image Processing Engine (Pillow):**
  - Iterative compression to target file size (e.g. `compress this to 300kb`).
  - Independent X/Y aspect ratio stretching (e.g. `stretch 2x wide`).
  - Proportional dimension resizing and format conversions (WebP, PNG, JPG).
- **Media Downloader Engine (yt-dlp + ffmpeg):**
  - Downloads video or extracts MP3 audio from YouTube, Shorts, TikTok, Instagram Reels, Twitter/X.
  - Automatically respects Telegram's 50MB upload limits.
- **Live Web Search (DuckDuckGo):** Real-time web search without paid API keys.
- **Zero-Cost 24/7 Operation:** Runs locally via Telegram Long Polling (no public IP, domain, or router port forwarding needed).

---

## 🚀 Quick Setup

### 1. Edit `.env`
Open `telegram_agent/.env` and insert your tokens:
```env
TELEGRAM_BOT_TOKEN=your_token_from_botfather
AI_PROVIDER=gemini
GEMINI_API_KEY=your_free_key_from_aistudio.google.com
```

### 2. Run the Bot
Double-click `run_bot.bat` or run in terminal:
```powershell
cd telegram_agent
.venv\Scripts\python.exe bot.py
```

---

## 💬 Usage Examples in Telegram
- **Photo Compression:** Send any picture with caption: `make this under 400kb`
- **Photo Stretching:** Send any picture with caption: `stretch this 2x wide and 0.8x height`
- **Video Download:** Send: `download this video https://youtu.be/...`
- **Audio Extraction:** Send: `extract audio from https://youtu.be/...`
- **Web Search:** Send: `search latest news about SpaceX`
