import asyncio
import sys
import uuid
from pathlib import Path
from datetime import datetime

BOT_DIR = Path(__file__).parent.parent.resolve() / "bot"
sys.path.insert(0, str(BOT_DIR))

import database

async def main():
    print("========================================")
    print("SAUX BOT - ASYNC DATABASE TEST SUITE")
    print("========================================")

    db = database.Database()
    test_uid = int(uuid.uuid4().int % 1000000000)
    referrer_uid = int(uuid.uuid4().int % 1000000000)

    # 1. add_new_user
    await db.add_new_user(referrer_uid, referrer_uid, username="ref_user", first_name="Ref")
    await db.add_new_user(test_uid, test_uid, username="test_user", first_name="Test")
    assert await db.check_if_user_exists(test_uid) is True
    print("  [PASS] add_new_user & check_if_user_exists")

    # 2. start_new_dialog
    d_id = await db.start_new_dialog(test_uid)
    assert d_id is not None
    assert await db.get_user_attribute(test_uid, "current_dialog_id") == d_id
    print("  [PASS] start_new_dialog & get_user_attribute")

    # 3. set_user_attribute & get_user_attribute
    await db.set_user_attribute(test_uid, "custom_field", "custom_val_123")
    assert await db.get_user_attribute(test_uid, "custom_field") == "custom_val_123"
    print("  [PASS] set_user_attribute & get_user_attribute")

    # 4. is_user_premium & get_premium_info & set_user_premium
    assert await db.is_user_premium(test_uid) is False
    p_info = await db.get_premium_info(test_uid)
    assert p_info["is_premium"] is False

    await db.set_user_premium(test_uid, days=30, stars_paid=150, tier_name="1 Month Pro Pass")
    assert await db.is_user_premium(test_uid) is True
    p_info = await db.get_premium_info(test_uid)
    assert p_info["is_premium"] is True
    assert p_info["stars_paid"] == 150
    assert p_info["tier_name"] == "1 Month Pro Pass"
    print("  [PASS] set_user_premium & is_user_premium & get_premium_info")

    # 5. record_referral & get_affiliate_info
    ref_recorded = await db.record_referral(test_uid, referrer_uid)
    assert ref_recorded is True
    # Duplicate referral should return False
    assert await db.record_referral(test_uid, referrer_uid) is False
    assert await db.get_user_attribute(test_uid, "referrer_id") == referrer_uid
    aff_info = await db.get_affiliate_info(referrer_uid)
    assert aff_info["referral_count"] == 1
    assert aff_info["is_partner"] is True
    print("  [PASS] record_referral & get_affiliate_info")

    # 6. credit_referral_commission
    ref_id, commission = await db.credit_referral_commission(test_uid, stars_paid=150)
    assert ref_id == referrer_uid
    assert commission == 75  # 50% of 150
    aff_info = await db.get_affiliate_info(referrer_uid)
    assert aff_info["stars_earned"] == 75
    print("  [PASS] credit_referral_commission")

    # 7. dialog_messages
    msgs = [{"user": "Hello bot", "bot": "Hello human"}]
    await db.set_dialog_messages(test_uid, msgs, dialog_id=d_id)
    retrieved_msgs = await db.get_dialog_messages(test_uid, dialog_id=d_id)
    assert len(retrieved_msgs) == 1
    assert retrieved_msgs[0]["user"] == "Hello bot"
    print("  [PASS] set_dialog_messages & get_dialog_messages")

    # 8. update_n_used_tokens
    await db.update_n_used_tokens(test_uid, "gpt-4o", 100, 50)
    toks = await db.get_user_attribute(test_uid, "n_used_tokens")
    assert toks["gpt-4o"]["n_input_tokens"] == 100
    assert toks["gpt-4o"]["n_output_tokens"] == 50
    print("  [PASS] update_n_used_tokens")

    # 9. Concurrency & Lost Update Stress Test
    print("\n--- Running Concurrent Lost-Update Prevention Stress Test ---")
    num_concurrent = 30
    async def increment_tokens():
        await db.update_n_used_tokens(test_uid, "stress-model", 10, 5)

    # 30 concurrent increments = exactly 300 input tokens, 150 output tokens
    await asyncio.gather(*[increment_tokens() for _ in range(num_concurrent)])
    toks = await db.get_user_attribute(test_uid, "n_used_tokens")
    stress_toks = toks["stress-model"]
    assert stress_toks["n_input_tokens"] == num_concurrent * 10, f"Expected {num_concurrent * 10}, got {stress_toks['n_input_tokens']}"
    assert stress_toks["n_output_tokens"] == num_concurrent * 5, f"Expected {num_concurrent * 5}, got {stress_toks['n_output_tokens']}"
    print(f"  [PASS] Concurrent RMW Token Increments: Exactly {num_concurrent * 10} / {num_concurrent * 5} tokens recorded (ZERO lost updates)!")

    # 10. Cancellation Safety Shielding Test
    print("\n--- Running Cancellation-Shielding Test ---")
    async def slow_write():
        await db.set_user_attribute(test_uid, "shield_test", "shielded_value")

    task = asyncio.create_task(slow_write())
    await asyncio.sleep(0.001)  # Yield to event loop so task enters execution
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Wait briefly for thread to finish writing
    await asyncio.sleep(0.05)
    val = await db.get_user_attribute(test_uid, "shield_test")
    assert val == "shielded_value", "Write should have been shielded and committed!"
    print("  [PASS] Cancellation Shielding: Write completed and verified after coroutine cancellation.")

    db.close()
    print("\n========================================")
    print("ALL 14 DATABASE APIS & CONCURRENCY TESTS PASSED 100%!")
    print("========================================")

if __name__ == "__main__":
    asyncio.run(main())
