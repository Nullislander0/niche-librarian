"""Keep the machine awake while a long job runs, and only then.

Two requests are held for the duration of the `with` block and released after it:
  1. Desktop idle inhibit via the xdg-desktop-portal Inhibit API (flag 8 = idle), the
     same mechanism a video player uses. Desktop idle-suspend (e.g. COSMIC's) honours this.
  2. A logind sleep/idle inhibitor (`systemd-inhibit`), for runs with no desktop session.
Either may be unavailable; failures are logged and the job runs anyway.
Set LIBRARIAN_NO_STAY_AWAKE=1 to disable.
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)


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


@contextlib.contextmanager
def stay_awake(reason: str = "niche-librarian job running"):
    if os.environ.get("LIBRARIAN_NO_STAY_AWAKE"):
        yield
        return
    conn, proc = _portal_inhibit(reason), _logind_inhibit(reason)
    held = [n for n, h in (("desktop idle", conn), ("logind", proc)) if h]
    log.info("keeping the machine awake (%s)", ", ".join(held) or "no inhibitor available")
    try:
        yield
    finally:
        if conn:
            with contextlib.suppress(Exception):
                conn.close()
        if proc:
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
