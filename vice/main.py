"""
Vice — Linux game clip recorder daemon + CLI.

Commands:
  vice start          Start the daemon (recorder + hotkey listener + share server)
  vice ui             Open the web UI in the default browser
  vice clip           Manually save a clip right now (daemon must be running)
  vice stop           Stop the daemon
  vice status         Show daemon status and recent clips
  vice config         Print the current config path and contents
  vice list-keys      Show available hotkey names (KEY_*)
  vice open-config    Open config in $EDITOR
  vice uninstall      Remove Vice cleanly (service, config, optionally clips)
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import asdict
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from . import __version__
from .config import load as load_config, save as save_config, CONFIG_PATH, CONFIG_DIR
from .hotkey import HotkeyListener, can_access_hotkeys, list_available_keys
from .recorder import create_recorder
from .runtime import actual_home_dir, normalize_runtime_environment, resolve_path
from .share import ShareServer
from . import audio

log = logging.getLogger("vice")

PID_FILE    = Path("/tmp/vice/vice.pid")
SOCKET_FILE = Path("/tmp/vice/vice.sock")
USER_BIN_DIR = actual_home_dir() / ".local" / "bin"
INSTALL_VENV_DIR = actual_home_dir() / ".local" / "share" / "vice" / "venv"
USER_DESKTOP_FILE = actual_home_dir() / ".local" / "share" / "applications" / "vice.desktop"
USER_ICON_FILE = (
    actual_home_dir()
    / ".local"
    / "share"
    / "icons"
    / "hicolor"
    / "scalable"
    / "apps"
    / "vice.svg"
)
DAEMON_LOG_FILE = actual_home_dir() / ".local" / "share" / "vice" / "vice.log"


# ──────────────────────────────────────────────────────────────────────────────
# Daemon
# ──────────────────────────────────────────────────────────────────────────────

class ViceDaemon:
    def __init__(self) -> None:
        self.cfg      = load_config()
        self.recorder = create_recorder(self.cfg)
        self.hotkeys  = HotkeyListener()
        self.share:   Optional[ShareServer] = None
        self.hotkeys_available = can_access_hotkeys()
        self._clip_lock  = asyncio.Lock()
        self._clip_count = 0
        # Session recording state
        self._session_active   = False
        self._session_path:    Optional[Path] = None
        self._session_highlights: list[dict] = []  # {time, label, color}
        self._recording_sig = self._recording_signature()
        self._pending_recording_apply = False
        self._config_apply_lock = asyncio.Lock()
        self._clip_task: Optional[asyncio.Task] = None

    async def run(self) -> None:
        Path("/tmp/vice").mkdir(parents=True, exist_ok=True)
        resolve_path(self.cfg.output.directory).mkdir(parents=True, exist_ok=True)

        # Share server (web UI + REST API + WebSocket)
        if self.cfg.sharing.enabled:
            self.share = ShareServer(self.cfg)
            self.share.trigger_clip_cb = self._handle_clip_hotkey
            self.share.get_status_cb   = self._get_status
            self.share.apply_config_cb = self._apply_live_config
            self.share._theme = self.cfg.sharing.theme_color
            try:
                await self.share.start()
            except Exception:
                log.exception(
                    "Failed to start share server on 127.0.0.1:%s",
                    self.cfg.sharing.port,
                )
                raise

        # Recorder callback — fires for both normal clips and session clips
        self.recorder.on_clip_saved(self._on_clip_saved)

        # Hotkeys
        self._bind_hotkeys()
        clip_key = self.cfg.hotkeys.clip

        PID_FILE.write_text(str(os.getpid()))

        server = await asyncio.start_unix_server(
            self._handle_ipc, path=str(SOCKET_FILE)
        )

        await self.hotkeys.start()
        self.hotkeys_available = self.hotkeys.available
        await self.recorder.start()
        if self.share:
            log.info("Vice local control UI: %s", self.share.local_base_url())
        else:
            log.info("Vice local control UI disabled by config")
        log.info(
            "Vice daemon ready (backend=%s, share_enabled=%s)",
            self.recorder.name,
            bool(self.share),
        )

        if self.share:
            asyncio.create_task(
                self.share.broadcast({
                    "type": "status", "recording": True,
                    "backend": self.recorder.name,
                    "session_active": self._session_active,
                    "clip_key": self.cfg.hotkeys.clip,
                    "hotkeys_available": self.hotkeys_available,
                })
            )

        click.echo(f"[Vice {__version__}] Recording started.")
        click.echo(f"  Backend   : {self.recorder.name}")
        click.echo(f"  Clip key  : {clip_key or '(none)'}")
        click.echo(f"  Output    : {self.cfg.output.directory}")
        if self.share and self.share.local_base_url():
            click.echo(f"  UI URL    : {self.share.local_base_url()}/")
        if self.share and self.share.public_base_url():
            click.echo(f"  Share URL : {self.share.public_base_url()}/")
        click.echo("Press Ctrl-C to stop.\n")

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        loop.add_signal_handler(signal.SIGINT,  stop_event.set)

        await stop_event.wait()
        await self._shutdown(server)

    def _recording_signature(self) -> str:
        """Stable representation of recording config for live-apply checks."""
        return json.dumps(asdict(self.cfg.recording), sort_keys=True)

    def _on_clip_saved(self, path: Path) -> None:
        self._clip_count += 1
        click.echo(f"\n[Vice] Clip saved: {path}")
        if self.share:
            # Session clips are added to the share server inside _stop_session;
            # only add here for regular replay-buffer clips (not sessions).
            if not path.name.startswith("Vice_Session_"):
                url = self.share.add_clip(path)
                click.echo(f"[Vice] Share URL:  {url}\n")
            asyncio.create_task(
                self.share.broadcast({
                    "type": "status", "recording": True,
                    "backend": self.recorder.name,
                    "session_active": self._session_active,
                    "clip_key": self.cfg.hotkeys.clip,
                    "hotkeys_available": self.hotkeys_available,
                })
            )

    async def _restart_recorder_for_config(self) -> bool:
        """Restart recorder without running two capture processes at once."""
        if self._session_active:
            self._pending_recording_apply = True
            log.info("Recording config changed during active session; applying after session ends")
            return False

        old_recorder = self.recorder
        # Snapshot config before live-apply mutates recorder behavior.
        old_cfg = copy.deepcopy(self.cfg)

        new_recorder = create_recorder(self.cfg)
        new_recorder.on_clip_saved(self._on_clip_saved)

        await old_recorder.stop()
        try:
            await new_recorder.start()
        except Exception:
            # Restore old config on the current recorder object before restart.
            for field in ("recording", "hotkeys", "output", "sharing"):
                setattr(self.cfg, field, getattr(old_cfg, field))

            # Try to restore the previous recorder so capture keeps running.
            try:
                await old_recorder.start()
            except Exception as restore_exc:
                log.error("Failed to restore previous recorder: %s", restore_exc)
            raise

        self.recorder = new_recorder
        self._recording_sig = self._recording_signature()
        self._pending_recording_apply = False
        return True

    def _bind_hotkeys(self) -> None:
        """(Re)bind runtime hotkeys from current config."""
        self.hotkeys.clear_bindings()
        clip_key = self.cfg.hotkeys.clip
        if clip_key:
            # Single tap → save clip (or add session highlight)
            self.hotkeys.on(clip_key, self._handle_clip_hotkey)
            # Double tap → toggle session recording
            self.hotkeys.on_double(clip_key, self._handle_session_toggle)

    async def _apply_live_config(self) -> None:
        """Apply config changes and restart recorder when recording settings changed."""
        async with self._config_apply_lock:
            self._bind_hotkeys()

            async with self._clip_lock:
                if self._recording_signature() != self._recording_sig:
                    await self._restart_recorder_for_config()

            if self.share:
                await self.share.broadcast({
                    "type": "status",
                    "recording": True,
                    "backend": self.recorder.name,
                    "session_active": self._session_active,
                    "clip_key": self.cfg.hotkeys.clip,
                    "hotkeys_available": self.hotkeys_available,
                })

    def _get_status(self) -> dict:
        return {
            "recording":      True,
            "backend":          self.recorder.name,
            "clips":            self._clip_count,
            "session_active":   self._session_active,
            "clip_key":         self.cfg.hotkeys.clip,
            "hotkeys_available": self.hotkeys_available,
        }

    async def _shutdown(self, server) -> None:
        click.echo("\n[Vice] Shutting down…")
        if self.share:
            try:
                await self.share.broadcast({"type": "status", "recording": False, "backend": ""})
            except Exception as exc:
                log.warning("Failed to broadcast shutdown status: %s", exc)

        server.close()

        try:
            await self.recorder.stop()
        except Exception as exc:
            log.error("Recorder stop failed during shutdown: %s", exc)

        try:
            await self.hotkeys.stop()
        except Exception as exc:
            log.warning("Hotkey stop failed during shutdown: %s", exc)

        if self.share:
            try:
                await self.share.stop()
            except Exception as exc:
                log.warning("Share server stop failed during shutdown: %s", exc)

        for p in (PID_FILE, SOCKET_FILE):
            try:
                if p.exists():
                    p.unlink()
            except OSError as exc:
                log.warning("Failed to remove %s during shutdown: %s", p, exc)

        click.echo("[Vice] Stopped.")

    async def _handle_clip_hotkey(self) -> None:
        if self._session_active:
            # During a session, single tap = add a highlight at current timestamp
            elapsed = self.recorder.session_elapsed()
            label   = f"Highlight {len(self._session_highlights) + 1}" if self._session_highlights else "Highlight"
            color   = "#f59e0b"
            entry   = {"time": round(elapsed, 3), "label": label, "color": color}
            self._session_highlights.append(entry)
            click.echo(f"[Vice] Session highlight at {elapsed:.1f}s", err=True)
            audio.play_highlight()
            if self.share:
                asyncio.create_task(
                    self.share.broadcast({
                        "type": "session_highlight",
                        "time": entry["time"],
                        "label": entry["label"],
                        "color": entry["color"],
                    })
                )
        else:
            if self._clip_task and not self._clip_task.done():
                log.info("Clip save already in progress; ignoring new trigger")
                return
            self._clip_task = asyncio.create_task(self._save_clip())
            self._clip_task.add_done_callback(self._clip_task_done)

    async def _save_clip(self) -> None:
        async with self._clip_lock:
            click.echo("[Vice] Clip triggered!", err=True)
            if self.share:
                await self.share.broadcast({"type": "clip_saving"})
            audio.play_clip()
            saved = await self.recorder.save_clip()
            if saved is None and self.share:
                await self.share.broadcast({
                    "type": "clip_error",
                    "error": "Clip save failed. Check vice.log for details.",
                })

    def _clip_task_done(self, task: asyncio.Task) -> None:
        if self._clip_task is task:
            self._clip_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            log.exception("Clip save task failed")
            if self.share:
                asyncio.create_task(self.share.broadcast({
                    "type": "clip_error",
                    "error": "Clip save failed. Check vice.log for details.",
                }))

    async def _handle_session_toggle(self) -> None:
        if self._session_active:
            await self._stop_session()
        else:
            await self._start_session()

    async def _start_session(self) -> None:
        click.echo("[Vice] Starting session recording…", err=True)
        self._session_highlights = []
        path = await self.recorder.start_session()
        if path is None:
            click.echo("[Vice] Session recording failed to start", err=True)
            return
        self._session_active = True
        self._session_path   = path
        audio.play_session_start()
        click.echo(f"[Vice] Session recording started → {path}", err=True)
        if self.share:
            asyncio.create_task(
                self.share.broadcast({
                    "type": "session_start",
                    "path": str(path),
                })
            )

    async def _stop_session(self) -> None:
        click.echo("[Vice] Stopping session recording…", err=True)
        self._session_active = False
        slug_before_stop = self._session_path.stem if self._session_path else None
        path = await self.recorder.stop_session()
        self._session_path = None

        audio.play_session_end()
        if path and self.share:
            slug = path.stem
            url  = self.share.add_clip(path)
            click.echo(f"[Vice] Session clip saved: {path}", err=True)
            click.echo(f"[Vice] Share URL: {url}", err=True)
            # Persist the highlights that were collected during the session
            if self._session_highlights:
                from .share import HIGHLIGHTS_DIR, _save_highlights
                HIGHLIGHTS_DIR.mkdir(parents=True, exist_ok=True)
                # Assign IDs
                hl_with_ids = [
                    {**h, "id": str(i + 1)}
                    for i, h in enumerate(self._session_highlights)
                ]
                _save_highlights(slug, hl_with_ids)
                click.echo(
                    f"[Vice] {len(hl_with_ids)} highlight(s) saved for {slug}", err=True
                )
            self._session_highlights = []

        if self.share:
            asyncio.create_task(
                self.share.broadcast({
                    "type": "session_stop",
                })
            )

        # Apply deferred recording config changes after session ends.
        if self._pending_recording_apply and self._recording_signature() != self._recording_sig:
            try:
                await self._apply_live_config()
            except Exception as exc:
                log.error("Deferred recording config apply failed: %s", exc)

    async def _handle_ipc(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            cmd = raw.decode().strip()
            if cmd == "clip":
                asyncio.create_task(self._handle_clip_hotkey())
                writer.write(b"ok\n")
            elif cmd == "stop":
                writer.write(b"ok\n")
                await writer.drain()
                os.kill(os.getpid(), signal.SIGTERM)
            elif cmd == "status":
                writer.write(json.dumps({
                    "running":        True,
                    "backend":        self.recorder.name,
                    "clips":          self._clip_count,
                    "output":         self.cfg.output.directory,
                    "local_url":      self.share.local_base_url() if self.share else None,
                    "public_url":     self.share.public_base_url() if self.share else None,
                    "share_url":      self.share.public_base_url() if self.share else None,
                    "session_active":  self._session_active,
                    "clip_key":        self.cfg.hotkeys.clip,
                    "hotkeys_available": self.hotkeys_available,
                }).encode() + b"\n")
            elif cmd == "url":
                url = self.share.local_base_url() if self.share else ""
                writer.write((url or "").encode() + b"\n")
            else:
                writer.write(b"unknown command\n")
            await writer.drain()
        except Exception as exc:
            log.debug("IPC error: %s", exc)
        finally:
            writer.close()


# ──────────────────────────────────────────────────────────────────────────────
# IPC client
# ──────────────────────────────────────────────────────────────────────────────

async def _ipc(command: str, timeout: float = 5.0) -> Optional[str]:
    if not SOCKET_FILE.exists():
        return None
    try:
        reader, writer = await asyncio.open_unix_connection(str(SOCKET_FILE))
        writer.write(command.encode() + b"\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.readline(), timeout=timeout)
        writer.close()
        return response.decode().strip()
    except Exception as exc:
        log.debug("IPC failed: %s", exc)
        return None


def _vice_command_path() -> Optional[Path]:
    exe = shutil.which("vice")
    if not exe:
        return None
    try:
        return Path(exe).resolve()
    except OSError:
        return Path(exe)


def _installed_via_aur() -> bool:
    pacman = shutil.which("pacman")
    vice_path = _vice_command_path()
    if not pacman or not vice_path:
        return False

    query = subprocess.run(
        [pacman, "-Q", "vice-clipper"],
        capture_output=True,
        text=True,
    )
    if query.returncode != 0:
        return False

    owner = subprocess.run(
        [pacman, "-Qo", str(vice_path)],
        capture_output=True,
        text=True,
    )
    if owner.returncode != 0:
        return False
    return "vice-clipper" in owner.stdout


def _using_install_script_venv() -> bool:
    for name in ("vice", "vice-app"):
        cmd = USER_BIN_DIR / name
        if not cmd.exists():
            continue
        try:
            resolved = cmd.resolve()
        except OSError:
            continue
        if INSTALL_VENV_DIR == resolved or INSTALL_VENV_DIR in resolved.parents:
            return True
    return INSTALL_VENV_DIR.exists()


def _remove_local_install_artifacts() -> list[Path]:
    removed: list[Path] = []
    for path in (
        USER_BIN_DIR / "vice",
        USER_BIN_DIR / "vice-app",
        USER_DESKTOP_FILE,
        USER_ICON_FILE,
    ):
        if not path.exists() and not path.is_symlink():
            continue
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def _refresh_desktop_caches() -> None:
    commands = [
        ["update-desktop-database", str(USER_DESKTOP_FILE.parent)],
        ["gtk-update-icon-cache", "-f", "-t", str(USER_ICON_FILE.parents[2])],
    ]
    for cmd in commands:
        exe = shutil.which(cmd[0])
        if not exe:
            continue
        subprocess.run([exe, *cmd[1:]], capture_output=True)


def _setup_daemon_logging(debug: bool) -> None:
    DAEMON_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(DAEMON_LOG_FILE)]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="vice")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Vice — Linux game clip recorder (Medal.tv for Linux)."""
    normalize_runtime_environment()
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
@click.option("--debug", is_flag=True, help="Enable verbose logging.")
@click.option("--open-ui/--no-open-ui", default=True,
              help="Open the web UI in the browser on start.")
def start(debug: bool, open_ui: bool) -> None:
    """Start the Vice recording daemon."""
    _setup_daemon_logging(debug)

    if SOCKET_FILE.exists():
        resp = asyncio.run(_ipc("status", timeout=1.5))
        if resp is not None:
            click.echo("Vice is already running. Use `vice stop` or `vice status`.", err=True)
            sys.exit(1)

        log.warning("Found stale IPC socket at %s, removing it", SOCKET_FILE)
        try:
            SOCKET_FILE.unlink()
        except OSError as exc:
            click.echo(f"Found stale socket at {SOCKET_FILE}, but could not remove it: {exc}", err=True)
            sys.exit(1)

    daemon = ViceDaemon()

    if open_ui and daemon.cfg.sharing.enabled:
        port = daemon.cfg.sharing.port
        from threading import Timer
        def _open():
            subprocess.Popen(
                ["xdg-open", f"http://localhost:{port}/"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        Timer(1.5, _open).start()

    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass


@cli.command()
def ui() -> None:
    """Open the Vice web UI in your browser."""
    raw = asyncio.run(_ipc("url"))
    if raw and raw.startswith("http"):
        url = raw
    else:
        cfg = load_config()
        url = f"http://localhost:{cfg.sharing.port}/"
        if not raw:
            click.echo("Daemon may not be running — opening default port anyway.")
    subprocess.Popen(
        ["xdg-open", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    click.echo(f"Opening {url}")


@cli.command()
def clip() -> None:
    """Save a clip right now (daemon must be running)."""
    resp = asyncio.run(_ipc("clip"))
    if resp is None:
        click.echo("Vice is not running. Start it with `vice start`.", err=True)
        sys.exit(1)
    click.echo("Clip triggered!")


@cli.command()
def stop() -> None:
    """Stop the Vice daemon."""
    resp = asyncio.run(_ipc("stop"))
    if resp is None:
        click.echo("Vice is not running.", err=True)
        sys.exit(1)
    click.echo("Stopped.")


@cli.command()
def status() -> None:
    """Show daemon status."""
    raw = asyncio.run(_ipc("status"))
    if raw is None:
        click.echo("Vice is not running.")
        return
    try:
        info = json.loads(raw)
        click.echo(f"Status   : {'running' if info['running'] else 'stopped'}")
        click.echo(f"Backend  : {info['backend']}")
        click.echo(f"Clips    : {info['clips']}")
        click.echo(f"Output   : {info['output']}")
        if info.get("local_url"):
            click.echo(f"UI URL   : {info['local_url']}/")
        if info.get("public_url"):
            click.echo(f"Share URL: {info['public_url']}/")
    except Exception:
        click.echo(raw)


@cli.command("config")
def show_config() -> None:
    """Print the config file path and its contents."""
    click.echo(f"Config: {CONFIG_PATH}\n")
    if CONFIG_PATH.exists():
        click.echo(CONFIG_PATH.read_text())
    else:
        click.echo("(no config file yet — will be created on first `vice start`)")


@cli.command("open-config")
def open_config() -> None:
    """Open the config file in $EDITOR."""
    if not CONFIG_PATH.exists():
        from .config import Config
        save_config(Config())
        click.echo(f"Created default config at {CONFIG_PATH}")
    editor = os.environ.get("EDITOR", "nano")
    os.execlp(editor, editor, str(CONFIG_PATH))


@cli.command("list-keys")
@click.option("--filter", "filt", default="", help="Filter by substring.")
def list_keys(filt: str) -> None:
    """List available hotkey names for use in config."""
    keys = list_available_keys()
    if filt:
        keys = [k for k in keys if filt.upper() in k]
    for k in keys:
        click.echo(k)


@cli.command()
def clips() -> None:
    """List saved clips in the output directory."""
    cfg = load_config()
    out_dir = resolve_path(cfg.output.directory)
    if not out_dir.exists():
        click.echo("No clips directory found.")
        return
    files = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        click.echo("No clips saved yet.")
        return
    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        click.echo(f"{f.name}  ({size_mb:.1f} MB)")


@cli.command()
@click.option("--yes", "-y", is_flag=True, help="Skip all confirmation prompts.")
def uninstall(yes: bool) -> None:
    """Remove Vice cleanly — config, service, and optionally clips."""
    click.echo("Vice uninstaller\n")

    if _installed_via_aur():
        click.echo("Vice was installed via AUR.")
        click.echo("Run: yay -Rns vice-clipper")
        return

    # 1. Stop daemon
    if SOCKET_FILE.exists():
        click.echo("Stopping daemon…")
        asyncio.run(_ipc("stop"))

    # 2. Disable systemd user service
    service = actual_home_dir() / ".config" / "systemd" / "user" / "vice.service"
    if service.exists():
        if yes or click.confirm("Disable and remove the systemd user service?", default=True):
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", "vice"],
                capture_output=True,
            )
            service.unlink()
            click.echo("  Removed systemd service.")

    # 3. Remove config
    if CONFIG_DIR.exists():
        if yes or click.confirm(f"Remove config directory {CONFIG_DIR}?", default=False):
            shutil.rmtree(CONFIG_DIR)
            click.echo(f"  Removed {CONFIG_DIR}.")

    # 4. Offer to remove clips
    try:
        cfg = load_config() if CONFIG_PATH.exists() else None
        clips_dir = resolve_path(cfg.output.directory) if cfg else actual_home_dir() / "Videos" / "Vice"
    except Exception:
        clips_dir = actual_home_dir() / "Videos" / "Vice"

    if clips_dir.exists():
        n = len(list(clips_dir.glob("*.mp4")))
        if n > 0 and (yes or click.confirm(
            f"Delete {n} saved clip(s) in {clips_dir}?", default=False
        )):
            shutil.rmtree(clips_dir)
            click.echo(f"  Deleted {n} clip(s).")

    using_venv = _using_install_script_venv()

    # 5. Remove the Python package or the dedicated install.sh virtualenv
    if using_venv:
        click.echo("\nRemoving Vice virtual environment…")
        shutil.rmtree(INSTALL_VENV_DIR, ignore_errors=True)
        click.echo(f"  Removed {INSTALL_VENV_DIR}.")
    else:
        click.echo("\nUninstalling Python package…")
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "vice", "-y"])

    removed = _remove_local_install_artifacts()
    if using_venv and INSTALL_VENV_DIR not in removed:
        removed.append(INSTALL_VENV_DIR)
    if removed:
        click.echo("\nRemoved local Vice install files:")
        for path in removed:
            click.echo(f"  {path}")
        _refresh_desktop_caches()

    click.echo("\nVice has been removed. Goodbye!")
