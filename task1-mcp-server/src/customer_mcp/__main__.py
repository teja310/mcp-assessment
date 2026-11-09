"""Entry point: `python -m customer_mcp`.

Order matters here: `logging_setup.configure()` must run before any other
project module is imported, so a log call made as a side effect of import
(none currently, but a future dependency might) can't slip out on stdout
before stderr-only logging is installed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from customer_mcp import logging_setup

logging_setup.configure()

from customer_mcp.server import build_server, run_stdio  # noqa: E402 - must follow configure()
from customer_mcp.store import CustomerStore  # noqa: E402

logger = logging.getLogger("customer_mcp.main")


async def _amain() -> None:
    loop = asyncio.get_running_loop()
    # Re-install the asyncio exception handler on the actual running loop
    # (configure() could only install it on a loop obtained best-effort,
    # before the loop that will actually run existed).
    loop.set_exception_handler(logging_setup._asyncio_exception_handler)

    logger.info("customer_mcp server starting (pid=%s)", os.getpid())
    store = CustomerStore()
    server = build_server(store)

    stop_event = asyncio.Event()

    def _handle_signal(sig: int) -> None:
        logger.info("received signal %s, shutting down", signal.Signals(sig).name)
        stop_event.set()

    # loop.add_signal_handler is POSIX-only (fine: this server targets
    # macOS/Linux); it lets SIGINT/SIGTERM trigger a clean asyncio-native
    # shutdown instead of an abrupt KeyboardInterrupt mid-stream-write.
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: _handle_signal(s))

    serve_task = asyncio.ensure_future(run_stdio(server))
    stop_task = asyncio.ensure_future(stop_event.wait())
    done, pending = await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

    if stop_task in done and serve_task not in done:
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
    elif serve_task in done:
        # EOF on stdin (or a clean server-side shutdown): not an error.
        exc = serve_task.exception()
        if exc is not None:
            raise exc


def main() -> int:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        # Belt-and-suspenders: if SIGINT arrives before the signal handler is
        # installed, fall back to the default asyncio.run() behavior cleanly.
        logger.info("interrupted, exiting")
    except Exception as exc:  # noqa: BLE001 - top-level safety net
        logging_setup.log_traceback("fatal error in main()", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
