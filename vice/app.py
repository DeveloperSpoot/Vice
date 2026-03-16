"""
Vice desktop app — opens the web UI in a native pywebview window.

Launched via `vice-app` (app icon, launcher, or command line).

Behaviour:
  • Starts the Vice daemon subprocess if it isn't already running.
  • Waits for the HTTP server to be ready, then opens a native window.
  • Exposes a JS API so the UI can call vice.quit() to stop the daemon
    and close the window cleanly.
  • Closing the window without vice.quit() keeps recording running.
  • Sending SIGTERM to vice-app (for example: killall vice-app) now
    forwards a clean stop request to the daemon before exit.
  • Re-launching vice-app when the daemon is already running just opens
    a new window connected to the existing session.

Falls back to xdg-open (browser) if pywebview is not installed.
Errors are logged to ~/.local/share/vice/vice-app.log when running
without a terminal (e.g. from the app launcher).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .runtime import actual_home_dir, normalize_runtime_environment

SOCKET_FILE = Path("/tmp/vice/vice.sock")
WINDOW_TITLE = "Vice"
LOG_FILE = actual_home_dir() / ".local" / "share" / "vice" / "vice-app.log"


# ── logging ───────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    """Log to file when stdout is not a TTY (i.e. launched from app menu)."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(LOG_FILE),
    ]
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [vice-app] %(levelname)s: %(message)s",
        handlers=handlers,
    )


log = logging.getLogger("vice-app")


def _handle_app_terminate(signum: int, _frame) -> None:
    """Stop daemon when vice-app is terminated externally."""
    log.info("Received signal %s, stopping daemon before exit", signum)
    try:
        _stop_daemon()
    finally:
        raise SystemExit(0)


# ── helpers ───────────────────────────────────────────────────────────────────

def _vice_cmd() -> list[str]:
    """Return the command to run the vice daemon.

    Tries (in order):
      1. Absolute ~/.local/bin/vice  (covers both pip-user and venv symlink)
      2. shutil.which("vice")        (works if PATH is set correctly)
      3. sys.executable -m vice.main (fallback using same Python as vice-app)
    """
    user_bin = actual_home_dir() / ".local" / "bin" / "vice"
    if user_bin.exists():
        return [str(user_bin)]
    found = shutil.which("vice")
    if found:
        return [found]
    # Last resort: run as a module with the same Python interpreter
    return [sys.executable, "-m", "vice.main"]


def _daemon_responds(timeout: float = 1.0) -> bool:
    """Return True when the Unix socket accepts an IPC request."""
    return _daemon_status(timeout=timeout) is not None


def _daemon_status(timeout: float = 1.0) -> dict | None:
    """Return daemon IPC status JSON, or None when the socket is unusable."""
    if not SOCKET_FILE.exists():
        return None

    async def _probe() -> dict | None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(SOCKET_FILE)),
                timeout=timeout,
            )
            writer.write(b"status\n")
            await writer.drain()
            resp = await asyncio.wait_for(reader.readline(), timeout=timeout)
            writer.close()
            await writer.wait_closed()
            if not resp:
                return None
            import json
            return json.loads(resp)
        except Exception:
            return None

    return asyncio.run(_probe())


def _start_daemon() -> None:
    """Launch the daemon as a detached background process (no-op if running)."""
    normalize_runtime_environment()

    if SOCKET_FILE.exists():
        if _daemon_responds():
            log.info("Daemon already running (socket is responsive)")
            return
        log.warning("Found stale daemon socket at %s; removing it", SOCKET_FILE)
        try:
            SOCKET_FILE.unlink()
        except OSError as exc:
            log.error("Could not remove stale socket %s: %s", SOCKET_FILE, exc)
            raise
    cmd = _vice_cmd() + ["start", "--no-open-ui"]
    log.info("Starting daemon: %s", " ".join(cmd))
    try:
        subprocess.Popen(
            cmd,
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,   # detach from our process group
        )
    except Exception as exc:
        log.error("Failed to start daemon: %s", exc)
        raise


def _stop_daemon() -> None:
    """Ask the daemon to shut down via IPC."""
    if not SOCKET_FILE.exists():
        return
    try:
        async def _send():
            reader, writer = await asyncio.open_unix_connection(str(SOCKET_FILE))
            writer.write(b"stop\n")
            await writer.drain()
            writer.close()
        asyncio.run(_send())
    except Exception as exc:
        log.debug("Stop IPC error: %s", exc)


def _wait_for_server(url: str, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1) as resp:
                status = getattr(resp, "status", 200)
                if 200 <= status < 400:
                    return True
        except HTTPError as exc:
            if 200 <= exc.code < 400:
                return True
            log.debug("Server probe failed for %s with HTTP %s", url, exc.code)
            time.sleep(0.25)
        except URLError:
            time.sleep(0.25)
        except Exception:
            time.sleep(0.25)
    return False


def _server_url_from_status(status: dict | None, fallback_url: str) -> str:
    raw = (status or {}).get("local_url")
    if not raw or not isinstance(raw, str):
        return fallback_url
    return raw.rstrip("/") + "/"


def _clear_stale_socket() -> None:
    if not SOCKET_FILE.exists():
        return
    if _daemon_responds():
        return
    log.warning("Removing stale daemon socket at %s", SOCKET_FILE)
    SOCKET_FILE.unlink(missing_ok=True)


def _ensure_server(default_url: str, startup_timeout: float = 20.0) -> str | None:
    status = _daemon_status()
    if status is not None:
        url = _server_url_from_status(status, default_url)
        if _wait_for_server(url, timeout=2.0):
            log.info("Daemon already running (IPC + HTTP healthy)")
            return url

        log.warning("Daemon IPC responded but UI server did not (%s); restarting daemon", url)
        _stop_daemon()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if _daemon_status(timeout=0.5) is None:
                break
            time.sleep(0.1)
        _clear_stale_socket()
    else:
        _clear_stale_socket()

    _start_daemon()

    if _wait_for_server(default_url, timeout=startup_timeout):
        return default_url

    status = _daemon_status()
    url = _server_url_from_status(status, default_url)
    if url != default_url and _wait_for_server(url, timeout=2.0):
        return url

    if status is not None:
        log.error("Daemon IPC is alive but HTTP UI is unavailable at %s", url)
    return None


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    normalize_runtime_environment()
    _setup_logging()
    signal.signal(signal.SIGTERM, _handle_app_terminate)
    signal.signal(signal.SIGINT, _handle_app_terminate)
    log.info("vice-app starting (python=%s)", sys.executable)

    try:
        from .config import load as load_config
        cfg  = load_config()
        port = cfg.sharing.port
    except Exception as exc:
        log.error("Failed to load config: %s", exc)
        port = 8765

    url = f"http://localhost:{port}/"

    try:
        server_url = _ensure_server(url)
    except Exception:
        # Error already logged; show a user-visible message and exit.
        _show_error(
            "Vice could not start the recording daemon.\n\n"
            f"Check the log for details:\n{LOG_FILE}"
        )
        sys.exit(1)

    log.info("Waiting for server at %s", url)
    if not server_url:
        log.error("Server did not start within 20 s")
        _show_error(
            "Vice started but the UI server did not respond.\n\n"
            f"Check the log for details:\n{LOG_FILE}"
        )
        sys.exit(1)

    log.info("Server ready at %s, opening window", server_url)
    # Disable WebKit GPU compositing — prevents a segfault crash on Wayland
    # compositors (Hyprland, sway, GNOME) where WebKit's GL backend conflicts
    # with the compositor's own rendering. Must be set before webview imports.
    os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
    os.environ.setdefault("WEBKIT_DISABLE_SANDBOX", "1")
    try:
        import webview  # type: ignore[import]
        _run_webview(server_url)
        log.info("Window closed")
    except ImportError:
        log.warning("pywebview not installed — falling back to browser")
        subprocess.Popen(
            ["xdg-open", server_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.error("pywebview crashed: %s", exc, exc_info=True)
        # Fall back to browser so the user isn't left with nothing
        log.warning("Falling back to browser")
        subprocess.Popen(
            ["xdg-open", server_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _show_error(message: str) -> None:
    """Show a visible error — GTK dialog if possible, otherwise print."""
    log.error("UI error: %s", message)
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        diag = Gtk.MessageDialog(
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text="Vice — Error",
            secondary_text=message,
        )
        diag.run()
        diag.destroy()
    except Exception:
        print(f"[vice-app] ERROR: {message}", file=sys.stderr)


# ── pywebview window ──────────────────────────────────────────────────────────

def _run_webview(url: str) -> None:
    import webview  # type: ignore[import]

    class _API:
        """Methods exposed to JavaScript as window.pywebview.api.*"""

        def __init__(self) -> None:
            self._win: webview.Window | None = None

        def _bind(self, win: "webview.Window") -> None:
            self._win = win

        def quit_app(self) -> None:
            """Stop the daemon and close the window."""
            _stop_daemon()
            if self._win:
                self._win.destroy()

        def keep_running(self) -> None:
            """Close the window but keep the daemon recording."""
            if self._win:
                self._win.destroy()

        def open_url(self, url: str) -> None:
            """Open a URL in the system's default browser via xdg-open."""
            import subprocess as _sp
            try:
                _sp.Popen(
                    ["xdg-open", url],
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
                )
            except Exception:
                pass

    api = _API()

    win = webview.create_window(
        title=WINDOW_TITLE,
        url=url,
        js_api=api,
        width=1280,
        height=820,
        min_size=(900, 600),
        background_color="#080b12",
        text_select=False,
        zoomable=False,
    )
    api._bind(win)

    webview.start(debug=False, private_mode=False)
    log.info("Window closed")


if __name__ == "__main__":
    main()
