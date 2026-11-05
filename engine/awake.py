"""Keep the machine awake while a long job runs, and only then.

Per platform, for the duration of the `with` block:
  Linux    a desktop idle inhibit via the xdg-desktop-portal Inhibit API (flag 8 = idle, the
           mechanism video players use; desktop idle-suspend such as COSMIC's honours it), plus
           a logind sleep/idle inhibitor (`systemd-inhibit`) for runs with no desktop session
  Windows  SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED); the display may
           still turn off
  macOS    `caffeinate -i -w <pid>` (prevents idle sleep; exits with this process)
Anything unavailable is logged and skipped; the job runs regardless.
Set LIBRARIAN_NO_STAY_AWAKE=1 to disable.
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


# -- Linux ---------------------------------------------------------------

def _portal_inhibit(reason: str):
    """Return an open D-Bus connection holding an idle inhibit, or None."""
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except ImportError:
        return None
    try:
        conn = open_dbus_connection(bus="SESSION")
        portal = DBusAddress("/org/freedesktop/portal/desktop", bus_name="org.freedesktop.portal.Desktop",
                             interface="org.freedesktop.portal.Inhibit")
        reply = conn.send_and_get_reply(
            new_method_call(portal, "Inhibit", "sua{sv}", ("", 8, {"reason": ("s", reason)})), timeout=5)
        if reply.header.message_type.name == "error":
            conn.close()
            log.debug("portal inhibit refused: %s", reply.body)
            return None
        return conn  # the inhibit lasts as long as this connection stays open
    except Exception as e:  # no session bus, no portal, ...
        log.debug("portal inhibit unavailable: %s", e)
        return None


def _logind_inhibit(reason: str):
    exe = shutil.which("systemd-inhibit")
    if not exe:
        return None
    try:
        return subprocess.Popen([exe, "--what=sleep:idle", "--who=niche-librarian", f"--why={reason}",
                                 "--mode=block", "sleep", "infinity"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log.debug("systemd-inhibit unavailable: %s", e)
        return None


# -- Windows and macOS -----------------------------------------------------

def _windows_inhibit():
    """Returns a release function, or None."""
    try:
        import ctypes
        set_state = ctypes.windll.kernel32.SetThreadExecutionState
    except (ImportError, AttributeError, OSError) as e:
        log.debug("SetThreadExecutionState unavailable: %s", e)
        return None
    if not set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
        return None
    return lambda: set_state(ES_CONTINUOUS)


def _macos_inhibit():
    exe = shutil.which("caffeinate")
    if not exe:
        return None
    try:
        return subprocess.Popen([exe, "-i", "-w", str(os.getpid())],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log.debug("caffeinate unavailable: %s", e)
        return None


def _stop(proc) -> None:
    if proc:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


@contextlib.contextmanager
def stay_awake(reason: str = "niche-librarian job running"):
    if os.environ.get("LIBRARIAN_NO_STAY_AWAKE"):
        yield
        return
    releases, held = [], []
    if sys.platform.startswith("linux"):
        conn, proc = _portal_inhibit(reason), _logind_inhibit(reason)
        if conn:
            held.append("desktop idle")
            releases.append(lambda: conn.close())
        if proc:
            held.append("logind")
            releases.append(lambda: _stop(proc))
    elif sys.platform == "win32":
        release = _windows_inhibit()
        if release:
            held.append("SetThreadExecutionState")
            releases.append(release)
    elif sys.platform == "darwin":
        proc = _macos_inhibit()
        if proc:
            held.append("caffeinate")
            releases.append(lambda: _stop(proc))
    log.info("keeping the machine awake (%s)", ", ".join(held) or "no inhibitor available")
    try:
        yield
    finally:
        for release in releases:
            with contextlib.suppress(Exception):
                release()
