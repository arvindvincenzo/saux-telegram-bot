import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent / "bot"))

import saux_config
from agent.tools.reclip_video import extract_true_video_title
from database import Database

def test_saux_promos():
    # 1. Video caption formatting
    title = "Spider-Man No Way Home Full Scene 4K"
    free_cap = saux_config.format_video_caption(title, is_premium=False)
    assert title in free_cap
    assert "SAUX Super Intelligence" in free_cap
    assert "https://t.me/strangerspace" in free_cap
    assert "/premium" in free_cap
    assert len(free_cap) <= 1024

    prem_cap = saux_config.format_video_caption(title, is_premium=True)
    assert prem_cap == title
    assert "SAUX" not in prem_cap

    # Length limit test
    long_title = "A" * 1200
    free_long_cap = saux_config.format_video_caption(long_title, is_premium=False)
    assert len(free_long_cap) <= 1024
    assert free_long_cap.endswith(saux_config.VIDEO_CAPTION_PROMO)

    # 2. AI reply formatting
    ai_text = "Here is the summary of quantum computing."
    free_ai = saux_config.format_ai_reply(ai_text, is_premium=False)
    assert ai_text in free_ai
    assert "SAUX Super Intelligence" in free_ai
    assert "https://t.me/strangerspace" in free_ai
    assert "/premium" in free_ai

    # No duplicate promo
    free_ai_twice = saux_config.format_ai_reply(free_ai, is_premium=False)
    assert free_ai_twice == free_ai

    prem_ai = saux_config.format_ai_reply(ai_text, is_premium=True)
    assert prem_ai == ai_text

    print("✅ Video & AI promotional format tests passed!")

def test_title_extraction():
    # Test Instagram placeholder fallback
    info1 = {
        "title": "Video by zenitsu.fx25",
        "description": "Epic battle scene from Demon Slayer season 3! #demonslayer #anime",
        "uploader": "zenitsu.fx25"
    }
    extracted1 = extract_true_video_title(info1)
    assert "Epic battle scene from Demon Slayer" in extracted1

    # Test when description is empty
    info2 = {
        "title": "Video by zenitsu.fx25",
        "description": "",
        "uploader": "zenitsu.fx25"
    }
    extracted2 = extract_true_video_title(info2)
    assert extracted2 == "Reel • @zenitsu.fx25"

    # Test normal YouTube title
    info3 = {
        "title": "Elon Musk's Insane Focus #motivation",
        "description": "Shorts video...",
        "uploader": "MotivationDaily"
    }
    extracted3 = extract_true_video_title(info3)
    assert extracted3 == "Elon Musk's Insane Focus #motivation"

    print("✅ Title extraction tests passed!")

def test_init_data_validation():
    import hmac
    import hashlib

    bot_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    parsed_dict = {
        "auth_date": "1620000000",
        "query_id": "AAHdE6wAAAAAAJ0TrAA",
        "user": '{"id":12345,"first_name":"Alex"}'
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_dict.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    valid_hash = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    import urllib.parse
    init_data_str = urllib.parse.urlencode(parsed_dict) + f"&hash={valid_hash}"

    valid_res = saux_config.validate_telegram_init_data(init_data_str, bot_token)
    assert valid_res is not None
    assert valid_res["auth_date"] == "1620000000"

    tampered_str = init_data_str + "tamper"
    assert saux_config.validate_telegram_init_data(tampered_str, bot_token) is None
    print("✅ Telegram initData validation tests passed!")

def test_database_premium():
    import uuid
    db = Database()
    test_uid = int(uuid.uuid4().int % 1000000000)
    db.add_new_user(test_uid, test_uid, username="test_saux")

    # Initially not premium
    assert db.is_user_premium(test_uid) is False
    p_info = db.get_premium_info(test_uid)
    assert p_info["is_premium"] is False

    # Grant 7 days
    db.set_user_premium(test_uid, days=7, stars_paid=50, tier_name="1 Week Starter Pass")
    assert db.is_user_premium(test_uid) is True
    p_info = db.get_premium_info(test_uid)
    assert p_info["is_premium"] is True
    assert p_info["stars_paid"] == 50
    assert p_info["days_remaining"] >= 6

    # Test expired date auto-downgrades
    yesterday = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    db.set_user_attribute(test_uid, "premium_expiry", yesterday)
    assert db.is_user_premium(test_uid) is False
    assert db.get_user_attribute(test_uid, "is_premium") is False
    print("✅ Database premium & auto-expiration tests passed!")

if __name__ == "__main__":
    test_saux_promos()
    test_title_extraction()
    test_init_data_validation()
    test_database_premium()
    print("\n🎉 ALL SAUX BOT FEATURE SUITE TESTS PASSED 100%!")
