import logging
from pathlib import Path
import yt_dlp
from agent.registry import register_tool

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path(__file__).parent.parent.parent.resolve() / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


@register_tool(
    name="get_media_info",
    description="Inspect and extract metadata for a video/audio URL (YouTube, Instagram, TikTok, Twitter/X) without downloading.",
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL of the video or audio to inspect"
            }
        },
        "required": ["url"]
    }
)
def get_media_info(url: str) -> str:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "Unknown Title")
            duration = info.get("duration", 0)
            uploader = info.get("uploader", "Unknown Uploader")
            views = info.get("view_count", "N/A")
            return f"Title: {title}\nUploader: {uploader}\nDuration: {duration}s\nViews: {views}\nURL: {url}"
    except Exception as e:
        logger.error(f"Error extracting media info: {e}")
        return f"Could not inspect media: {e}"


@register_tool(
    name="download_media",
    description="Download a video or audio from YouTube, TikTok, Reels, or Twitter to send to the user.",
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The media URL to download"
            },
            "format_type": {
                "type": "string",
                "enum": ["best_video", "best_audio"],
                "description": "Download best video (mp4) or audio (mp3)",
                "default": "best_video"
            }
        },
        "required": ["url"]
    }
)
def download_media(url: str, format_type: str = "best_video") -> str:
    out_tmpl = str(DOWNLOAD_DIR / "%(id)s.%(ext)s")
    if format_type == "best_audio":
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": 50 * 1024 * 1024,  # Telegram limit 50MB
        }
    else:
        ydl_opts = {
            "format": "best[ext=mp4][filesize<48M]/best[filesize<48M]/best",
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": 50 * 1024 * 1024,
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            title = info.get("title", "Media")
            return f"Downloaded: '{title}' [FILE: {filepath}] [TITLE: {title}]. Ready to deliver."
    except Exception as e:
        logger.error(f"Error downloading media: {e}")
        return f"Failed to download media: {e}"
