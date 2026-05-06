#!/usr/bin/env python3
"""
Production-Ready Exit Management System (Merged Implementation)

Highlights:
- Graceful shutdown with robust signal handling (SIGINT, SIGTERM, SIGQUIT/SIGBREAK)
- Cleanup callback registry with priorities and per-task timeouts
- Overall shutdown deadline to prevent indefinite hangs
- Standardized Unix exit codes (sysexits.h-style)
- Thread-safe singleton with idempotent exit
- Escalation: second signal forces immediate termination
- Logging integration and context manager support
- atexit safety to ensure cleanup on normal interpreter termination
"""

import atexit
import enum
import logging
import os
import signal
import sys
import threading
import time
from typing import Callable, List, Optional, Union

# Module-level logger
logger = logging.getLogger(__name__)


class ExitCode(enum.IntEnum):
    """Standard Unix exit codes based on sysexits.h and common conventions."""
    SUCCESS = 0
    FAILURE = 1
    USAGE = 64          # command line usage error
    DATAERR = 65        # data format error
    NOINPUT = 66        # cannot open input
    NOUSER = 67         # addressee unknown
    NOHOST = 68         # host name unknown
    UNAVAILABLE = 69    # service unavailable
    SOFTWARE = 70       # internal software error
    OSERR = 71          # system error (e.g., can't fork)
    OSFILE = 72         # critical OS file missing
    CANTCREAT = 73      # can't create user output file
    IOERR = 74          # input/output error
    TEMPFAIL = 75       # temp failure; user is invited to retry
    PROTOCOL = 76       # remote error in protocol
    NOPERM = 77         # permission denied
    CONFIG = 78         # configuration error


class ExitError(Exception):
    """Custom exception class for exit-related errors."""
    pass


class CleanupCallback:
    """
    Represents a cleanup callback with metadata.

    Attributes:
        func: Callable to invoke during cleanup.
        name: Human-readable name used for logging.
        priority: Lower value executes earlier (0-100 typical).
        timeout: Maximum seconds to wait for this task.
    """
    __slots__ = ("func", "name", "priority", "timeout")

    def __init__(
        self,
        func: Callable[[], None],
        name: str,
        priority: int = 50,
        timeout: Optional[float] = None,
    ):
        self.func = func
        self.name = name
        self.priority = int(priority)
        self.timeout = float(timeout) if timeout is not None else 30.0

    def __call__(self) -> bool:
        try:
            logger.debug(f"Executing cleanup task: {self.name}")
            self.func()
            return True
        except Exception as e:
            logger.error(f"Cleanup task '{self.name}' raised an exception: {e}", exc_info=True)
            return False


def _sanitize_exit_code(code: Union[int, ExitCode]) -> int:
    """
    Sanitize exit code to 0-255 range for POSIX compatibility.
    """
    try:
        n = int(code)
    except Exception:
        logger.warning(f"Invalid exit code '{code}', defaulting to {int(ExitCode.FAILURE)}")
        n = int(ExitCode.FAILURE)
    if n < 0:
        n = 256 + (n % 256)
    # POSIX shells only see 0-255; ensure in range
    n = n % 256
    return n


class ExitManager:
    """
    Manages application lifecycle and graceful shutdown.

    - Singleton instance ensures process-wide coordination.
    - Thread-safe registration and execution of cleanup callbacks.
    - Signal handling for SIGINT/SIGTERM (+ SIGQUIT on POSIX, SIGBREAK on Windows).
    - Escalation: the second signal forces immediate process exit.
    """

    _instance: Optional["ExitManager"] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs) -> "ExitManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    cls._instance = inst
                    cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        shutdown_timeout: float = 30.0,  # overall shutdown deadline
        register_atexit: bool = True,
    ):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self._handlers_lock = threading.RLock()
        self._cleanup_handlers: List[CleanupCallback] = []

        self._is_shutting_down = False
        self._shutdown_event = threading.Event()
        self._shutdown_started_at: Optional[float] = None
        self._shutdown_timeout = float(shutdown_timeout)
        self._exit_code: int = int(ExitCode.SUCCESS)

        self._signals_registered = False
        self._signal_count = 0
        self._original_handlers = {}  # type: ignore[var-annotated]

        if register_atexit:
            # Ensure cleanup runs if interpreter exits without explicit ExitManager.exit()
            atexit.register(self._atexit_guard)

    # Public API

    def register_signal_handlers(self) -> None:
        """
        Register OS signal handlers for graceful shutdown.

        Notes:
        - Should be called from the main thread.
        - Idempotent: calling multiple times is safe.
        """
        if self._signals_registered:
            return

        def safe_register(sig, handler):
            try:
                self._original_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, handler)
            except Exception as e:
                logger.debug(f"Skipping registration for signal {sig}: {e}")

        # Cross-platform friendly signals
        safe_register(signal.SIGINT, self._signal_handler)
        safe_register(signal.SIGTERM, self._signal_handler)

        # POSIX-only
        if hasattr(signal, "SIGQUIT"):
            safe_register(signal.SIGQUIT, self._force_exit_handler)

        # Windows Ctrl+Break
        if hasattr(signal, "SIGBREAK"):
            safe_register(signal.SIGBREAK, self._signal_handler)

        self._signals_registered = True
        logger.info("Signal handlers registered for graceful shutdown")

    def add_cleanup(
        self,
        func: Callable[[], None],
        name: Optional[str] = None,
        priority: int = 50,
        timeout: Optional[float] = None,
    ) -> None:
        """
        Register a cleanup function to be invoked during shutdown.

        Args:
            func: Callable with no arguments.
            name: Optional human-friendly name; defaults to func.__name__.
            priority: Lower numbers execute first (0 is highest priority).
            timeout: Per-task timeout in seconds (defaults to 30 seconds).
        """
        if not callable(func):
            raise ValueError("func must be callable")

        nm = name or getattr(func, "__name__", repr(func))
        pr = max(min(int(priority), 1000000), -1000000)  # clamp to reasonable range

        cb = CleanupCallback(func=func, name=nm, priority=pr, timeout=timeout)

        with self._handlers_lock:
            self._cleanup_handlers.append(cb)
            self._cleanup_handlers.sort(key=lambda c: c.priority)
            logger.debug(f"Registered cleanup handler: {nm} (priority={pr}, timeout={cb.timeout}s)")

    def remove_cleanup(self, name_or_func: Union[str, Callable[[], None]]) -> bool:
        """
        Remove a cleanup handler by name or function.

        Returns True if a handler was removed.
        """
        with self._handlers_lock:
            before = len(self._cleanup_handlers)
            if isinstance(name_or_func, str):
                self._cleanup_handlers = [h for h in self._cleanup_handlers if h.name != name_or_func]
            else:
                self._cleanup_handlers = [h for h in self._cleanup_handlers if h.func is not name_or_func]
            removed = len(self._cleanup_handlers) < before
            if removed:
                logger.debug(f"Removed cleanup handler: {name_or_func}")
            return removed

    def is_shutting_down(self) -> bool:
        """Returns True if the shutdown sequence has started."""
        return self._is_shutting_down

    def wait_for_shutdown(self, timeout: Optional[float] = None) -> bool:
        """
        Block until shutdown is initiated or timeout expires.
        Returns True if shutdown started, False if timeout elapsed.
        """
        return self._shutdown_event.wait(timeout)

    def exit(
        self,
        code: Union[int, ExitCode] = ExitCode.SUCCESS,
        immediate: bool = False,
        message: Optional[str] = None,
    ) -> None:
        """
        Initiate shutdown sequence.

        Args:
            code: Exit code to return to the OS (0-255).
            immediate: If True, skip cleanup and exit immediately.
            message: Optional log message.
        """
        if self._is_shutting_down and not immediate:
            logger.warning("Exit already in progress; ignoring duplicate call")
            return

        if message:
            logger.info(message)

        sanitized = _sanitize_exit_code(code)
        self._exit_code = sanitized

        if immediate:
            logger.warning("Immediate exit requested; skipping cleanup")
            self._safe_sys_exit(self._exit_code)  # triggers atexit handlers; we guard against double-run
            return

        # Begin graceful shutdown
        self._is_shutting_down = True
        self._shutdown_started_at = time.monotonic()
        self._shutdown_event.set()

        try:
            self._execute_cleanup()
        finally:
            # Final exit (after cleanup)
            self._safe_sys_exit(self._exit_code)

    # Internal mechanics

    def _signal_handler(self, signum: int, frame) -> None:
        """Handle SIGINT/SIGTERM (and SIGBREAK on Windows) with escalation."""
        self._signal_count += 1
        sig_name = None
        try:
            sig_name = signal.Signals(signum).name  # type: ignore[attr-defined]
        except Exception:
            sig_name = f"SIG({signum})"
        if self._signal_count == 1:
            logger.info(f"Received {sig_name}; initiating graceful shutdown")
            # Default to SUCCESS unless caller overrides elsewhere; keep current code if non-success
            if not self._is_shutting_down:
                self.exit(self._exit_code or ExitCode.SUCCESS, immediate=False)
        else:
            logger.error(f"Received {sig_name} again; forcing immediate termination")
            os._exit(int(ExitCode.FAILURE))

    def _force_exit_handler(self, signum: int, frame) -> None:
        """Handle SIGQUIT for immediate termination without cleanup."""
        try:
            sig_name = signal.Signals(signum).name  # type: ignore[attr-defined]
        except Exception:
            sig_name = f"SIG({signum})"
        logger.warning(f"Received {sig_name}; forcing immediate termination (no cleanup)")
        os._exit(int(ExitCode.FAILURE))

    def _execute_cleanup(self) -> None:
        """
        Execute all registered cleanup handlers by priority with timeouts and an overall deadline.
        If any handler fails or times out and the exit code was SUCCESS, upgrade to SOFTWARE error.
        """
        with self._handlers_lock:
            handlers = list(self._cleanup_handlers)

        if not handlers:
            logger.debug("No cleanup handlers registered; proceeding to exit")
            return

        overall_deadline = (self._shutdown_started_at or time.monotonic()) + self._shutdown_timeout
        logger.info(f"Executing {len(handlers)} cleanup handler(s) with overall timeout {self._shutdown_timeout:.1f}s")

        any_failed = False

        for handler in handlers:
            now = time.monotonic()
            remaining_overall = overall_deadline - now
            if remaining_overall <= 0:
                logger.error("Overall shutdown timeout exceeded; skipping remaining cleanup")
                any_failed = True
                break

            per_task_timeout = min(handler.timeout, remaining_overall)
            if per_task_timeout <= 0:
                logger.warning(f"Skipping cleanup '{handler.name}' due to insufficient overall time remaining")
                any_failed = True
                continue

            try:
                ok = self._run_with_timeout(handler, per_task_timeout)
                if not ok:
                    logger.error(f"Cleanup '{handler.name}' failed or timed out")
                    any_failed = True
            except Exception as e:
                logger.exception(f"Unexpected error in cleanup '{handler.name}': {e}")
                any_failed = True

        if any_failed and self._exit_code == int(ExitCode.SUCCESS):
            logger.warning("One or more cleanup tasks failed; updating exit code to SOFTWARE")
            self._exit_code = int(ExitCode.SOFTWARE)

    def _run_with_timeout(self, handler: CleanupCallback, timeout: float) -> bool:
        """
        Execute a cleanup handler with a timeout by running it in a daemon thread.
        Returns True on success, False on timeout or failure.
        """
        if timeout <= 0:
            return handler()

        result = {"ok": False}

        def target():
            result["ok"] = handler()

        t = threading.Thread(target=target, name=f"cleanup:{handler.name}", daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            logger.error(f"Cleanup '{handler.name}' timed out after {timeout:.1f}s")
            return False
        return bool(result["ok"])

    def _safe_sys_exit(self, code: int) -> None:
        """
        Exit the process via sys.exit with guarded atexit to prevent duplicate cleanup.
        """
        try:
            sys.exit(code)
        except SystemExit as se:
            raise se

    def _atexit_guard(self) -> None:
        """
        atexit hook to run cleanup if the interpreter exits without explicit ExitManager.exit().
        Ensures idempotency.
        """
        if self._is_shutting_down:
            return
        logger.info("Interpreter exiting; performing best-effort cleanup via atexit")
        self._is_shutting_down = True
        self._shutdown_started_at = time.monotonic()
        self._shutdown_event.set()
        try:
            self._execute_cleanup()
        except Exception as e:
            logger.error(f"Error during atexit cleanup: {e}", exc_info=True)

    # Context manager support

    def __enter__(self) -> "ExitManager":
        self.register_signal_handlers()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is not None:
            logger.error(f"Unhandled exception: {exc_val}", exc_info=True)
            # Use SOFTWARE exit code when leaving due to exception if not already shutting down
            if not self._is_shutting_down:
                self.exit(ExitCode.SOFTWARE, immediate=False)
        # Do not suppress exceptions
        return False


def setup_logging(level: int = logging.INFO) -> None:
    """
    Configure basic logging for the module/application.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def die(message: str, code: Union[int, ExitCode] = ExitCode.FAILURE) -> None:
    """
    Log an error and exit immediately (skips registered cleanup handlers).
    """
    logger.error(message)
    os._exit(_sanitize_exit_code(code))


# Example usage
if __name__ == "__main__":
    setup_logging(logging.DEBUG)
    manager = ExitManager(shutdown_timeout=15.0)
    manager.register_signal_handlers()

    def close_database():
        logger.info("Closing database connections...")
        time.sleep(1.0)

    def flush_logs():
        logger.info("Flushing logs...")
        time.sleep(0.2)

    def notify_monitoring():
        logger.info("Notifying monitoring system...")
        time.sleep(0.3)

    manager.add_cleanup(close_database, "db_close", priority=10, timeout=5.0)
    manager.add_cleanup(flush_logs, "log_flush", priority=50, timeout=3.0)
    manager.add_cleanup(notify_monitoring, "metrics", priority=100, timeout=3.0)

    try:
        with manager:
            logger.info("Application running. Press Ctrl+C to terminate.")
            while not manager.is_shutting_down():
                time.sleep(1.0)
                logger.debug("Working...")
    except KeyboardInterrupt:
        # Handled by signal handler; nothing else to do here.
        pass
