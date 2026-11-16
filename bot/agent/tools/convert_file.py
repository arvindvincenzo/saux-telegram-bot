import os
import uuid
import logging
import zipfile
import tarfile
from pathlib import Path
from typing import Optional, List, Dict
from PIL import Image

try:
    import config
    CONVERT_TEMP_DIR = getattr(config, "converted_dir", Path(__file__).parent.parent.parent.resolve() / "converted_files")
except Exception:
    CONVERT_TEMP_DIR = Path(__file__).parent.parent.parent.resolve() / "converted_files"

CONVERT_TEMP_DIR.mkdir(parents=True, exist_ok=True)

try:
    from services.process_runner import BoundedProcessRunner
except ImportError:
    from bot.services.process_runner import BoundedProcessRunner

from agent.registry import register_tool

logger = logging.getLogger(__name__)

# Supported format groups
IMAGE_FORMATS = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "ico", "pdf"}
AUDIO_FORMATS = {"mp3", "wav", "aac", "ogg", "flac", "m4a", "opus", "wma"}
VIDEO_FORMATS = {"mp4", "mkv", "avi", "webm", "mov", "flv", "wmv", "gif"}
ARCHIVE_FORMATS = {"zip", "tar", "tar.gz", "tgz"}


@register_tool(
    name="convert_file",
    description="Universal media and file format converter. Converts images (PNG, JPG, WEBP, GIF, PDF), audio (MP3, WAV, AAC, FLAC, M4A, OGG), video (MP4, MKV, AVI, WEBM, MOV), and archives (ZIP, TAR).",
    parameters={
        "type": "object",
        "properties": {
            "input_path": {
                "type": "string",
                "description": "Path to the local input file to convert"
            },
            "target_format": {
                "type": "string",
                "description": "Target output format extension (e.g. 'mp3', 'wav', 'png', 'jpg', 'webp', 'mp4', 'mkv', 'zip')"
            },
            "quality": {
                "type": "string",
                "enum": ["high", "medium", "compact"],
                "description": "Output quality preference (default: high)",
                "default": "high"
            }
        },
        "required": ["input_path", "target_format"]
    }
)
def convert_file(input_path: str, target_format: str, quality: str = "high") -> str:
    """
    Executes file and media conversion based on format category.
    Guarantees bounded subprocess execution, process group isolation, and returns formatted result string.
    """
    src = Path(input_path)
    if not src.exists():
        return f"Error: Input file '{input_path}' does not exist."

    src_ext = src.suffix.lower().lstrip(".")
    tgt_ext = target_format.lower().lstrip(".")

    if src_ext == tgt_ext:
        return f"File '{src.name}' is already in {tgt_ext.upper()} format."

    job_id = uuid.uuid4().hex[:8]
    out_file = CONVERT_TEMP_DIR / f"{src.stem}_{job_id}.{tgt_ext}"

    logger.info(f"Converting '{src.name}' ({src_ext}) to {tgt_ext}...")

    # 1. Image Conversion
    if src_ext in IMAGE_FORMATS and tgt_ext in IMAGE_FORMATS:
        return _convert_image(src, out_file, tgt_ext, quality)

    # 2. Audio to Audio Conversion
    if src_ext in AUDIO_FORMATS and tgt_ext in AUDIO_FORMATS:
        return _convert_audio(src, out_file, tgt_ext, quality)

    # 3. Video to Audio Conversion (e.g. MP4 to MP3/WAV)
    if src_ext in VIDEO_FORMATS and tgt_ext in AUDIO_FORMATS:
        return _convert_video_to_audio(src, out_file, tgt_ext)

    # 4. Video to Video Conversion
    if src_ext in VIDEO_FORMATS and tgt_ext in VIDEO_FORMATS:
        return _convert_video(src, out_file, tgt_ext, quality)

    # 5. Archive / Compression
    if tgt_ext in ARCHIVE_FORMATS:
        return _compress_archive(src, out_file, tgt_ext)

    return (
        f"Conversion from '.{src_ext}' to '.{tgt_ext}' is not supported.\n"
        f"Supported formats:\n"
        f"- Images: {', '.join(sorted(IMAGE_FORMATS))}\n"
        f"- Audio: {', '.join(sorted(AUDIO_FORMATS))}\n"
        f"- Video: {', '.join(sorted(VIDEO_FORMATS))}\n"
        f"- Archives: {', '.join(sorted(ARCHIVE_FORMATS))}"
    )


def _convert_image(src: Path, dest: Path, target_ext: str, quality: str) -> str:
    try:
        with Image.open(src) as img:
            q_map = {"high": 95, "medium": 80, "compact": 60}
            q_val = q_map.get(quality, 90)

            # Convert RGBA to RGB for JPEG/BMP
            if target_ext in ("jpg", "jpeg", "bmp") and img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")

            if target_ext in ("jpg", "jpeg"):
                img.save(dest, format="JPEG", quality=q_val, optimize=True)
            elif target_ext == "png":
                img.save(dest, format="PNG", optimize=True)
            elif target_ext == "webp":
                img.save(dest, format="WEBP", quality=q_val)
            elif target_ext == "pdf":
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(dest, format="PDF")
            elif target_ext == "ico":
                img.save(dest, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
            else:
                img.save(dest)

        size_kb = dest.stat().st_size / 1024
        title = f"{src.stem} ({target_ext.upper()})"
        return f"✅ Image converted successfully to {target_ext.upper()}: '{title}' ({size_kb:.1f} KB) [FILE: {dest.resolve()}] [TITLE: {title}]"
    except Exception as e:
        logger.error(f"Image conversion error: {e}")
        return f"Image conversion failed: {e}"


def _convert_audio(src: Path, dest: Path, target_ext: str, quality: str) -> str:
    bitrate = "320k" if quality == "high" else "192k" if quality == "medium" else "128k"
    cmd = ["ffmpeg", "-y", "-i", str(src), "-b:a", bitrate, str(dest)]
    try:
        retcode, stdout, stderr = BoundedProcessRunner.run_bounded_sync(cmd, timeout=120)
        if retcode == 0 and dest.exists():
            size_mb = dest.stat().st_size / (1024 * 1024)
            title = f"{src.stem} ({target_ext.upper()})"
            return f"✅ Audio converted to {target_ext.upper()}: '{title}' ({size_mb:.2f} MB, {bitrate}) [FILE: {dest.resolve()}] [TITLE: {title}]"
        err_msg = stderr.strip().splitlines()[-1] if stderr.strip() else "Unknown error"
        return f"Audio conversion error: {err_msg}"
    except Exception as e:
        return f"Audio conversion failed: {e}"


def _convert_video_to_audio(src: Path, dest: Path, target_ext: str) -> str:
    cmd = ["ffmpeg", "-y", "-i", str(src), "-vn", "-b:a", "192k", str(dest)]
    try:
        retcode, stdout, stderr = BoundedProcessRunner.run_bounded_sync(cmd, timeout=180)
        if retcode == 0 and dest.exists():
            size_mb = dest.stat().st_size / (1024 * 1024)
            title = f"{src.stem} ({target_ext.upper()})"
            return f"✅ Extracted audio track ({target_ext.upper()}): '{title}' ({size_mb:.2f} MB) [FILE: {dest.resolve()}] [TITLE: {title}]"
        err_msg = stderr.strip().splitlines()[-1] if stderr.strip() else "Unknown error"
        return f"Audio extraction error: {err_msg}"
    except Exception as e:
        return f"Audio extraction failed: {e}"


def _convert_video(src: Path, dest: Path, target_ext: str, quality: str) -> str:
    crf = "18" if quality == "high" else "23" if quality == "medium" else "28"
    if target_ext == "gif":
        cmd = ["ffmpeg", "-y", "-i", str(src), "-vf", "fps=10,scale=480:-1:flags=lanczos", str(dest)]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(src),
            "-c:v", "libx264" if target_ext == "mp4" else "libvpx-vp9" if target_ext == "webm" else "default",
            "-crf", crf,
            "-preset", "fast",
            "-c:a", "aac",
            str(dest)
        ]
    try:
        retcode, stdout, stderr = BoundedProcessRunner.run_bounded_sync(cmd, timeout=240)
        if retcode == 0 and dest.exists():
            size_mb = dest.stat().st_size / (1024 * 1024)
            title = f"{src.stem} ({target_ext.upper()})"
            return f"✅ Video converted to {target_ext.upper()}: '{title}' ({size_mb:.2f} MB) [FILE: {dest.resolve()}] [TITLE: {title}]"
        err_msg = stderr.strip().splitlines()[-1] if stderr.strip() else "Unknown error"
        return f"Video conversion error: {err_msg}"
    except Exception as e:
        return f"Video conversion failed: {e}"


def _compress_archive(src: Path, dest: Path, target_ext: str) -> str:
    try:
        if target_ext == "zip":
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                if src.is_dir():
                    for root, _, files in os.walk(src):
                        for file in files:
                            p = Path(root) / file
                            zf.write(p, arcname=p.relative_to(src.parent))
                else:
                    zf.write(src, arcname=src.name)
        elif target_ext in ("tar", "tar.gz", "tgz"):
            mode = "w:gz" if "gz" in target_ext or target_ext == "tgz" else "w"
            with tarfile.open(dest, mode) as tf:
                tf.add(src, arcname=src.name)

        size_kb = dest.stat().st_size / 1024
        return f"✅ Compressed to {target_ext.upper()} archive: '{dest.name}' ({size_kb:.1f} KB) at {dest}."
    except Exception as e:
        return f"Archive compression error: {e}"
