"""Guard the shared status contract shape (v0). If W0's real schema differs, this
test is where the divergence should surface first."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice_local.status_stub import get_project_status  # noqa: E402

V0_TOP_KEYS = {"project", "generated_at", "tickets", "tests", "deploy",
               "visual_review", "decisions", "open_questions"}


def test_known_project_shape():
    rep = get_project_status("dstrader")
    assert V0_TOP_KEYS <= set(rep), "missing top-level StatusReport keys"
    assert set(rep["tickets"]) >= {"todo", "in_progress", "done", "blocked", "items"}
    assert set(rep["tests"]) >= {"count", "passing", "failing", "coverage_pct", "last_run"}
    assert set(rep["deploy"]) >= {"last_deployed_at", "env", "commit", "status"}


def test_unknown_project_returns_valid_empty_report():
    rep = get_project_status("does-not-exist")
    assert V0_TOP_KEYS <= set(rep)
    assert rep["tickets"]["todo"] == 0
    assert rep["open_questions"], "should flag that no data was found"


def test_case_insensitive_lookup():
    assert get_project_status("DSTRADER")["tickets"]["done"] == 11
