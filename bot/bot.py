import io
import re
import time
import uuid
import logging
from pathlib import Path
import asyncio
import traceback
import html
import json
from datetime import datetime, timedelta
import urllib.parse
import openai
from collections import OrderedDict

import telegram
from telegram import (
    Update,
    User,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    BotCommand,
    LabeledPrice,
    MenuButtonCommands,
    MenuButtonWebApp,
    WebAppInfo,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    AIORateLimiter,
    filters
)
from telegram.constants import ParseMode, ChatAction
from telegram.request import HTTPXRequest

import config
from services.queue_manager import video_download_queue, media_convert_queue
import database
import openai_utils
from saux_config import (
    format_video_caption,
    format_ai_reply,
    validate_telegram_init_data,
    OFFICIAL_CHANNEL_URL,
    OFFICIAL_SUPERGROUP_URL,
    MINI_APP_NAME,
    MINI_APP_URL,
    PREMIUM_PLANS
)

import base64

# setup
db = database.Database()
logger = logging.getLogger(__name__)

user_semaphores = {}
user_tasks = {}

class _LRUCache(OrderedDict):
    """Bounded LRU cache to prevent memory leaks in high-traffic deployments."""
    def __init__(self, maxsize=2000):
        super().__init__()
        self.maxsize = maxsize

    def get_time(self, key: int) -> float:
        return self.get(key, 0.0)

    def record(self, key: int, timestamp: float):
        if key in self:
            self.move_to_end(key)
        self[key] = timestamp
        if len(self) > self.maxsize:
            self.popitem(last=False)

user_media_cooldowns = _LRUCache(maxsize=2000)
MEDIA_COOLDOWN_SECONDS: float = 3.0
MEDIA_URL_REGEX = re.compile(
    r'https?://(?:www\.)?(?:youtube\.com|youtu\.be|instagram\.com|tiktok\.com|twitter\.com|x\.com|fb\.watch|facebook\.com|reddit\.com|pinterest\.com)',
    re.IGNORECASE
)

HELP_MESSAGE = """Commands:
⚪ /retry – Regenerate last bot answer
⚪ /new – Start new dialog
⚪ /mode – Select chat mode
⚪ /settings – Show settings
⚪ /balance – Show balance
⚪ /help – Show help

🎨 Generate images from text prompts in <b>👩‍🎨 Artist</b> /mode
👥 Add bot to <b>group chat</b>: /help_group_chat
🎤 You can send <b>Voice Messages</b> instead of text
"""

HELP_GROUP_CHAT_MESSAGE = """You can add bot to any <b>group chat</b> to help and entertain its participants!

Instructions (see <b>video</b> below):
1. Add the bot to the group chat
2. Make it an <b>admin</b>, so that it can see messages (all other rights can be restricted)
3. You're awesome!

To get a reply from the bot in the chat – @ <b>tag</b> it or <b>reply</b> to its message.
For example: "{bot_username} write a poem about Telegram"
"""


def split_text_into_chunks(text, chunk_size):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


async def register_user_if_not_exists(update: Any, context: CallbackContext, user: User):
    if user is None:
        return

    effective_chat = getattr(update, "effective_chat", None)
    if effective_chat:
        chat_id = effective_chat.id
    elif hasattr(update, "message") and getattr(update.message, "chat", None):
        chat_id = update.message.chat.id
    else:
        chat_id = user.id

    if not await db.check_if_user_exists(user.id):
        await db.add_new_user(
            user.id,
            chat_id,
            username=user.username or "",
            first_name=user.first_name or "",
            last_name=user.last_name or ""
        )
        await db.start_new_dialog(user.id)

    # Ensure bottom-left corner has the Crate mini app button
    try:
        eff_chat = getattr(update, "effective_chat", None) or (getattr(update, "message", None) and getattr(update.message, "chat", None))
        if eff_chat and getattr(eff_chat, "type", None) == "private":
            await context.bot.set_chat_menu_button(
                chat_id=user.id,
                menu_button=MenuButtonWebApp(text="Crate", web_app=WebAppInfo(url="https://saux.si/"))
            )
    except Exception:
        pass

    if await db.get_user_attribute(user.id, "current_dialog_id") is None:
        await db.start_new_dialog(user.id)

    if user.id not in user_semaphores:
        user_semaphores[user.id] = asyncio.Semaphore(1)

    if await db.get_user_attribute(user.id, "current_model") is None:
        await db.set_user_attribute(user.id, "current_model", config.models["available_text_models"][0])

    # back compatibility for n_used_tokens field
    n_used_tokens = await db.get_user_attribute(user.id, "n_used_tokens")
    if isinstance(n_used_tokens, (int, float)):  # old format
        new_n_used_tokens = {
            "gpt-3.5-turbo": {
                "n_input_tokens": 0,
                "n_output_tokens": n_used_tokens
            }
        }
        await db.set_user_attribute(user.id, "n_used_tokens", new_n_used_tokens)

    # voice message transcription
    if await db.get_user_attribute(user.id, "n_transcribed_seconds") is None:
        await db.set_user_attribute(user.id, "n_transcribed_seconds", 0.0)

    # image generation
    if await db.get_user_attribute(user.id, "n_generated_images") is None:
        await db.set_user_attribute(user.id, "n_generated_images", 0)


async def is_bot_mentioned(update: Update, context: CallbackContext):
     try:
         message = update.message

         if message.chat.type == "private":
             return True

         if message.text is not None and ("@" + context.bot.username) in message.text:
             return True

         if message.reply_to_message is not None:
             if message.reply_to_message.from_user.id == context.bot.id:
                 return True
     except Exception:
         return True
     else:
         return False


def get_main_reply_keyboard(user_id: int = None):
    # Both Free & Premium users have immediate access to 'Your Earnings'
    return ReplyKeyboardMarkup(
        [
            ["👤 Profile", "🎭 AI Chat Modes"],
            ["👥 Strangerspace ↗", "✨ Commands"],
            ["💰 Your Earnings"]
        ],
        resize_keyboard=True,
        is_persistent=True
    )


def get_commands_hub_card(user_id: int = None):
    text = (
        "⚡ <b>Quick Commands Hub</b> 🚀\n\n"
        "Tap any blue link or button below to run commands with <b>zero typing</b>:\n\n"
        "• /new — Start a fresh AI dialog (clears memory)\n"
        "• /mode — Switch AI chat modes & personas 🎭\n"
        "• /profile — View your account, usage & Stars balance 👤\n"
        "• /premium — Pay with Stars to unlock VIP speed & perks ⭐\n"
        "• /earn — Telegram Affiliate Program & 50% Stars commission 💰\n"
        "• /crate — Open your media dashboard & tools 🗂️\n"
        "• /settings — Adjust AI model & parameters ⚙️\n"
        "• /help — Full instructions & features guide ℹ️\n\n"
        "<i>Or tap the 1-touch buttons below:</i>"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 New Dialog", callback_data="start_new_dialog_hub"),
            InlineKeyboardButton("🎭 AI Modes", callback_data="show_chat_modes")
        ],
        [
            InlineKeyboardButton("👤 Profile", callback_data="show_profile_btn"),
            InlineKeyboardButton("⭐ Pay with Stars", callback_data="open_premium_plans")
        ],
        [
            InlineKeyboardButton("💰 Your Earnings", callback_data="open_unlock_earn"),
            InlineKeyboardButton("⚙️ Settings", callback_data="open_settings_hub")
        ],
        [
            InlineKeyboardButton("🗂️ Launch Crate", web_app=WebAppInfo(url="https://saux.si/")),
            InlineKeyboardButton("👥 Strangerspace ↗", url=OFFICIAL_SUPERGROUP_URL)
        ]
    ])
    return text, keyboard


def get_about_card(user_id: int = None):
    text = (
        "🚀 <b>SAUX Super Intelligence</b> ✨\n\n"
        "Your all-in-one AI & high-speed media downloader on Telegram.\n\n"
        "⚡ <b>What I do:</b>\n"
        "• <b>Fast Video Fetcher:</b> Send any YouTube, Instagram, TikTok, or X link\n"
        "• <b>Audio & Trimmer:</b> Extract MP3 tracks & clip precise timestamps\n"
        "• <b>Smart AI & Web:</b> Factual search, 120B reasoning & universal file conversion\n\n"
        "<i>Paste a video link, ask a question, or choose an option below:</i>"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 Profile", callback_data="show_profile_btn"),
            InlineKeyboardButton("🎭 AI Chat Modes", callback_data="show_chat_modes")
        ],
        [
            InlineKeyboardButton("👥 Strangerspace ↗", url=OFFICIAL_SUPERGROUP_URL),
            InlineKeyboardButton("✨ Commands", callback_data="open_commands_hub")
        ],
        [
            InlineKeyboardButton("💰 Your Earnings", callback_data="open_unlock_earn")
        ]
    ])
    return text, keyboard


async def get_unlock_and_earn_card(user_id: int):
    # Telegram Stars Affiliate Program Hub
    # Open to every user: Free & Premium alike
    is_premium = await db.is_user_premium(user_id)
    info = await db.get_affiliate_info(user_id)
    ref_count = info.get("referral_count", 0)
    stars_earned = info.get("stars_earned", 0)
    
    bot_username = "sauxsibot"
    # Guaranteed trackable links tied to user_id
    ref_link = f"https://t.me/{bot_username}?start=aff_{user_id}"
    mini_app_ref_link = f"https://t.me/{bot_username}/crate?startapp=aff_{user_id}"
    
    share_text = urllib.parse.quote("Join SAUX Super Intelligence & Crate — Autonomous Media & 120B AI on Telegram! 🚀")
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(ref_link)}&text={share_text}"

    account_badge = "👑 Premium VIP" if is_premium else "🌱 Free Account"

    text = (
        "⭐ <b>Telegram Stars Affiliate Program</b> 🚀\n\n"
        f"• <b>Account Status:</b> {account_badge}\n"
        "• <b>Affiliate Status:</b> 🏆 <b>Official Partner</b> (50% Commission)\n"
        "• <b>Commission Rate:</b> <b>50% (500‰)</b> on all Telegram Stars purchases\n"
        "• <b>Payout Destination:</b> Direct to your <b>Telegram Star Balance</b>\n"
        "• <b>Withdrawals:</b> Converted to <b>TON / USD</b> via <b>Fragment</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔗 <b>Your Guaranteed Tracking Links:</b>\n\n"
        "🤖 <b>Bot Direct Link:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        "📱 <b>Mini App Direct Link:</b>\n"
        f"<code>{mini_app_ref_link}</code>\n\n"
        "<i>💡 Tap either link above to copy it instantly. Every user who clicks your link is permanently credited to your account!</i>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>Your Live Affiliate Stats:</b>\n"
        f"• ⭐ <b>Commissions Earned:</b> <b>{stars_earned} ⭐</b>\n"
        f"• 👥 <b>Referred Users:</b> <b>{ref_count} users</b>\n"
        "• 💸 <b>Reward Rate:</b> <b>50% of every Stars transaction</b>\n\n"
        "ℹ️ <b>How it works:</b>\n"
        "1. Tap <b>'🚀 Share Referral Link'</b> below to share with your friends, groups, or channels.\n"
        "2. When someone joins and purchases Premium with Stars, you receive 50% commission automatically.\n"
        "3. You can also view your native Telegram Star stats in <b>Telegram Settings → My Stars</b>."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Share Referral Link", url=share_url)
        ],
        [
            InlineKeyboardButton("⭐ Pay with Stars", callback_data="open_premium_plans"),
            InlineKeyboardButton("👤 View Profile", callback_data="show_profile_btn")
        ],
        [
            InlineKeyboardButton("👥 Strangerspace ↗", url=OFFICIAL_SUPERGROUP_URL),
            InlineKeyboardButton("« Back", callback_data="back_to_about")
        ]
    ])

    return text, keyboard


async def get_premium_plans_card(user_id: int = None):
    status_header = ""
    if user_id:
        p_info = await db.get_premium_info(user_id)
        if p_info.get("is_premium"):
            days = p_info.get("days_remaining", 0)
            expiry = p_info.get("expiry_date", "Active")
            tier = p_info.get("tier_name", "VIP")
            status_header = (
                f"👑 <b>YOUR CURRENT STATUS: ACTIVE ({tier})</b>\n"
                f"• <b>Status:</b> Premium VIP (100% Ad-Free)\n"
                f"• <b>Valid Until:</b> {expiry} ({days} days remaining)\n\n"
                f"<i>You can extend or renew your subscription below:</i>\n\n"
            )
        else:
            status_header = (
                "🆓 <b>YOUR CURRENT STATUS: FREE TIER</b>\n"
                "• <b>Status:</b> Standard limits & ad-supported\n"
                "• <b>Upgrade:</b> Pay with Stars to unlock instant VIP speeds & maximum limits!\n\n"
            )

    stars_tip = (
        "💡 <i>Tip: You may be able to buy Telegram Stars at a lower price through Telegram Web, @PremiumBot or Fragment instead of in-app purchases. Compare prices before buying, as availability and pricing may vary by platform and region.</i>"
    )

    text = (
        f"{status_header}"
        "⭐ <b>SAUX Super Intelligence Premium</b> ⭐\n\n"
        "Upgrade to maximum limits and priority cloud performance using <b>Telegram Stars</b>!\n\n"
        "<b>💎 Premium Capabilities:</b>\n"
        "• 🚀 <b>Unlimited Video Downloads:</b> Zero daily quota or throttling across all sites\n"
        "• ⚡ <b>VIP Priority Queue:</b> Instant processing for videos, audio, and file conversion\n"
        "• 🧠 <b>Unconstrained AI Reasoning:</b> Maximum token limits with 120B parameter model\n"
        "• 🚫 <b>Ad-Free Experience:</b> All promotional captions & footers completely removed\n"
        "• 🌐 <b>Deep Web Search:</b> Multi-source live information retrieval\n"
        "• 👑 <b>VIP Status Badge:</b> Priority support and early access to new agent tools\n\n"
        "<b>Select a Telegram Stars plan to activate instantly:</b>\n\n"
        f"{stars_tip}"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ 50 Stars — 1 Week Starter Pass", callback_data="buy_stars_50"),
        ],
        [
            InlineKeyboardButton("⭐ 150 Stars — 1 Month Pro Pass", callback_data="buy_stars_150"),
        ],
        [
            InlineKeyboardButton("⭐ 350 Stars — Lifetime VIP Access", callback_data="buy_stars_350"),
        ],
        [
            InlineKeyboardButton("👥 Strangerspace ↗", url=OFFICIAL_SUPERGROUP_URL),
            InlineKeyboardButton("« Back to Overview", callback_data="back_to_about"),
        ]
    ])
    return text, keyboard


async def premium_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    text, keyboard = await get_premium_plans_card(user_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def about_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    text, keyboard = get_about_card(user_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def crate_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    p_info = await db.get_premium_info(user_id)
    prem_badge = "👑 Premium VIP" if p_info.get("is_premium") else "🆓 Free Tier"
    text = (
        "🗂️ <b>Welcome to Crate</b> — <i>Your Autonomous Media Dashboard</i>\n\n"
        "Crate lets you organize and access your media files, download history, and conversion tools directly inside Telegram!\n\n"
        f"• <b>Active Account:</b> {update.message.from_user.full_name}\n"
        f"• <b>Membership:</b> {prem_badge}\n"
        f"• 📥 <b>Stored Media:</b> Quick access to downloaded videos & audio tracks\n"
        f"• ✂️ <b>Trimmer & Converter:</b> Instant audio extraction and format conversion\n"
        f"• ⭐ <b>Subscription Management:</b> Top up or check Telegram Stars balance with /premium\n\n"
        f"Tap the bottom menu button <b>Crate</b> to open anytime!"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗂️ Launch Crate", web_app=WebAppInfo(url="https://saux.si/")),
            InlineKeyboardButton("👥 Strangerspace ↗", url=OFFICIAL_SUPERGROUP_URL)
        ],
        [
            InlineKeyboardButton("⭐ Pay with Stars", callback_data="open_premium_plans")
        ]
    ])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def web_app_data_handle(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    raw_data = update.message.web_app_data.data if update.message.web_app_data else ""
    logger.info(f"Received web_app_data from user {user_id}: {raw_data}")
    p_info = await db.get_premium_info(user_id)
    status_label = "👑 Premium VIP" if p_info.get("is_premium") else "🆓 Free Tier"
    reply_msg = (
        f"🗂️ <b>Crate Sync Completed</b>\n\n"
        f"Status: <b>{status_label}</b>\n"
        f"Data synced successfully with Telegram backend."
    )
    await update.message.reply_text(reply_msg, parse_mode=ParseMode.HTML)


async def premium_callback_handle(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if data in ("open_premium_plans", "buy_premium_stars"):
        text, keyboard = await get_premium_plans_card(user_id)
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception:
            await query.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    elif data == "open_crate_info":
        p_info = await db.get_premium_info(user_id)
        prem_badge = "👑 Premium VIP" if p_info.get("is_premium") else "🆓 Free Tier"
        text = (
            "🗂️ <b>SAUX Crate Mini App</b>\n\n"
            f"• <b>Account:</b> {query.from_user.full_name}\n"
            f"• <b>Tier:</b> {prem_badge}\n"
            "• <b>Access:</b> Tap the persistent bottom bar button <b>[ 🗂️ SAUX Crate ]</b> anytime to manage your media crate!\n\n"
            f"Join our community for crate templates and new features:\n"
            f"📢 <a href=\"{OFFICIAL_CHANNEL_URL}\">StrangerSpace Channel</a>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Channel", url=OFFICIAL_CHANNEL_URL), InlineKeyboardButton("👥 Super Group", url=OFFICIAL_SUPERGROUP_URL)],
            [InlineKeyboardButton("« Back", callback_data="back_to_about")]
        ])
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception:
            await query.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    elif data == "back_to_about":
        text, keyboard = get_about_card(user_id)
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception:
            await query.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    elif data in ("show_balance_btn", "show_profile_btn"):
        await show_balance_handle(update, context)
    elif data == "open_unlock_earn":
        text, keyboard = await get_unlock_and_earn_card(user_id)
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception:
            await query.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    elif data == "open_commands_hub":
        text, keyboard = get_commands_hub_card(user_id)
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception:
            await query.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    elif data == "start_new_dialog_hub":
        await db.start_new_dialog(user_id)
        chat_mode = await db.get_user_attribute(user_id, "current_chat_mode")
        mode_name = config.chat_modes.get(chat_mode, {}).get("name", "General")
        try:
            await query.answer("Starting new dialog ✅")
        except Exception:
            pass
        await query.message.reply_text(
            f"🔄 <b>New dialog started!</b>\n\nCurrent mode: <b>{mode_name}</b>\nPrevious conversation history has been cleared.",
            parse_mode=ParseMode.HTML
        )
    elif data == "open_settings_hub":
        text, reply_markup = await get_settings_menu(user_id)
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    elif data.startswith("buy_stars_"):
        plan_meta = PREMIUM_PLANS.get(data)
        if plan_meta:
            stars = plan_meta["stars"]
            title = plan_meta["name"]
            desc = plan_meta["description"]
            try:
                await context.bot.send_invoice(
                    chat_id=chat_id,
                    title=f"SAUX Premium: {title}",
                    description=desc,
                    payload=f"saux_premium_{stars}_{user_id}",
                    currency="XTR",
                    prices=[LabeledPrice(f"{title} ({stars} ⭐)", stars)],
                    provider_token="",  # Telegram Stars requires empty provider_token
                )
            except Exception as e:
                logger.error(f"Error creating Telegram Stars invoice: {e}")
                await query.message.reply_text(f"⚠️ Could not generate invoice: {e}")


async def precheckout_handle(update: Update, context: CallbackContext):
    query = update.pre_checkout_query
    if query.invoice_payload.startswith("saux_premium"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Invalid payment request.")


async def successful_payment_handle(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    payment = update.message.successful_payment
    stars_amount = payment.total_amount
    payload = payment.invoice_payload

    days = 30
    tier_name = "1 Month Pro Pass"
    if "_50_" in payload:
        days = 7
        tier_name = "1 Week Starter Pass"
    elif "_350_" in payload:
        days = 3650  # Lifetime (10 years)
        tier_name = "Lifetime VIP Access"

    await db.set_user_premium(user_id, days=days, stars_paid=stars_amount, tier_name=tier_name)
    p_info = await db.get_premium_info(user_id)
    expiry_date = p_info.get("expiry_date", "")

    success_text = (
        f"🎉 <b>Welcome to SAUX Premium!</b> 👑\n\n"
        f"Thank you for supporting us with <b>{stars_amount} ⭐ Telegram Stars</b>!\n\n"
        f"<b>Active Membership:</b> {tier_name}\n"
        f"<b>Valid Until:</b> {expiry_date}\n\n"
        f"<b>Your Unlocked Perks:</b>\n"
        f"• 🚀 Unlimited ultra-fast video downloads (zero throttling)\n"
        f"• ⚡ Top priority processing in cloud worker queue\n"
        f"• 🧠 Unconstrained 120B AI reasoning & search limits\n"
        f"• 🚫 100% Ad-Free experience (all promotional bars disabled)\n"
        f"• 📢 Exclusive access to VIP community perks\n\n"
        f"Join Strangerspace:\n"
        f"👥 <a href=\"{OFFICIAL_SUPERGROUP_URL}\">Strangerspace ↗</a>"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 Strangerspace ↗", url=OFFICIAL_SUPERGROUP_URL),
            InlineKeyboardButton("⚡ Try a Video Link", switch_inline_query_current_chat=""),
        ]
    ])
    await update.message.reply_text(success_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    try:
        reply_kb = get_main_reply_keyboard(user_id)
        await update.message.reply_text(
            "👑 <i>Your navigation bar has been updated with active Premium features:</i>",
            reply_markup=reply_kb,
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

    # Credit 50% lifetime commission to referrer if paying user was invited
    try:
        referrer_id, commission = await db.credit_referral_commission(user_id, stars_amount)
        if referrer_id and commission > 0:
            await context.bot.send_message(
                chat_id=referrer_id,
                text=(
                    "⭐ <b>Affiliate Commission Earned!</b> 💰\n\n"
                    f"A friend you referred just purchased a Premium Pass ({stars_amount} ⭐)!\n"
                    f"Your <b>50% commission</b> of <b>+{commission} ⭐</b> has been credited to your affiliate balance!\n\n"
                    "Tap <b>💰 Your Earnings</b> on your chatbar to check your balance."
                ),
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        logger.warning(f"Could not credit referral commission: {e}")


async def start_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id

    await db.set_user_attribute(user_id, "last_interaction", datetime.now())
    await db.start_new_dialog(user_id)

    # Handle native affiliate & referral links: /start aff_<referrer_id>, ref_<id>, app_<id>, or raw ID
    if context.args and len(context.args) > 0:
        arg = context.args[0].strip()
        ref_id_str = None
        if arg.startswith("aff_"):
            ref_id_str = arg[4:]
        elif arg.startswith("ref_"):
            ref_id_str = arg[4:]
        elif arg.startswith("app_"):
            ref_id_str = arg[4:]
        elif arg.isdigit():
            ref_id_str = arg

        if ref_id_str and ref_id_str.isdigit():
            try:
                referrer_id = int(ref_id_str)
                if referrer_id != user_id and await db.check_if_user_exists(referrer_id):
                    is_new = await db.record_referral(user_id, referrer_id)
                    if is_new:
                        ref_info = await db.get_affiliate_info(referrer_id)
                        try:
                            await context.bot.send_message(
                                chat_id=referrer_id,
                                text=(
                                    "👥 <b>New Referral Alert!</b> 🚀\n\n"
                                    f"<b>{html.escape(update.message.from_user.first_name)}</b> joined using your affiliate link!\n\n"
                                    f"• <b>Total Referrals:</b> <b>{ref_info['referral_count']} users</b>\n"
                                    f"• <b>Commission Rate:</b> <b>50% (500‰)</b> on eligible Telegram Stars purchases\n\n"
                                    "<i>Commissions are credited automatically as Telegram Stars.</i>"
                                ),
                                parse_mode=ParseMode.HTML
                            )
                        except Exception as e:
                            logger.warning(f"Could not notify referrer {referrer_id}: {e}")
            except Exception as e:
                logger.warning(f"Error parsing referral argument {arg}: {e}")

    text, keyboard = get_about_card(user_id)
    reply_kb = get_main_reply_keyboard(user_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    try:
        await update.message.reply_text(
            "👇 <i>Quick navigation buttons:</i>",
            reply_markup=reply_kb,
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass


async def help_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    await db.set_user_attribute(user_id, "last_interaction", datetime.now())
    text, keyboard = get_about_card(user_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def help_group_chat_handle(update: Update, context: CallbackContext):
     await register_user_if_not_exists(update, context, update.message.from_user)
     user_id = update.message.from_user.id
     await db.set_user_attribute(user_id, "last_interaction", datetime.now())

     text = HELP_GROUP_CHAT_MESSAGE.format(bot_username="@" + context.bot.username)

     await update.message.reply_text(text, parse_mode=ParseMode.HTML)
     await update.message.reply_video(config.help_group_chat_video_path)


async def retry_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    await db.set_user_attribute(user_id, "last_interaction", datetime.now())

    dialog_messages = await db.get_dialog_messages(user_id, dialog_id=None)
    if len(dialog_messages) == 0:
        await update.message.reply_text("No message to retry 🤷‍♂️")
        return

    last_dialog_message = dialog_messages.pop()
    await db.set_dialog_messages(user_id, dialog_messages, dialog_id=None)  # last message was removed from the context

    await message_handle(update, context, message=last_dialog_message["user"], use_new_dialog_timeout=False)

async def _vision_message_handle_fn(
    update: Update, context: CallbackContext, use_new_dialog_timeout: bool = True
):
    logger.info('_vision_message_handle_fn')
    user_id = update.message.from_user.id
    current_model = await db.get_user_attribute(user_id, "current_model")

    if not config.models["info"][current_model].get("vision", False):
        await update.message.reply_text(
            "🥲 Image understanding is only available for <b>vision-capable</b> models (e.g. GPT-4o, GPT-4o mini, GPT-5.5 or Claude). Please change your model in /settings",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_mode = await db.get_user_attribute(user_id, "current_chat_mode")

    # new dialog timeout
    if use_new_dialog_timeout:
        last_int = await db.get_user_attribute(user_id, "last_interaction")
        cur_d_msgs = await db.get_dialog_messages(user_id)
        if last_int and (datetime.now() - last_int).seconds > config.new_dialog_timeout and len(cur_d_msgs) > 0:
            await db.start_new_dialog(user_id)
            await update.message.reply_text(f"Starting new dialog due to timeout (<b>{config.chat_modes[chat_mode]['name']}</b> mode) ✅", parse_mode=ParseMode.HTML)
    await db.set_user_attribute(user_id, "last_interaction", datetime.now())

    buf = None
    if update.message.effective_attachment:
        photo = update.message.effective_attachment[-1]
        photo_file = await context.bot.get_file(photo.file_id)

        # store file in memory, not on disk
        buf = io.BytesIO()
        await photo_file.download_to_memory(buf)
        buf.name = "image.jpg"  # file extension is required
        buf.seek(0)  # move cursor to the beginning of the buffer

    # in case of CancelledError
    n_input_tokens, n_output_tokens = 0, 0

    try:
        # send placeholder message to user
        placeholder_message = await update.message.reply_text("...")
        message = update.message.caption or update.message.text or ''

        # send typing action
        await update.message.chat.send_action(action="typing")

        dialog_messages = await db.get_dialog_messages(user_id, dialog_id=None)
        parse_mode = {"html": ParseMode.HTML, "markdown": ParseMode.MARKDOWN}[
            config.chat_modes[chat_mode]["parse_mode"]
        ]

        chatgpt_instance = openai_utils.ChatGPT(model=current_model)
        if config.enable_message_streaming:
            gen = chatgpt_instance.send_vision_message_stream(
                message,
                dialog_messages=dialog_messages,
                image_buffer=buf,
                chat_mode=chat_mode,
            )
        else:
            (
                answer,
                (n_input_tokens, n_output_tokens),
                n_first_dialog_messages_removed,
            ) = await chatgpt_instance.send_vision_message(
                message,
                dialog_messages=dialog_messages,
                image_buffer=buf,
                chat_mode=chat_mode,
            )

            async def fake_gen():
                yield "finished", answer, (
                    n_input_tokens,
                    n_output_tokens,
                ), n_first_dialog_messages_removed

            gen = fake_gen()

        prev_answer = ""
        async for gen_item in gen:
            (
                status,
                answer,
                (n_input_tokens, n_output_tokens),
                n_first_dialog_messages_removed,
            ) = gen_item

            answer = answer[:4096]  # telegram message limit

            # update only when 100 new symbols are ready
            if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                continue

            try:
                await context.bot.edit_message_text(
                    answer,
                    chat_id=placeholder_message.chat_id,
                    message_id=placeholder_message.message_id,
                    parse_mode=parse_mode,
                )
            except telegram.error.BadRequest as e:
                if str(e).startswith("Message is not modified"):
                    continue
                else:
                    await context.bot.edit_message_text(
                        answer,
                        chat_id=placeholder_message.chat_id,
                        message_id=placeholder_message.message_id,
                    )

            await asyncio.sleep(0.01)  # wait a bit to avoid flooding

            prev_answer = answer

        # update user data
        if buf is not None:
            base_image = base64.b64encode(buf.getvalue()).decode("utf-8")
            new_dialog_message = {"user": [
                        {
                            "type": "text",
                            "text": message,
                        },
                        {
                            "type": "image",
                            "image": base_image,
                        }
                    ]
                , "bot": answer, "date": datetime.now()}
        else:
            new_dialog_message = {"user": [{"type": "text", "text": message}], "bot": answer, "date": datetime.now()}
        
        prev_msgs = await db.get_dialog_messages(user_id, dialog_id=None)
        await db.set_dialog_messages(
            user_id,
            prev_msgs + [new_dialog_message],
            dialog_id=None
        )

        await db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

    except asyncio.CancelledError:
        # note: intermediate token updates only work when enable_message_streaming=True (config.yml)
        await db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
        raise

    except Exception as e:
        error_text = f"Something went wrong during completion. Reason: {e}"
        logger.error(error_text)
        await update.message.reply_text(error_text)
        return

async def unsupport_message_handle(update: Update, context: CallbackContext, message=None):
    # check if bot was mentioned (for group chats)
    if not await is_bot_mentioned(update, context):
        return

    error_text = f"I don't know how to read files or videos. Send the picture in normal mode (Quick Mode)."
    logger.error(error_text)
    await update.message.reply_text(error_text)
    return

async def message_handle(update: Update, context: CallbackContext, message=None, use_new_dialog_timeout=True):
    # check if bot was mentioned (for group chats)
    if not await is_bot_mentioned(update, context):
        return

    # check if message is edited
    if update.edited_message is not None:
        await edited_message_handle(update, context)
        return

    effective_user = update.effective_user or (update.message.from_user if update.message else None)
    if effective_user:
        await register_user_if_not_exists(update, context, effective_user)

    if await is_previous_message_not_answered_yet(update, context):
        return

    _message = message or (update.message.text if update.message else None) or (update.message.caption if update.message else None) or ""

    # remove bot mention (in group chats)
    if update.message and update.message.chat.type != "private":
        _message = _message.replace("@" + context.bot.username, "").strip()

    user_id = effective_user.id if effective_user else (update.message.from_user.id if update.message and update.message.from_user else 0)

    # Handle quick action buttons (under chatbar ReplyKeyboardMarkup) with zero delay
    clean_msg = _message.strip()
    if clean_msg in ("👤 Profile", "Profile", "/profile"):
        await show_balance_handle(update, context)
        return
    elif clean_msg in (
        "🎭 AI Chat Modes", "AI Chat Modes", "🎭 AI Modes", "AI Modes",
        "🎭 Modes", "Modes", "/modes", "/mode", "mode", "modes"
    ):
        await show_chat_modes_handle(update, context)
        return
    elif clean_msg in ("👥 Strangerspace ↗", "👥 Strangerspace", "Strangerspace"):
        await update.message.reply_text(
            f"👥 <b>Strangerspace Official Community ↗</b>\n\n"
            f"Connect with our community of creators and developers:\n"
            f"• Official feature releases & announcements\n"
            f"• Community discussions & fast help\n"
            f"• Exclusive rewards and giveaways\n\n"
            f"👉 <a href=\"{OFFICIAL_SUPERGROUP_URL}\">Join Strangerspace on Telegram ↗</a>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 Open Strangerspace ↗", url=OFFICIAL_SUPERGROUP_URL)]
            ]),
            parse_mode=ParseMode.HTML
        )
        return
    elif clean_msg in ("💰 Your Earnings", "Your Earnings", "🎁 Unlock & Earn", "Unlock & Earn", "/affiliate", "/earn"):
        text, keyboard = await get_unlock_and_earn_card(user_id)
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return
    elif clean_msg in ("✨ Commands", "⚡ Commands", "Commands", "commands", "/commands", "Menu", "menu", "⚡", "✨"):
        text, keyboard = get_commands_hub_card(user_id)
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return
    elif clean_msg.lower() in ("new", "clear", "reset"):
        await db.start_new_dialog(user_id)
        chat_mode = await db.get_user_attribute(user_id, "current_chat_mode")
        mode_name = config.chat_modes.get(chat_mode, {}).get("name", "General")
        await update.message.reply_text(
            f"🔄 <b>New dialog started!</b>\n\nCurrent mode: <b>{mode_name}</b>\nPrevious conversation history has been cleared.",
            parse_mode=ParseMode.HTML
        )
        return
    elif clean_msg.lower() in ("settings", "config"):
        text, reply_markup = await get_settings_menu(user_id)
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        return
    elif clean_msg.lower() in ("help", "guide"):
        text, keyboard = get_commands_hub_card(user_id)
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return
    elif clean_msg.lower() in ("premium", "pay with stars", "stars"):
        text, keyboard = await get_premium_plans_card(user_id)
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return

    # Media request rate limiting / debounce (3s cooldown per user on heavy downloads/conversions/uploads)
    is_media_request = bool(
        (update.message and (update.message.document or update.message.video or update.message.audio or update.message.photo or update.message.voice))
        or (_message and MEDIA_URL_REGEX.search(_message))
    )
    if is_media_request and user_id:
        now = time.time()
        last_t = user_media_cooldowns.get_time(user_id)
        if now - last_t < MEDIA_COOLDOWN_SECONDS:
            rem = round(MEDIA_COOLDOWN_SECONDS - (now - last_t), 1)
            await update.message.reply_text(
                f"⏳ <b>Please wait {rem}s before sending another media or download request.</b>",
                parse_mode=ParseMode.HTML
            )
            return
        user_media_cooldowns.record(user_id, now)

    # handle attached documents or videos for agentic conversion / clipping
    if update.message and update.message.document:
        try:
            doc = update.message.document
            doc_file = await context.bot.get_file(doc.file_id)
            out_dir = config.converted_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            safe_name = "".join(c for c in (doc.file_name or "file") if c not in r'\/:*?"<>|')
            saved_path = out_dir / f"{uuid.uuid4().hex[:6]}_{safe_name}"
            await doc_file.download_to_drive(str(saved_path))
            _message = f"[Attached file: {saved_path}]\n" + (_message or f"Process or convert this file: {safe_name}")
        except Exception as e:
            logger.error(f"Error downloading document attachment: {e}")
    elif update.message and update.message.video:
        try:
            vid = update.message.video
            vid_file = await context.bot.get_file(vid.file_id)
            out_dir = config.converted_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            safe_name = "".join(c for c in (vid.file_name or "video.mp4") if c not in r'\/:*?"<>|')
            saved_path = out_dir / f"{uuid.uuid4().hex[:6]}_{safe_name}"
            await vid_file.download_to_drive(str(saved_path))
            _message = f"[Attached file: {saved_path}]\n" + (_message or f"Process or clip this video: {safe_name}")
        except Exception as e:
            logger.error(f"Error downloading video attachment: {e}")

    chat_mode = await db.get_user_attribute(user_id, "current_chat_mode")

    if chat_mode == "artist":
        await generate_image_handle(update, context, message=message)
        return

    current_model = await db.get_user_attribute(user_id, "current_model")

    async def message_handle_fn():
        # new dialog timeout
        if use_new_dialog_timeout:
            last_int = await db.get_user_attribute(user_id, "last_interaction")
            cur_d_msgs = await db.get_dialog_messages(user_id)
            if last_int and (datetime.now() - last_int).seconds > config.new_dialog_timeout and len(cur_d_msgs) > 0:
                await db.start_new_dialog(user_id)
                await update.message.reply_text(f"Starting new dialog due to timeout (<b>{config.chat_modes[chat_mode]['name']}</b> mode) ✅", parse_mode=ParseMode.HTML)
        await db.set_user_attribute(user_id, "last_interaction", datetime.now())

        # in case of CancelledError
        n_input_tokens, n_output_tokens = 0, 0
        t_req_start = time.perf_counter()

        try:
            # send placeholder message to user
            placeholder_message = await update.message.reply_text("...")

            # send typing action
            await update.message.chat.send_action(action="typing")

            if _message is None or len(_message) == 0:
                 await update.message.reply_text("🥲 You sent <b>empty message</b>. Please, try again!", parse_mode=ParseMode.HTML)
                 return

            dialog_messages = await db.get_dialog_messages(user_id, dialog_id=None)
            current_video_metadata = await db.get_user_attribute(user_id, "current_video_metadata")
            recent_videos = await db.get_user_attribute(user_id, "recent_videos_metadata") or []

            parse_mode = {
                "html": ParseMode.HTML,
                "markdown": ParseMode.MARKDOWN
            }[config.chat_modes[chat_mode]["parse_mode"]]

            chatgpt_instance = openai_utils.ChatGPT(model=current_model)
            gen = chatgpt_instance.send_agent_message(
                _message,
                dialog_messages=dialog_messages,
                chat_mode=chat_mode,
                video_metadata=current_video_metadata,
                recent_videos=recent_videos
            )

            prev_answer = ""
            last_edit_time = 0.0
            last_status_edit_time = 0.0
            last_display_answer = ""

            async for gen_item in gen:
                status, answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed = gen_item

                if status == "status":
                    now_t = time.time()
                    if now_t - last_status_edit_time >= 1.5:
                        try:
                            await context.bot.edit_message_text(f"{answer} ⏳", chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, parse_mode=ParseMode.HTML)
                            last_status_edit_time = now_t
                        except Exception:
                            pass
                    continue

                answer = answer[:4096]  # telegram message limit
                current_time = time.time()

                # Clean consumer-facing text of any server disk paths
                display_answer = re.sub(r'\[FILE:\s*[^\]]+\]', '', answer, flags=re.IGNORECASE)
                display_answer = re.sub(r'\[TITLE:\s*[^\]]+\]', '', display_answer, flags=re.IGNORECASE)
                display_answer = re.sub(r'(?:[A-Za-z]:\\[^\s\n*`<>]+|/(?:home|root|app|Users|var|tmp)/[^\s\n*`<>]+|converted_files[\\/][^\s\n*`<>]+)', '', display_answer).strip()
                last_display_answer = display_answer

                # Check if media delivery is pending
                has_media = bool(getattr(chatgpt_instance, "last_generated_files", [])) or bool(getattr(chatgpt_instance, "last_video_metadata", {})) or bool(re.search(r'\[FILE:\s*[^\]]+\]', answer, re.IGNORECASE))

                # If this message delivers a file, do NOT show "Here is your video" before uploading
                if status == "finished" and has_media:
                    continue

                # update only when 0.6s passed or finished, avoiding telegram throttling
                if status != "finished":
                    if (current_time - last_edit_time < 0.6) or abs(len(display_answer) - len(prev_answer)) < 40:
                        continue

                render_answer = display_answer
                render_parse_mode = parse_mode
                if status == "finished" and not has_media:
                    is_prem = await db.is_user_premium(user_id)
                    render_answer = format_ai_reply(display_answer, is_premium=is_prem)
                    render_parse_mode = ParseMode.HTML

                try:
                    await context.bot.edit_message_text(render_answer, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, parse_mode=render_parse_mode)
                    last_edit_time = current_time
                    prev_answer = render_answer
                except telegram.error.BadRequest as e:
                    if str(e).startswith("Message is not modified"):
                        continue
                    else:
                        try:
                            await context.bot.edit_message_text(render_answer, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id)
                            last_edit_time = current_time
                            prev_answer = render_answer
                        except Exception:
                            pass
                except telegram.error.RetryAfter as e:
                    await asyncio.sleep(e.retry_after)
            
            # Clean answer for database persistence
            clean_db_answer = re.sub(r'\[FILE:\s*[^\]]+\]', '', answer, flags=re.IGNORECASE)
            clean_db_answer = re.sub(r'\[TITLE:\s*[^\]]+\]', '', clean_db_answer, flags=re.IGNORECASE)
            clean_db_answer = re.sub(r'[A-Za-z]:\\[^\s\n*`<>]+', '', clean_db_answer).strip()

            # update user data
            new_dialog_message = {"user": [{"type": "text", "text": _message}], "bot": clean_db_answer, "date": datetime.now()}

            prev_msgs = await db.get_dialog_messages(user_id, dialog_id=None)
            await db.set_dialog_messages(
                user_id,
                prev_msgs + [new_dialog_message],
                dialog_id=None
            )

            await db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

            # Persist video metadata to conversation context
            new_meta = getattr(chatgpt_instance, "last_video_metadata", None)
            if new_meta and isinstance(new_meta, dict):
                await db.set_user_attribute(user_id, "current_video_metadata", new_meta)
                existing_recents = await db.get_user_attribute(user_id, "recent_videos_metadata") or []
                filtered_recents = [v for v in existing_recents if v.get("source_url") != new_meta.get("source_url")]
                filtered_recents.insert(0, new_meta)
                await db.set_user_attribute(user_id, "recent_videos_metadata", filtered_recents[:5])
                logger.info(f"Updated video metadata context for user {user_id}: {new_meta.get('video_title')} by {new_meta.get('creator_name')}")

            # Auto-deliver generated file attachment directly to Telegram chat
            files_to_send = []
            for gf in getattr(chatgpt_instance, "last_generated_files", []):
                p = Path(gf)
                if p.exists() and p.is_file() and p not in files_to_send:
                    files_to_send.append(p)

            for pattern in (r'\[FILE:\s*([^\]]+)\]', r'\[File:\s*([^\]]+)\]', r'at ([A-Za-z]:\\[^\s\n<]+?\.[A-Za-z0-9]+)', r'Location:\s*([A-Za-z]:\\[^\s\n<]+?\.[A-Za-z0-9]+)'):
                for m in re.finditer(pattern, answer, re.IGNORECASE):
                    try:
                        filepath = Path(m.group(1).strip().rstrip('.'))
                        if filepath.exists() and filepath.is_file() and filepath not in files_to_send:
                            files_to_send.append(filepath)
                    except Exception:
                        pass

            file_titles = getattr(chatgpt_instance, "last_file_titles", {})
            direct_cdn_url = new_meta.get("direct_url") if (new_meta and isinstance(new_meta, dict)) else None
            video_title = new_meta.get("video_title") if (new_meta and isinstance(new_meta, dict)) else None
            is_premium_user = await db.is_user_premium(user_id)
            caption_text = format_video_caption(video_title or "Video", is_premium=is_premium_user)
            delivered = False

            # Fast-Path: Deliver video instantly via direct CDN URL to Telegram if available
            if direct_cdn_url and "googlevideo.com" not in direct_cdn_url and "youtube" not in direct_cdn_url:
                try:
                    logger.info(f"Attempting ultra-fast Telegram direct CDN delivery for {video_title}...")
                    t_up_start = time.perf_counter()
                    await update.message.reply_video(
                        video=direct_cdn_url,
                        caption=caption_text,
                        parse_mode=ParseMode.HTML,
                        supports_streaming=True,
                        write_timeout=25,
                        read_timeout=25
                    )
                    delivered = True
                    t_upload = time.perf_counter() - t_up_start
                    t_total = time.perf_counter() - t_req_start
                    logger.info(f"[PERF] Delivered {video_title} via Telegram CDN direct in {t_upload:.2f}s (total req: {t_total:.2f}s)")
                except Exception as cdn_err:
                    logger.warning(f"Telegram direct CDN delivery failed ({cdn_err}), falling back to local file upload...")

            # Resilient Fallback: If direct CDN delivery failed and no files are ready, trigger local download
            if not delivered and not files_to_send and new_meta and new_meta.get("source_url"):
                try:
                    logger.info("Direct CDN delivery unavailable or failed, falling back to local download...")
                    from agent.tools.reclip_video import reclip_download_video
                    fallback_res = await reclip_download_video(new_meta["source_url"], format="video")
                    for pattern in (r'\[FILE:\s*([^\]]+)\]', r'\[File:\s*([^\]]+)\]'):
                        for m in re.finditer(pattern, fallback_res, re.IGNORECASE):
                            try:
                                fp = Path(m.group(1).strip().rstrip('.'))
                                if fp.exists() and fp.is_file() and fp not in files_to_send:
                                    files_to_send.append(fp)
                            except Exception:
                                pass
                except Exception as fb_err:
                    logger.error(f"Fallback download error: {fb_err}")

            # Local files delivery (for audio, images, documents, or fallback video)
            if not delivered or any(f.suffix.lower() not in ('.mp4', '.mkv', '.webm', '.mov') for f in files_to_send):
                for filepath in files_to_send:
                    ext = filepath.suffix.lower()
                    if delivered and ext in ('.mp4', '.mkv', '.webm', '.mov'):
                        continue
                    logger.info(f"Delivering generated file {filepath} to user {user_id}...")
                    try:
                        file_size_mb = filepath.stat().st_size / (1024 * 1024)
                        if file_size_mb > 49.5:
                            logger.warning(f"File {filepath} exceeds 50MB Telegram limit ({file_size_mb:.1f} MB)")
                            await update.message.reply_text(
                                f"⚠️ <b>File Exceeds Telegram Bot Limit</b> ({file_size_mb:.1f} MB)\n\n"
                                f"Telegram restricts direct bot uploads to 50 MB.\n"
                                f"<i>Tip: Request a shorter timestamp clip or audio extract with /mode!</i>",
                                parse_mode=ParseMode.HTML
                            )
                            continue

                        raw_title = file_titles.get(str(filepath)) or file_titles.get(filepath.name) or video_title or filepath.stem
                        if ext in ('.mp4', '.mkv', '.webm', '.mov'):
                            item_caption = format_video_caption(raw_title, is_premium=is_premium_user)
                            item_parse_mode = ParseMode.HTML
                        else:
                            item_caption = str(raw_title)[:1024]
                            item_parse_mode = None

                        t_up_start = time.perf_counter()

                        if ext in ('.mp4', '.mkv', '.webm', '.mov'):
                            try:
                                await update.message.chat.send_action(action="upload_video")
                            except Exception:
                                pass
                            with open(filepath, 'rb') as f:
                                await update.message.reply_video(video=f, caption=item_caption, parse_mode=item_parse_mode, supports_streaming=True, write_timeout=120, read_timeout=60, connect_timeout=30)
                            delivered = True
                            t_upload = time.perf_counter() - t_up_start
                            t_total = time.perf_counter() - t_req_start
                            logger.info(f"[PERF] Delivered {item_caption} via local upload in {t_upload:.2f}s (total req: {t_total:.2f}s)")
                        elif ext in ('.mp3', '.m4a', '.wav', '.ogg', '.flac'):
                            with open(filepath, 'rb') as f:
                                await update.message.reply_audio(audio=f, caption=item_caption, write_timeout=120, read_timeout=60, connect_timeout=30)
                            delivered = True
                        elif ext in ('.jpg', '.jpeg', '.png', '.webp'):
                            with open(filepath, 'rb') as f:
                                await update.message.reply_photo(photo=f, caption=item_caption, write_timeout=120, read_timeout=60, connect_timeout=30)
                            delivered = True
                        else:
                            with open(filepath, 'rb') as f:
                                await update.message.reply_document(document=f, caption=item_caption, write_timeout=120, read_timeout=60, connect_timeout=30)
                            delivered = True
                    except Exception as e:
                        logger.error(f"Failed to auto-deliver generated file {filepath}: {e}")

            # Now update placeholder message to final text so text and video arrive in perfect sync
            if (delivered or files_to_send) and last_display_answer:
                try:
                    await context.bot.edit_message_text(last_display_answer, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, parse_mode=parse_mode)
                except Exception:
                    try:
                        await context.bot.edit_message_text(last_display_answer, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id)
                    except Exception:
                        pass

        except asyncio.CancelledError:
            await db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

            files_to_send = []
            for gf in getattr(chatgpt_instance, "last_generated_files", []):
                p = Path(gf)
                if p.exists() and p.is_file() and p not in files_to_send:
                    files_to_send.append(p)

            for pattern in (r'\[FILE:\s*([^\]]+)\]', r'\[File:\s*([^\]]+)\]'):
                for m in re.finditer(pattern, answer, re.IGNORECASE):
                    try:
                        filepath = Path(m.group(1).strip().rstrip('.'))
                        if filepath.exists() and filepath.is_file() and filepath not in files_to_send:
                            files_to_send.append(filepath)
                    except Exception:
                        pass

            file_titles = getattr(chatgpt_instance, "last_file_titles", {})

            is_premium_user = await db.is_user_premium(user_id)
            for filepath in files_to_send:
                try:
                    ext = filepath.suffix.lower()
                    raw_title = file_titles.get(str(filepath)) or file_titles.get(filepath.name) or filepath.stem
                    if ext in ('.mp4', '.mkv', '.webm', '.mov'):
                        caption_text = format_video_caption(raw_title, is_premium=is_premium_user)
                        c_parse_mode = ParseMode.HTML
                    else:
                        caption_text = str(raw_title)[:1024]
                        c_parse_mode = None
                    with open(filepath, 'rb') as f:
                        if ext in ('.mp4', '.mkv', '.webm', '.mov'):
                            await update.message.reply_video(video=f, caption=caption_text, parse_mode=c_parse_mode, supports_streaming=True, write_timeout=120, read_timeout=60, connect_timeout=30)
                        elif ext in ('.mp3', '.m4a', '.wav', '.ogg', '.flac'):
                            await update.message.reply_audio(audio=f, caption=caption_text, write_timeout=120, read_timeout=60, connect_timeout=30)
                        elif ext in ('.jpg', '.jpeg', '.png', '.webp'):
                            await update.message.reply_photo(photo=f, caption=caption_text, write_timeout=120, read_timeout=60, connect_timeout=30)
                        else:
                            await update.message.reply_document(document=f, caption=caption_text, write_timeout=120, read_timeout=60, connect_timeout=30)
                except Exception as e:
                    logger.error(f"Failed to auto-deliver generated file {filepath}: {e}")
            raise

        except Exception as e:
            error_text = f"Something went wrong during completion. Reason: {e}"
            logger.error(error_text)
            await update.message.reply_text(error_text)
            return

        # send message if some messages were removed from the context
        if n_first_dialog_messages_removed > 0:
            if n_first_dialog_messages_removed == 1:
                text = "✍️ <i>Note:</i> Your current dialog is too long, so your <b>first message</b> was removed from the context.\n Send /new command to start new dialog"
            else:
                text = f"✍️ <i>Note:</i> Your current dialog is too long, so <b>{n_first_dialog_messages_removed} first messages</b> were removed from the context.\n Send /new command to start new dialog"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async with user_semaphores[user_id]:
        model_supports_vision = config.models["info"][current_model].get("vision", False)
        photo_sent = update.message.photo is not None and len(update.message.photo) > 0
        if model_supports_vision or photo_sent:
            if not model_supports_vision:
                # a photo was sent but the selected model can't read images:
                # fall back to a vision-capable default
                current_model = "gpt-4o"
                await db.set_user_attribute(user_id, "current_model", "gpt-4o")
            task = asyncio.create_task(
                _vision_message_handle_fn(update, context, use_new_dialog_timeout=use_new_dialog_timeout)
            )
        else:
            task = asyncio.create_task(
                message_handle_fn()
            )

        user_tasks[user_id] = task

        try:
            await task
        except asyncio.CancelledError:
            await update.message.reply_text("✅ Canceled", parse_mode=ParseMode.HTML)
        else:
            pass
        finally:
            if user_id in user_tasks:
                del user_tasks[user_id]


async def is_previous_message_not_answered_yet(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    if user_semaphores[user_id].locked():
        text = "⏳ Please <b>wait</b> for a reply to the previous message\n"
        text += "Or you can /cancel it"
        await update.message.reply_text(text, reply_to_message_id=update.message.id, parse_mode=ParseMode.HTML)
        return True
    else:
        return False


async def voice_message_handle(update: Update, context: CallbackContext):
    # check if bot was mentioned (for group chats)
    if not await is_bot_mentioned(update, context):
        return

    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    await db.set_user_attribute(user_id, "last_interaction", datetime.now())

    voice = update.message.voice
    voice_file = await context.bot.get_file(voice.file_id)
    
    # store file in memory, not on disk
    buf = io.BytesIO()
    await voice_file.download_to_memory(buf)
    buf.name = "voice.oga"  # file extension is required
    buf.seek(0)  # move cursor to the beginning of the buffer

    transcribed_text = await openai_utils.transcribe_audio(buf)
    text = f"🎤: <i>{transcribed_text}</i>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # update n_transcribed_seconds
    cur_sec = await db.get_user_attribute(user_id, "n_transcribed_seconds") or 0.0
    await db.set_user_attribute(user_id, "n_transcribed_seconds", voice.duration + cur_sec)

    await message_handle(update, context, message=transcribed_text)


async def generate_image_handle(update: Update, context: CallbackContext, message=None):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    await db.set_user_attribute(user_id, "last_interaction", datetime.now())

    await update.message.chat.send_action(action="upload_photo")

    message = message or update.message.text

    try:
        images = await openai_utils.generate_images(message, n_images=config.return_n_generated_images, size=config.image_size)
    except openai.BadRequestError as e:
        if str(e).startswith("Your request was rejected as a result of our safety system"):
            text = "🥲 Your request <b>doesn't comply</b> with OpenAI's usage policies.\nWhat did you write there, huh?"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            return
        else:
            raise

    # usage accounting
    cur_imgs = await db.get_user_attribute(user_id, "n_generated_images") or 0
    await db.set_user_attribute(user_id, "n_generated_images", len(images) + cur_imgs)

    for image in images:
        await update.message.chat.send_action(action="upload_photo")
        await update.message.reply_photo(io.BytesIO(image))


async def new_dialog_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    await db.set_user_attribute(user_id, "last_interaction", datetime.now())
    await db.set_user_attribute(user_id, "current_model", config.models["available_text_models"][0])

    await db.start_new_dialog(user_id)
    await update.message.reply_text("Starting new dialog ✅")

    chat_mode = await db.get_user_attribute(user_id, "current_chat_mode")
    await update.message.reply_text(f"{config.chat_modes[chat_mode]['welcome_message']}", parse_mode=ParseMode.HTML)


async def cancel_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    await db.set_user_attribute(user_id, "last_interaction", datetime.now())

    if user_id in user_tasks:
        task = user_tasks[user_id]
        task.cancel()
    else:
        await update.message.reply_text("<i>Nothing to cancel...</i>", parse_mode=ParseMode.HTML)


def get_chat_mode_menu(page_index: int, current_mode: str = None):
    n_chat_modes_per_page = 8  # 4 rows of 2 buttons each
    if current_mode and current_mode in config.chat_modes:
        cur_name = config.chat_modes[current_mode]["name"]
        text = f"🎭 Current Mode: <b>{cur_name}</b>\n\nSelect an <b>AI Chat Mode</b> below ({len(config.chat_modes)} available):"
    else:
        text = f"Select <b>AI Chat Mode</b> ({len(config.chat_modes)} modes available):"

    # buttons in two columns (two rows per place)
    chat_mode_keys = list(config.chat_modes.keys())
    page_chat_mode_keys = chat_mode_keys[page_index * n_chat_modes_per_page:(page_index + 1) * n_chat_modes_per_page]

    keyboard = []
    current_row = []
    for chat_mode_key in page_chat_mode_keys:
        name = config.chat_modes[chat_mode_key]["name"]
        if current_mode == chat_mode_key:
            name = f"✅ {name}"
        current_row.append(InlineKeyboardButton(name, callback_data=f"set_chat_mode|{chat_mode_key}"))
        if len(current_row) == 2:
            keyboard.append(current_row)
            current_row = []
    if current_row:
        keyboard.append(current_row)

    # navigation row
    nav_row = []
    is_first_page = (page_index == 0)
    is_last_page = ((page_index + 1) * n_chat_modes_per_page >= len(chat_mode_keys))

    if not is_first_page:
        nav_row.append(InlineKeyboardButton("« Prev", callback_data=f"show_chat_modes|{page_index - 1}"))
    nav_row.append(InlineKeyboardButton("« Back", callback_data="back_to_about"))
    if not is_last_page:
        nav_row.append(InlineKeyboardButton("Next »", callback_data=f"show_chat_modes|{page_index + 1}"))

    keyboard.append(nav_row)
    reply_markup = InlineKeyboardMarkup(keyboard)

    return text, reply_markup


async def show_chat_modes_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    await db.set_user_attribute(user_id, "last_interaction", datetime.now())
    current_mode = await db.get_user_attribute(user_id, "current_chat_mode") or "general"

    text, reply_markup = get_chat_mode_menu(0, current_mode=current_mode)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def show_chat_modes_callback_handle(update: Update, context: CallbackContext):
     await register_user_if_not_exists(update, context, update.callback_query.from_user)
     if await is_previous_message_not_answered_yet(update.callback_query, context): return

     user_id = update.callback_query.from_user.id
     await db.set_user_attribute(user_id, "last_interaction", datetime.now())
     current_mode = await db.get_user_attribute(user_id, "current_chat_mode") or "general"

     query = update.callback_query
     try:
         await query.answer()
     except Exception:
         pass

     parts = query.data.split("|")
     page_index = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
     if page_index < 0:
         page_index = 0

     text, reply_markup = get_chat_mode_menu(page_index, current_mode=current_mode)
     try:
         await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
     except Exception:
         try:
             await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
         except Exception:
             pass


async def set_chat_mode_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    try:
        await query.answer("Chat mode updated! ✅")
    except Exception:
        pass

    parts = query.data.split("|")
    chat_mode = parts[1] if len(parts) > 1 else "general"

    await db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
    await db.start_new_dialog(user_id)

    chat_title = config.chat_modes.get(chat_mode, {}).get("name", chat_mode.title())
    welcome_msg = config.chat_modes.get(chat_mode, {}).get("welcome_message", f"Switched to {chat_title} mode ✅")

    confirm_text = (
        f"🎭 <b>AI Chat Mode Activated: {chat_title}</b> ✅\n\n"
        f"{welcome_msg}\n\n"
        f"<i>Previous conversation memory cleared for this mode. You can now chat!</i>"
    )

    try:
        await query.edit_message_text(confirm_text, parse_mode=ParseMode.HTML)
    except Exception:
        await context.bot.send_message(
            update.callback_query.message.chat.id,
            confirm_text,
            parse_mode=ParseMode.HTML
        )


async def get_settings_menu(user_id: int):
    current_model = await db.get_user_attribute(user_id, "current_model")
    text = config.models["info"][current_model]["description"]

    text += "\n\n"
    score_dict = config.models["info"][current_model]["scores"]
    for score_key, score_value in score_dict.items():
        text += "🟢" * score_value + "⚪️" * (5 - score_value) + f" – {score_key}\n\n"

    text += "\nSelect <b>model</b>:"

    # buttons to choose models (chunked into rows of 2 to stay within
    # Telegram's per-row button limit as the model list grows)
    buttons = []
    for model_key in config.models["available_text_models"]:
        title = config.models["info"][model_key]["name"]
        if model_key == current_model:
            title = "✅ " + title

        buttons.append(
            InlineKeyboardButton(title, callback_data=f"set_settings|{model_key}")
        )

    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    reply_markup = InlineKeyboardMarkup(keyboard)

    return text, reply_markup


async def settings_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    await db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = await get_settings_menu(user_id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def set_settings_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    _, model_key = query.data.split("|")
    await db.set_user_attribute(user_id, "current_model", model_key)
    await db.start_new_dialog(user_id)

    text, reply_markup = await get_settings_menu(user_id)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass


async def show_balance_handle(update: Update, context: CallbackContext):
    user = update.effective_user
    if not user:
        return
    await register_user_if_not_exists(update, context, user)

    user_id = user.id
    await db.set_user_attribute(user_id, "last_interaction", datetime.now())

    # count total usage statistics
    total_n_spent_dollars = 0
    total_n_used_tokens = 0

    n_used_tokens_dict = await db.get_user_attribute(user_id, "n_used_tokens") or {}
    n_generated_images = await db.get_user_attribute(user_id, "n_generated_images") or 0
    n_transcribed_seconds = await db.get_user_attribute(user_id, "n_transcribed_seconds") or 0.0

    details_text = "🏷️ Details:\n"
    for model_key in sorted(n_used_tokens_dict.keys()):
        # skip models that are no longer configured (e.g. removed legacy models
        # a user spent tokens on in the past) to avoid a KeyError
        if model_key not in config.models["info"]:
            continue

        n_input_tokens, n_output_tokens = n_used_tokens_dict[model_key]["n_input_tokens"], n_used_tokens_dict[model_key]["n_output_tokens"]
        total_n_used_tokens += n_input_tokens + n_output_tokens

        n_input_spent_dollars = config.models["info"][model_key]["price_per_1000_input_tokens"] * (n_input_tokens / 1000)
        n_output_spent_dollars = config.models["info"][model_key]["price_per_1000_output_tokens"] * (n_output_tokens / 1000)
        total_n_spent_dollars += n_input_spent_dollars + n_output_spent_dollars

        details_text += f"- {model_key}: <b>{n_input_spent_dollars + n_output_spent_dollars:.03f}$</b> / <b>{n_input_tokens + n_output_tokens} tokens</b>\n"

    # image generation
    image_price = 0.02
    if "gpt-image-1" in config.models["info"]:
        image_price = config.models["info"]["gpt-image-1"].get("price_per_1_image", 0.02)
    image_generation_n_spent_dollars = image_price * n_generated_images
    if n_generated_images != 0:
        details_text += f"- GPT Image (image generation): <b>{image_generation_n_spent_dollars:.03f}$</b> / <b>{n_generated_images} generated images</b>\n"

    total_n_spent_dollars += image_generation_n_spent_dollars

    # voice recognition
    whisper_price = 0.001
    if "whisper" in config.models["info"]:
        whisper_price = config.models["info"]["whisper"].get("price_per_1_min", 0.001)
    elif "whisper-large-v3-turbo" in config.models["info"]:
        whisper_price = config.models["info"]["whisper-large-v3-turbo"].get("price_per_1_min", 0.001)
    voice_recognition_n_spent_dollars = whisper_price * (n_transcribed_seconds / 60)
    if n_transcribed_seconds != 0:
        details_text += f"- Whisper (voice recognition): <b>{voice_recognition_n_spent_dollars:.03f}$</b> / <b>{n_transcribed_seconds:.01f} seconds</b>\n"

    total_n_spent_dollars += voice_recognition_n_spent_dollars

    is_prem = await db.is_user_premium(user_id)
    p_info = await db.get_premium_info(user_id)
    aff_info = await db.get_affiliate_info(user_id)

    full_name_clean = html.escape(user.full_name)
    username_clean = f"@{user.username}" if user.username else "<i>None</i>"

    if is_prem:
        tier_label = p_info.get("tier_name", "VIP")
        status_header = f"👑 <b>Active Premium Member</b> ({tier_label})"
        membership_details = (
            f"• <b>Status:</b> Premium VIP (100% Ad-Free)\n"
            f"• <b>Valid Until:</b> <code>{p_info.get('expiry_date')}</code> ({p_info.get('days_remaining', 0)} days remaining)\n"
            f"• <b>Perks:</b> Unlimited downloads, VIP priority cloud queue, 120B reasoning"
        )
    else:
        status_header = "🌱 <b>Free Account</b> (Ad-Supported • Standard Limits)"
        membership_details = (
            f"• <b>Status:</b> Standard limits & ad-supported\n"
            f"• <b>Upgrade:</b> Pay with Stars anytime for instant VIP speeds & maximum limits!"
        )

    # Official Telegram Affiliate Program: 50% Stars commission for all users
    ref_link = f"https://t.me/sauxsibot?start=aff_{user_id}"
    earning_status_lines = (
        f"• <b>Program:</b> Telegram Stars Affiliate Program 🏆 (50% Commission)\n"
        f"• <b>Stars Earned:</b> <b>{aff_info['stars_earned']} ⭐</b> | <b>Referred:</b> <b>{aff_info['referral_count']} users</b>\n"
        f"• 🔗 <b>Your Link:</b> <code>{ref_link}</code>"
    )
    earn_button_text = "💰 Your Earnings"

    text = (
        f"👤 <b>Connected Telegram Profile</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📛 <b>Name:</b> {full_name_clean}\n"
        f"🔗 <b>Username:</b> {username_clean}\n"
        f"🆔 <b>Telegram ID:</b> <code>{user_id}</code>\n\n"
        f"⭐ <b>Account Status:</b>\n"
        f"{status_header}\n"
        f"{membership_details}\n\n"
        f"🎁 <b>Earning Relationship:</b>\n"
        f"{earning_status_lines}\n\n"
        f"📊 <b>Usage Statistics:</b>\n"
        f"• Estimated Cost: <b>${total_n_spent_dollars:.03f}</b>\n"
        f"• Tokens Used: <b>{total_n_used_tokens:,}</b> tokens\n\n"
        f"{details_text}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ Pay with Stars", callback_data="open_premium_plans"),
            InlineKeyboardButton("💰 Your Earnings", callback_data="open_unlock_earn")
        ],
        [
            InlineKeyboardButton("👥 Strangerspace ↗", url=OFFICIAL_SUPERGROUP_URL),
            InlineKeyboardButton("« Back", callback_data="back_to_about")
        ]
    ])

    if update.callback_query:
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception:
            await query.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def edited_message_handle(update: Update, context: CallbackContext):
    if update.edited_message.chat.type == "private":
        text = "🥲 Unfortunately, message <b>editing</b> is not supported"
        await update.edited_message.reply_text(text, parse_mode=ParseMode.HTML)


async def error_handle(update: Update, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    try:
        if update and update.callback_query:
            try:
                await update.callback_query.answer("⚠️ An error occurred. Please try again.", show_alert=True)
            except Exception:
                pass
        elif update and update.effective_chat:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ Something went wrong processing your request. Please try again."
            )
    except Exception:
        pass

async def periodic_cleanup_loop():
    """
    Background routine that runs every 10 minutes to prune temporary media files
    older than 30 minutes in downloads/ and converted_files/ preventing disk leaks.
    """
    base_dir = Path(__file__).parent.parent.resolve()
    target_dirs = [
        config.downloads_dir,
        config.converted_dir,
        base_dir / "downloads",
        base_dir / "converted_files"
    ]
    while True:
        try:
            now = time.time()
            cutoff = now - (30 * 60)  # 30 minutes TTL
            for d in target_dirs:
                if d.exists() and d.is_dir():
                    for f in d.iterdir():
                        if f.is_file() and not f.name.startswith("."):
                            try:
                                if f.stat().st_mtime < cutoff:
                                    f.unlink()
                                    logger.debug(f"[CLEANUP] Pruned expired temporary file: {f.name}")
                            except Exception:
                                pass
        except Exception as e:
            logger.warning(f"Error in periodic file cleanup: {e}")
        await asyncio.sleep(600)


async def post_init(application: Application):
    # Launch background auto-cleanup and start bounded worker task pools
    video_download_queue.start()
    media_convert_queue.start()
    asyncio.create_task(periodic_cleanup_loop())

    commands = [
        BotCommand("/start", "Start & quick overview 🚀"),
        BotCommand("/profile", "User profile & usage 👤"),
        BotCommand("/mode", "Select AI chat mode 🎭"),
        BotCommand("/earn", "Your Earnings — 50% Stars affiliate 💰"),
        BotCommand("/crate", "Open Crate media dashboard 🗂️"),
        BotCommand("/commands", "Quick commands hub ⚡"),
        BotCommand("/premium", "Pay with Stars ⭐"),
        BotCommand("/balance", "Usage & membership 💳"),
        BotCommand("/new", "Start new dialog 🔄"),
        BotCommand("/retry", "Re-generate response 🔁"),
        BotCommand("/settings", "Bot settings ⚙️"),
        BotCommand("/about", "About SAUX Super Intelligence ✨"),
        BotCommand("/help", "Help & guide ℹ️"),
    ]
    await application.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
    try:
        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Crate", web_app=WebAppInfo(url="https://saux.si/"))
        )
    except Exception as e:
        logger.warning(f"Could not set default chat menu button: {e}")

async def unlock_and_earn_command_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    await db.set_user_attribute(user_id, "last_interaction", datetime.now())
    text, keyboard = await get_unlock_and_earn_card(user_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def commands_command_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    await db.set_user_attribute(user_id, "last_interaction", datetime.now())
    text, keyboard = get_commands_hub_card(user_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

def run_bot() -> None:
    request = HTTPXRequest(http_version="1.1", connect_timeout=30.0, read_timeout=30.0)
    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .request(request)
        .get_updates_request(request)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .post_init(post_init)
        .build()
    )

    # add handlers
    user_filter = filters.ALL
    if len(config.allowed_telegram_usernames) > 0:
        usernames = [x for x in config.allowed_telegram_usernames if isinstance(x, str)]
        any_ids = [x for x in config.allowed_telegram_usernames if isinstance(x, int)]
        user_ids = [x for x in any_ids if x > 0]
        group_ids = [x for x in any_ids if x < 0]
        user_filter = filters.User(username=usernames) | filters.User(user_id=user_ids) | filters.Chat(chat_id=group_ids)

    application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
    application.add_handler(CommandHandler("about", about_handle, filters=user_filter))
    application.add_handler(CommandHandler("profile", show_balance_handle, filters=user_filter))
    application.add_handler(CommandHandler("earn", unlock_and_earn_command_handle, filters=user_filter))
    application.add_handler(CommandHandler("affiliate", unlock_and_earn_command_handle, filters=user_filter))
    application.add_handler(CommandHandler("crate", crate_handle, filters=user_filter))
    application.add_handler(CommandHandler("help", help_handle, filters=user_filter))
    application.add_handler(CommandHandler("commands", commands_command_handle, filters=user_filter))
    application.add_handler(CommandHandler("menu", commands_command_handle, filters=user_filter))
    application.add_handler(CommandHandler("premium", premium_handle, filters=user_filter))
    application.add_handler(CommandHandler("stars", premium_handle, filters=user_filter))
    application.add_handler(CommandHandler("subscribe", premium_handle, filters=user_filter))
    application.add_handler(CommandHandler("help_group_chat", help_group_chat_handle, filters=user_filter))

    application.add_handler(CallbackQueryHandler(premium_callback_handle, pattern="^(open_premium_plans|buy_premium_stars|buy_stars_|back_to_about|show_balance_btn|show_profile_btn|open_unlock_earn|open_crate_info|open_commands_hub|start_new_dialog_hub|open_settings_hub)"))
    application.add_handler(PreCheckoutQueryHandler(precheckout_handle))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handle))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handle))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(CommandHandler("retry", retry_handle, filters=user_filter))
    application.add_handler(CommandHandler("new", new_dialog_handle, filters=user_filter))
    application.add_handler(CommandHandler("cancel", cancel_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.VOICE & user_filter, voice_message_handle))

    application.add_handler(CommandHandler("mode", show_chat_modes_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(show_chat_modes_callback_handle, pattern="^show_chat_modes"))
    application.add_handler(CallbackQueryHandler(set_chat_mode_handle, pattern="^set_chat_mode"))

    application.add_handler(CommandHandler("settings", settings_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(set_settings_handle, pattern="^set_settings"))

    application.add_handler(CommandHandler("balance", show_balance_handle, filters=user_filter))

    application.add_error_handler(error_handle)

    # start the bot
    application.run_polling(bootstrap_retries=10, timeout=30)


if __name__ == "__main__":
    run_bot()
