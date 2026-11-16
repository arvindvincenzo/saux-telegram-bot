import asyncio
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent / "bot"))

from openai_utils import MultiKeyClientPool


class TestMultiKeyClientPool(unittest.IsolatedAsyncioTestCase):
    async def test_single_key_init(self):
        pool = MultiKeyClientPool(api_keys=["key1"], base_url="https://api.groq.com/openai/v1")
        self.assertEqual(len(pool._clients), 1)

    async def test_round_robin_rotation(self):
        pool = MultiKeyClientPool(api_keys=["key1", "key2", "key3"], base_url="https://api.groq.com/openai/v1")
        self.assertEqual(len(pool._clients), 3)

        # Mock each client's create method
        client_calls = []
        for i, c in enumerate(pool._clients):
            mock_res = MagicMock()
            mock_res.choices = [MagicMock(message=MagicMock(content=f"reply_from_{i}"))]
            async def make_coro(*args, idx=i, res=mock_res, **kwargs):
                client_calls.append(idx)
                return res
            c.chat.completions.create = AsyncMock(side_effect=make_coro)

        # Execute 3 calls
        r0 = await pool.chat.completions.create(model="test", messages=[])
        r1 = await pool.chat.completions.create(model="test", messages=[])
        r2 = await pool.chat.completions.create(model="test", messages=[])

        # Verify each of the 3 keys was called in round-robin order
        self.assertEqual(client_calls, [0, 1, 2])
        self.assertEqual(r0.choices[0].message.content, "reply_from_0")
        self.assertEqual(r1.choices[0].message.content, "reply_from_1")
        self.assertEqual(r2.choices[0].message.content, "reply_from_2")

    async def test_rate_limit_automatic_failover(self):
        pool = MultiKeyClientPool(api_keys=["bad_key_429", "good_key"], base_url="https://api.groq.com/openai/v1")
        
        # bad_key raises 429
        pool._clients[0].chat.completions.create = AsyncMock(side_effect=Exception("Error 429: rate_limit_exceeded"))
        
        # good_key succeeds
        mock_good = MagicMock()
        mock_good.choices = [MagicMock(message=MagicMock(content="recovered_answer"))]
        pool._clients[1].chat.completions.create = AsyncMock(return_value=mock_good)

        # Execute call: should automatically catch 429 and recover via good_key
        res = await pool.chat.completions.create(model="test", messages=[])
        self.assertEqual(res.choices[0].message.content, "recovered_answer")
        self.assertIn(0, pool._cooldowns)


if __name__ == "__main__":
    unittest.main()
