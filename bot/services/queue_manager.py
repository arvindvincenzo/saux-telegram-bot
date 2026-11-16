import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)


class BoundedTaskQueue:
    """
    Bounded worker queue controller with per-user limits and backpressure.
    - Limits concurrent worker tasks according to physical hardware capabilities.
    - Limits waiting queue depth to prevent memory ballooning under traffic spikes.
    - Enforces per-user concurrency ceilings (default: 1) to prevent single-user starvation.
    - Guarantees user state cleanup in all execution paths (success, error, cancellation).
    - Supports graceful shutdown and queue draining.
    """

    def __init__(self, name: str, max_workers: int, max_queue: int, max_per_user: int = 1):
        self.name = name
        self.max_workers = max_workers
        self.max_queue = max_queue
        self.max_per_user = max_per_user

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self._user_counts: Dict[int, int] = {}
        self._lock = asyncio.Lock()
        self._workers: List[asyncio.Task] = []
        self._is_shutdown = False

    def start(self):
        """Spawns dedicated consumer worker tasks."""
        if self._workers:
            return  # Already running

        self._is_shutdown = False
        for i in range(self.max_workers):
            task = asyncio.create_task(self._worker_loop(i), name=f"{self.name}-worker-{i}")
            self._workers.append(task)
        logger.info(f"[QUEUE] Started {self.name} with {self.max_workers} workers (max_queue={self.max_queue}, max_per_user={self.max_per_user})")

    async def submit(self, user_id: int, coro_fn: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        """
        Submits a unit of work to the queue.
        Raises BufferError if queue is full or user limit reached.
        """
        if self._is_shutdown:
            raise RuntimeError(f"Queue {self.name} is shutting down.")

        async with self._lock:
            # 1. Per-User Limit Check
            current_user_jobs = self._user_counts.get(user_id, 0)
            if current_user_jobs >= self.max_per_user:
                raise BufferError(
                    f"You already have a {self.name} operation in progress. "
                    "Please wait for it to complete before submitting another."
                )

            # 2. Queue Backpressure Check
            if self._queue.qsize() >= self.max_queue:
                raise BufferError(
                    f"The {self.name} processing queue is temporarily full. "
                    "Please try again in a few moments."
                )

            # Increment user active counter
            self._user_counts[user_id] = current_user_jobs + 1

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        job = (user_id, coro_fn, fut)

        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            # Rollback user count if queue rejected
            async with self._lock:
                self._decrement_user(user_id)
            raise BufferError(f"The {self.name} queue is full. Request rejected under backpressure.")

        try:
            return await fut
        except asyncio.CancelledError:
            fut.cancel()
            raise

    async def _worker_loop(self, worker_id: int):
        """Consumer worker processing queue items."""
        while not self._is_shutdown:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                break

            if item is None:  # Shutdown sentinel
                self._queue.task_done()
                break

            user_id, coro_fn, fut = item

            # If caller cancelled while waiting in queue, skip execution
            if fut.cancelled():
                async with self._lock:
                    self._decrement_user(user_id)
                self._queue.task_done()
                continue

            # Forward cancellation from fut to running worker task
            exec_task = asyncio.create_task(coro_fn())
            def _on_fut_done(f):
                if f.cancelled() and not exec_task.done():
                    exec_task.cancel()
            fut.add_done_callback(_on_fut_done)

            try:
                result = await exec_task
                if not fut.cancelled():
                    fut.set_result(result)
            except asyncio.CancelledError:
                if not fut.cancelled():
                    fut.cancel()
            except Exception as e:
                logger.error(f"[QUEUE] Error in {self.name} worker {worker_id} for user {user_id}: {e}")
                if not fut.cancelled():
                    fut.set_exception(e)
            finally:
                async with self._lock:
                    self._decrement_user(user_id)
                self._queue.task_done()

    def _decrement_user(self, user_id: int):
        new_count = self._user_counts.get(user_id, 1) - 1
        if new_count <= 0:
            self._user_counts.pop(user_id, None)
        else:
            self._user_counts[user_id] = new_count

    async def shutdown(self, timeout: float = 5.0):
        """Gracefully drains and stops all worker tasks."""
        self._is_shutdown = True
        logger.info(f"[QUEUE] Shutting down {self.name}...")

        # Push shutdown sentinels
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except Exception:
                pass

        try:
            await asyncio.wait_for(asyncio.gather(*self._workers, return_exceptions=True), timeout=timeout)
        except asyncio.TimeoutError:
            for w in self._workers:
                w.cancel()
        self._workers.clear()
        self._user_counts.clear()
        logger.info(f"[QUEUE] {self.name} shut down cleanly.")


# Global singleton queues configured for 16-Core / 16GB Host
# yt-dlp is primarily network bound; max 3 concurrent streams
video_download_queue = BoundedTaskQueue(
    name="media-downloader",
    max_workers=3,
    max_queue=15,
    max_per_user=1
)

# FFmpeg is CPU bound; max 2 concurrent conversion processes (leaves 12 cores free)
media_convert_queue = BoundedTaskQueue(
    name="media-converter",
    max_workers=2,
    max_queue=10,
    max_per_user=1
)
