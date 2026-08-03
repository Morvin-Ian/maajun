"""Tests for the animated terminal status indicators."""

from rich.console import Console

from maajun.progress import WorkingStatus, working


def _render(renderable) -> str:
    console = Console(width=80, force_terminal=False)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def test_working_status_renders_phase():
    status = WorkingStatus("Preparing workspace")
    assert "Preparing workspace" in _render(status)


def test_working_status_set_changes_phase_and_resets_timer():
    status = WorkingStatus("Preparing workspace")
    status._started -= 5  # pretend 5s elapsed on the first phase
    status.set("Analyzing with AI")
    out = _render(status)
    assert "Analyzing with AI" in out
    assert "Preparing workspace" not in out
    # Timer resets on phase change, so the new phase shows ~0s, not 5s.
    assert "(0s)" in out


def test_working_status_set_same_phase_keeps_timer():
    status = WorkingStatus("Analyzing with AI")
    before = status._started
    status.set("Analyzing with AI")
    assert status._started == before


def test_working_context_manager_yields_status():
    console = Console(width=80, force_terminal=False)
    with working(console, "Working") as status:
        assert isinstance(status, WorkingStatus)
        status.set("Opening PR")
