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
    "• Identity: You are SAUX Super Intelligence — Autonomous AI Agent.\n"
    "• Tone & Style: Direct, engaging, empathetic, and exceptionally clear.\n"
    "• Strict length: Strictly between 2 and 7 lines maximum. Never write a single dismissive line, and never exceed 7 lines.\n"
    "• Conversation Context: You maintain continuity and remember recent turns in this conversation.\n"
    "• When live facts are provided in <live-web-data>, synthesize them factually into 2 to 5 clear sentences. Never dump raw search snippets, URLs, or metadata.\n"
    "• Never state you are an AI model, OpenCode, or Inkling, and never talk about coding workspaces or tools.\n"
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
                        return enforce_2_to_7_lines(raw_reply)

    raise TimeoutError("Model took too long to complete.")

def enforce_2_to_7_lines(text: str) -> str:
    """Enforces that the reply is strictly between 2 and 7 lines, with clean readability."""
    if not text:
        return "I'm here and ready to help!\nWhat would you like to explore next?"

    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return "I'm here and ready to help!\nWhat would you like to explore next?"

    if len(lines) > 7:
        lines = lines[:7]

    if len(lines) == 1:
        single = lines[0]
        if not single.endswith((".", "!", "?")):
            single += "."
        lines = [single, "Feel free to let me know if you need more details or have another question! 🚀"]

    return "\n".join(lines)

def extract_clean_prompt(prompt: str) -> str:
    """Extracts raw user question even if wrapped in system or web data tags."""
    import re
    if "Question:" in prompt:
        m = re.search(r"Question:\s*(.*?)(?:\nAnswer|\Z)", prompt, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
    clean = re.sub(r"<live-web-data>.*?</live-web-data>", "", prompt, flags=re.DOTALL)
    clean = re.sub(r"<system-reminder>.*?</system-reminder>", "", clean, flags=re.DOTALL)
    return clean.strip()

def handle_conversational_fallback(prompt: str, history: list[dict] | None = None) -> str | None:
    """Provides natural, empathetic 2-7 line responses for greetings, clarifications, mood, and identity."""
    import re
    clean_p = extract_clean_prompt(prompt).lower().strip()

    # 1. Greetings
    if re.search(r"^(hi+|hey+(\s+there)*|hello+|hola+|sup|yo|greetings|good\s*(morning|afternoon|evening|day|night))\b", clean_p):
        return (
            "👋 **Hey there! Welcome to SAUX Super Intelligence.**\n"
            "I'm your autonomous assistant for deep research, live web facts, media downloads, and image engineering.\n"
            "What would you like to work on today?"
        )

    # 2. Clarifications / Follow-up reactions (e.g. 'why u saying about that?', 'why did you say that')
    if re.search(r"\b(why (are you|did you|u|you) say|what do you mean|why saying that|why u saying|who asked that)\b", clean_p):
        return (
            "I apologize for the confusion earlier!\n"
            "My automated search trigger misread the context and pulled web results instead of speaking directly to you.\n"
            "I've adjusted our conversation to be much clearer and directly focused on what you need.\n"
            "What can I help you with right now?"
        )

    # 3. Emotions, Mood, Feelings, Feeling down/sad
    if re.search(r"\b(my mood|mood is bad|mood so bad|feel bad|feeling bad|i am sad|i'm sad|feeling low|i feel down|depressed|cheer me up|what to do when sad|bad mood)\b", clean_p):
        return (
            "I'm really sorry you're feeling down today. 💙\n"
            "When your mood drops, try taking a slow deep breath and pausing whatever you're working on for a moment.\n"
            "A short 5-minute walk, drinking a glass of cold water, or putting on your favorite comfort music can help reset your thoughts.\n"
            "Be gentle with yourself—you don't have to carry everything all at once.\n"
            "I'm right here if you want to chat, vent, or distract yourself with something interesting!"
        )

    # 4. Bot identity / capability questions
    if any(q in clean_p for q in ["who are you", "what bot", "what are you", "what can you do", "introduce yourself", "tell me about yourself", "your name"]):
        return (
            "🤖 **I am SAUX Super Intelligence — Autonomous AI Agent.**\n\n"
            "• 🧠 **Autonomous Q&A & Research:** In-depth answers, explanations, and creative assistance\n"
            "• ⚡ **Instant Media Downloader:** 1080p downloads for YouTube, TikTok, Reels, and Shorts\n"
            "• 🖼 **Image Engineering & Vision:** Smart image compression, resizing, and visual understanding\n\n"
            "Just send me a message, image, or media link anytime to get started!"
        )

    # 5. Status / How are you
    if any(q in clean_p for q in ["how are you", "how are u", "how do you do", "todsy good time", "today good time"]):
        return (
            "⚡ **All systems operating at peak performance!**\n"
            "Ready to assist with your research, live web searches, media downloads, and image tasks.\n"
            "How is your day treating you so far?"
        )

    # 6. Thanks / gratitude
    if any(q in clean_p for q in ["thanks", "thank you", "thx", "appreciate", "thank u"]):
        return (
            "You're very welcome! Always glad to help.\n"
            "Let me know whenever you're ready for the next task or question. 🚀"
        )

    # 7. Conversational small talk acknowledgments
    if re.match(r"^(ok|okay|cool|nice|awesome|great|got it|understood|lol|haha)$", clean_p):
        return (
            "Sounds great! 👍\n"
            "Let me know if you'd like to research a topic, download media, or edit an image."
        )

    return None

async def generate_chat_reply(
    session_id: str | None,
    prompt: str,
    image_path: str | None = None,
    history: list[dict] | None = None
) -> tuple[str, str]:
    """
    Submits prompt to inkling-2-small-free with auto-healing,
    with multimodal vision support and seamless fallback to Gemini/Groq/Web extraction.
    Returns: (reply_text, valid_session_id)
    """
    from config import gemini_key_pool, groq_key_pool

    # Quick Conversational Intent Check (avoids unnecessary LLM latency for greetings / small-talk / clarifications)
    if not image_path and "<live-web-data>" not in prompt:
        conv_quick = handle_conversational_fallback(prompt, history)
        if conv_quick:
            return enforce_2_to_7_lines(conv_quick), "conversational-intent"

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
                    contents = []
                    if history:
                        for h in history[-6:]:
                            role = "user" if h.get("role") == "user" else "model"
                            contents.append({"role": role, "parts": [{"text": h.get("content", "")}]})
                    contents.append({
                        "role": "user",
                        "parts": [
                            {"text": f"{AGENTIC_SYSTEM_PROMPT}{prompt}"},
                            {"inline_data": {"mime_type": mime, "data": b64_data}}
                        ]
                    })
                    async with httpx.AsyncClient(timeout=25.0) as client:
                        resp = await client.post(url, json={
                            "contents": contents,
                            "generationConfig": {"maxOutputTokens": 300, "temperature": 0.2}
                        })
                        if resp.status_code == 200:
                            candidates = resp.json().get("candidates", [])
                            if candidates:
                                text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                                return enforce_2_to_7_lines(text), "gemini-vision"
                except Exception as e:
                    logger.warning(f"Gemini vision error: {e}")

            groq_key = groq_key_pool.get_key()
            if groq_key:
                try:
                    url = "https://api.groq.com/openai/v1/chat/completions"
                    messages = [{"role": "system", "content": AGENTIC_SYSTEM_PROMPT}]
                    if history:
                        for h in history[-6:]:
                            role = "user" if h.get("role") == "user" else "assistant"
                            messages.append({"role": role, "content": h.get("content", "")})
                    messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64_data}"}}
                        ]
                    })
                    async with httpx.AsyncClient(timeout=25.0) as client:
                        resp = await client.post(
                            url,
                            headers={"Authorization": f"Bearer {groq_key}"},
                            json={
                                "model": "llama-3.2-11b-vision-preview",
                                "messages": messages,
                                "max_tokens": 250,
                                "temperature": 0.2
                            }
                        )
                        if resp.status_code == 200:
                            text = resp.json()["choices"][0]["message"]["content"].strip()
                            return enforce_2_to_7_lines(text), "groq-vision"
                except Exception as e:
                    logger.warning(f"Groq vision error: {e}")

            return enforce_2_to_7_lines(
                "🤖 **Image Received**\n"
                "I see your photo! To enable multimodal visual answers, set a free Gemini or Groq API key.\n"
                "You can also tap below to compress this photo into ~1KB ultra-compact size."
            ), "photo-ack"

    is_ready = await is_openchamber_ready()
    if is_ready or OPENCODE_EXE.exists():
        await ensure_opencode_service()

        # Step 1: Try existing session if available
        if session_id and session_id != "cloud-fallback":
            try:
                reply = await _poll_for_answer(session_id, prompt, timeout_seconds=15)
                return enforce_2_to_7_lines(reply), session_id
            except Exception as e:
                logger.warning(f"Session {session_id} stalled ({e}). Auto-healing with fresh session...")

        # Step 2: Auto-heal with brand new session
        try:
            fresh_session_id = await create_fresh_session()
            reply = await _poll_for_answer(fresh_session_id, prompt, timeout_seconds=20)
            return enforce_2_to_7_lines(reply), fresh_session_id
        except Exception as e:
            logger.warning(f"Local OpenCode failed ({e}). Proceeding to cloud fallback...")

    # Cloud / Headless Fallback:
    from config import gemini_key_pool, groq_key_pool
    gem_key = gemini_key_pool.get_key()
    if gem_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gem_key}"
            contents = []
            if history:
                for h in history[-6:]:
                    role = "user" if h.get("role") == "user" else "model"
                    contents.append({"role": role, "parts": [{"text": h.get("content", "")}]})
            contents.append({"role": "user", "parts": [{"text": f"{AGENTIC_SYSTEM_PROMPT}{prompt}"}]})
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, json={
                    "contents": contents,
                    "generationConfig": {"maxOutputTokens": 300, "temperature": 0.3}
                })
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                        return enforce_2_to_7_lines(text), "gemini-cloud"
        except Exception as e:
            logger.warning(f"Gemini cloud fallback error: {e}")

    groq_key = groq_key_pool.get_key()
    if groq_key:
        try:
            url = "https://api.groq.com/openai/v1/chat/completions"
            messages = [{"role": "system", "content": AGENTIC_SYSTEM_PROMPT}]
            if history:
                for h in history[-6:]:
                    role = "user" if h.get("role") == "user" else "assistant"
                    messages.append({"role": role, "content": h.get("content", "")})
            messages.append({"role": "user", "content": prompt})
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {groq_key}"},
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "messages": messages,
                        "max_tokens": 250,
                        "temperature": 0.3
                    }
                )
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"].strip()
                    return enforce_2_to_7_lines(text), "groq-cloud"
        except Exception as e:
            logger.warning(f"Groq cloud fallback error: {e}")

    # Conversational Fallback: handles greetings, identity, clarifications, and feelings
    conv_reply = handle_conversational_fallback(prompt, history)
    if conv_reply:
        return enforce_2_to_7_lines(conv_reply), "conversational-intent"

    # Synthesized Context Extraction for explicit live web queries
    if "<live-web-data>" in prompt:
        import re
        snippets = re.findall(r"•\s*\[NEWS\]\s*([^:\n]+):\s*([^\n]+)", prompt)
        if not snippets:
            snippets = re.findall(r"•\s*([^:\n]+):\s*([^\n]+)", prompt)
        if not snippets:
            raw_bullets = re.findall(r"•\s*([^\n]+)", prompt)
            snippets = [(b, "") for b in raw_bullets]

        clean_lines = []
        for s in snippets[:4]:
            title = s[0].strip()
            body = s[1].strip() if len(s) > 1 else ""
            body = re.sub(r"^[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}\s*[·\-–]\s*", "", body)
            body = re.sub(r"\.{2,}$", "", body).strip()
            if body:
                clean_lines.append(f"• **{title}**: {body[:130]}.")
            else:
                clean_lines.append(f"• **{title}**")
        if clean_lines:
            return enforce_2_to_7_lines("\n".join(clean_lines)), "web-summary"

    clean_q = extract_clean_prompt(prompt)
    fallback_text = (
        f"I have received your request: *\"{clean_q[:60]}\"*.\n"
        "To enable full generative reasoning, add your free Google Gemini API key to Railway:\n"
        "`railway variables --set GEMINI_API_KEYS=\"your_key\"`\n"
        "You can also send any media link to download, or send an image to edit/compress!"
    )
    return enforce_2_to_7_lines(fallback_text), "key-required"
