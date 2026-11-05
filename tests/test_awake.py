import sys

from engine import awake


def _enable(monkeypatch, platform):
    monkeypatch.delenv("LIBRARIAN_NO_STAY_AWAKE")
    monkeypatch.setattr(sys, "platform", platform)


def test_linux_holds_and_releases_both_inhibitors(monkeypatch):
    events = []

    class Conn:
        def close(self): events.append("portal released")

    class Proc:
        def terminate(self): events.append("logind released")
        def wait(self, timeout=None): pass

    _enable(monkeypatch, "linux")
    monkeypatch.setattr(awake, "_portal_inhibit", lambda r: events.append("portal held") or Conn())
    monkeypatch.setattr(awake, "_logind_inhibit", lambda r: events.append("logind held") or Proc())
    with awake.stay_awake("test"):
        events.append("job")
    assert events == ["portal held", "logind held", "job", "portal released", "logind released"]


def test_windows_and_macos_paths(monkeypatch):
    events = []
    _enable(monkeypatch, "win32")
    monkeypatch.setattr(awake, "_windows_inhibit", lambda: events.append("win held") or (lambda: events.append("win released")))
    with awake.stay_awake("test"):
        events.append("job")

    class Proc:
        def terminate(self): events.append("caffeinate stopped")
        def wait(self, timeout=None): pass

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(awake, "_macos_inhibit", lambda: events.append("caffeinate") or Proc())
    with awake.stay_awake("test"):
        events.append("job")
    assert events == ["win held", "job", "win released", "caffeinate", "job", "caffeinate stopped"]


def test_job_still_runs_without_inhibitors(monkeypatch):
    _enable(monkeypatch, "linux")
    monkeypatch.setattr(awake, "_portal_inhibit", lambda r: None)
    monkeypatch.setattr(awake, "_logind_inhibit", lambda r: None)
    ran = []
    with awake.stay_awake("test"):
        ran.append(1)
    assert ran == [1]


def test_released_even_if_job_fails(monkeypatch):
    released = []

    class Conn:
        def close(self): released.append(1)

    _enable(monkeypatch, "linux")
    monkeypatch.setattr(awake, "_portal_inhibit", lambda r: Conn())
    monkeypatch.setattr(awake, "_logind_inhibit", lambda r: None)
    try:
        with awake.stay_awake("test"):
            raise RuntimeError("crawl crashed")
    except RuntimeError:
        pass
    assert released == [1]
