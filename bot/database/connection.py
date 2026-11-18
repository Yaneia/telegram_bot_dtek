"""Async SQLite database connection management and PRAGMA configuration."""

import asyncio
from pathlib import Path
import queue
import sqlite3
import threading
from typing import Any, Callable, Optional, Union

try:
    import aiosqlite
    _AIOSQLITE_REAL = True
except ImportError:  # pragma: no cover
    _AIOSQLITE_REAL = False

    class _AsyncCursorCompat:
        """Asynchronous cursor adapter for sqlite3.Cursor."""

        def __init__(self, conn: "_AsyncConnectionCompat", cursor: sqlite3.Cursor) -> None:
            self._conn = conn
            self._cursor = cursor

        @property
        def lastrowid(self) -> Optional[int]:
            return self._cursor.lastrowid

        @property
        def rowcount(self) -> int:
            return self._cursor.rowcount

        @property
        def description(self) -> Any:
            return self._cursor.description

        async def fetchone(self) -> Any:
            def _fetchone(raw_conn: sqlite3.Connection) -> Any:
                return self._cursor.fetchone()

            return await self._conn._dispatch(_fetchone)

        async def fetchall(self) -> Any:
            def _fetchall(raw_conn: sqlite3.Connection) -> Any:
                return self._cursor.fetchall()

            return await self._conn._dispatch(_fetchall)

        async def close(self) -> None:
            def _close(raw_conn: sqlite3.Connection) -> None:
                self._conn._open_cursors.discard(self._cursor)
                self._cursor.close()

            await self._conn._dispatch(_close)

        def __aiter__(self) -> "_AsyncCursorCompat":
            return self

        async def __anext__(self) -> Any:
            row = await self.fetchone()
            if row is None:
                raise StopAsyncIteration
            return row

        async def __aenter__(self) -> "_AsyncCursorCompat":
            return self

        async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            await self.close()

    class _AsyncConnectionCompat:
        """Asynchronous connection adapter maintaining a dedicated worker thread per connection."""

        def __init__(self, database: Union[str, Path], **kwargs: Any) -> None:
            kwargs.setdefault("check_same_thread", False)
            kwargs.setdefault("timeout", 30.0)
            self.database = str(database)
            self.kwargs = kwargs
            self._row_factory = sqlite3.Row
            self._open_cursors: set[sqlite3.Cursor] = set()
            self._queue: queue.Queue[Any] = queue.Queue()
            self._closed = False
            self._init_event = threading.Event()
            self._init_error: Optional[BaseException] = None

            self._thread = threading.Thread(
                target=self._worker,
                name=f"AsyncSqliteWorker-{self.database}",
                daemon=True,
            )
            self._thread.start()
            self._init_event.wait(timeout=10.0)
            if self._init_error is not None:
                raise self._init_error

        @property
        def row_factory(self) -> Any:
            return self._row_factory

        @row_factory.setter
        def row_factory(self, factory: Any) -> None:
            self._row_factory = factory

            def _set_rf(conn: sqlite3.Connection) -> None:
                conn.row_factory = factory

            if not self._closed:
                self._queue.put((_set_rf, (), None, None))

        def _worker(self) -> None:
            try:
                conn = sqlite3.connect(self.database, **self.kwargs)
                conn.row_factory = sqlite3.Row
                self._init_event.set()
            except BaseException as exc:
                self._init_error = exc
                self._init_event.set()
                return

            while True:
                item = self._queue.get()
                if item is None:
                    for cur in list(self._open_cursors):
                        try:
                            cur.close()
                        except Exception:
                            pass
                    self._open_cursors.clear()
                    try:
                        conn.close()
                    except Exception:
                        pass
                    self._queue.task_done()
                    break

                func, args, future, loop = item
                if future is not None and future.cancelled():
                    self._queue.task_done()
                    continue

                try:
                    res = func(conn, *args)
                    if future is not None and not future.cancelled():
                        loop.call_soon_threadsafe(future.set_result, res)
                except BaseException as exc:
                    if future is not None and not future.cancelled():
                        loop.call_soon_threadsafe(future.set_exception, exc)
                self._queue.task_done()

        async def _dispatch(self, func: Callable[..., Any], *args: Any) -> Any:
            if self._closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            loop = asyncio.get_running_loop()
            future: asyncio.Future[Any] = loop.create_future()
            self._queue.put((func, args, future, loop))
            return await future

        async def execute(self, sql: str, parameters: Any = ()) -> _AsyncCursorCompat:
            def _exec(conn: sqlite3.Connection) -> sqlite3.Cursor:
                cur = conn.execute(sql, parameters)
                self._open_cursors.add(cur)
                return cur

            cur = await self._dispatch(_exec)
            return _AsyncCursorCompat(self, cur)

        async def executemany(self, sql: str, parameters: Any) -> _AsyncCursorCompat:
            def _execm(conn: sqlite3.Connection) -> sqlite3.Cursor:
                cur = conn.executemany(sql, parameters)
                self._open_cursors.add(cur)
                return cur

            cur = await self._dispatch(_execm)
            return _AsyncCursorCompat(self, cur)

        async def executescript(self, sql: str) -> _AsyncCursorCompat:
            def _execs(conn: sqlite3.Connection) -> sqlite3.Cursor:
                cur = conn.executescript(sql)
                self._open_cursors.add(cur)
                return cur

            cur = await self._dispatch(_execs)
            return _AsyncCursorCompat(self, cur)

        async def commit(self) -> None:
            def _commit(conn: sqlite3.Connection) -> None:
                for c in list(self._open_cursors):
                    try:
                        c.close()
                    except Exception:
                        pass
                self._open_cursors.clear()
                conn.commit()

            await self._dispatch(_commit)

        async def rollback(self) -> None:
            def _rollback(conn: sqlite3.Connection) -> None:
                for c in list(self._open_cursors):
                    try:
                        c.close()
                    except Exception:
                        pass
                self._open_cursors.clear()
                conn.rollback()

            await self._dispatch(_rollback)

        async def close(self) -> None:
            if self._closed:
                return
            self._closed = True
            loop = asyncio.get_running_loop()
            future: asyncio.Future[Any] = loop.create_future()

            def _signal_close(conn: sqlite3.Connection) -> None:
                pass

            self._queue.put((_signal_close, (), future, loop))
            self._queue.put(None)
            try:
                await future
            except Exception:
                pass
            await asyncio.to_thread(self._thread.join, 2.0)

        async def __aenter__(self) -> "_AsyncConnectionCompat":
            return self

        async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            if exc_type is not None:
                try:
                    await self.rollback()
                except Exception:
                    pass
            await self.close()

    class aiosqlite:  # type: ignore
        Row = sqlite3.Row
        Connection = _AsyncConnectionCompat

        @staticmethod
        async def connect(database: Union[str, Path], **kwargs: Any) -> _AsyncConnectionCompat:
            return _AsyncConnectionCompat(database, **kwargs)


async def configure_connection(conn: Union[aiosqlite.Connection, Any]) -> None:
    """Configure connection-level SQLite PRAGMAs for high concurrency and data integrity."""
    # 30-second busy timeout to mitigate database locks under concurrency
    await conn.execute("PRAGMA busy_timeout = 30000;")
    # WAL journal mode (for file-based databases)
    await conn.execute("PRAGMA journal_mode = WAL;")
    # Normal synchronous mode for WAL durability without blocking on fsync
    await conn.execute("PRAGMA synchronous = NORMAL;")
    # Foreign key enforcement must be enabled per-connection
    await conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = aiosqlite.Row


class _ConnectionContextManager:
    """Class-based asynchronous context manager for SQLite connections."""

    def __init__(self, manager: "DatabaseConnectionManager") -> None:
        self.manager = manager
        self.conn: Optional[Union[aiosqlite.Connection, Any]] = None

    async def __aenter__(self) -> Union[aiosqlite.Connection, Any]:
        self.manager._ensure_dir()
        if str(self.manager.db_path) == ":memory:":
            if self.manager._mem_conn is None:
                self.manager._mem_conn = await aiosqlite.connect(
                    ":memory:",
                    check_same_thread=False,
                    timeout=30.0,
                )
                await configure_connection(self.manager._mem_conn)
            self.conn = self.manager._mem_conn
            return self.conn

        self.conn = await aiosqlite.connect(
            str(self.manager.db_path),
            check_same_thread=False,
            timeout=30.0,
        )
        try:
            await configure_connection(self.conn)
            return self.conn
        except BaseException:
            await self.conn.close()
            raise

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.conn is not None and str(self.manager.db_path) != ":memory:":
            await self.conn.close()
            self.conn = None


_ConnectionContext = _ConnectionContextManager


class DatabaseConnectionManager:
    """Manages SQLite connections with WAL mode and foreign key enforcement."""

    def __init__(self, db_path: Union[str, Path] = "data/bot.db") -> None:
        self.db_path = Path(db_path) if str(db_path) != ":memory:" else ":memory:"
        self._mem_conn: Optional[Union[aiosqlite.Connection, Any]] = None

    def _ensure_dir(self) -> None:
        if str(self.db_path) != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def get_connection(self) -> _ConnectionContextManager:
        """Deliver a configured SQLite connection context manager."""
        return _ConnectionContextManager(self)

    async def close(self) -> None:
        """Close any persistent in-memory connections."""
        if self._mem_conn is not None:
            await self._mem_conn.close()
            self._mem_conn = None


__all__ = [
    "DatabaseConnectionManager",
    "_ConnectionContextManager",
    "_ConnectionContext",
    "configure_connection",
    "aiosqlite",
]

