import os
import glob
from pathlib import Path
import yt_dlp
import httpx
import logging

logger = logging.getLogger("video_tool")

def get_media_info(url: str) -> dict:
    """
    Extracts title, duration, thumbnail, and media type without downloading.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
    }
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

    # 2. General yt-dlp Options
    out_template = os.path.join(output_dir, "%(title).50s [%(id)s].%(ext)s")
    ydl_opts = {
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": max_filesize_mb * 1024 * 1024,
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
        duration = info.get("duration", 0)

        # Locate downloaded file
        downloaded_file = None
        if "requested_downloads" in info:
            for d in info["requested_downloads"]:
                if os.path.exists(d.get("filepath", "")):
                    downloaded_file = d["filepath"]
                    break

        if not downloaded_file:
            files = list(Path(output_dir).glob("*"))
            if files:
                downloaded_file = str(max(files, key=os.path.getmtime))

        if not downloaded_file or not os.path.exists(downloaded_file):
            raise RuntimeError("Media downloaded but output file could not be found.")

        size_mb = round(os.path.getsize(downloaded_file) / (1024 * 1024), 2)

        return {
            "file_path": downloaded_file,
            "title": title,
            "duration": duration,
            "size_mb": size_mb,
            "media_type": media_type
        }
