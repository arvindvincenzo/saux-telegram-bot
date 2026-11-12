import os
from pathlib import Path
from PIL import Image, ImageOps

def process_image(
    input_path: str,
    output_path: str,
    action: str = "compress",
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    target_kb: int = 500,
    quality: int = 80,
    target_format: str | None = None
) -> dict:
    """
    Processes an image using Pillow:
    - compress: reduces file size iteratively or by quality
    - stretch: changes width/height by independent scale factors
    - resize: proportional resizing
    - convert: format change (e.g. WEBP, PNG, JPEG)
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    with Image.open(input_path) as img:
        # Correct orientation if EXIF tag exists
        img = ImageOps.exif_transpose(img)
        orig_width, orig_height = img.size
        orig_size_bytes = os.path.getsize(input_path)

        # Handle Stretching / Resizing
        if action in ("stretch", "resize") or (scale_x != 1.0 or scale_y != 1.0):
            new_width = max(1, int(orig_width * scale_x))
            new_height = max(1, int(orig_height * scale_y))
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

        # Convert color mode for JPEG compatibility if needed
        save_format = target_format.upper() if target_format else (img.format or "JPEG")
        if save_format in ("JPG", "JPEG") and img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # Determine output file extension
        ext = f".{save_format.lower()}"
        if ext == ".jpeg":
            ext = ".jpg"
        if not output_path.lower().endswith(ext):
            output_path = f"{output_path}{ext}"

        # Handle Compression
        if action == "compress":
            curr_quality = quality
            img.save(output_path, format=save_format, quality=curr_quality, optimize=True)
            
            # If target_kb is specified and file is larger, adjust quality down
            target_bytes = target_kb * 1024
            while os.path.getsize(output_path) > target_bytes and curr_quality > 20:
                curr_quality -= 10
                img.save(output_path, format=save_format, quality=curr_quality, optimize=True)
        else:
            img.save(output_path, format=save_format, quality=quality, optimize=True)

        final_size_bytes = os.path.getsize(output_path)
        final_width, final_height = img.size

        return {
            "output_path": output_path,
            "orig_size_kb": round(orig_size_bytes / 1024, 2),
            "final_size_kb": round(final_size_bytes / 1024, 2),
            "orig_dimensions": f"{orig_width}x{orig_height}",
            "final_dimensions": f"{final_width}x{final_height}",
            "format": save_format
        }
