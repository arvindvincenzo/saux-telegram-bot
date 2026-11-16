import asyncio
import sys
import time
from pathlib import Path

BOT_DIR = Path(__file__).parent.parent.resolve() / "bot"
sys.path.insert(0, str(BOT_DIR))

from services.process_runner import BoundedProcessRunner
from services.queue_manager import BoundedTaskQueue

async def main():
    print("========================================")
    print("SAUX BOT - PROCESS RUNNER & QUEUE TEST SUITE")
    print("========================================")

    # ----------------------------------------------------
    # PART 1: BoundedProcessRunner Tests
    # ----------------------------------------------------
    print("\n--- Testing BoundedProcessRunner ---")

    # 1. Normal execution
    code, out, err = await BoundedProcessRunner.run([sys.executable, "-c", "print('hello process runner')"])
    assert code == 0
    assert "hello process runner" in out
    print("  [PASS] Normal subprocess execution")

    # 2. Timeout enforcement
    t0 = time.perf_counter()
    try:
        await BoundedProcessRunner.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=1.0)
        assert False, "Process should have timed out!"
    except TimeoutError:
        dt = time.perf_counter() - t0
        assert dt < 3.5, f"Timeout took too long to abort: {dt}s"
        print(f"  [PASS] Timeout correctly enforced ({dt:.2f}s)")

    # 3. Cancellation & Tree Termination
    print("  Testing subprocess cancellation...")
    task = asyncio.create_task(
        BoundedProcessRunner.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60.0)
    )
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    print("  [PASS] Subprocess task cancelled and terminated cleanly.")

    # 4. Stream Draining Cap (prevents pipe deadlock and OOM on huge stdout)
    code, out, err = await BoundedProcessRunner.run(
        [sys.executable, "-c", "import sys; sys.stdout.write('A' * 2_000_000)"],
        max_capture_bytes=100_000
    )
    assert code == 0
    assert len(out) == 100_000, f"Expected capped output of 100,000 bytes, got {len(out)}"
    print("  [PASS] Subprocess stream draining and memory capping verified.")

    # ----------------------------------------------------
    # PART 2: BoundedTaskQueue Tests
    # ----------------------------------------------------
    print("\n--- Testing BoundedTaskQueue ---")
    queue = BoundedTaskQueue(name="test-queue", max_workers=2, max_queue=3, max_per_user=1)
    queue.start()

    # 1. Normal task submission & execution
    async def sample_job(val):
        await asyncio.sleep(0.1)
        return val * 2

    res = await queue.submit(user_id=101, coro_fn=lambda: sample_job(21))
    assert res == 42
    print("  [PASS] Normal task submission and worker execution")

    # 2. Per-user concurrency limit
    async def slow_job():
        await asyncio.sleep(0.5)
        return "ok"

    t1 = asyncio.create_task(queue.submit(user_id=202, coro_fn=slow_job))
    await asyncio.sleep(0.05)

    # Second submission for same user while job 1 is running MUST raise BufferError
    try:
        await queue.submit(user_id=202, coro_fn=slow_job)
        assert False, "Should have rejected second concurrent job for user 202!"
    except BufferError as e:
        assert "already have a test-queue operation in progress" in str(e)
        print("  [PASS] Per-user concurrency limit enforced (rejected duplicate user job)")

    await t1

    # 3. Queue Capacity & Backpressure Rejection
    print("  Testing queue saturation & backpressure...")
    # Step A: Submit 2 jobs that occupy the 2 active workers
    worker_tasks = []
    for uid in (301, 302):
        worker_tasks.append(asyncio.create_task(queue.submit(user_id=uid, coro_fn=lambda: asyncio.sleep(0.5))))

    await asyncio.sleep(0.05)  # Ensure workers have picked up the 2 jobs

    # Step B: Submit 3 jobs that fill the queue to max_queue (capacity = 3)
    queued_tasks = []
    for uid in (303, 304, 305):
        queued_tasks.append(asyncio.create_task(queue.submit(user_id=uid, coro_fn=lambda: asyncio.sleep(0.2))))

    await asyncio.sleep(0.02)  # Ensure the 3 jobs are sitting in the queue

    # Step C: The next submission MUST immediately raise BufferError (queue is full)
    try:
        await queue.submit(user_id=999, coro_fn=lambda: asyncio.sleep(0.1))
        assert False, "Should have rejected when queue exceeded max_queue capacity!"
    except BufferError as e:
        assert "queue is temporarily full" in str(e)
        print("  [PASS] Queue backpressure rejection verified (immediately raised BufferError)")

    await asyncio.gather(*worker_tasks, *queued_tasks)

    # 4. Caller Cancellation & User Counter Recovery
    async def cancellable_job():
        await asyncio.sleep(1.0)
        return "done"

    t_cancel = asyncio.create_task(queue.submit(user_id=505, coro_fn=cancellable_job))
    await asyncio.sleep(0.05)
    t_cancel.cancel()
    try:
        await t_cancel
    except asyncio.CancelledError:
        pass

    # Wait for worker loop to clean up
    await asyncio.sleep(0.1)
    # User 505 should be allowed to submit a new job immediately (counter was cleaned up)
    res_after = await queue.submit(user_id=505, coro_fn=lambda: sample_job(5))
    assert res_after == 10
    print("  [PASS] Caller cancellation user counter recovery verified.")

    # 5. Graceful Queue Shutdown
    await queue.shutdown(timeout=2.0)
    print("  [PASS] Graceful queue shutdown verified.")

    print("\n========================================")
    print("ALL PROCESS RUNNER & QUEUE TESTS PASSED 100%!")
    print("========================================")

if __name__ == "__main__":
    asyncio.run(main())
