import sys
import logging
from pathlib import Path

# Add bot directory to sys.path
sys.path.insert(0, str(Path(__file__).parent / "bot"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

from bot import run_bot

if __name__ == "__main__":
    run_bot()
