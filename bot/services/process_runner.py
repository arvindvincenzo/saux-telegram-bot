import os
import sys
import signal
import asyncio
import logging
import subprocess
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Maximum stdout/stderr bytes kept in memory to prevent OOM on runaway logs
DEFAULT_MAX_CAPTURE_BYTES = 10 * 1024 * 1024  # 10 MB per stream


class BoundedProcessRunner:
    """
    Reusable asynchronous process lifecycle manager.
    - POSIX: Creates independent process groups (start_new_session=True) and sends
      graceful SIGTERM then SIGKILL strictly to child process group (never bot parent).
    - Windows: Uses CREATE_NEW_PROCESS_GROUP and non-blocking taskkill tree termination.
    - Drains pipes continuously to EOF to prevent child deadlocks, while capping in-memory capture.
    - Reaps all completed and terminated processes cleanly to prevent OS zombie processes.
    """

    @staticmethod
    async def run(
        cmd: List[str],
        cwd: Optional[str] = None,
        timeout: float = 180.0,
        max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
        env: Optional[dict] = None
    ) -> Tuple[int, str, str]:
        is_win = (sys.platform == "win32")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if is_win else 0
        start_new_session = not is_win  # POSIX: setsid() decouples child pgid = proc.pid

        # Merge environment if provided
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        logger.debug(f"[PROCESS] Spawning: {' '.join(cmd[:3])} (timeout={timeout}s, cwd={cwd})")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=proc_env,
            creationflags=creationflags,
            start_new_session=start_new_session
        )

        async def _drain_stream(stream: asyncio.StreamReader) -> str:
            buffer = bytearray()
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                if len(buffer) < max_capture_bytes:
                    space_left = max_capture_bytes - len(buffer)
                    buffer.extend(chunk[:space_left])
            return buffer.decode("utf-8", errors="replace")

        try:
            # Concurrently drain stdout and stderr to prevent pipe buffer deadlock
            stdout_coro = _drain_stream(proc.stdout)
            stderr_coro = _drain_stream(proc.stderr)

            stdout_str, stderr_str = await asyncio.wait_for(
                asyncio.gather(stdout_coro, stderr_coro),
                timeout=timeout
            )
            await proc.wait()
            return proc.returncode, stdout_str, stderr_str

        except (asyncio.CancelledError, asyncio.TimeoutError) as err:
            logger.warning(f"[PROCESS] Process interrupted ({type(err).__name__}), terminating tree PID {proc.pid}: {' '.join(cmd[:2])}")
            await BoundedProcessRunner._terminate_tree(proc, is_win)
            if isinstance(err, asyncio.TimeoutError):
                raise TimeoutError(f"Process exceeded execution ceiling of {timeout}s: {' '.join(cmd[:2])}")
            raise

    @staticmethod
    async def _terminate_tree(proc: asyncio.subprocess.Process, is_win: bool):
        """Cleanly terminates process tree without blocking the asyncio event loop."""
        if proc.returncode is not None:
            return  # Already terminated

        try:
            if is_win:
                # Execute taskkill asynchronously to avoid blocking the event loop
                kill_proc = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
                try:
                    await asyncio.wait_for(kill_proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    try:
                        kill_proc.kill()
                    except Exception:
                        pass
            else:
                # POSIX: Send SIGTERM first for graceful tree shutdown
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    return

                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                    return
                except asyncio.TimeoutError:
                    # Escalate to SIGKILL if child ignored SIGTERM
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        return
        except Exception as e:
            logger.debug(f"[PROCESS] Tree termination error for PID {proc.pid}: {e}")
            try:
                proc.kill()
            except Exception:
                pass

        # Reap zombie and close open stream pipes cleanly
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    @classmethod
    def run_bounded_sync(
        cls,
        cmd: List[str],
        cwd: Optional[str] = None,
        timeout: float = 180.0,
        max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
        env: Optional[dict] = None
    ) -> Tuple[int, str, str]:
        """
        Runs BoundedProcessRunner safely across sync boundaries with its own dedicated loop.
        Works seamlessly whether called outside an event loop OR inside an active running event loop.
        Guarantees process group isolation, memory bounds, clean zombie reaping, and returns (returncode, stdout, stderr).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is None:
            return asyncio.run(cls.run(cmd, cwd=cwd, timeout=timeout, max_capture_bytes=max_capture_bytes, env=env))
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, cls.run(cmd, cwd=cwd, timeout=timeout, max_capture_bytes=max_capture_bytes, env=env)).result()


