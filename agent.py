import os
from dataclasses import dataclass, field
from pathlib import Path
from pydantic_ai import Agent, RunContext, models
from pydantic import Field
from openai import AsyncOpenAI
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.groq import GroqProvider

from config import (
    AI_PROVIDER,
    GEMINI_MODEL,
    GROQ_MODEL,
    OLLAMA_MODEL,
    TEMP_DIR,
    gemini_key_pool,
    groq_key_pool
)
from tools.image_tool import process_image
from tools.video_tool import download_media
from tools.web_tool import search_web
import db

OPENCODE_INFERENCE_URL = "https://opencode.ai/inference/openai/v1"
OPENCODE_DEFAULT_MODEL = "openai:big-pickle"

@dataclass
class BotContext:
    user_id: int
    chat_id: int
    has_photo: bool = False
    photo_path: str | None = None
    detected_urls: list[str] = field(default_factory=list)
    action_result: dict | None = None

def get_model_for_user(user_id: int) -> models.Model | str:
    """
    Selects the OpenCode model with user's access token and organization header.
    """
    token, org_id = db.get_opencode_credentials(user_id)
    if token:
        headers = {}
        if org_id:
            headers["x-opencode-org-id"] = org_id

        oai_client = AsyncOpenAI(
            base_url=OPENCODE_INFERENCE_URL,
            api_key=token,
            default_headers=headers
        )
        provider = OpenAIProvider(openai_client=oai_client)
        return models.infer_model(
            OPENCODE_DEFAULT_MODEL,
            provider_factory=lambda name: provider
        )

    # Server fallback if configured
    if AI_PROVIDER == "gemini":
        key = gemini_key_pool.get_key()
        if key:
            return models.infer_model(
                GEMINI_MODEL,
                provider_factory=lambda name: GoogleProvider(api_key=key)
            )

    return "need_auth"

# Base Agent definition
agent = Agent(
    model="test",
    deps_type=BotContext,
    system_prompt="""
You are an intelligent, autonomous Telegram Agent.
Your role is to understand user requests (including vague instructions) and make high-accuracy decisions:

1. IMAGE EDITING & TRANSFORMATION:
- When a user provides an image with a request like "compress", "make it smaller", "reduce file size", choose the `process_image_tool` with action='compress'.
- If they ask to "stretch", "make it wider", "distort", choose action='stretch' and set scale_x / scale_y (e.g. 1.5, 2.0).
- If they ask to resize dimensions, choose action='resize'.
- If they ask to convert (e.g. to PNG, JPG, WebP), choose action='convert' and specify target_format.
- Pick sensible defaults if specific numbers are not given (e.g., default compression target: 500 KB).

2. VIDEO / AUDIO DOWNLOADING:
- When a user sends a video link (YouTube, TikTok, Instagram, Twitter/X, etc.), determine if they want the full video or just audio/mp3.
- Call `download_media_tool` with the URL and audio_only setting.

3. WEB SEARCH & RESEARCH:
- If a user asks for current news, facts, documentation, or links, call `web_search_tool`.

4. GENERAL CONVERSATION & CODING:
- Answer normally and concisely in Markdown if no special tool is required.
"""
)

@agent.tool
async def process_image_tool(
    ctx: RunContext[BotContext],
    action: str = Field(description="Action to perform: 'compress', 'stretch', 'resize', or 'convert'"),
    scale_x: float = Field(default=1.0, description="Horizontal scaling multiplier (e.g., 2.0 to stretch width)"),
    scale_y: float = Field(default=1.0, description="Vertical scaling multiplier"),
    target_kb: int = Field(default=500, description="Target file size in KB when compressing"),
    quality: int = Field(default=80, description="Quality percentage (1-100)"),
    target_format: str | None = Field(default=None, description="Target format: 'JPEG', 'PNG', 'WEBP'")
) -> str:
    if not ctx.deps.has_photo or not ctx.deps.photo_path:
        return "Error: No photo found in this request. Please send an image along with your instruction."

    out_name = f"proc_{ctx.deps.user_id}_{Path(ctx.deps.photo_path).stem}"
    out_base = str(TEMP_DIR / out_name)

    try:
        result = process_image(
            input_path=ctx.deps.photo_path,
            output_path=out_base,
            action=action,
            scale_x=scale_x,
            scale_y=scale_y,
            target_kb=target_kb,
            quality=quality,
            target_format=target_format
        )
        ctx.deps.action_result = {
            "type": "image",
            **result
        }
        return (
            f"Successfully processed image ({action}). "
            f"Dimensions: {result['orig_dimensions']} -> {result['final_dimensions']}. "
            f"Size: {result['orig_size_kb']} KB -> {result['final_size_kb']} KB."
        )
    except Exception as e:
        return f"Failed to process image: {str(e)}"

@agent.tool
async def download_media_tool(
    ctx: RunContext[BotContext],
    url: str = Field(description="The media URL to download"),
    audio_only: bool = Field(default=False, description="True to extract audio as MP3, False for MP4 video")
) -> str:
    user_out_dir = str(TEMP_DIR / f"dl_{ctx.deps.user_id}")
    try:
        result = download_media(
            url=url,
            output_dir=user_out_dir,
            audio_only=audio_only,
            max_filesize_mb=48
        )
        ctx.deps.action_result = {
            "type": "media",
            **result
        }
        return f"Downloaded '{result['title']}' ({result['size_mb']} MB)."
    except Exception as e:
        return f"Failed to download media from {url}: {str(e)}"

@agent.tool
async def web_search_tool(
    ctx: RunContext[BotContext],
    query: str = Field(description="The search query to look up on the web")
) -> str:
    return search_web(query)
