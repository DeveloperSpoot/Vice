"""Runtime helpers for robust daemon startup under launchers and user services."""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from pathlib import Path


def actual_home_dir() -> Path:
    """Return the current user's real home directory without trusting $HOME."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path(os.path.expanduser("~"))


def _needs_shell_expansion(value: str | None) -> bool:
    if not value:
        return True
    return "${" in value or "$(" in value


def load_user_systemd_env() -> None:
    """Hydrate graphical session vars from the user systemd manager when needed."""
    if shutil.which("systemctl") is None:
        return

    try:
        out = subprocess.check_output(
            ["systemctl", "--user", "show-environment"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except Exception:
        return

    wanted = {"WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
    for line in out.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key not in wanted or not value:
            continue
        if not os.environ.get(key) or _needs_shell_expansion(os.environ.get(key)):
            os.environ[key] = value


def normalize_runtime_environment() -> None:
    """Repair common broken service env vars before Vice touches config or capture."""
    real_home = str(actual_home_dir())
    runtime_dir = f"/run/user/{os.getuid()}"

    if _needs_shell_expansion(os.environ.get("HOME")):
        os.environ["HOME"] = real_home

    if _needs_shell_expansion(os.environ.get("XDG_RUNTIME_DIR")):
        os.environ["XDG_RUNTIME_DIR"] = runtime_dir

    if (
        not os.environ.get("WAYLAND_DISPLAY")
        and not os.environ.get("DISPLAY")
    ) or _needs_shell_expansion(os.environ.get("XDG_RUNTIME_DIR")):
        load_user_systemd_env()

    if _needs_shell_expansion(os.environ.get("HOME")):
        os.environ["HOME"] = real_home

    if _needs_shell_expansion(os.environ.get("XDG_RUNTIME_DIR")):
        os.environ["XDG_RUNTIME_DIR"] = runtime_dir


def resolve_path(path_like: str | Path) -> Path:
    """Expand home-directory placeholders in config-driven filesystem paths."""
    text = os.fspath(path_like)
    home = str(actual_home_dir())

    if text.startswith("~"):
        text = text.replace("~", home, 1)

    text = text.replace("${HOME}", home).replace("$HOME", home)
    text = os.path.expandvars(text)
    return Path(text)
