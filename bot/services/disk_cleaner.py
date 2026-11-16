import asyncio
import logging
import time
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


def prune_directory(dir_path: Path, max_age_seconds: int = 7200) -> int:
    """
    Deletes files older than max_age_seconds in dir_path.
    Returns number of pruned files.
    """
    if not dir_path.exists():
        return 0
    now = time.time()
    pruned_count = 0
    try:
        for item in dir_path.iterdir():
            if item.is_file():
                try:
                    mtime = item.stat().st_mtime
                    if now - mtime > max_age_seconds:
                        item.unlink(missing_ok=True)
                        pruned_count += 1
                except Exception as e:
                    logger.warning(f"[DISK_CLEANER] Could not remove {item}: {e}")
    except Exception as e:
        logger.warning(f"[DISK_CLEANER] Error scanning {dir_path}: {e}")

    if pruned_count > 0:
        logger.info(f"[DISK_CLEANER] Pruned {pruned_count} old file(s) from {dir_path.name}")
    return pruned_count


async def disk_cleanup_loop(directories: List[Path], interval_seconds: int = 3600, max_age_seconds: int = 7200):
    """
    Background worker that runs every interval_seconds to prune stale temporary files.
    """
    logger.info(f"[DISK_CLEANER] Started background disk cleaner (interval={interval_seconds}s, max_age={max_age_seconds}s)")
    # Initial run on startup
    for d in directories:
        prune_directory(d, max_age_seconds=max_age_seconds)

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            for d in directories:
                prune_directory(d, max_age_seconds=max_age_seconds)
        except asyncio.CancelledError:
            logger.info("[DISK_CLEANER] Background cleaner cancelled.")
            break
        except Exception as e:
            logger.error(f"[DISK_CLEANER] Error in cleanup loop: {e}")
