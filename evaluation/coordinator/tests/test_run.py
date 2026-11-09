"""run.py's C2 loop: saving each condition and resuming after a stop (no stack: a fake one)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run  # noqa: E402
from stack import DETECTORS  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
CONDITIONS = 1 + len(DETECTORS) * len(run.FAULTS)   # healthy + every fault on every detector


class FakeStack:
    """Remembers how each switch is set, so the fake journeys can vote like the real stack would."""

    def __init__(self):
        self.modes = {}

    def switch(self, detector, **control):
        self.modes[detector] = control

    def clear_weights(self):
        pass

    def set_weight(self, detector, reliability):
        pass


def fake_journey(stack, j, stop_at=None, played=None):
    """One check-in; each detector votes as its switch says (abstain when slow or dead)."""
    broken = [d for d, c in stack.modes.items() if c != run.HEALTHY]
    if played is not None:
        played.append(tuple(broken))
    if stop_at is not None and len(played) == stop_at:
        raise SystemExit("laptop asleep")
    votes = []
    for d in DETECTORS:
        c = stack.modes.get(d, run.HEALTHY)
        if c.get("mode") == "error" or c.get("delayMs"):
            votes.append({"detectorId": d, "verdict": "abstain", "reason": "timeout"})
        elif c.get("mode") == "wrong":
            votes.append({"detectorId": d, "verdict": "problem", "reason": f"{run.WRONG_REASON}: real vote was normal"})
        else:
            votes.append({"detectorId": d, "verdict": "normal", "reason": "real"})
    return {"scenario": "normal", "rows": [{"decision": {"k": 4, "votes": votes}}], "incidents": []}


@pytest.fixture
def one_journey(monkeypatch):
    monkeypatch.setattr(run, "select_journeys", lambda seeds, run_id: [{"scenario": "normal"}])


def test_every_condition_is_saved_as_it_finishes(tmp_path, monkeypatch, one_journey):
    progress = tmp_path / "C2-progress.json"
    played = []
    monkeypatch.setattr(run, "run_journey", lambda s, j: fake_journey(s, j, stop_at=5, played=played))
    with pytest.raises(SystemExit):
        run.c2(FakeStack(), 5, "R1", "2026-11-09T00:00:00", progress)
    saved = json.loads(progress.read_text())
    assert len(saved["conditions"]) == 4                     # the 5th stopped before it finished
    assert (saved["runId"], saved["seeds"]) == ("R1", 5)


def test_a_rerun_resumes_after_the_last_saved_condition(tmp_path, monkeypatch, one_journey):
    progress = tmp_path / "C2-progress.json"
    first = []
    monkeypatch.setattr(run, "run_journey", lambda s, j: fake_journey(s, j, stop_at=5, played=first))
    with pytest.raises(SystemExit):
        run.c2(FakeStack(), 5, "R1", "2026-11-09T00:00:00", progress)
    played = []
    monkeypatch.setattr(run, "run_journey", lambda s, j: fake_journey(s, j, played=played))
    data = run.c2(FakeStack(), 5, "R2", "2026-11-09T01:00:00", progress)
    assert len(played) == CONDITIONS - 4                     # only the ones not saved
    assert len(data["conditions"]) == CONDITIONS
    assert data["runId"] == "R1" and data["startedAt"] == "2026-11-09T00:00:00"   # still the first run
    assert [(c["fault"], c["detector"]) for c in data["conditions"]][:2] == [("healthy", None), ("slow", DETECTORS[0])]


def test_fresh_ignores_an_unfinished_run(tmp_path, monkeypatch, one_journey):
    progress = tmp_path / "C2-progress.json"
    progress.write_text(json.dumps({"runId": "OLD", "startedAt": "x", "seeds": 5, "conditions": []}))
    monkeypatch.setattr(run, "run_journey", lambda s, j: fake_journey(s, j))
    assert run.c2(FakeStack(), 5, "NEW", "y", progress, fresh=True)["runId"] == "NEW"


def test_resuming_with_other_seeds_is_refused(tmp_path):
    progress = tmp_path / "C2-progress.json"
    progress.write_text(json.dumps({"runId": "R1", "startedAt": "x", "seeds": 1, "conditions": []}))
    with pytest.raises(SystemExit, match="--seeds 1"):
        run.c2(FakeStack(), 5, "R2", "y", progress)


def test_only_runs_just_that_fault_on_every_detector(tmp_path, monkeypatch, one_journey):
    played = []
    monkeypatch.setattr(run, "run_journey", lambda s, j: fake_journey(s, j, played=played))
    data = run.c2(FakeStack(), 5, "R3", "z", tmp_path / "p.json", only="wrong, silenced")
    assert [(c["fault"], c["detector"]) for c in data["conditions"]] == [("wrong, silenced", d) for d in DETECTORS]
    assert len(played) == len(DETECTORS)


def test_a_rerun_replaces_only_its_conditions_and_says_so():
    base = {"runId": "R1", "seeds": 5, "conditions": [
        {"fault": "healthy", "detector": None, "journeys": "old"},
        {"fault": "wrong, silenced", "detector": "dwell-time", "journeys": "old"},
        {"fault": "dead", "detector": "dwell-time", "journeys": "old"}]}
    rerun = {"conditions": [{"fault": "wrong, silenced", "detector": "dwell-time", "journeys": "new"}]}
    merged = run.merge_rerun(base, rerun, "wrong, silenced", "R2", "t")
    assert [c["journeys"] for c in merged["conditions"]] == ["old", "new", "old"]   # same order
    assert merged["runId"] == "R1" and merged["reruns"] == [{"fault": "wrong, silenced", "runId": "R2", "at": "t"}]


def test_the_wrong_fault_is_recognised_by_the_switchs_own_reason():
    # run.py checks the fault reached the Coordinator by this prefix; it must match what the switch writes.
    switch = (REPO / "services" / "fault-switch" / "app" / "main.py").read_text()
    assert f'"reason": "{run.WRONG_REASON}:' in switch
