"""STDIO isolation.

Over the stdio transport, stdout carries nothing but newline-delimited
JSON-RPC messages -- a single stray byte written anywhere else on stdout
(a library's warning, a print(), a traceback) corrupts the stream and the
client can no longer parse it. Everything in this module exists to make
that structurally hard to get wrong:

1. Logging is configured to write to stderr only, before any other
   module is imported (import side effects can log).
2. `print()` is replaced with a version that defaults to stderr, as a
   safety net for code (ours or a dependency's) that forgot rule 1.
3. Uncaught exceptions (`sys.excepthook`) and uncaught asyncio task
   exceptions (the event loop's exception handler) are routed to stderr
   instead of whatever their default destination would be.

Call `configure()` first thing in `__main__`, before importing `server`.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import sys
import traceback
from typing import Any

logger = logging.getLogger("customer_mcp")

_real_print = builtins.print


def _stderr_print(*args: Any, **kwargs: Any) -> None:
    """`print()` replacement: defaults `file=sys.stderr` unless the caller
    explicitly asked for somewhere else."""
    kwargs.setdefault("file", sys.stderr)
    _real_print(*args, **kwargs)


def _excepthook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
    logger.critical("uncaught exception", exc_info=(exc_type, exc, tb))


def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    message = context.get("message", "unhandled exception in event loop")
    exc = context.get("exception")
    if exc is not None:
        logger.error("asyncio: %s", message, exc_info=exc)
    else:
        logger.error("asyncio: %s | context=%s", message, context)


def configure(level: int = logging.INFO) -> None:
    """Install every stdout safeguard. Idempotent."""
    # basicConfig with force=True so a second call (e.g. in tests) re-applies
    # cleanly instead of being a no-op because a handler already exists.
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    builtins.print = _stderr_print
    sys.excepthook = _excepthook
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.set_exception_handler(_asyncio_exception_handler)


def log_traceback(prefix: str, exc: BaseException) -> None:
    """Log a full traceback to stderr. Never raises."""
    logger.error("%s\n%s", prefix, "".join(traceback.format_exception(exc)))
