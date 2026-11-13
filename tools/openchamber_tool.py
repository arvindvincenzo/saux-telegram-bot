import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
import httpx

logger = logging.getLogger("openchamber_tool")

OPENCODE_PORT = 57123
OPENCODE_AUTH = ("opencode", "sauxagent2026")
PRIMARY_MODEL = "inkling-2-small-free"

OPENCODE_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "@openchamberelectron" / "resources" / "opencode-cli" / "opencode.exe"

AGENTIC_SYSTEM_PROMPT = (
    "<system-reminder>\n"
    "CRITICAL AGENTIC DIRECTIVE:\n"
    "• Identity: You are SAUX AI Assistant for SAUX Super Inventory.\n"
    "• Never state you are an AI model, OpenCode, or Inkling, and never talk about coding workspaces or tools.\n"
    "• Strict length: Exactly 1, 2, or 3 lines maximum.\n"
    "• When live facts are provided in <live-web-data>, answer strictly and accurately from those live facts.\n"
    "• Never use outdated or pre-2026 historical training memory when live facts are present.\n"
    "• Direct, helpful answer. No greetings, no preamble. Jump straight to the point.\n"
    "</system-reminder>\n\n"
)

async def ensure_opencode_service():
    """Ensures opencode serve background process is running on port 57123."""
    if await is_openchamber_ready():
        return True
    
    if OPENCODE_EXE.exists():
        logger.info(f"Starting opencode server on port {OPENCODE_PORT}...")
        env = os.environ.copy()
        env["OPENCODE_SERVER_PASSWORD"] = "sauxagent2026"
        try:
            subprocess.Popen(
                [str(OPENCODE_EXE), "serve", "--port", str(OPENCODE_PORT)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            for _ in range(10):
                await asyncio.sleep(0.5)
                if await is_openchamber_ready():
                    logger.info("OpenCode server is up and responsive!")
                    return True
        except Exception as e:
            logger.error(f"Failed to auto-spawn OpenCode: {e}")
    return False

async def is_openchamber_ready() -> bool:
    """Checks if OpenCode server is healthy and responding on port 57123."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"http://127.0.0.1:{OPENCODE_PORT}/api/session", auth=OPENCODE_AUTH)
            return resp.status_code == 200
    except Exception:
        return False

async def create_fresh_session() -> str:
    """Creates a brand new OpenCode session pinned to inkling-2-small-free."""
    await ensure_opencode_service()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"http://127.0.0.1:{OPENCODE_PORT}/api/session",
            auth=OPENCODE_AUTH,
            json={},
            headers={"Content-Type": "application/json"}
        )
        resp.raise_for_status()
        new_id = resp.json().get("data", {}).get("id")
        if not new_id:
            raise RuntimeError("Failed to obtain session ID.")

        # Pin directly to inkling-2-small-free
        try:
            await client.post(
                f"http://127.0.0.1:{OPENCODE_PORT}/api/session/{new_id}/model",
                auth=OPENCODE_AUTH,
                json={"model": {"providerID": "opencode", "id": PRIMARY_MODEL}},
                headers={"Content-Type": "application/json"}
            )
        except Exception:
            pass
        return new_id

async def _poll_for_answer(session_id: str, prompt: str, timeout_seconds: int = 20) -> str:
    """Submits prompt and polls for completed assistant response."""
    full_prompt = f"{AGENTIC_SYSTEM_PROMPT}{prompt.strip()}"
    async with httpx.AsyncClient(timeout=25.0) as client:
        # 1. Fetch current message IDs
        try:
            r = await client.get(f"http://127.0.0.1:{OPENCODE_PORT}/api/session/{session_id}/message", auth=OPENCODE_AUTH)
            prev_ids = {m.get("id") for m in r.json().get("data", [])} if r.status_code == 200 else set()
        except Exception:
            prev_ids = set()

        # 2. Submit prompt
        p_resp = await client.post(
            f"http://127.0.0.1:{OPENCODE_PORT}/api/session/{session_id}/prompt",
            auth=OPENCODE_AUTH,
            json={"text": full_prompt},
            headers={"Content-Type": "application/json"}
        )
        if p_resp.status_code != 200:
            raise RuntimeError(f"Prompt rejected ({p_resp.status_code}): {p_resp.text}")

        # 3. Poll for reply
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < timeout_seconds:
            await asyncio.sleep(0.5)
            msg_resp = await client.get(
                f"http://127.0.0.1:{OPENCODE_PORT}/api/session/{session_id}/message",
                auth=OPENCODE_AUTH
            )
            if msg_resp.status_code != 200:
                continue

            all_msgs = msg_resp.json().get("data", [])
            new_msgs = [m for m in all_msgs if m.get("id") not in prev_ids]
            
            idle_completed = any(m.get("type") == "idle" for m in new_msgs)
            
            for m in reversed(new_msgs):
                if m.get("type") == "assistant":
                    content = m.get("content", [])
                    texts = [c.get("text", "") for c in content if c.get("type") == "text" and c.get("text", "").strip()]
                    if texts and (idle_completed or m.get("finish") == "stop"):
                        raw_reply = "\n".join(texts).strip()
                        lines = [l.strip() for l in raw_reply.splitlines() if l.strip()]
                        return "\n".join(lines[:3]) if len(lines) > 3 else "\n".join(lines)

    raise TimeoutError("Model took too long to complete.")

def handle_conversational_fallback(prompt: str) -> str | None:
    """Provides natural responses for greetings, bot identity, and status when no LLM key is configured."""
    import re
    p = prompt.strip().lower()
    
    # Greetings
    if re.search(r"^(hi+|hey+|hello+|hola+|greetings+|good\s*(morning|afternoon|evening|day|night))\b", p):
        return (
            "👋 **Hello! Welcome to SAUX Super Intelligence.**\n"
            "I'm your autonomous assistant for research, web intelligence, video downloads, and image editing.\n"
            "How can I help you today?"
        )
    
    # Bot identity / capability questions
    if any(q in p for q in ["who are you", "what bot", "what are you", "what can you do", "introduce yourself", "tell me about yourself", "your name"]):
        return (
            "🤖 **I am SAUX Super Intelligence — Autonomous AI Agent.**\n\n"
            "• 🧠 **Autonomous Q&A & Research:** Ask questions, solve queries, or analyze topics\n"
            "• 🔍 **Live Web Intelligence:** Type `search <topic>` for real-time web facts\n"
            "• ⚡ **Instant Media Downloader:** Paste YouTube, Shorts, TikTok, or Reels links for 1080p/4K downloads\n"
            "• 🖼 **Image Engineering & Vision:** Send photos with questions or `compress to 100kb`"
        )
        
    # How are you / status
    if any(q in p for q in ["how are you", "how are u", "how do you do", "todsy good time", "today good time"]):
        return (
            "⚡ **All systems operating at peak performance!**\n"
            "Ready to assist with your research, live web searches, media downloads, and image tasks."
        )

    # Thanks / gratitude
    if any(q in p for q in ["thanks", "thank you", "thx", "appreciate"]):
        return "You're very welcome! Let me know whenever you need anything else. 🚀"

    return None

async def generate_chat_reply(session_id: str | None, prompt: str, image_path: str | None = None) -> tuple[str, str]:
    """
    Submits prompt to inkling-2-small-free with auto-healing,
    with multimodal vision support and seamless fallback to Gemini/Groq/Web extraction.
    Returns: (reply_text, valid_session_id)
    """
    from config import gemini_key_pool, groq_key_pool

    # 1. Multimodal Vision Pipeline: processes images directly via Gemini 2.0 Flash / Groq Vision
    if image_path and os.path.exists(image_path):
        import base64
        import mimetypes
        mime, _ = mimetypes.guess_type(image_path)
        mime = mime or "image/jpeg"
        try:
            with open(image_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            logger.error(f"Failed to read image for vision: {e}")
            b64_data = None

        if b64_data:
            gem_key = gemini_key_pool.get_key()
            if gem_key:
                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gem_key}"
                    async with httpx.AsyncClient(timeout=25.0) as client:
                        resp = await client.post(url, json={
                            "contents": [{
                                "parts": [
                                    {"text": f"{AGENTIC_SYSTEM_PROMPT}{prompt}"},
                                    {"inline_data": {"mime_type": mime, "data": b64_data}}
                                ]
                            }],
                            "generationConfig": {"maxOutputTokens": 300, "temperature": 0.2}
                        })
                        if resp.status_code == 200:
                            candidates = resp.json().get("candidates", [])
                            if candidates:
                                text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                                lines = [l.strip() for l in text.splitlines() if l.strip()]
                                return "\n".join(lines[:3]), "gemini-vision"
                except Exception as e:
                    logger.warning(f"Gemini vision error: {e}")

            groq_key = groq_key_pool.get_key()
            if groq_key:
                try:
                    url = "https://api.groq.com/openai/v1/chat/completions"
                    async with httpx.AsyncClient(timeout=25.0) as client:
                        resp = await client.post(
                            url,
                            headers={"Authorization": f"Bearer {groq_key}"},
                            json={
                                "model": "llama-3.2-11b-vision-preview",
                                "messages": [
                                    {"role": "system", "content": AGENTIC_SYSTEM_PROMPT},
                                    {
                                        "role": "user",
                                        "content": [
                                            {"type": "text", "text": prompt},
                                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64_data}"}}
                                        ]
                                    }
                                ],
                                "max_tokens": 250,
                                "temperature": 0.2
                            }
                        )
                        if resp.status_code == 200:
                            text = resp.json()["choices"][0]["message"]["content"].strip()
                            lines = [l.strip() for l in text.splitlines() if l.strip()]
                            return "\n".join(lines[:3]), "groq-vision"
                except Exception as e:
                    logger.warning(f"Groq vision error: {e}")

            return (
                "🤖 **Image Received**\n"
                "I see your photo! To enable multimodal visual answers, set a free Gemini or Groq API key.\n"
                "You can also tap below to compress this photo into ~1KB ultra-compact size.",
                "photo-ack"
            )

    is_ready = await is_openchamber_ready()
    if is_ready or OPENCODE_EXE.exists():
        await ensure_opencode_service()

        # Step 1: Try existing session if available
        if session_id and session_id != "cloud-fallback":
            try:
                reply = await _poll_for_answer(session_id, prompt, timeout_seconds=15)
                return reply, session_id
            except Exception as e:
                logger.warning(f"Session {session_id} stalled ({e}). Auto-healing with fresh session...")

        # Step 2: Auto-heal with brand new session
        try:
            fresh_session_id = await create_fresh_session()
            reply = await _poll_for_answer(fresh_session_id, prompt, timeout_seconds=20)
            return reply, fresh_session_id
        except Exception as e:
            logger.warning(f"Local OpenCode failed ({e}). Proceeding to cloud fallback...")

    # Cloud / Headless Fallback:
    from config import gemini_key_pool, groq_key_pool
    gem_key = gemini_key_pool.get_key()
    if gem_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gem_key}"
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, json={
                    "contents": [{"parts": [{"text": f"{AGENTIC_SYSTEM_PROMPT}{prompt}"}]}],
                    "generationConfig": {"maxOutputTokens": 300, "temperature": 0.3}
                })
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                        lines = [l.strip() for l in text.splitlines() if l.strip()]
                        return "\n".join(lines[:3]), "gemini-cloud"
        except Exception as e:
            logger.warning(f"Gemini cloud fallback error: {e}")

    groq_key = groq_key_pool.get_key()
    if groq_key:
        try:
            url = "https://api.groq.com/openai/v1/chat/completions"
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {groq_key}"},
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "messages": [
                            {"role": "system", "content": AGENTIC_SYSTEM_PROMPT},
                            {"role": "user", "content": prompt}
                        ],
                        "max_tokens": 250,
                        "temperature": 0.3
                    }
                )
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"].strip()
                    lines = [l.strip() for l in text.splitlines() if l.strip()]
                    return "\n".join(lines[:3]), "groq-cloud"
        except Exception as e:
            logger.warning(f"Groq cloud fallback error: {e}")

    # Conversational Fallback: handles greetings, identity, and common queries
    conv_reply = handle_conversational_fallback(prompt)
    if conv_reply:
        return conv_reply, "conversational-intent"

    # Fallback to direct context extraction if live web data was fetched
    if "<live-web-data>" in prompt:
        import re
        snippets = re.findall(r"•\s*\[NEWS\]\s*([^:\n]+):\s*([^\n]+)", prompt)
        if not snippets:
            snippets = re.findall(r"•\s*([^\n]+)", prompt)
        if snippets:
            clean_lines = []
            for s in snippets[:3]:
                if isinstance(s, tuple):
                    clean_lines.append(f"• {s[0]}: {s[1][:120]}...")
                else:
                    clean_lines.append(f"• {s[:140]}...")
            return "\n".join(clean_lines), "web-summary"

    # Auto-fetch live web intelligence for queries without local/cloud keys
    try:
        from tools.web_tool import get_live_web_context
        web_facts = get_live_web_context(prompt, max_results=3)
        if web_facts:
            lines = [l.strip() for l in web_facts.splitlines() if l.strip() and not l.startswith("<")]
            if lines:
                return "🔍 **Live Web Intelligence:**\n\n" + "\n".join(lines[:3]), "web-summary"
    except Exception as e:
        logger.debug(f"Auto-fetch web fallback error: {e}")

    return (
        "🤖 **SAUX Super Intelligence**\n"
        "To enable deep generative AI reasoning and multimodal vision on this cloud instance, add your free Google Gemini API key to Railway:\n"
        "`railway variables --set GEMINI_API_KEYS=\"your_gemini_key\"`\n"
        "*(Get a free key in 30 seconds at https://aistudio.google.com/)*",
        "key-required"
    )
