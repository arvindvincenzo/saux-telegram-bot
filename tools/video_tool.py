import os
import glob
import re
import urllib.parse
from pathlib import Path
import yt_dlp
import httpx
import logging

logger = logging.getLogger("video_tool")

# Extractor args and User-Agent to bypass YouTube bot detection on cloud/datacenter IPs
YTDLP_EXTRACTOR_ARGS = {
    "youtube": {
        "player_client": ["android", "ios", "mweb", "tv_embedded"],
        "player_skip": ["webpage", "configs"],
    }
}

YTDLP_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def clean_media_url(url: str) -> str:
    """Fixes malformed URL parameters (e.g., ?si- instead of ?si=) and cleans up tracking."""
    url = url.strip()
    if "?si-" in url:
        url = url.replace("?si-", "?si=")
    return url

def get_media_info(url: str) -> dict:
    """
    Extracts title, duration, thumbnail, and media type without downloading.
    Includes mobile client emulation to avoid YouTube bot confirmation blocks.
    """
    url = clean_media_url(url)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        "extractor_args": YTDLP_EXTRACTOR_ARGS,
        "http_headers": YTDLP_HTTP_HEADERS,
        "socket_timeout": 20,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "Media Post")
            duration = info.get("duration", 0)
            thumbnail = info.get("thumbnail")
            extractor = info.get("extractor_key", "Media").lower()

            # Check if it's an image post/carousel (e.g. Instagram/Twitter photos)
            is_image_post = False
            if info.get("_type") == "playlist" and not any(e.get("duration") for e in info.get("entries", [])):
                is_image_post = True
            elif info.get("ext") in ["jpg", "png", "webp", "jpeg"]:
                is_image_post = True

            return {
                "title": title,
                "duration": duration,
                "thumbnail": thumbnail,
                "extractor": extractor,
                "is_image_post": is_image_post,
                "webpage_url": info.get("webpage_url", url)
            }
    except Exception as e:
        err_msg = str(e)
        logger.warning(f"Primary yt-dlp extract failed: {err_msg}")

        # Fallback oEmbed metadata for YouTube
        if "youtube" in url.lower() or "youtu.be" in url.lower():
            try:
                with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                    clean_yt_url = url.split("?")[0]
                    oembed_url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(clean_yt_url)}&format=json"
                    r = client.get(oembed_url)
                    if r.status_code == 200:
                        data = r.json()
                        return {
                            "title": data.get("title", "YouTube Video"),
                            "duration": 0,
                            "thumbnail": data.get("thumbnail_url"),
                            "extractor": "youtube",
                            "is_image_post": False,
                            "webpage_url": url
                        }
            except Exception as oe_err:
                logger.debug(f"oEmbed fallback error: {oe_err}")

        err_lower = err_msg.lower()
        if "sign in to confirm" in err_lower or "not a bot" in err_lower:
            raise RuntimeError("⚠️ YouTube verification challenge triggered on this video. Please try another video or retry in a few moments.")
        if "unavailable" in err_lower or "not found" in err_lower or "private" in err_lower:
            raise RuntimeError("⚠️ This video is unavailable, deleted, private, or region-locked on YouTube.")
        raise RuntimeError(f"Unable to parse media stream: {err_msg[:120]}")

def download_media_custom(
    url: str,
    output_dir: str,
    mode: str = "720p",
    thumb_url: str | None = None,
    max_filesize_mb: int = 48
) -> dict:
    """
    Downloads media according to user-selected quality/mode:
    - 1080p, 720p, 480p, 360p (video)
    - mp3 (audio only)
    - thumbnail (image)
    - post_images (photo posts)
    """
    url = clean_media_url(url)
    os.makedirs(output_dir, exist_ok=True)

    # 1. Thumbnail Download Mode
    if mode == "thumbnail" and thumb_url:
        thumb_path = os.path.join(output_dir, "thumbnail.jpg")
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                r = client.get(thumb_url)
                r.raise_for_status()
                with open(thumb_path, "wb") as f:
                    f.write(r.content)
            size_mb = round(os.path.getsize(thumb_path) / (1024 * 1024), 2)
            return {
                "file_path": thumb_path,
                "title": "Thumbnail",
                "size_mb": size_mb,
                "media_type": "image"
            }
        except Exception as e:
            logger.warning(f"Direct thumb download failed: {e}. Falling back to yt-dlp.")

    # 2. General yt-dlp Options with anti-bot mobile player clients
    out_template = os.path.join(output_dir, "%(title).50s [%(id)s].%(ext)s")
    ydl_opts = {
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": max_filesize_mb * 1024 * 1024,
        "extractor_args": YTDLP_EXTRACTOR_ARGS,
        "http_headers": YTDLP_HTTP_HEADERS,
        "socket_timeout": 30,
        "retries": 3,
    }

    if mode == "mp3":
        ydl_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
        media_type = "audio"

    elif mode == "thumbnail":
        ydl_opts.update({
            "skip_download": True,
            "writethumbnail": True,
        })
        media_type = "image"

    elif mode == "post_images":
        ydl_opts.update({
            "format": "best",
        })
        media_type = "image"

    else:
        # Video resolution mode (1080p, 720p, 480p, 360p)
        height_map = {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
        h = height_map.get(mode, 720)
        ydl_opts.update({
            "format": f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best[filesize<{max_filesize_mb}M]/best",
            "merge_output_format": "mp4",
        })
        media_type = "video"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title", "Media")
        
        # Locate downloaded file
        downloaded_file = None
        if "requested_downloads" in info and info["requested_downloads"]:
            downloaded_file = info["requested_downloads"][0].get("filepath")
            
        if not downloaded_file or not os.path.exists(downloaded_file):
            files = list(Path(output_dir).glob("*"))
            if files:
                downloaded_file = str(max(files, key=os.path.getctime))

        if not downloaded_file or not os.path.exists(downloaded_file):
            raise FileNotFoundError("Could not locate downloaded media file.")

        size_mb = round(os.path.getsize(downloaded_file) / (1024 * 1024), 2)
        return {
            "file_path": downloaded_file,
            "title": title,
            "size_mb": size_mb,
            "media_type": media_type
        }
