import asyncio
import base64
from datetime import datetime
from io import BytesIO
import config
import logging
import time
from typing import List, Dict

import tiktoken
from openai import AsyncOpenAI, BadRequestError


class MultiKeyClientPool:
    """
    Transparent AsyncOpenAI drop-in wrapper supporting multiple API keys.
    - Round-robin load balancing across all active keys to distribute load.
    - Automatic failover to the next healthy key if any key encounters a 429 RateLimitError.
    - Per-key cooldown timers (30s) to give rate-limited keys time to recover.
    """
    def __init__(self, api_keys: List[str], base_url: str):
        self.api_keys = [k.strip() for k in api_keys if k and isinstance(k, str) and k.strip()]
        self.base_url = base_url
        if not self.api_keys:
            # Fallback to single config key if present
            if config.openai_api_key:
                self.api_keys = [config.openai_api_key.strip()]
            else:
                self.api_keys = ["placeholder_key"]

        self._clients = [AsyncOpenAI(api_key=k, base_url=base_url) for k in self.api_keys]
        self._index = 0
        self._lock = asyncio.Lock()
        self._cooldowns: Dict[int, float] = {}

        # Expose OpenAI-compatible .chat.completions interface
        self.chat = type("Chat", (), {"completions": self})()

    def _get_next_client(self):
        now = time.time()
        n = len(self._clients)
        # Find next healthy client starting from current index
        for i in range(n):
            idx = (self._index + i) % n
            if self._cooldowns.get(idx, 0) <= now:
                self._index = (idx + 1) % n
                return idx, self._clients[idx]
        # If all keys in cooldown, pick the one with earliest cooldown expiry
        min_idx = min(range(n), key=lambda i: self._cooldowns.get(i, 0))
        self._index = (min_idx + 1) % n
        return min_idx, self._clients[min_idx]

    async def create(self, *args, **kwargs):
        attempts = len(self._clients)
        last_error = None

        for attempt in range(attempts):
            async with self._lock:
                idx, client = self._get_next_client()

            try:
                return await client.chat.completions.create(*args, **kwargs)
            except Exception as e:
                err_str = str(e).lower()
                if "rate_limit" in err_str or "429" in err_str or "quota" in err_str:
                    logger.warning(
                        f"[MULTI-KEY POOL] Key {idx+1}/{len(self._clients)} rate-limited (attempt {attempt+1}/{attempts}): {e}. "
                        "Cooling down key for 30s and rotating to next available key..."
                    )
                    self._cooldowns[idx] = time.time() + 30.0
                    last_error = e
                    continue
                raise e

        if last_error:
            raise last_error

    @property
    def audio(self):
        # Delegate to currently active client
        now = time.time()
        n = len(self._clients)
        for i in range(n):
            idx = (self._index + i) % n
            if self._cooldowns.get(idx, 0) <= now:
                return self._clients[idx].audio
        return self._clients[0].audio

    @property
    def images(self):
        # Delegate to currently active client
        now = time.time()
        n = len(self._clients)
        for i in range(n):
            idx = (self._index + i) % n
            if self._cooldowns.get(idx, 0) <= now:
                return self._clients[idx].images
        return self._clients[0].images


# setup multi-key rotating openai client
openai_client = MultiKeyClientPool(
    api_keys=config.openai_api_keys,
    base_url=config.openai_api_base,
)

# optional OpenRouter client (OpenAI-compatible) for models declared with
# "provider: openrouter" in config/models.yml (e.g. Claude or other vendors)
openrouter_client = None
if config.openrouter_api_key:
    openrouter_client = AsyncOpenAI(
        api_key=config.openrouter_api_key,
        base_url=config.openrouter_api_base,
    )

logger = logging.getLogger(__name__)


def _get_client_for_model(model):
    provider = config.models["info"].get(model, {}).get("provider", "openai")
    if provider == "openrouter":
        if openrouter_client is None:
            raise ValueError(
                "OpenRouter API key is not configured. "
                "Set openrouter_api_key in config/config.yml"
            )
        return openrouter_client
    return openai_client


OPENAI_COMPLETION_OPTIONS = {
    "temperature": 0.7,
    "max_tokens": 700,
    "top_p": 1,
    "frequency_penalty": 0,
    "presence_penalty": 0,
    "timeout": 60.0,
}


class ChatGPT:
    def __init__(self, model=None):
        if model is None:
            model = config.models["available_text_models"][0]
        assert model in config.models["info"], f"Unknown model: {model}"
        self.model = model
        self._client = _get_client_for_model(model)
        self.last_generated_files = []
        self.last_file_titles = {}
        self.last_file_creators = {}
        self.last_video_metadata = None

    async def send_message(self, message, dialog_messages=[], chat_mode="assistant", video_metadata=None, recent_videos=None):
        if chat_mode not in config.chat_modes.keys():
            raise ValueError(f"Chat mode {chat_mode} is not supported")

        n_dialog_messages_before = len(dialog_messages)
        answer = None
        while answer is None:
            try:
                if config.models["info"][self.model]["type"] == "chat_completion":
                    messages = self._generate_prompt_messages(message, dialog_messages, chat_mode, video_metadata=video_metadata, recent_videos=recent_videos)

                    fallback_models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
                    cands = [self.model] + [m for m in fallback_models if m != self.model]
                    r = None
                    for c_model in cands:
                        try:
                            r = await self._client.chat.completions.create(
                                model=c_model,
                                messages=messages,
                                **OPENAI_COMPLETION_OPTIONS
                            )
                            self.model = c_model
                            break
                        except Exception as rate_e:
                            if "429" in str(rate_e) or "rate_limit" in str(rate_e).lower():
                                continue
                            raise rate_e
                    if r is None:
                        raise RuntimeError("All models in fallback pool exhausted.")
                    answer = r.choices[0].message.content
                else:
                    raise ValueError(f"Unknown model: {self.model}")

                answer = self._postprocess_answer(answer)
                n_input_tokens, n_output_tokens = r.usage.prompt_tokens, r.usage.completion_tokens
            except BadRequestError as e:  # too many tokens
                if len(dialog_messages) == 0:
                    raise ValueError("Dialog messages is reduced to zero, but still has too many tokens to make completion") from e

                # forget first message in dialog_messages
                dialog_messages = dialog_messages[1:]

        n_first_dialog_messages_removed = n_dialog_messages_before - len(dialog_messages)

        return answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

    async def send_message_stream(self, message, dialog_messages=[], chat_mode="assistant", video_metadata=None, recent_videos=None):
        if chat_mode not in config.chat_modes.keys():
            raise ValueError(f"Chat mode {chat_mode} is not supported")

        n_dialog_messages_before = len(dialog_messages)
        answer = None
        while answer is None:
            try:
                if config.models["info"][self.model]["type"] == "chat_completion":
                    messages = self._generate_prompt_messages(message, dialog_messages, chat_mode, video_metadata=video_metadata, recent_videos=recent_videos)

                    fallback_models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
                    cands = [self.model] + [m for m in fallback_models if m != self.model]
                    r_gen = None
                    for c_model in cands:
                        try:
                            r_gen = await self._client.chat.completions.create(
                                model=c_model,
                                messages=messages,
                                stream=True,
                                **OPENAI_COMPLETION_OPTIONS
                            )
                            self.model = c_model
                            break
                        except Exception as rate_e:
                            if "429" in str(rate_e) or "rate_limit" in str(rate_e).lower():
                                continue
                            raise rate_e
                    if r_gen is None:
                        raise RuntimeError("All models in fallback pool exhausted.")

                    answer = ""
                    async for r_item in r_gen:
                        if len(r_item.choices) == 0:
                            continue
                        delta = r_item.choices[0].delta

                        if delta.content:
                            answer += delta.content
                            yield "not_finished", answer, (0, 0), 0

                answer = self._postprocess_answer(answer)
                n_input_tokens, n_output_tokens = self._count_tokens_from_messages(messages, answer, model=self.model)

            except BadRequestError as e:  # too many tokens
                if len(dialog_messages) == 0:
                    raise e

                # forget first message in dialog_messages
                dialog_messages = dialog_messages[1:]

        n_first_dialog_messages_removed = n_dialog_messages_before - len(dialog_messages)
        yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed  # sending final answer

    async def send_agent_message(self, message, dialog_messages=[], chat_mode="assistant", video_metadata=None, recent_videos=None):
        if chat_mode not in config.chat_modes.keys():
            raise ValueError(f"Chat mode {chat_mode} is not supported")

        n_dialog_messages_before = len(dialog_messages)
        messages = self._generate_prompt_messages(message, dialog_messages, chat_mode, video_metadata=video_metadata, recent_videos=recent_videos)

        from agent import AgentOrchestrator
        orchestrator = AgentOrchestrator(client=self._client, model=self.model)

        async for step_type, text, meta in orchestrator.run(messages, enable_tools=True):
            if step_type == "status":
                yield "status", text, (0, 0), 0
            elif step_type == "finished":
                self.last_generated_files = list(meta.get("generated_files", []))
                self.last_file_titles = dict(meta.get("file_titles", {}))
                self.last_file_creators = dict(meta.get("file_creators", {}))
                self.last_video_metadata = meta.get("video_metadata")
                answer = self._postprocess_answer(text)
                n_input_tokens = meta.get("n_input_tokens", 0)
                n_output_tokens = meta.get("n_output_tokens", 0)
                if n_input_tokens == 0 or n_output_tokens == 0:
                    n_input_tokens, n_output_tokens = self._count_tokens_from_messages(messages, answer, model=self.model)
                n_first_dialog_messages_removed = n_dialog_messages_before - len(dialog_messages)
                yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

    async def send_vision_message(
        self,
        message,
        dialog_messages=[],
        chat_mode="assistant",
        image_buffer: BytesIO = None,
    ):
        n_dialog_messages_before = len(dialog_messages)
        answer = None
        while answer is None:
            try:
                if config.models["info"][self.model].get("vision", False):
                    messages = self._generate_prompt_messages(
                        message, dialog_messages, chat_mode, image_buffer
                    )
                    r = await self._client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        **OPENAI_COMPLETION_OPTIONS
                    )
                    answer = r.choices[0].message.content
                else:
                    raise ValueError(f"Unsupported model: {self.model}")

                answer = self._postprocess_answer(answer)
                n_input_tokens, n_output_tokens = (
                    r.usage.prompt_tokens,
                    r.usage.completion_tokens,
                )
            except BadRequestError as e:  # too many tokens
                if len(dialog_messages) == 0:
                    raise ValueError(
                        "Dialog messages is reduced to zero, but still has too many tokens to make completion"
                    ) from e

                # forget first message in dialog_messages
                dialog_messages = dialog_messages[1:]

        n_first_dialog_messages_removed = n_dialog_messages_before - len(
            dialog_messages
        )

        return (
            answer,
            (n_input_tokens, n_output_tokens),
            n_first_dialog_messages_removed,
        )

    async def send_vision_message_stream(
        self,
        message,
        dialog_messages=[],
        chat_mode="assistant",
        image_buffer: BytesIO = None,
    ):
        n_dialog_messages_before = len(dialog_messages)
        answer = None
        while answer is None:
            try:
                if config.models["info"][self.model].get("vision", False):
                    messages = self._generate_prompt_messages(
                        message, dialog_messages, chat_mode, image_buffer
                    )

                    r_gen = await self._client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        stream=True,
                        **OPENAI_COMPLETION_OPTIONS,
                    )

                    answer = ""
                    async for r_item in r_gen:
                        if len(r_item.choices) == 0:
                            continue
                        delta = r_item.choices[0].delta
                        if delta.content:
                            answer += delta.content
                            (
                                n_input_tokens,
                                n_output_tokens,
                            ) = self._count_tokens_from_messages(
                                messages, answer, model=self.model
                            )
                            n_first_dialog_messages_removed = (
                                n_dialog_messages_before - len(dialog_messages)
                            )
                            yield "not_finished", answer, (
                                n_input_tokens,
                                n_output_tokens,
                            ), n_first_dialog_messages_removed

                answer = self._postprocess_answer(answer)

            except BadRequestError as e:  # too many tokens
                if len(dialog_messages) == 0:
                    raise e
                # forget first message in dialog_messages
                dialog_messages = dialog_messages[1:]

        yield "finished", answer, (
            n_input_tokens,
            n_output_tokens,
        ), n_first_dialog_messages_removed

    def _encode_image(self, image_buffer: BytesIO) -> bytes:
        return base64.b64encode(image_buffer.read()).decode("utf-8")

    def _generate_prompt_messages(self, message, dialog_messages, chat_mode, image_buffer: BytesIO = None, video_metadata: dict = None, recent_videos: list = None):
        base_prompt = config.chat_modes[chat_mode]["prompt_start"]
        now = datetime.now()
        now_str = now.strftime("%A, %B %d, %Y, %I:%M:%S %p")

        video_context_block = ""
        if video_metadata and isinstance(video_metadata, dict):
            v_title = video_metadata.get("video_title") or "Video"
            v_creator = video_metadata.get("creator_name") or "Unknown Creator"
            v_url = video_metadata.get("source_url") or ""
            v_desc = video_metadata.get("description") or "None"
            v_duration = f"{video_metadata.get('duration')}s" if video_metadata.get("duration") else "Unknown"

            video_context_block = (
                f"\n\n[ACTIVE VIDEO CONTEXT (GROUND TRUTH)]:\n"
                f"The user is discussing or has recently fetched the following video in this conversation:\n"
                f"- Video Title: \"{v_title}\"\n"
                f"- Creator / Channel / Uploader: \"{v_creator}\"\n"
                f"- Source URL: {v_url}\n"
                f"- Duration: {v_duration}\n"
                f"- Video Description / Caption: \"{v_desc}\"\n"
                f"- Visual Frames / Speech Audio: NONE PROCESSED. (Only metadata was extracted).\n\n"
                f"[STRICT GROUNDING & ANTI-HALLUCINATION RULES FOR VIDEOS]:\n"
                f"1. TITLE VS CREATOR SEPARATION:\n"
                f"   - The video TITLE is \"{v_title}\".\n"
                f"   - The CREATOR is \"{v_creator}\".\n"
                f"   - NEVER swap, confuse, or substitute the title with the creator's name. They are distinct.\n"
                f"2. DIRECT METADATA QUESTIONS:\n"
                f"   - If the user asks 'What is the title?' or 'What is this video called?' -> Answer directly: \"{v_title}\".\n"
                f"   - If the user asks 'Who created this video?' or 'Who is the author/channel?' -> Answer directly: \"{v_creator}\".\n"
                f"3. VIDEO CONTENT QUESTIONS ('What is this video about?', 'Summarize this video'):\n"
                f"   - State clearly: It appears to be a video titled \"{v_title}\" by {v_creator}.\n"
                f"   - State that you have the metadata but not the video frames or speech transcript, so you cannot describe the exact scene actions without a transcript or video analysis.\n"
                f"   - ABSOLUTELY NEVER fabricate or hallucinate visual scenes, actions, characters, or animals (e.g. dogs in snow, actions never mentioned). Stick strictly to verified metadata.\n"
            )
            if recent_videos and len(recent_videos) > 1:
                video_context_block += "4. PREVIOUS VIDEOS IN THIS CONVERSATION:\n"
                for idx, pv in enumerate(recent_videos[1:4], start=2):
                    p_title = pv.get("video_title", "Video")
                    p_creator = pv.get("creator_name", "Unknown")
                    p_url = pv.get("source_url", "")
                    video_context_block += f"   - Video #{idx}: Title: \"{p_title}\" | Creator: \"{p_creator}\" | URL: {p_url}\n"
                video_context_block += "   - If the user refers to 'the previous video' or compares videos, refer to the correct video above.\n"

        from saux_config import SAUX_KNOWLEDGE_BASE

        system_instruction = (
            f"You are SAUX Super Intelligence, an advanced autonomous AI assistant equipped with real-time web search and multimedia engineering capabilities.\n\n"
            f"[SAUX OFFICIAL IDENTITY & ECOSYSTEM KNOWLEDGE]:\n"
            f"{SAUX_KNOWLEDGE_BASE}\n\n"
            f"[REAL-TIME SYSTEM CLOCK]:\n"
            f"- Current Real-World Date and Time: {now_str}\n"
            f"- When asked about the current time, date, today's day, month, or year, always answer directly and accurately using this exact real-time clock above. NEVER claim that you lack real-time access or system clock.\n\n"
            f"[CONSUMER PRIVACY & MEDIA DELIVERY RULES]:\n"
            f"- You are a consumer-facing Telegram AI assistant. Downloaded and processed media files are automatically delivered directly into this Telegram chat for the user.\n"
            f"- NEVER mention or output internal server paths, file paths (such as C:\\Users\\...), or directory names. The user cannot access your server storage.\n"
            f"- NEVER tell the user that files are downloaded to 'their computer' or 'their Downloads folder'.\n"
            f"- When confirming downloaded media, always refer to it naturally by its real Video Title (e.g. 'Here is your video: **Spider-Man vs Electro**') and format/size.\n"
            f"- Never output hash filenames like '0ac0ea0b60.mp4'.\n\n"
            f"[MANDATORY AGENT TOOL POLICIES]:\n"
            f"1. LIVE WEB & NEWS SEARCH (`web_search`):\n"
            f"   - Your internal pretraining weights are from the past. You MUST ALWAYS call `web_search` whenever the user asks about:\n"
            f"     * Current political leaders or office holders (e.g. current Chief Minister / CM, current Prime Minister / PM, President, Governor, ministers)\n"
            f"     * Latest news, current events, live sports scores, stock prices, weather, or 'what is happening today'\n"
            f"     * Any fact that could have changed recently. NEVER guess current roles from memory—ALWAYS verify live via `web_search`.\n"
            f"2. VIDEO & MEDIA DOWNLOAD (`download_video`):\n"
            f"   - You HAVE direct backend video fetching and downloading capabilities.\n"
            f"   - When a user provides or pastes ANY video or media link (Instagram Reel/Post, YouTube/Shorts, TikTok, Twitter/X, Facebook)—EVEN IF ONLY THE URL IS SENT—it must be downloaded.\n"
            f"   - ABSOLUTELY NEVER say 'I cannot download or access content from Instagram directly' or cite terms of service or technical restrictions. Your backend is fully authorized and equipped to download them.\n"
            f"   - NEVER claim that you cannot download videos or lack storage. The backend handles the download and sends the video file directly to the user.\n"
            f"3. VIDEO EDITING & AUDIO EXTRACTION (`clip_video`, `extract_audio`):\n"
            f"   - For trimming/clipping videos between timestamps, call `clip_video`.\n"
            f"   - For extracting MP3/audio tracks from video links or files, call `extract_audio`.\n"
            f"4. UNIVERSAL FILE CONVERSION (`convert_file`):\n"
            f"   - For converting images (PNG/JPG/WEBP), audio (MP3/WAV/AAC), video (MP4/MKV/WEBM), documents, or archives, call `convert_file`.\n"
            f"5. PRECISION CALCULATOR (`calculator`):\n"
            f"   - For mathematical calculations or formulas, call `calculator`.\n"
            f"{video_context_block}\n\n"
            f"{base_prompt}"
        )

        messages = [{"role": "system", "content": system_instruction}]

        # Sliding-window context guard: Keep the most recent 12 turns (24 messages) to prevent context bloat
        recent_dialog = dialog_messages[-12:] if len(dialog_messages) > 12 else dialog_messages
        for dialog_message in recent_dialog:
            messages.append({"role": "user", "content": dialog_message["user"]})
            messages.append({"role": "assistant", "content": dialog_message["bot"]})

        if image_buffer is not None:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": message,
                        },
                        {
                            "type": "image_url",
                            "image_url" : {

                                "url": f"data:image/jpeg;base64,{self._encode_image(image_buffer)}",
                                "detail":"high"
                            }
                        }
                    ]
                }

            )
        else:
            messages.append({"role": "user", "content": message})

        return messages

    def _postprocess_answer(self, answer):
        answer = answer.strip()
        return answer

    def _count_tokens_from_messages(self, messages, answer, model=None):
        if model is None:
            model = config.models["available_text_models"][0]
        try:
            try:
                encoding = tiktoken.encoding_for_model(model)
            except Exception:
                encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            encoding = None

        def _encode_len(text):
            if encoding is not None:
                try:
                    return len(encoding.encode(text))
                except Exception:
                    pass
            return max(1, len(str(text)) // 4)

        tokens_per_message = 3
        tokens_per_name = 1

        # input
        n_input_tokens = 0
        for message in messages:
            n_input_tokens += tokens_per_message
            if isinstance(message["content"], list):
                for sub_message in message["content"]:
                    if "type" in sub_message:
                        if sub_message["type"] == "text":
                            n_input_tokens += _encode_len(sub_message["text"])
                        elif sub_message["type"] == "image_url":
                            pass
            else:
                if "type" in message:
                    if message["type"] == "text":
                        n_input_tokens += _encode_len(message["text"])
                    elif message["type"] == "image_url":
                        pass
                elif isinstance(message.get("content"), str):
                    n_input_tokens += _encode_len(message["content"])

        n_input_tokens += 2

        # output
        n_output_tokens = 1 + _encode_len(answer)

        return n_input_tokens, n_output_tokens


async def transcribe_audio(audio_file) -> str:
    r = await openai_client.audio.transcriptions.create(model="whisper-large-v3-turbo", file=audio_file)
    return r.text or ""


async def generate_images(prompt, n_images=1, size="1024x1024"):
    # If base_url is not Groq, try standard OpenAI image generation
    is_groq = "groq.com" in str(getattr(config, "openai_api_base", "")).lower()
    if not is_groq:
        try:
            r = await openai_client.images.generate(
                model="gpt-image-1", prompt=prompt, n=n_images, size=size
            )
            images = [base64.b64decode(item.b64_json) for item in r.data]
            if images:
                return images
        except Exception as e:
            logger.warning(f"OpenAI image generation failed or unsupported ({e}), switching to AI image engine fallback.")

    # High-performance, resilient AI image generator (Pollinations AI)
    import urllib.parse
    import httpx

    width, height = 1024, 1024
    if size and "x" in size:
        try:
            parts = size.split("x")
            width, height = int(parts[0]), int(parts[1])
        except Exception:
            pass

    images = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async with httpx.AsyncClient(timeout=45.0, headers=headers) as client:
        for i in range(n_images):
            encoded_prompt = urllib.parse.quote(prompt.strip())
            seed = int(time.time() * 1000) + i
            url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&seed={seed}&nologo=true"
            try:
                resp = await client.get(url, follow_redirects=True)
                if resp.status_code == 200 and resp.content:
                    images.append(resp.content)
                else:
                    logger.warning(f"Image engine returned HTTP {resp.status_code}")
            except Exception as e:
                logger.error(f"Image request error: {e}")

    if not images:
        raise RuntimeError("Failed to generate image from AI provider.")
    return images
