import io
import uuid
import logging
from pathlib import Path
from PIL import Image
from agent.registry import register_tool

logger = logging.getLogger(__name__)

# Guard against decompression bomb DoS attacks (max 50 megapixels)
Image.MAX_IMAGE_PIXELS = 50_000_000


@register_tool(
    name="compress_image",
    description="Compress an image to a target file size in kilobytes (e.g. 100kb) with optimal quality preservation.",
    parameters={
        "type": "object",
        "properties": {
            "image_path": {
                "type": "string",
                "description": "Path to the image file to compress"
            },
            "target_kb": {
                "type": "integer",
                "description": "Desired file size in KB (kilobytes), e.g. 100",
                "default": 100
            }
        },
        "required": ["image_path", "target_kb"]
    }
)
def compress_image(image_path: str, target_kb: int = 100) -> str:
    path = Path(image_path)
    if not path.exists():
        return f"Error: Image at {image_path} not found."

    target_bytes = target_kb * 1024
    try:
        with Image.open(path) as raw_img:
            img = raw_img.convert("RGB") if raw_img.mode in ("RGBA", "P") else raw_img.copy()

        # Atomic unique filename to eliminate concurrency overwrite collisions
        unique_suffix = uuid.uuid4().hex[:6]
        out_path = path.parent / f"compressed_{unique_suffix}_{path.stem}.jpg"

        # Downscale progressively if image resolution exceeds target byte ceiling
        scale_attempts = 0
        while scale_attempts < 4:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=5, optimize=True)
            if buf.tell() <= target_bytes or scale_attempts >= 3:
                break
            # Reduce dimensions by 25% to reach target file size
            w, h = img.size
            if w <= 100 or h <= 100:
                break
            img = img.resize((int(w * 0.75), int(h * 0.75)), Image.Resampling.LANCZOS)
            scale_attempts += 1

        quality_min, quality_max = 5, 95
        best_quality = quality_min

        while quality_min <= quality_max:
            mid = (quality_min + quality_max) // 2
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=mid, optimize=True)
            size = buf.tell()
            if size <= target_bytes:
                best_quality = mid
                quality_min = mid + 1
            else:
                quality_max = mid - 1

        img.save(out_path, format="JPEG", quality=best_quality, optimize=True)
        final_kb = out_path.stat().st_size / 1024
        title = f"Compressed Image ({final_kb:.1f} KB)"
        return f"Image compressed successfully to {final_kb:.1f} KB [FILE: {out_path.resolve()}] [TITLE: {title}]"
    except Exception as e:
        logger.error(f"Image compression error: {e}")
        return f"Failed to compress image: {e}"
