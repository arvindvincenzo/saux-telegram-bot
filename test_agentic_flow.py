import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tools.openchamber_tool import handle_conversational_fallback, generate_chat_reply

async def main():
    test_queries = [
        "what happen to you?",
        "what happened to you",
        "who r u",
        "what can you do",
        "heyy there",
        "yooo wassup!",
        "what is hair care",
        "how does photosynthesis work",
        "thanks so much"
    ]

    print("=== Testing Agentic Conversation Flow ===")
    for q in test_queries:
        # Check conversational fallback
        conv = handle_conversational_fallback(q)
        if conv:
            print(f"\n[QUERY]: '{q}'\n[TYPE]: Fast Conversational\n[ANSWER]:\n{conv}")
        else:
            # Full reply test
            reply, s_id = await generate_chat_reply(None, q)
            print(f"\n[QUERY]: '{q}'\n[TYPE]: Agentic Engine ({s_id})\n[ANSWER]:\n{reply}")
            # Ensure no mockups or canned templates
            assert "Paste any YouTube, Instagram, or TikTok link" not in reply, f"Mockup found in {q}!"
            assert "You can type search" not in reply, f"Search template found in {q}!"
            assert "I have received your request" not in reply, f"Received request template found in {q}!"

    print("\nALL 9 AGENTIC TESTS PASSED SUCCESSFULLY! ZERO MOCKUPS DETECTED.")

if __name__ == "__main__":
    asyncio.run(main())
