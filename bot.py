import asyncio
import logging
import os
import re
import uuid
from pathlib import Path
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ChatAction

from config import TELEGRAM_BOT_TOKEN, TEMP_DIR, DAILY_USER_LIMIT
from tools.image_tool import process_image
from tools.video_tool import get_media_info, download_media_custom
from tools.web_tool import search_web, get_live_web_context
from tools.openchamber_tool import is_openchamber_ready, generate_chat_reply
import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("telegram_agent_bot")

dp = Dispatcher()

# Concurrency limiters & cache
TASK_SEMAPHORE = asyncio.Semaphore(3)
URL_REGEX = re.compile(r"https?://\S+")
PENDING_DOWNLOADS = {}

def parse_image_intent(caption: str) -> dict:
    """Extracts agentic intent from natural language image captions."""
    text = caption.lower().strip()
    
    # 1. Stretch intent
    if "stretch" in text or "wide" in text:
        match = re.search(r"(\d+(\.\d+)?)\s*x", text)
        scale = float(match.group(1)) if match else 2.0
        return {"action": "stretch", "scale_x": scale, "scale_y": 1.0}
    
    # 2. Target size compression
    kb_match = re.search(r"(\d+)\s*(kb|k)", text)
    mb_match = re.search(r"(\d+(\.\d+)?)\s*mb", text)
    if kb_match:
        return {"action": "compress", "target_kb": int(kb_match.group(1))}
    elif mb_match:
        return {"action": "compress", "target_kb": int(float(mb_match.group(1)) * 1024)}
    
    # 3. Format conversion
    if "png" in text:
        return {"action": "convert", "target_format": "PNG"}
    elif "webp" in text:
        return {"action": "convert", "target_format": "WEBP"}
    elif "jpg" in text or "jpeg" in text:
        return {"action": "convert", "target_format": "JPEG"}
        
    return {"action": "compress", "target_kb": 500}

MINI_APP_URL = os.getenv("MINI_APP_URL", "https://saux.si")

def get_quick_action_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌐 Weightless Browser", url=f"{MINI_APP_URL}"),
            InlineKeyboardButton(text="📊 Quota", callback_data="act:quota"),
            InlineKeyboardButton(text="🔄 Reset", callback_data="act:reset")
        ]
    ])

@dp.callback_query(F.data.startswith("act:"))
async def handle_quick_actions(callback: CallbackQuery):
    action = callback.data.split(":")[1]
    user_id = callback.from_user.id
    
    if action == "quota":
        stats = db.get_user_stats(user_id, DAILY_USER_LIMIT)
        await callback.answer(
            f"⚡ Agent Usage Limit: {stats['remaining']} / {DAILY_USER_LIMIT} units remaining today.",
            show_alert=True
        )
    elif action == "reset":
        db.clear_user_session(user_id)
        await callback.answer("🔄 Session reset. Ready for a new task!", show_alert=True)

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    stats = db.get_user_stats(user_id, DAILY_USER_LIMIT)

    welcome_text = (
        "🤖 **Autonomous AI Agent**\n\n"
        f"⚡ **Agent Usage Limit:** `{stats['remaining']}` / `{DAILY_USER_LIMIT}` units available today.\n\n"
        "**Core Agentic Capabilities:**\n"
        "• 🧠 **Direct Agentic Tasks:** Ask any technical task, problem, or query (strict 1–3 line answers)\n"
        "• 🖼 **Image Operations:** Send a photo with caption `compress to 300kb`, `stretch 2x wide`, or `convert to png`\n"
        "• 🎬 **Media Extraction:** Send any YouTube, Shorts, TikTok, or Reels link — choose quality (1080p/720p/480p/360p), MP3, or thumbnail\n"
        "• 📷 **Post Downloader:** Send post link to download images/captions\n"
        "• 🔍 **Live Search Intelligence:** Send `search <topic>` for real-time web facts\n\n"
        "Commands:\n"
        "• /quota — View your current agent usage limit\n"
        "• /new or /reset — Reset agent context"
    )
    await message.reply(welcome_text, reply_markup=get_quick_action_keyboard(), parse_mode="Markdown")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "🛠 **Agent Commands & Usage:**\n\n"
        "1. **Agentic Tasks:** Simply type your task or question.\n"
        "2. **Compress Photo:** Send image with caption `compress this to 400kb`\n"
        "3. **Stretch Photo:** Send image with caption `stretch this 2x wide`\n"
        "4. **Download Video / Post:** Send any video/post URL ➔ Select Quality/Format from interactive menu\n"
        "5. **Web Search:** Send `search <query>`\n\n"
        "• /quota — Check remaining agent usage limit\n"
        "• /new — Reset conversation context"
    )
    await message.reply(help_text, reply_markup=get_quick_action_keyboard(), parse_mode="Markdown")

@dp.message(Command("reset"))
@dp.message(Command("new"))
async def cmd_reset(message: types.Message):
    user_id = message.from_user.id
    db.clear_user_session(user_id)
    await message.reply("🔄 **Agent session reset.** Ready for a fresh task.", parse_mode="Markdown")

@dp.message(Command("quota"))
@dp.message(Command("stats"))
async def cmd_quota(message: types.Message):
    stats = db.get_user_stats(message.from_user.id, DAILY_USER_LIMIT)
    await message.reply(
        f"📊 **Agent Usage Limit:**\n\n"
        f"• Units Utilized Today: `{stats['used_today']}`\n"
        f"• Units Available: `{stats['remaining']}` / `{DAILY_USER_LIMIT}`\n\n"
        "*(Resets automatically every 24 hours)*",
        parse_mode="Markdown"
    )

@dp.message(F.text.startswith("/"))
async def handle_unknown_command(message: types.Message):
    await message.reply(
        "❓ **Unknown command.**\n\n"
        "Tap the **Menu** / **[/]** button on your chatbar or use:\n"
        "• /start — Start the AI Agent\n"
        "• /quota — Check remaining agent limits\n"
        "• /new — Reset conversation context\n"
        "• /help — View full capabilities & guide",
        parse_mode="Markdown"
    )

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or ""

    allowed, remaining, limit = db.check_and_consume_quota(user_id, username, DAILY_USER_LIMIT)
    if not allowed:
        await message.reply(
            f"⚠️ **Agent Usage Limit Reached:** You have utilized your daily quota of `{limit}` units. Resets in 24 hours.",
            parse_mode="Markdown"
        )
        return

    caption = message.caption or "compress to 500kb"
    intent = parse_image_intent(caption)
    photo = message.photo[-1]
    
    status_msg = await message.reply("⚡ *Executing image operation...*", parse_mode="Markdown")
    
    temp_input = TEMP_DIR / f"in_{user_id}_{photo.file_id}.jpg"
    out_base = str(TEMP_DIR / f"proc_{user_id}_{photo.file_id}")
    files_to_clean = [temp_input]

    async with TASK_SEMAPHORE:
        try:
            file_info = await message.bot.get_file(photo.file_id)
            await message.bot.download_file(file_info.file_path, destination=temp_input)

            result = process_image(
                input_path=str(temp_input),
                output_path=out_base,
                action=intent.get("action", "compress"),
                scale_x=intent.get("scale_x", 1.0),
                scale_y=intent.get("scale_y", 1.0),
                target_kb=intent.get("target_kb", 500),
                target_format=intent.get("target_format")
            )
            out_file = Path(result["output_path"])
            files_to_clean.append(out_file)

            reply_caption = (
                f"✅ **Operation Complete**\n"
                f"• Dimensions: `{result['orig_dimensions']}` ➔ `{result['final_dimensions']}`\n"
                f"• Size: `{result['orig_size_kb']} KB` ➔ `{result['final_size_kb']} KB`\n"
                f"• Format: `{result['format']}`\n\n"
                f"*(⚡ Agent Usage Limit: `{remaining}` / `{DAILY_USER_LIMIT}` available)*"
            )
            await message.reply_document(
                document=FSInputFile(str(out_file)),
                caption=reply_caption,
                parse_mode="Markdown"
            )
            await status_msg.delete()

        except Exception as e:
            logger.error(f"Error handling photo: {e}", exc_info=True)
            await status_msg.edit_text(f"❌ Failed to process image: {str(e)}")
        finally:
            for f in files_to_clean:
                if f.exists():
                    try:
                        f.unlink()
                    except Exception:
                        pass

@dp.callback_query(F.data.startswith("dl:"))
async def handle_download_callback(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Invalid request.")
        return

    _, token, mode = parts
    data = PENDING_DOWNLOADS.get(token)
    if not data:
        await callback.answer("Session expired. Please paste the link again.", show_alert=True)
        return

    user_id = callback.from_user.id
    username = callback.from_user.username or ""

    allowed, remaining, limit = db.check_and_consume_quota(user_id, username, DAILY_USER_LIMIT)
    if not allowed:
        await callback.message.reply(
            f"⚠️ **Agent Usage Limit Reached:** You have utilized your daily quota of `{limit}` units. Resets in 24 hours.",
            parse_mode="Markdown"
        )
        await callback.answer()
        return

    mode_labels = {
        "1080p": "1080p Full HD",
        "720p": "720p HD",
        "480p": "480p Standard",
        "360p": "360p Data Saver",
        "mp3": "MP3 Audio",
        "thumbnail": "Thumbnail Image",
        "post_images": "Post Photo(s)"
    }
    label = mode_labels.get(mode, mode)
    await callback.message.edit_text(f"⚡ *Downloading {label}...*", parse_mode="Markdown")

    url = data["url"]
    thumb_url = data.get("thumb_url")
    user_out_dir = str(TEMP_DIR / f"dl_{user_id}_{token}")
    files_to_clean = []

    async with TASK_SEMAPHORE:
        try:
            result = download_media_custom(
                url=url,
                output_dir=user_out_dir,
                mode=mode,
                thumb_url=thumb_url,
                max_filesize_mb=48
            )
            file_path = Path(result["file_path"])
            files_to_clean.append(file_path)

            title = result.get("title", data["title"])
            size_mb = result.get("size_mb", 0)
            media_type = result.get("media_type", "video")

            caption = (
                f"✅ **{title}**\n"
                f"• Format: `{label}` | Size: `{size_mb} MB`\n"
                f"*(⚡ Agent Usage Limit: `{remaining}` / `{DAILY_USER_LIMIT}` available)*"
            )

            if media_type == "audio":
                await callback.message.reply_audio(
                    audio=FSInputFile(str(file_path)),
                    caption=caption,
                    title=title,
                    parse_mode="Markdown"
                )
            elif media_type == "image":
                await callback.message.reply_document(
                    document=FSInputFile(str(file_path)),
                    caption=caption,
                    parse_mode="Markdown"
                )
            else:
                await callback.message.reply_video(
                    video=FSInputFile(str(file_path)),
                    caption=caption,
                    parse_mode="Markdown"
                )

            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.answer("Download complete!")

        except Exception as e:
            logger.error(f"Error in custom download: {e}", exc_info=True)
            # CRITICAL: Clean error message. NEVER include raw yt-dlp github URLs or stacktraces!
            raw_err = str(e)
            clean_err = re.sub(r'https?://\S+', '', raw_err)
            clean_err = clean_err.replace("ERROR:", "").replace("[youtube]", "").replace("DownloadError:", "").strip()

            err_lower = raw_err.lower()
            if "not a bot" in err_lower or "sign in" in err_lower:
                user_msg = "⚠️ **YouTube Cloud Verification**\nYouTube cloud protection triggered for this video stream. Please try an alternate video link or audio mode."
            elif "unavailable" in err_lower or "not found" in err_lower or "private" in err_lower:
                user_msg = "⚠️ **Video Unavailable**\nThis video is deleted, private, invalid, or region-locked on YouTube."
            else:
                user_msg = f"❌ **Failed to download {label}:**\n{clean_err[:120]}"

            try:
                await callback.message.edit_text(user_msg, disable_web_page_preview=True, parse_mode="Markdown")
            except Exception:
                try:
                    await callback.message.edit_text(user_msg, disable_web_page_preview=True)
                except Exception:
                    pass
        finally:
            for f in files_to_clean:
                if f.exists():
                    try:
                        f.unlink()
                    except Exception:
                        pass
            PENDING_DOWNLOADS.pop(token, None)

@dp.message(F.text)
async def handle_text(message: types.Message):
    if message.text.startswith("/"):
        return

    user_id = message.from_user.id
    username = message.from_user.username or ""

    user_prompt = message.text.strip()
    detected_urls = URL_REGEX.findall(user_prompt)

    # 1. Interactive Video / Post / Audio Download Menu
    if detected_urls:
        url = detected_urls[0]
        status_msg = await message.reply("🔍 *Analyzing media URL...*", parse_mode="Markdown")
        try:
            info = get_media_info(url)
            token = uuid.uuid4().hex[:8]
            PENDING_DOWNLOADS[token] = {
                "url": url,
                "title": info["title"],
                "thumb_url": info["thumbnail"],
                "is_image_post": info["is_image_post"],
                "user_id": user_id
            }

            title = info["title"][:55]
            if info["is_image_post"]:
                # Post-only interactive options
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="📸 Download Photo(s)", callback_data=f"dl:{token}:post_images"),
                        InlineKeyboardButton(text="🖼 Thumbnail / Cover", callback_data=f"dl:{token}:thumbnail")
                    ]
                ])
                menu_text = (
                    f"📷 **Post Detected:**\n"
                    f"*{title}*\n\n"
                    f"Select what to download from this post:"
                )
            else:
                # Video interactive options
                duration_str = f" ({info['duration']}s)" if info['duration'] else ""
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🎬 1080p (Full HD)", callback_data=f"dl:{token}:1080p"),
                        InlineKeyboardButton(text="📹 720p (HD)", callback_data=f"dl:{token}:720p")
                    ],
                    [
                        InlineKeyboardButton(text="📱 480p (Medium)", callback_data=f"dl:{token}:480p"),
                        InlineKeyboardButton(text="⚡ 360p (Fast)", callback_data=f"dl:{token}:360p")
                    ],
                    [
                        InlineKeyboardButton(text="🎵 MP3 (Audio Only)", callback_data=f"dl:{token}:mp3"),
                        InlineKeyboardButton(text="🖼 Thumbnail Only", callback_data=f"dl:{token}:thumbnail")
                    ]
                ])
                menu_text = (
                    f"🎬 **Video Detected:**\n"
                    f"*{title}*{duration_str}\n\n"
                    f"Select your format & quality:"
                )

            await status_msg.edit_text(menu_text, reply_markup=keyboard, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error inspecting media: {e}", exc_info=True)
            err_lower = str(e).lower()
            if "sign in to confirm" in err_lower or "confirm you're not a bot" in err_lower:
                user_msg = "⚠️ **YouTube Cloud Protection**\nYouTube cloud bot challenge triggered on this video. Please try another video or retry shortly."
            elif "unavailable" in err_lower or "not found" in err_lower or "404" in err_lower or "private" in err_lower:
                user_msg = "⚠️ **Video Unavailable**\nThis video is deleted, private, or region-locked on YouTube."
            else:
                user_msg = f"❌ **Could not analyze media link:**\n`{str(e)[:150]}`"
            try:
                await status_msg.edit_text(user_msg, reply_markup=get_quick_action_keyboard(), disable_web_page_preview=True, parse_mode="Markdown")
            except Exception:
                await status_msg.edit_text(user_msg, reply_markup=get_quick_action_keyboard(), disable_web_page_preview=True)
        return

    # Quota Check for text/search/agent tasks
    allowed, remaining, limit = db.check_and_consume_quota(user_id, username, DAILY_USER_LIMIT)
    if not allowed:
        await message.reply(
            f"⚠️ **Agent Usage Limit Reached:** You have utilized your daily quota of `{limit}` units. Resets in 24 hours.",
            parse_mode="Markdown"
        )
        return

    # 2. Live Web Search Task
    if user_prompt.lower().startswith("search "):
        query = re.sub(r"^search\s+", "", user_prompt, flags=re.IGNORECASE)
        status_msg = await message.reply("🔍 *Querying live web...*", parse_mode="Markdown")
        try:
            results = search_web(query)
            reply_text = f"🌐 **Search Results for:** `{query}`\n\n{results}\n\n*(⚡ Agent Usage Limit: `{remaining}` / `{DAILY_USER_LIMIT}` available)*"
            try:
                await status_msg.edit_text(reply_text, reply_markup=get_quick_action_keyboard(), parse_mode="Markdown")
            except Exception:
                await status_msg.edit_text(reply_text, reply_markup=get_quick_action_keyboard())
        except Exception as e:
            await status_msg.edit_text(f"❌ Search error: {str(e)}")
        return

    # 3. Agent Execution (Strict 1-3 lines, inkling-2-small-free)
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    existing_session = db.get_user_session(user_id)
    
    # Check if query asks for factual/real-time/news information
    query_lower = user_prompt.lower()
    needs_live_data = any(k in query_lower for k in [
        "who", "what", "where", "when", "how", "cm", "pm", "chief minister", "president",
        "news", "trending", "latest", "today", "current", "update", "scandal", "price",
        "weather", "score", "election", "minister", "ceo", "status", "?"
    ])

    prompt_for_agent = user_prompt
    session_to_use = existing_session

    if needs_live_data:
        live_facts = get_live_web_context(user_prompt)
        if live_facts:
            prompt_for_agent = (
                f"<live-web-data>\n{live_facts}\n</live-web-data>\n\n"
                f"Question: {user_prompt}\n"
                "Answer factually using the verified live web data above. Max 3 lines."
            )
            # Fresh session avoids stale refusal baggage
            session_to_use = None

    try:
        reply_text, session_id = await generate_chat_reply(
            session_id=session_to_use,
            prompt=prompt_for_agent
        )
        if session_id:
            db.set_user_session(user_id, session_id)
        
        reply_with_quota = f"{reply_text}\n\n*(⚡ Agent Usage Limit: `{remaining}` / `{DAILY_USER_LIMIT}` available)*"
        try:
            await message.reply(reply_with_quota, reply_markup=get_quick_action_keyboard(), parse_mode="Markdown")
        except Exception:
            await message.reply(reply_with_quota, reply_markup=get_quick_action_keyboard())

    except Exception as e:
        logger.error(f"Error calling agent engine: {e}", exc_info=True)
        db.clear_user_session(user_id)
        await message.reply(
            f"⚠️ Agent engine error: {str(e)}"
        )

async def main():
    if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
        logger.error("\n[!] TELEGRAM_BOT_TOKEN is missing or invalid in .env!\n")
        return

    ready = await is_openchamber_ready()
    if ready:
        logger.info("⚡ Inkling-2-Small Agentic Engine is connected and READY!")
    else:
        logger.warning("⚠️ OpenChamber desktop not detected on default port.")

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    logger.info("Registering Telegram Chatbar Slash Commands Menu...")
    commands = [
        types.BotCommand(command="start", description="🤖 Start the AI Agent"),
        types.BotCommand(command="quota", description="📊 Check Agent Usage Limit"),
        types.BotCommand(command="new", description="🔄 Reset Conversation Session"),
        types.BotCommand(command="help", description="🛠 Capabilities & Guide"),
    ]
    await bot.set_my_commands(commands)
    try:
        from aiogram.types import MenuButtonWebApp, WebAppInfo
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="🌐 Weightless Browser",
                web_app=WebAppInfo(url="https://saux.si")
            )
        )
        logger.info("Successfully registered Telegram Bot Menu Button -> 🌐 Weightless Browser (https://saux.si)")
    except Exception as e:
        logger.warning(f"Could not set chat menu button: {e}")
    logger.info("Starting Autonomous Agentic Telegram Bot...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
