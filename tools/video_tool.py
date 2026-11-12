import os
import glob
import re
import io
import base64
import tempfile
import urllib.parse
from pathlib import Path
from PIL import Image
import yt_dlp
import httpx
import logging

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

logger = logging.getLogger("video_tool")

# Optimized extractor args: Use web_embedded and tv_samsung clients.
# These bypass YouTube bot verification on cloud/datacenter IP addresses (e.g. Railway)
# while working seamlessly for Shorts and regular videos.
YTDLP_EXTRACTOR_ARGS = {}

YTDLP_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

_temp_cookie_path: str | None = None
_PROXY_COUNTER = 0

def get_proxy(user_id: int | None = None, attempt: int = 0) -> str | None:
    """
    Returns a dedicated sticky proxy from the pool (1-10) for this user's download session.
    Keeps the IP constant for the duration of the video+audio chunk merge to prevent HTTP 403,
    while rotating across different user downloads.
    """
    global _PROXY_COUNTER
    env_proxy = os.getenv("YTDLP_PROXY") or os.getenv("PROXY_URL")
    if env_proxy and env_proxy.strip():
        return env_proxy.strip()

    # Webshare sticky pool (indexes 1 through 10)
    if user_id:
        idx = ((abs(hash(str(user_id))) + attempt) % 10) + 1
    else:
        _PROXY_COUNTER = (_PROXY_COUNTER % 10) + 1
        idx = ((_PROXY_COUNTER + attempt - 1) % 10) + 1
        
    return f"http://gyeuukdz-{idx}:kvfs9tl7lopg@p.webshare.io:80/"

def get_cookie_file() -> str | None:
    """Finds cookies.txt or decodes from YT_COOKIES / YT_COOKIES_B64 environment variable if provided."""
    global _temp_cookie_path
    
    # 1. Check raw YT_COOKIES environment variable
    raw_cookies = os.getenv("YT_COOKIES")
    if raw_cookies and raw_cookies.strip():
        try:
            if not _temp_cookie_path or not os.path.exists(_temp_cookie_path):
                tf = tempfile.NamedTemporaryFile(delete=False, suffix="_yt_cookies.txt", mode="w", encoding="utf-8")
                tf.write(raw_cookies)
                tf.close()
                _temp_cookie_path = tf.name
            return _temp_cookie_path
        except Exception as e:
            logger.warning(f"Failed to write YT_COOKIES: {e}")

    # 2. Check base64 encoded YT_COOKIES_B64
    b64_cookies = os.getenv("YT_COOKIES_B64")
    if b64_cookies and b64_cookies.strip():
        try:
            if not _temp_cookie_path or not os.path.exists(_temp_cookie_path):
                decoded = base64.b64decode(b64_cookies.strip())
                tf = tempfile.NamedTemporaryFile(delete=False, suffix="_yt_cookies.txt", mode="wb")
                tf.write(decoded)
                tf.close()
                _temp_cookie_path = tf.name
            return _temp_cookie_path
        except Exception as e:
            logger.warning(f"Failed to decode YT_COOKIES_B64: {e}")

    # 3. Check local cookie files and cookies directory
    candidates = [
        Path.cwd() / "cookies.txt",
        Path(__file__).resolve().parent / "cookies.txt",
        Path(__file__).resolve().parent.parent / "cookies.txt",
    ]
    # Check for cookies/*.txt multi-account pool
    cookies_dir = Path.cwd() / "cookies"
    if cookies_dir.is_dir():
        for cf in sorted(cookies_dir.glob("*.txt")):
            candidates.append(cf)

    for p in candidates:
        if p.is_file():
            return str(p)
    return None

def clean_media_url(url: str) -> str:
    """Fixes malformed URL parameters and cleans up tracking."""
    url = url.strip()
    if "?si-" in url:
        url = url.replace("?si-", "?si=")

    # Strip YouTube tracking parameters like ?si=..., &feature=...
    if "youtube.com" in url or "youtu.be" in url:
        url = re.sub(r'([?&])si=[^&]+', '', url)
        url = re.sub(r'[?&]feature=[^&]+', '', url)
        url = re.sub(r'[?&]$', '', url)
    
    # Strip Instagram tracking parameters
    if "instagram.com" in url:
        match = re.search(r'(https?://(?:www\.)?instagram\.com/(?:reel|p|tv|stories)/[\w\-]+)/?', url)
        if match:
            url = match.group(1) + "/"
            
    return url

def download_media_instant(url: str, output_dir: str, max_filesize_mb: int = 48, user_id: int | None = None) -> dict:
    """
    Direct high-performance media downloader.
    Downloads the video immediately with optimal audio/video streams,
    extracting title, thumbnail, and duration in a single pass.
    """
    url = clean_media_url(url)
    os.makedirs(output_dir, exist_ok=True)
    out_template = os.path.join(output_dir, "%(title).50s [%(id)s].%(ext)s")

    cookie_file = get_cookie_file()
    proxy = get_proxy(user_id=user_id)

    # Direct connection first (like native terminal yt-dlp), then proxy fallback if IP blocked
    connection_attempts = [None]
    if proxy:
        connection_attempts.append(proxy)

    info = None
    title = "Media"
    last_err = None

    for conn_proxy in connection_attempts:
        ydl_opts = {
            "outtmpl": out_template,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "http_headers": YTDLP_HTTP_HEADERS,
            "socket_timeout": 30,
            "retries": 3,
        }
        if cookie_file:
            ydl_opts["cookiefile"] = cookie_file
        if conn_proxy:
            ydl_opts["proxy"] = conn_proxy

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    title = info.get("title", "Media")
                    break
        except Exception as e:
            last_err = e
            logger.warning(f"Download attempt (proxy={bool(conn_proxy)}) failed: {e}. Trying next...")
            continue

    if not info and last_err:
        raise last_err

    downloaded_file = None
    if info and "requested_downloads" in info and info["requested_downloads"]:
        downloaded_file = info["requested_downloads"][0].get("filepath")

    if not downloaded_file or not os.path.exists(downloaded_file):
        files = list(Path(output_dir).glob("*"))
        if files:
            downloaded_file = str(max(files, key=os.path.getctime))

    if not downloaded_file or not os.path.exists(downloaded_file):
        raise FileNotFoundError("Could not locate downloaded media file.")

    size_mb = round(os.path.getsize(downloaded_file) / (1024 * 1024), 2)
    thumb_url = info.get("thumbnail") if info else None
    duration = info.get("duration", 0) if info else 0

    is_image = downloaded_file.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))

    return {
        "file_path": downloaded_file,
        "title": title,
        "size_mb": size_mb,
        "thumb_url": thumb_url,
        "duration": duration,
        "media_type": "image" if is_image else "video",
        "url": url
    }

def compress_image_ultra(input_path: str, output_path: str, target_kb: int = 1) -> dict:
    """
    Compresses an image down to ~1KB ultra-compact size while preserving visual aspect ratio.
    Utilizes adaptive resolution downscaling and WebP/lossy quantization.
    """
    orig_bytes = os.path.getsize(input_path)
    orig_kb = round(orig_bytes / 1024, 2)

    img = Image.open(input_path).convert("RGB")
    w, h = img.size

    # Scale down gracefully to prevent massive raw byte payloads
    scale = min(1.0, 320.0 / max(w, h))
    new_w = max(64, int(w * scale))
    new_h = max(64, int(h * scale))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    best_buf = None
    best_size = 99999999

    for q in [35, 25, 15, 10, 5]:
        buf = io.BytesIO()
        resized.save(buf, format="WEBP", quality=q, method=6)
        size = buf.tell()
        if size < best_size:
            best_size = size
            best_buf = buf.getvalue()
        if size <= target_kb * 1024:
            break

    # If still above target, fallback to low-res tiny JPEG
    if best_size > target_kb * 1024 * 2:
        buf = io.BytesIO()
        tiny = img.resize((160, max(40, int(160 * h / w))), Image.Resampling.LANCZOS)
        tiny.save(buf, format="JPEG", quality=20, optimize=True)
        if buf.tell() < best_size:
            best_buf = buf.getvalue()

    with open(output_path, "wb") as f:
        f.write(best_buf)

    comp_bytes = os.path.getsize(output_path)
    comp_kb = round(comp_bytes / 1024, 2)
    saved_pct = round((1 - (comp_kb / max(0.01, orig_kb))) * 100, 1)

    return {
        "file_path": output_path,
        "orig_kb": orig_kb,
        "compressed_kb": comp_kb,
        "saved_pct": max(0, saved_pct)
    }

def get_media_info(url: str) -> dict:
    """
    Extracts title, duration, thumbnail, and media type without downloading.
    """
    url = clean_media_url(url)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        "extractor_args": YTDLP_EXTRACTOR_ARGS,
        "http_headers": YTDLP_HTTP_HEADERS,
        "js_runtimes": {"node": {}},
        "socket_timeout": 20,
    }
    cookie_file = get_cookie_file()
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file
    proxy = get_proxy()
    if proxy:
        ydl_opts["proxy"] = proxy

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "Media Post")
            duration = info.get("duration", 0)
            thumbnail = info.get("thumbnail")
            extractor = info.get("extractor_key", "Media").lower()

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
        if "instagram" in err_lower and ("empty media response" in err_lower or "not granting access" in err_lower or "login" in err_lower):
            raise RuntimeError("⚠️ Instagram login protection: Instagram requires a login session for this post.")
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
    - 4k, 1080p, 720p, 480p, 360p (video)
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

    # 2. General yt-dlp Options with anti-bot mobile player clients and Node js_runtime
    out_template = os.path.join(output_dir, "%(title).50s [%(id)s].%(ext)s")
    ydl_opts = {
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": max_filesize_mb * 1024 * 1024,
        "extractor_args": YTDLP_EXTRACTOR_ARGS,
        "http_headers": YTDLP_HTTP_HEADERS,
        "js_runtimes": {"node": {}},
        "socket_timeout": 30,
        "retries": 3,
    }
    cookie_file = get_cookie_file()
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file
    proxy = get_proxy()
    if proxy:
        ydl_opts["proxy"] = proxy

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
        # Video resolution mode (4k, 1080p, 720p, 480p, 360p)
        height_map = {"4k": 2160, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
        h = height_map.get(mode, 720)
        ydl_opts.update({
            "format": f"bestvideo[height<={h}]+bestaudio/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
        })
        media_type = "video"

    info = None
    title = "Media"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                title = info.get("title", "Media")
    except Exception as primary_err:
        logger.warning(f"Primary format download failed: {primary_err}. Retrying with universal direct format...")
        ydl_opts["format"] = "best[ext=mp4]/best"
        ydl_opts.pop("merge_output_format", None)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                title = info.get("title", "Media")

    # Locate downloaded file (OUTSIDE except block)
    downloaded_file = None
    if info and "requested_downloads" in info and info["requested_downloads"]:
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
