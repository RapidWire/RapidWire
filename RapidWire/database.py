import aiomysql
from contextvars import ContextVar

# Context variables to store per-task state
_connection = ContextVar("connection", default=None)
_cursor = ContextVar("cursor", default=None)
_nesting_level = ContextVar("nesting_level", default=0)

class DatabaseConnection:
    def __init__(self, pool: aiomysql.Pool):
        self.pool = pool

    async def __aenter__(self):
        level = _nesting_level.get()
        if level == 0:
            connection = await self.pool.acquire()
            try:
                cursor = await connection.cursor(aiomysql.DictCursor)
                _connection.set(connection)
                _cursor.set(cursor)
            except Exception:
                self.pool.release(connection)
                raise

        _nesting_level.set(level + 1)
        return _cursor.get()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        level = _nesting_level.get()
        _nesting_level.set(level - 1)

        if level - 1 == 0:
            connection = _connection.get()
            cursor = _cursor.get()
            try:
                if exc_type:
                    await connection.rollback()
                else:
                    await connection.commit()
            finally:
                if cursor:
                    await cursor.close()
                    _cursor.set(None)
                if connection:
                    self.pool.release(connection)
                    _connection.set(None)
        elif level - 1 < 0:
            _nesting_level.set(0)
