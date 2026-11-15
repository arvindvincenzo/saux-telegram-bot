import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
import httpx

logger = logging.getLogger("openchamber_tool")

OPENCODE_PORT = 57123
OPENCODE_AUTH = ("opencode", "sauxagent2026")
PRIMARY_MODEL = "inkling-2-small-free"
_opencode_backoff_until = 0.0

def find_opencode_bin() -> Path | None:
    """Locates OpenCode CLI binary on Linux (Railway) or Windows (local)."""
    custom_bin = os.environ.get("OPENCODE_BIN")
    if custom_bin and Path(custom_bin).exists():
        return Path(custom_bin)
    which_bin = shutil.which("opencode")
    if which_bin:
        return Path(which_bin)
    for lp in [
        Path("/usr/local/bin/opencode"),
        Path("/root/.opencode/bin/opencode"),
        Path.home() / ".opencode" / "bin" / "opencode"
    ]:
        if lp.exists():
            return lp
    win_path = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "@openchamberelectron" / "resources" / "opencode-cli" / "opencode.exe"
    if win_path.exists():
        return win_path
    return None

OPENCODE_EXE = find_opencode_bin()

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
    
    bin_path = find_opencode_bin()
    if bin_path and bin_path.exists():
        logger.info(f"Starting opencode server on port {OPENCODE_PORT} using {bin_path}...")
        env = os.environ.copy()
        env["OPENCODE_SERVER_PASSWORD"] = "sauxagent2026"
        env["OPENCODE_SERVER_USERNAME"] = "opencode"
        try:
            cmd = [str(bin_path), "serve", "--hostname", "127.0.0.1", "--port", str(OPENCODE_PORT)]
            subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            for _ in range(12):
                await asyncio.sleep(0.5)
                if await is_openchamber_ready():
                    logger.info("OpenCode server is up and responsive!")
                    return True
        except Exception as e:
            logger.error(f"Failed to auto-spawn OpenCode: {e}")
    else:
        logger.warning("opencode binary not found on this system.")
    return False

async def is_openchamber_ready() -> bool:
    """Checks if OpenCode server is healthy and responding on port 57123."""
    try:
        async with httpx.AsyncClient(timeout=0.4) as client:
            resp = await client.get(f"http://127.0.0.1:{OPENCODE_PORT}/api/session", auth=OPENCODE_AUTH)
            return resp.status_code == 200
    except Exception:
        return False

async def create_fresh_session() -> str:
    """Creates a brand new OpenCode session pinned to inkling-2-small-free."""
    await ensure_opencode_service()
    async with httpx.AsyncClient(timeout=2.0) as client:
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

async def _poll_for_answer(session_id: str, prompt: str, timeout_seconds: float = 2.5) -> str:
    """Submits prompt and polls for completed assistant response from OpenCode."""
    full_prompt = f"{AGENTIC_SYSTEM_PROMPT}{prompt.strip()}"
    async with httpx.AsyncClient(timeout=4.0) as client:
        # 1. Fetch current message IDs
        try:
            r = await client.get(f"http://127.0.0.1:{OPENCODE_PORT}/api/session/{session_id}/message", auth=OPENCODE_AUTH)
            data_val = r.json().get("data", []) if r.status_code == 200 else []
            if isinstance(data_val, list):
                prev_ids = {m.get("id") for m in data_val if isinstance(m, dict) and m.get("id")}
            else:
                prev_ids = set()
        except Exception:
            prev_ids = set()

        # 2. Submit prompt (supports OpenCode v1.18+ schema {"prompt": {"text": ...}} and legacy {"text": ...})
        prompt_payload = {"prompt": {"text": full_prompt}}
        p_resp = await client.post(
            f"http://127.0.0.1:{OPENCODE_PORT}/api/session/{session_id}/prompt",
            auth=OPENCODE_AUTH,
            json=prompt_payload,
            headers={"Content-Type": "application/json"}
        )
        if p_resp.status_code == 400:
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
        last_extracted_text = ""
        stable_count = 0

        while asyncio.get_event_loop().time() - start_time < timeout_seconds:
            await asyncio.sleep(0.5)
            try:
                msg_resp = await client.get(
                    f"http://127.0.0.1:{OPENCODE_PORT}/api/session/{session_id}/message",
                    auth=OPENCODE_AUTH
                )
            except Exception:
                continue

            if msg_resp.status_code != 200:
                continue

            resp_json = msg_resp.json()
            all_msgs = resp_json.get("data", []) if isinstance(resp_json, dict) else resp_json
            if not isinstance(all_msgs, list):
                continue

            new_msgs = [m for m in all_msgs if isinstance(m, dict) and m.get("id") not in prev_ids]

            for m in reversed(new_msgs):
                # Check if this message is from assistant/model
                role = (m.get("role") or m.get("type") or m.get("sender") or "").lower()
                if isinstance(m.get("data"), dict):
                    role = role or m["data"].get("role", "").lower()

                if role in ("assistant", "model", "bot") or ("prompt" not in m and "user" not in role):
                    texts = []
                    # Check 'parts' array (OpenCode standard)
                    for p in (m.get("parts") or []):
                        if isinstance(p, dict) and p.get("text"):
                            texts.append(p["text"])
                        elif isinstance(p, str):
                            texts.append(p)

                    # Check 'content'
                    content = m.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("text"):
                                texts.append(c["text"])
                            elif isinstance(c, str):
                                texts.append(c)
                    elif isinstance(content, str) and content.strip():
                        texts.append(content)

                    # Check direct 'text' field
                    if m.get("text") and isinstance(m["text"], str):
                        texts.append(m["text"])

                    current_text = "\n".join(texts).strip()
                    if current_text:
                        # Check stop condition
                        finish = (m.get("finish") or m.get("finishReason") or m.get("status") or "").lower()
                        is_finished = finish in ("stop", "end_turn", "completed", "done", "success") or m.get("completed") is True
                        
                        if is_finished:
                            return enforce_2_to_7_lines(current_text)
                        
                        # If text has stayed stable across 2 polling cycles (1.0s) and is substantial
                        if current_text == last_extracted_text and len(current_text) > 20:
                            stable_count += 1
                            if stable_count >= 2:
                                return enforce_2_to_7_lines(current_text)
                        else:
                            last_extracted_text = current_text
                            stable_count = 0

        # If loop timed out but we captured real text from assistant, return it directly!
        if last_extracted_text and len(last_extracted_text) > 10:
            return enforce_2_to_7_lines(last_extracted_text)

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
    """Provides natural, empathetic 2-7 line responses for greetings, slang, clarifications, mood, and identity."""
    import re
    clean_p = extract_clean_prompt(prompt).lower().strip()

    # 1. Hearing, Presence & Audio checks (Highest priority so "yoo can you hear me" is answered correctly)
    if re.search(r"\b(can\s+(you|u)\s+hear(\s+me)?|hear\s+me|are\s+(you|u)\s+(there|listening)|can\s+(you|u)\s+see\s+me|you\s+there|u\s+there|listen(ing)?\s+to\s+me|you\s+listening)\b", clean_p) or "can you hear me" in clean_p or "can u hear me" in clean_p or "hear me" in clean_p:
        return (
            "Yes! I hear you loud and clear. 🔊\n"
            "I'm fully active, online, and listening.\n"
            "What would you like to work on right now?"
        )

    # 2. Single-word confusion / What / Huh
    if clean_p in ["what", "what?", "huh", "huh?", "pardon", "pardon?", "say again", "repeat", "eh?", "eh"]:
        return (
            "I'm right here! 👍\n"
            "You can ask me any question, research a topic, or paste a video link to download."
        )

    # 3. Clarifications / Follow-up reactions (e.g. 'why u saying about that?', 'why did you say that')
    if re.search(r"\b(why (are you|did you|u|you) say|what do you mean|why saying that|why u saying|who asked that)\b", clean_p):
        return (
            "I apologize for the confusion earlier!\n"
            "My automated search trigger misread the context and pulled web results instead of speaking directly to you.\n"
            "I've adjusted our conversation to be much clearer and directly focused on what you need.\n"
            "What can I help you with right now?"
        )

    # 4. Emotions, Mood, Feelings, Feeling down/sad
    if re.search(r"\b(my mood|mood is bad|mood so bad|feel bad|feeling bad|i am sad|i'm sad|feeling low|i feel down|depressed|cheer me up|what to do when sad|bad mood)\b", clean_p):
        return (
            "I'm really sorry you're feeling down today. 💙\n"
            "When your mood drops, try taking a slow deep breath and pausing whatever you're working on for a moment.\n"
            "A short 5-minute walk, drinking a glass of cold water, or putting on your favorite comfort music can help reset your thoughts.\n"
            "Be gentle with yourself—you don't have to carry everything all at once.\n"
            "I'm right here if you want to chat, vent, or distract yourself with something interesting!"
        )

    # 5. Bot identity / capability questions
    capability_triggers = [
        "who are you", "who r u", "who r you", "who are u", "who is this", "what r u", "what r you",
        "what bot", "what are you", "what can you do", "introduce yourself",
        "tell me about yourself", "your name", "features", "capabilities", "what do you do",
        "what do u do", "what can u do", "how to use", "how do you work", "things you can do",
        "what you can do", "what u can do"
    ]
    if any(q in clean_p for q in capability_triggers) or re.search(r"\b(who|what)\s+(are|r|is)\s+(u|you|this|bot)\b", clean_p) or re.search(r"\bwhat\b.*\b(things\b.*\bdo|can (you|u) do|do (you|u) do|features|capabilities)\b", clean_p):
        return (
            "🤖 **I am SAUX Super Intelligence — Autonomous AI Agent.**\n\n"
            "• 🧠 **Autonomous Q&A & Research:** In-depth answers, explanations, and creative assistance\n"
            "• ⚡ **Instant Media Downloader:** 1080p downloads for YouTube, Instagram Reels, TikTok, and Shorts\n"
            "• 🖼 **Image Engineering & Vision:** Smart image compression, resizing, and visual understanding\n"
            "• 🔍 **Live Web Intelligence:** Type `search <topic>` for real-time web facts\n\n"
            "Just send me a message, image, or media link anytime to get started!"
        )

    # 6. Greetings & Slang
    greeting_pattern = r"^(hi+|hey+(\s+there)*|hello+|hola+|sup|yo+|wassup|wazzup|what'?s\s*up|wsp|yew+|ye+|howdy|greetings|good\s*(morning|afternoon|evening|day|night))\b"
    if re.search(greeting_pattern, clean_p) or clean_p in ["wassup", "wazzup", "whatsup", "whats up", "what's up", "wsp", "yoo", "yooo", "yeww", "yew", "yo"]:
        return (
            "👋 **Hey there! Welcome to SAUX Super Intelligence.**\n"
            "I'm your autonomous assistant for deep research, live web facts, media downloads, and image engineering.\n"
            "What would you like to work on today?"
        )

    # 7. Status / How are you / What happened
    status_triggers = [
        "how are you", "how are u", "how do you do", "how r u", "todsy good time", "today good time",
        "what happen to you", "what happened to you", "what happen", "what happened",
        "whats wrong", "what's wrong", "what is wrong", "are you ok", "are you okay", "you ok"
    ]
    if any(q in clean_p for q in status_triggers) or re.search(r"\b(what('?s|\s+is|\s+has)?\s*(happen(ed)?|wrong|up)\s*(to|with)?\s*(u|you)|are\s+you\s+ok(ay)?|you\s+alive)\b", clean_p):
        return (
            "⚡ **All systems operating at peak performance!**\n"
            "I'm fully online, responsive, and ready for your requests.\n"
            "Whether you need in-depth research, live web facts, media downloads, or image tools, what would you like to work on?"
        )

    # 8. Thanks / gratitude
    if any(q in clean_p for q in ["thanks", "thank you", "thx", "appreciate", "thank u"]):
        return (
            "You're very welcome! Always glad to help.\n"
            "Let me know whenever you're ready for the next task or question. 🚀"
        )

    # 9. Conversational small talk acknowledgments & Casual Reactions
    if re.match(r"^(ok|okay|cool|nice|awesome|great|got it|understood|lol+|haha+|lmao|sure|yep|yeah|alright|roger|bruh|bro|dude|yeww?)$", clean_p):
        return (
            "Got it! 👍\n"
            "Whenever you're ready, let me know if you want to research a topic, download media, or chat."
        )

    return None

async def query_zero_cost_llm(prompt: str, history: list[dict] | None = None) -> str | None:
    """Queries zero-cost public LLM backends with ultra-fast timeout (<=2s)."""
    clean_p = extract_clean_prompt(prompt)
    messages = [
        {
            "role": "system",
            "content": (
                "You are SAUX Super Intelligence — an autonomous, highly capable AI Assistant. "
                "Tone: direct, empathetic, engaging, and exceptionally clear. "
                "Strict length: strictly between 2 and 6 lines. Never output just one sentence, and never exceed 6 lines. "
                "Never say you are an AI model or mention third-party platforms."
            )
        }
    ]
    if history:
        for h in history[-3:]:
            r = "user" if h.get("role") == "user" else "assistant"
            c = h.get("content", "").strip()
            if c:
                messages.append({"role": r, "content": c[:200]})
    messages.append({"role": "user", "content": clean_p})

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.post(
                "https://text.pollinations.ai/",
                headers={
                    "User-Agent": "SAUX-Super-Intelligence/2.0",
                    "Content-Type": "application/json"
                },
                json={"messages": messages, "model": "openai-fast"}
            )
            if resp.status_code == 200 and resp.text.strip():
                txt = resp.text.strip()
                if not txt.startswith("Free limit reached") and len(txt) > 10:
                    return enforce_2_to_7_lines(txt)
    except Exception as e:
        logger.debug(f"Zero-cost model attempt error: {e}")

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

    # Instant Live Web Context Synthesis (if live web facts are already in prompt, deliver in 0.001s)
    if "<live-web-data>" in prompt:
        import re
        snippets = re.findall(r"•\s*\[NEWS\]\s*([^:\n]+):\s*([^\n]+)", prompt)
        if not snippets:
            snippets = re.findall(r"•\s*([^:\n]+):\s*([^\n]+)", prompt)
        if not snippets:
            raw_bullets = re.findall(r"•\s*([^\n]+)", prompt)
            snippets = [(b, "") for b in raw_bullets]

        clean_lines = []
        for s in snippets[:3]:
            title = s[0].strip()
            body = s[1].strip() if len(s) > 1 else ""
            body = re.sub(r"^[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}\s*[·\-–]\s*", "", body)
            body = re.sub(r"\.{2,}$", "", body).strip()
            if body:
                clean_lines.append(f"• **{title}**: {body[:140]}.")
            else:
                clean_lines.append(f"• **{title}**")
        if clean_lines:
            return enforce_2_to_7_lines("\n".join(clean_lines)), "web-summary"

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

    global _opencode_backoff_until
    now = asyncio.get_event_loop().time()
    if now >= _opencode_backoff_until and await is_openchamber_ready():
        target_sid = session_id if (session_id and session_id != "cloud-fallback") else None
        try:
            if not target_sid:
                target_sid = await create_fresh_session()
            reply = await _poll_for_answer(target_sid, prompt, timeout_seconds=2.0)
            return enforce_2_to_7_lines(reply), target_sid
        except Exception as e:
            logger.warning(f"Local OpenCode failed ({e}). Backing off for 300s...")
            _opencode_backoff_until = now + 300.0

    # 1. High-Throughput Distributed Groq Engine (Dynamic 8B/70B + Multi-Key Auto-Failover)
    from config import gemini_key_pool, groq_key_pool
    if groq_key_pool.total_keys > 0:
        is_complex = len(prompt.split()) > 60 or any(k in prompt.lower() for k in ["code", "script", "algorithm", "deep analysis", "write an essay", "compare and contrast"])
        chosen_model = "llama-3.3-70b-versatile" if is_complex else "llama-3.1-8b-instant"

        messages = [{"role": "system", "content": AGENTIC_SYSTEM_PROMPT}]
        if history:
            for h in history[-6:]:
                role = "user" if h.get("role") == "user" else "assistant"
                messages.append({"role": role, "content": h.get("content", "")})
        messages.append({"role": "user", "content": prompt})

        max_attempts = min(3, groq_key_pool.total_keys)
        for _ in range(max_attempts):
            groq_key = groq_key_pool.get_key()
            if not groq_key:
                break
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {groq_key}"},
                        json={
                            "model": chosen_model,
                            "messages": messages,
                            "max_tokens": 300,
                            "temperature": 0.3
                        }
                    )
                    if resp.status_code == 200:
                        text = resp.json()["choices"][0]["message"]["content"].strip()
                        return enforce_2_to_7_lines(text), f"groq-{chosen_model}"
                    elif resp.status_code == 429:
                        logger.warning("Groq key burst rate-limited (429). Hopping to next key in pool...")
                        continue
            except Exception as e:
                logger.warning(f"Groq attempt error: {e}")
                continue

    # 2. Distributed Google Gemini Engine (with Multi-Key Failover)
    if gemini_key_pool.total_keys > 0:
        max_gemini_attempts = min(3, gemini_key_pool.total_keys)
        for _ in range(max_gemini_attempts):
            gem_key = gemini_key_pool.get_key()
            if not gem_key:
                break
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gem_key}"
                contents = []
                if history:
                    for h in history[-6:]:
                        role = "user" if h.get("role") == "user" else "model"
                        contents.append({"role": role, "parts": [{"text": h.get("content", "")}]})
                contents.append({"role": "user", "parts": [{"text": f"{AGENTIC_SYSTEM_PROMPT}{prompt}"}]})
                async with httpx.AsyncClient(timeout=3.0) as client:
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
                    elif resp.status_code == 429:
                        logger.warning("Gemini key burst rate-limited (429). Hopping to next key in pool...")
                        continue
            except Exception as e:
                logger.warning(f"Gemini cloud fallback error: {e}")
                continue

    # Conversational Fallback: handles greetings, identity, clarifications, and feelings
    conv_reply = handle_conversational_fallback(prompt, history)
    if conv_reply:
        return enforce_2_to_7_lines(conv_reply), "conversational-intent"

    # Zero-Cost LLM Fallback (Provides real agentic answers without keys)
    try:
        zero_cost_reply = await asyncio.wait_for(query_zero_cost_llm(prompt, history), timeout=1.0)
        if zero_cost_reply:
            return enforce_2_to_7_lines(zero_cost_reply), "zero-cost-ai"
    except Exception as e:
        logger.debug(f"Zero-cost fallback error: {e}")

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

    # Autonomous Knowledge Fallback: fetches real factual information only for explicit search/factual queries
    clean_q = extract_clean_prompt(prompt)
    conv_check = handle_conversational_fallback(clean_q)
    if conv_check:
        return enforce_2_to_7_lines(conv_check), "conversational-intent"

    # Conversational presence, listening, or emotional check
    personal_check = clean_q.lower().strip()
    if any(p in personal_check for p in ["you hear", "hear me", "you there", "talk to me", "are you", "can you", "do you know me", "who am i", "my name", "love you", "miss you", "listen"]):
        return enforce_2_to_7_lines(
            "Yes! I'm right here with you and listening attentively. 🤖\n"
            "I'm fully active and ready to help. What would you like to explore or work on next?"
        ), "conversational-intent"

    # Only fetch live web facts if query is an explicit search or fact lookup
    search_keywords = ["search", "find", "who is", "what is", "where is", "when did", "latest", "news", "price", "vs", "versus", "weather", "today", "history of", "score", "wikipedia"]
    is_factual_query = any(k in personal_check for k in search_keywords)

    if is_factual_query:
        try:
            from tools.web_tool import get_live_web_context
            web_facts = get_live_web_context(clean_q, max_results=2)
            if web_facts:
                raw_lines = [l.strip() for l in web_facts.splitlines() if l.strip() and not l.startswith("<")]
                if raw_lines:
                    clean_lines = []
                    for l in raw_lines[:2]:
                        clean_lines.append(l.replace("• [NEWS] ", "• "))
                    formatted_knowledge = "Here is what I found on that:\n" + "\n".join(clean_lines)
                    return enforce_2_to_7_lines(formatted_knowledge), "web-summary"
        except Exception as e:
            logger.debug(f"Knowledge fallback error: {e}")

    # General conversational AI response
    fallback_text = (
        f"I'm right here and ready to help! 🚀\n"
        f"You can ask me anything, paste a media link to download, or type `search {clean_q[:30]}` for real-time web facts."
    )
    return enforce_2_to_7_lines(fallback_text), "conversational-help"
