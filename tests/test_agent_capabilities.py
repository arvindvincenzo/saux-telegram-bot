import sys
import asyncio
import subprocess
from pathlib import Path

# Add bot to sys.path
BOT_DIR = Path(__file__).parent.parent.resolve() / "bot"
sys.path.insert(0, str(BOT_DIR))

import config
from openai import AsyncOpenAI
from agent.registry import get_tool_schemas, execute_tool
from agent.orchestrator import AgentOrchestrator
from agent.tools.searxng_search import searxng_search
from agent.tools.reclip_video import reclip_inspect, reclip_clip_video, reclip_extract_audio
from agent.tools.convert_file import convert_file
from PIL import Image

passed_count = 0
failed_count = 0


def record(name, success, detail=""):
    global passed_count, failed_count
    if success:
        passed_count += 1
        print(f"  [PASS] {name} {detail}")
    else:
        failed_count += 1
        print(f"  [FAIL] {name} - {detail}")


async def test_searxng():
    print("\n--- Testing SearXNG Capability ---")
    try:
        res = await searxng_search("quantum computing breakthroughs", category="science", max_results=2)
        record("SearXNG Search with Category", len(res) > 20 and "quantum" in res.lower(), f"(len={len(res)})")
    except Exception as e:
        record("SearXNG Search with Category", False, str(e))

    try:
        news_res = await searxng_search("world news today", category="news", max_results=2)
        record("SearXNG News Search", len(news_res) > 20, f"(len={len(news_res)})")
    except Exception as e:
        record("SearXNG News Search", False, str(e))


def test_convert_capability():
    print("\n--- Testing Convert Capability ---")
    temp_dir = BOT_DIR.parent / "test_scratch"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create a sample test image
    img_path = temp_dir / "sample.png"
    img = Image.new("RGB", (100, 100), color="blue")
    img.save(img_path)

    # Test Image -> JPG
    res = convert_file(str(img_path), "jpg")
    record("Convert Image (PNG -> JPG)", "successfully" in res.lower(), res)

    # Test Image -> WEBP
    res_webp = convert_file(str(img_path), "webp")
    record("Convert Image (PNG -> WEBP)", "successfully" in res_webp.lower(), res_webp)

    # Test Image -> PDF
    res_pdf = convert_file(str(img_path), "pdf")
    record("Convert Image (PNG -> PDF)", "successfully" in res_pdf.lower(), res_pdf)

    # 2. Create a 2-second test video with ffmpeg
    vid_path = temp_dir / "sample.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=3",
        "-c:v", "libx264", "-c:a", "aac",
        str(vid_path)
    ]
    subprocess.run(cmd, capture_output=True, timeout=30)
    assert vid_path.exists(), "Failed to generate test video with ffmpeg"

    # Test Video -> MP3 (Video to Audio extraction)
    res_audio = convert_file(str(vid_path), "mp3")
    record("Convert Video to Audio (MP4 -> MP3)", "converted" in res_audio.lower() or "extracted" in res_audio.lower(), res_audio)

    # Test Video -> GIF
    res_gif = convert_file(str(vid_path), "gif")
    record("Convert Video to GIF (MP4 -> GIF)", "converted" in res_gif.lower(), res_gif)

    # Test Invalid format handling
    res_invalid = convert_file(str(img_path), "xyz123")
    record("Convert Unsupported Format Handling", "not supported" in res_invalid.lower(), res_invalid)


def test_reclip_capability():
    print("\n--- Testing Reclip Video Capability ---")
    temp_dir = BOT_DIR.parent / "test_scratch"
    vid_path = temp_dir / "sample.mp4"

    # Test Reclip Clipping
    res_clip = reclip_clip_video(str(vid_path), start_time="00:00:01", end_time="00:00:02")
    record("Reclip Clip Video (00:00:01 to 00:00:02)", "successfully" in res_clip.lower(), res_clip)

    # Test Reclip Extract Audio
    res_aud = reclip_extract_audio(str(vid_path), audio_format="mp3")
    record("Reclip Extract Audio", "extracted" in res_aud.lower(), res_aud)


async def test_agent_orchestrator():
    print("\n--- Testing Agent Orchestrator & Tool Routing ---")
    client = AsyncOpenAI(api_key=config.openai_api_key, base_url=config.openai_api_base)
    orchestrator = AgentOrchestrator(client=client, model="qwen/qwen3.8-27b")

    # 1. Test conversational message (No tools should trigger)
    conv_messages = [
        {"role": "system", "content": "You are a helpful AI assistant with tools."},
        {"role": "user", "content": "Hello! How are you today?"}
    ]
    tool_triggered = False
    ans = ""
    async for step, content, _ in orchestrator.run(conv_messages):
        if step == "status":
            tool_triggered = True
        elif step == "finished":
            ans = content
    record("Conversational Zero-Tool Routing", not tool_triggered and len(ans) > 5, f"(Tool triggered={tool_triggered})")

    # 2. Test Agentic Live Search Routing
    search_messages = [
        {"role": "system", "content": "You are a helpful AI assistant with tools. Use searxng_search when asked for current/live web information."},
        {"role": "user", "content": "Search what is the latest release date for GTA 6"}
    ]
    tool_triggered = False
    search_ans = ""
    async for step, content, _ in orchestrator.run(search_messages):
        if step == "status":
            tool_triggered = True
        elif step == "finished":
            search_ans = content
    record("Agentic Search Tool Selection", tool_triggered and len(search_ans) > 20, f"(Tool triggered={tool_triggered})")


async def main():
    print("========================================")
    print("SAUX BOT - AGENTIC CAPABILITIES VERIFICATION")
    print("========================================")
    print(f"Registered tools ({len(get_tool_schemas())}):")
    for s in get_tool_schemas():
        print(f"  • {s['function']['name']}: {s['function']['description'][:65]}...")

    await test_searxng()
    test_convert_capability()
    test_reclip_capability()
    await test_agent_orchestrator()

    print("\n========================================")
    print(f"TOTAL: {passed_count} PASSED, {failed_count} FAILED")
    print("========================================")
    assert failed_count == 0, f"{failed_count} tests failed!"


if __name__ == "__main__":
    asyncio.run(main())
