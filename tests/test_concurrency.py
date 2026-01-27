import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import aiomysql

import sys
from pathlib import Path
parent_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(parent_dir))

from RapidWire.database import DatabaseConnection

class TestDatabaseConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_access(self):
        # Mock pool and connection
        pool = MagicMock(spec=aiomysql.Pool)
        connection = AsyncMock()
        cursor = AsyncMock()

        # Make pool.acquire awaitable
        async def acquire_side_effect():
            return connection
        pool.acquire.side_effect = acquire_side_effect

        # Make connection.cursor awaitable
        async def cursor_side_effect(cursor_type=None):
            return cursor
        connection.cursor.side_effect = cursor_side_effect

        db = DatabaseConnection(pool)

        async def task_sim(task_id):
            async with db as c:
                # Simulate work
                await asyncio.sleep(0.01)
                return f"Task {task_id} done"

        # Run two concurrent tasks
        results = await asyncio.gather(task_sim(1), task_sim(2))

        self.assertEqual(results, ["Task 1 done", "Task 2 done"])
        self.assertEqual(pool.acquire.call_count, 2)

if __name__ == '__main__':
    unittest.main()
