import os
import uuid
import time
import json
import logging
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any
import httpx
import yt_dlp

try:
    import config
    DOWNLOAD_DIR = getattr(config, "downloads_dir", Path(__file__).parent.parent.parent.resolve() / "downloads")
except Exception:
    DOWNLOAD_DIR = Path(__file__).parent.parent.parent.resolve() / "downloads"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

try:
    from services.process_runner import BoundedProcessRunner
except ImportError:
    from bot.services.process_runner import BoundedProcessRunner

from agent.registry import register_tool

logger = logging.getLogger(__name__)

RECLIP_API_BASE = os.environ.get("RECLIP_API_BASE", "http://127.0.0.1:8899")


def extract_true_video_title(info: dict) -> str:
    """
    Extracts the genuine human-readable video title or post caption from metadata.
    For Instagram, Twitter, and TikTok, yt-dlp defaults to generic placeholders like
    'Video by <user>' as title while the real post caption/title is in description, caption,
    or webpage_title.
    """
    if not isinstance(info, dict):
        return "Video"

    raw_title = (info.get("title") or info.get("fulltitle") or "").strip()
    raw_desc = (info.get("description") or info.get("caption") or info.get("alt_title") or "").strip()
    webpage_title = (info.get("webpage_title") or "").strip()
    uploader = (info.get("uploader") or info.get("channel") or info.get("uploader_id") or "").strip()

    # Look for caption inside webpage_title e.g. "zenitsu.fx25 on Instagram: \"actual title text\""
    if not raw_desc and webpage_title:
        import re as _re
        quote_match = _re.search(r'["“\']([^"”\']{3,})["”\']', webpage_title)
        if quote_match:
            candidate = quote_match.group(1).strip()
            if candidate and not candidate.lower().startswith("video by"):
                raw_desc = candidate

    lower_title = raw_title.lower()
    is_placeholder = False
    if lower_title.startswith("video by ") or lower_title.startswith("post by ") or lower_title.startswith("reel by "):
        is_placeholder = True
    elif uploader and lower_title in (uploader.lower(), f"@{uploader.lower()}"):
        is_placeholder = True
    elif raw_title in ("Video", "Instagram Reel", "TikTok Video", "Shorts"):
        is_placeholder = True

    # 1. If title is genuine and not a placeholder, use it directly
    if raw_title and not is_placeholder:
        return raw_title[:140]

    # 2. If title is a placeholder (e.g. "Video by <user>"), extract from description or caption
    if raw_desc:
        lines = [line.strip() for line in raw_desc.splitlines() if line.strip()]
        for line in lines:
            if not line.startswith("#"):
                return line[:140]
        if lines:
            return lines[0][:140]

    # 3. If tags exist, use relevant tags
    tags = info.get("tags") or []
    if tags and isinstance(tags, list):
        tag_line = " ".join(f"#{t}" for t in tags[:5] if t)
        if tag_line:
            return tag_line[:100]

    # 4. If all else fails and it was a placeholder with uploader
    if is_placeholder and uploader:
        return f"Reel • @{uploader.lstrip('@')}"

    return raw_title or "Video"


def extract_creator_name(info: dict) -> str:
    """
    Extracts the distinct creator or uploader name from source metadata.
    Never confuses creator name with video title.
    """
    if not isinstance(info, dict):
        return "Unknown Creator"
    uploader = (info.get("uploader") or "").strip()
    channel = (info.get("channel") or "").strip()
    creator = (info.get("creator") or "").strip()
    uploader_id = (info.get("uploader_id") or "").strip()
    candidate = uploader or channel or creator or uploader_id or "Unknown Creator"
    return candidate[:60].strip()


def extract_direct_video_url(info: dict) -> Optional[str]:
    """
    Finds progressive direct MP4 stream URL with combined audio/video if available,
    allowing ultra-fast CDN-to-Telegram transmission in 1-2 seconds.
    Excludes YouTube / GoogleVideo URLs because Google restricts CDN streams to the resolver IP.
    """
    if not isinstance(info, dict):
        return None

    # Exclude YouTube / Google Video because Google's CDN blocks Telegram data center IPs
    extractor = (info.get("extractor") or "").lower()
    webpage_url = (info.get("webpage_url") or "").lower()
    if "youtube" in extractor or "googlevideo" in webpage_url or "youtube.com" in webpage_url or "youtu.be" in webpage_url:
        return None

    formats = info.get("formats", [])

    # 1. Check Instagram / TikTok numbered formats (format 3 > 2 > 1 > 0)
    for fid in ("3", "2", "1", "0"):
        for f in formats:
            if str(f.get("format_id")) == fid and f.get("url"):
                u = f.get("url")
                if "googlevideo.com" not in u:
                    return u

    # 2. Look for explicit progressive MP4 formats with both video and audio across all platforms
    for f in reversed(formats):
        if f.get("ext") == "mp4" and f.get("url"):
            u = f.get("url")
            if "googlevideo.com" in u:
                continue
            vcodec = str(f.get("vcodec") or "").lower()
            acodec = str(f.get("acodec") or "").lower()
            # In yt-dlp, format is complete unless explicitly marked as video-only or audio-only ('none')
            if vcodec != "none" and acodec != "none":
                return u

    # 3. Direct URL if mp4
    direct_u = info.get("url")
    if direct_u and "googlevideo.com" not in direct_u and (info.get("ext") == "mp4" or ".mp4" in direct_u):
        return direct_u

    return None


@register_tool(
    name="reclip_inspect",
    description="Inspect video/audio information, title, duration, and available quality formats from a YouTube, Shorts, TikTok, or Instagram URL.",
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Video or audio URL to inspect"
            }
        },
        "required": ["url"]
    }
)
async def reclip_inspect(url: str) -> str:
    """
    Inspects media metadata using Reclip service or direct yt-dlp fallback.
    """
    # 1. Try local Reclip service if active
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.post(f"{RECLIP_API_BASE}/api/info", json={"url": url})
            if r.status_code == 200:
                data = r.json()
                title = data.get("title", "Unknown")
                duration = data.get("duration", 0)
                uploader = data.get("uploader", "Unknown")
                formats = [f["label"] for f in data.get("formats", [])[:4]]
                return (
                    f"🎬 Video Details:\n"
                    f"Title: {title}\n"
                    f"Creator: {uploader}\n"
                    f"Duration: {duration} seconds\n"
                    f"Available Qualities: {', '.join(formats) if formats else 'Standard'}\n"
                    f"URL: {url}"
                )
    except Exception:
        pass

    # 2. Direct yt-dlp fallback
    try:
        ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "Unknown Title")
            duration = info.get("duration", 0)
            uploader = info.get("uploader", "Unknown")
            return f"🎬 Video Details:\nTitle: {title}\nCreator: {uploader}\nDuration: {duration}s\nURL: {url}"
    except Exception as e:
        return f"Could not inspect media URL: {e}"


@register_tool(
    name="download_video",
    description="Download a full video from YouTube, YouTube Shorts, TikTok, Instagram Reels, or Twitter to send to the user.",
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL of the video to download"
            }
        },
        "required": ["url"]
    }
)
@register_tool(
    name="reclip_download_video",
    description="Alias for download_video. Downloads video from YouTube, Shorts, TikTok, Reels, Twitter.",
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL of the video to download"
            }
        },
        "required": ["url"]
    }
)
async def reclip_download_video(url: str, format: str = "video", status_callback: Optional[Any] = None) -> str:
    """
    Downloads or resolves media using high-performance stream extraction.
    For video requests, immediately resolves progressive direct CDN stream in ~1.5s
    so Telegram can deliver directly to the user in 2-3s without waiting for local disk writes.
    """
    import asyncio
    t0 = time.perf_counter()
    logger.info(f"Reclip processing video request: {url} [format={format}]")
    format_choice = "audio" if format == "audio" else "video"

    if status_callback:
        await status_callback("⚡ Connecting to media source...")

    is_youtube = any(domain in url.lower() for domain in ("youtube.com", "youtu.be", "googlevideo.com"))

    # Fast-Path: Resolve direct progressive CDN stream for video (Instagram, TikTok, Twitter, Reddit, etc.)
    if format_choice == "video" and not is_youtube:
        opts_resolve = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "no_playlist": True,
        }
        try:
            def do_resolve():
                with yt_dlp.YoutubeDL(opts_resolve) as ydl:
                    return ydl.extract_info(url, download=False)

            res_info = await asyncio.to_thread(do_resolve)
            t_resolve = time.perf_counter() - t0
            logger.info(f"[PERF] Fast stream resolution finished in {t_resolve:.2f}s for {url}")

            if res_info:
                video_title = extract_true_video_title(res_info)
                creator_name = extract_creator_name(res_info)
                direct_url = extract_direct_video_url(res_info)
                desc = (res_info.get("description") or "").strip()
                duration = res_info.get("duration")
                thumbnail = res_info.get("thumbnail")

                if direct_url:
                    meta_obj = {
                        "source_url": url,
                        "video_title": video_title,
                        "creator_name": creator_name,
                        "duration": duration,
                        "thumbnail": thumbnail,
                        "description": desc[:300] if desc else None,
                        "transcript": None,
                        "direct_url": direct_url
                    }
                    meta_json = json.dumps(meta_obj, ensure_ascii=False)

                    # Non-blocking background caching to disk for subsequent clipping/editing
                    safe_title = "".join(c for c in video_title if c not in r'\/:*?"<>|').strip()[:60].strip() or "video"
                    cached_file = DOWNLOAD_DIR / f"{safe_title}.mp4"

                    async def _bg_cache():
                        try:
                            if not cached_file.exists():
                                logger.info(f"[CACHE] Non-blocking background cache started for {safe_title}...")
                                dl_opts = {
                                    "outtmpl": str(DOWNLOAD_DIR / f"{safe_title}.%(ext)s"),
                                    "quiet": True,
                                    "no_warnings": True,
                                    "no_playlist": True,
                                    "format": "best[ext=mp4][filesize<48M]/bestvideo[ext=mp4]+bestaudio/best",
                                    "merge_output_format": "mp4",
                                }
                                def _run_cache():
                                    with yt_dlp.YoutubeDL(dl_opts) as ydl:
                                        ydl.extract_info(url, download=True)
                                await asyncio.to_thread(_run_cache)
                                logger.info(f"[CACHE] Non-blocking cache ready: {cached_file.name}")
                        except Exception as cache_err:
                            logger.debug(f"[CACHE] Cache error: {cache_err}")

                    asyncio.create_task(_bg_cache())

                    # Fast-path return with direct CDN URL for instantaneous Telegram dispatch
                    return (
                        f"✅ Video stream resolved: '{video_title}' "
                        f"[TITLE: {video_title}] "
                        f"[CREATOR: {creator_name}] "
                        f"[DIRECT_URL: {direct_url}] "
                        f"[META: {meta_json}]"
                    )
        except Exception as resolve_err:
            logger.warning(f"Fast stream resolution failed ({resolve_err}), falling back to direct download...")

    # High-Performance Single-Pass Download Path (Used for YouTube, audio extracts, or non-CDN streams)
    job_id = uuid.uuid4().hex[:10]
    out_tmpl = str(DOWNLOAD_DIR / f"dl_{job_id}.%(ext)s")

    dl_opts = {
        "outtmpl": out_tmpl,
        "quiet": True,
        "no_warnings": True,
        "no_playlist": True,
        "buffersize": 1024 * 1024,
        "http_chunk_size": 10485760,
    }
    if is_youtube:
        dl_opts["extractor_args"] = {"youtube": {"player_client": ["ios", "android"]}}
        dl_opts["format"] = "best[ext=mp4][filesize<48M][height<=720]/best[height<=720][filesize<48M]/best[filesize<48M]/best"
    elif format_choice == "audio":
        dl_opts["format"] = "bestaudio/best"
        dl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        dl_opts["format"] = "best[ext=mp4][filesize<48M]/bestvideo[ext=mp4][filesize<40M]+bestaudio[ext=m4a]/best[filesize<48M]/best"
        dl_opts["merge_output_format"] = "mp4"

    # Auto-load cookies.txt if provided to bypass anti-bot bot challenges
    for c_dir in (Path(__file__).parent.parent.parent.resolve(), Path(__file__).parent.parent.parent.parent.resolve()):
        c_cand = c_dir / "cookies.txt"
        if c_cand.exists() and c_cand.is_file():
            dl_opts["cookiefile"] = str(c_cand)
            logger.info(f"Loaded yt-dlp cookies from: {c_cand}")
            break

    video_title = "Video"
    try:
        def do_download():
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                return ydl.extract_info(url, download=True)

        res_info = await asyncio.to_thread(do_download)
        t_dl = time.perf_counter() - t0
        logger.info(f"[PERF] Single-pass download completed in {t_dl:.2f}s for {url}")

        if res_info:
            video_title = extract_true_video_title(res_info)
            creator_name = extract_creator_name(res_info)
            direct_url = extract_direct_video_url(res_info)
            desc = (res_info.get("description") or "").strip()
            duration = res_info.get("duration")
            thumbnail = res_info.get("thumbnail")
        else:
            creator_name = "Unknown Creator"
            direct_url = None
            desc = ""
            duration = None
            thumbnail = None

        meta_obj = {
            "source_url": url,
            "video_title": video_title,
            "creator_name": creator_name,
            "duration": duration,
            "thumbnail": thumbnail,
            "description": desc[:300] if desc else None,
            "transcript": None,
            "direct_url": direct_url
        }
        meta_json = json.dumps(meta_obj, ensure_ascii=False)

        # Find the resulting file
        expected_ext = ".mp3" if format_choice == "audio" else ".mp4"
        candidate_files = list(DOWNLOAD_DIR.glob(f"dl_{job_id}.*"))
        target_file = None
        for cf in candidate_files:
            if cf.suffix.lower() == expected_ext:
                target_file = cf
                break
        if not target_file and candidate_files:
            target_file = candidate_files[0]

        if target_file and target_file.exists():
            safe_title = "".join(c for c in video_title if c not in r'\/:*?"<>|').strip()[:60].strip()
            if safe_title:
                new_path = DOWNLOAD_DIR / f"{safe_title}{target_file.suffix}"
                try:
                    if new_path.exists():
                        new_path.unlink()
                    target_file.rename(new_path)
                    target_file = new_path
                except Exception as rn_err:
                    logger.debug(f"Could not rename file to safe title: {rn_err}")

            size_mb = target_file.stat().st_size / (1024 * 1024)
            return (
                f"✅ Video downloaded successfully: '{video_title}' ({size_mb:.2f} MB) "
                f"[FILE: {target_file.resolve()}] "
                f"[TITLE: {video_title}] "
                f"[CREATOR: {creator_name}] "
                f"[DIRECT_URL: {direct_url or ''}] "
                f"[META: {meta_json}]"
            )

    except Exception as dl_err:
        logger.warning(f"Direct yt-dlp single-pass download failed: {dl_err}. Falling back to Reclip service...")

    # 4. Fallback to ReClip local service if direct download failed
    try:
        if status_callback:
            await status_callback("⚡ Connecting to download worker...")
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{RECLIP_API_BASE}/api/download",
                json={"url": url, "format": format_choice, "title": video_title}
            )
            if r.status_code == 200:
                job_id = r.json().get("job_id")
                for _ in range(25):
                    await asyncio.sleep(2.0)
                    st_resp = await client.get(f"{RECLIP_API_BASE}/api/status/{job_id}")
                    if st_resp.status_code == 200:
                        st = st_resp.json()
                        if st.get("status") == "done":
                            filename = st.get("filename") or f"{safe_title}.mp4"
                            local_file = DOWNLOAD_DIR / filename
                            file_resp = await client.get(f"{RECLIP_API_BASE}/api/file/{job_id}", timeout=60.0)
                            if file_resp.status_code == 200:
                                with open(local_file, "wb") as f:
                                    f.write(file_resp.content)
                                size_mb = local_file.stat().st_size / (1024 * 1024)
                                if status_callback:
                                    await status_callback("🚀 Uploading video to Telegram...")
                                fb_meta = {
                                    "source_url": url,
                                    "video_title": video_title,
                                    "creator_name": "Unknown Creator",
                                    "duration": None,
                                    "thumbnail": None,
                                    "description": None,
                                    "transcript": None,
                                    "direct_url": None
                                }
                                return (
                                    f"✅ Video downloaded successfully: '{video_title}' ({size_mb:.2f} MB) "
                                    f"[FILE: {local_file.resolve()}] "
                                    f"[TITLE: {video_title}] "
                                    f"[CREATOR: Unknown Creator] "
                                    f"[DIRECT_URL: ] "
                                    f"[META: {json.dumps(fb_meta, ensure_ascii=False)}]"
                                )
                        elif st.get("status") == "error":
                            return f"Download failed: {st.get('error', 'Video unavailable')}"
    except Exception as e:
        logger.error(f"Reclip fallback also failed: {e}")

    return f"Download failed for URL: {url}"


@register_tool(
    name="clip_video",
    description="Clip or trim a video between specific start and end timestamps (e.g. start='00:00:15', end='00:00:45'). Supports local video or online URL.",
    parameters={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Video file path or online video URL to clip from"
            },
            "start_time": {
                "type": "string",
                "description": "Start timestamp in HH:MM:SS or MM:SS format (e.g. '00:00:10')"
            },
            "end_time": {
                "type": "string",
                "description": "End timestamp in HH:MM:SS or MM:SS format (e.g. '00:00:40')"
            },
            "output_format": {
                "type": "string",
                "enum": ["mp4", "webm", "gif"],
                "description": "Desired output format (default: mp4)",
                "default": "mp4"
            }
        },
        "required": ["source", "start_time", "end_time"]
    }
)
@register_tool(
    name="reclip_clip_video",
    description="Alias for clip_video. Clip or trim a video between specific start and end timestamps.",
    parameters={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Video file path or online video URL to clip from"
            },
            "start_time": {
                "type": "string",
                "description": "Start timestamp in HH:MM:SS or MM:SS format (e.g. '00:00:10')"
            },
            "end_time": {
                "type": "string",
                "description": "End timestamp in HH:MM:SS or MM:SS format (e.g. '00:00:40')"
            },
            "output_format": {
                "type": "string",
                "enum": ["mp4", "webm", "gif"],
                "description": "Desired output format (default: mp4)",
                "default": "mp4"
            }
        },
        "required": ["source", "start_time", "end_time"]
    }
)
def reclip_clip_video(
    source: str,
    start_time: str,
    end_time: str,
    output_format: str = "mp4"
) -> str:
    """
    Clips and trims a video using ffmpeg or yt-dlp download-sections.
    Guarantees bounded subprocess execution, process group isolation, and returns formatted result string.
    """
    job_id = uuid.uuid4().hex[:8]
    output_path = DOWNLOAD_DIR / f"clip_{job_id}.{output_format}"

    # If source is an online URL, download section directly via yt-dlp
    if source.startswith("http://") or source.startswith("https://"):
        section_spec = f"*{start_time}-{end_time}"
        out_tmpl = str(DOWNLOAD_DIR / f"clip_{job_id}.%(ext)s")
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--download-sections", section_spec,
            "-f", "bestvideo[filesize<45M]+bestaudio/best[filesize<45M]/best",
            "--merge-output-format", output_format,
            "-o", out_tmpl,
            source
        ]
        try:
            retcode, stdout, stderr = BoundedProcessRunner.run_bounded_sync(cmd, timeout=180)
            if retcode == 0:
                files = list(DOWNLOAD_DIR.glob(f"clip_{job_id}.*"))
                if files:
                    actual = files[0]
                    size_mb = actual.stat().st_size / (1024 * 1024)
                    clip_title = f"Clip ({start_time} - {end_time})"
                    return f"✅ Clip generated successfully: '{clip_title}' ({size_mb:.2f} MB) [FILE: {actual.resolve()}] [TITLE: {clip_title}]"
            # If download-sections fails, fallback to full download then ffmpeg clip
        except Exception as e:
            logger.error(f"yt-dlp clip section error: {e}")

    # If source is a local file (or downloaded), use ffmpeg
    local_source = Path(source)
    if not local_source.exists():
        return f"Error: Source video file '{source}' not found."

    cmd = [
        "ffmpeg", "-y",
        "-ss", start_time,
        "-to", end_time,
        "-i", str(local_source),
        "-c", "copy" if output_format == local_source.suffix.lstrip(".") else "default",
        str(output_path)
    ]
    # Fast re-encode fallback if stream-copy fails
    try:
        retcode, stdout, stderr = BoundedProcessRunner.run_bounded_sync(cmd, timeout=120)
        if retcode != 0 or not output_path.exists():
            # Retry with re-encoding
            cmd_reencode = [
                "ffmpeg", "-y",
                "-ss", start_time,
                "-to", end_time,
                "-i", str(local_source),
                "-c:v", "libx264",
                "-c:a", "aac",
                str(output_path)
            ]
            retcode2, stdout2, stderr2 = BoundedProcessRunner.run_bounded_sync(cmd_reencode, timeout=120)
            if retcode2 != 0:
                err_msg = stderr2.strip().splitlines()[-1] if stderr2.strip() else "Unknown ffmpeg error"
                return f"Error clipping video with ffmpeg: {err_msg}"

        size_mb = output_path.stat().st_size / (1024 * 1024)
        clip_title = f"{local_source.stem} (Clip {start_time}-{end_time})"
        return f"✅ Clip extracted successfully: '{clip_title}' ({size_mb:.2f} MB) [FILE: {output_path.resolve()}] [TITLE: {clip_title}]"
    except Exception as e:
        return f"Clipping failed: {e}"


@register_tool(
    name="extract_audio",
    description="Extract the audio track from a video URL or video file into an MP3 or M4A audio file.",
    parameters={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Video URL or local video file path"
            },
            "audio_format": {
                "type": "string",
                "enum": ["mp3", "m4a", "wav"],
                "description": "Target audio format (default: mp3)",
                "default": "mp3"
            }
        },
        "required": ["source"]
    }
)
@register_tool(
    name="reclip_extract_audio",
    description="Alias for extract_audio. Extract the audio track from a video URL or video file into an MP3 or M4A audio file.",
    parameters={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Video URL or local video file path"
            },
            "audio_format": {
                "type": "string",
                "enum": ["mp3", "m4a", "wav"],
                "description": "Target audio format (default: mp3)",
                "default": "mp3"
            }
        },
        "required": ["source"]
    }
)
def reclip_extract_audio(source: str, audio_format: str = "mp3") -> str:
    """
    Extracts the audio track from a video URL or video file.
    Guarantees bounded subprocess execution, process group isolation, and returns formatted result string.
    """
    job_id = uuid.uuid4().hex[:8]
    output_path = DOWNLOAD_DIR / f"audio_{job_id}.{audio_format}"

    if source.startswith("http://") or source.startswith("https://"):
        out_tmpl = str(DOWNLOAD_DIR / f"audio_{job_id}.%(ext)s")
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "-x",
            "--audio-format", audio_format,
            "-o", out_tmpl,
            source
        ]
        try:
            retcode, stdout, stderr = BoundedProcessRunner.run_bounded_sync(cmd, timeout=180)
            files = list(DOWNLOAD_DIR.glob(f"audio_{job_id}.*"))
            if files:
                actual = files[0]
                size_mb = actual.stat().st_size / (1024 * 1024)
                audio_title = f"{actual.stem} ({audio_format.upper()})"
                return f"🎵 Audio extracted successfully: '{audio_title}' ({size_mb:.2f} MB) [FILE: {actual.resolve()}] [TITLE: {audio_title}]"
            err_msg = stderr.strip().splitlines()[-1] if stderr.strip() else "Unknown extraction error"
            return f"Audio extraction failed: {err_msg}"
        except Exception as e:
            return f"Audio extraction error: {e}"

    local_source = Path(source)
    if not local_source.exists():
        return f"Error: Source file '{source}' not found."

    cmd = [
        "ffmpeg", "-y",
        "-i", str(local_source),
        "-vn",
        "-acodec", "libmp3lame" if audio_format == "mp3" else "copy",
        str(output_path)
    ]
    try:
        retcode, stdout, stderr = BoundedProcessRunner.run_bounded_sync(cmd, timeout=90)
        if retcode == 0 and output_path.exists():
            size_mb = output_path.stat().st_size / (1024 * 1024)
            audio_title = f"{local_source.stem} ({audio_format.upper()})"
            return f"🎵 Audio extracted: '{audio_title}' ({size_mb:.2f} MB) [FILE: {output_path.resolve()}] [TITLE: {audio_title}]"
        err_msg = stderr.strip().splitlines()[-1] if stderr.strip() else "Unknown ffmpeg error"
        return f"ffmpeg audio extraction error: {err_msg}"
    except Exception as e:
        return f"Audio extraction error: {e}"

