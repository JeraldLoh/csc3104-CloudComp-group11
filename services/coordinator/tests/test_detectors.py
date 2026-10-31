"""DetectorClient against fake detectors behind httpx.MockTransport (no network)."""
import asyncio
import json
import time
from pathlib import Path

import httpx

from app.detectors import DetectorClient
from app.selection import parse_urls

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"
REQUEST = json.loads((CONTRACTS / "detector-request.json").read_text())
VOTE = json.loads((CONTRACTS / "detector-vote.json").read_text())
URLS = parse_urls("")


def vote(verdict="normal", confidence=0.8, **extra) -> dict:
    return {**VOTE, "verdict": verdict, "confidence": confidence,
            "problemType": "off_route" if verdict == "problem" else None, **extra}


def run(behaviours: dict, detector_ids: list[str], timeout_s=0.2, request=REQUEST):
    """behaviours: service host -> async handler(request) -> httpx.Response.
    Returns (votes, seen) where seen lists (host, request body) the detectors received."""
    seen = []

    async def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.host, json.loads(req.content)))
        return await behaviours[req.url.host](req)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await DetectorClient(http, URLS, timeout_s).call_many(detector_ids, request)

    return asyncio.run(go()), seen


def answers(body: dict, delay_s: float = 0.0, status: int = 200):
    async def h(_):
        if delay_s:
            await asyncio.sleep(delay_s)
        return httpx.Response(status, json=body)
    return h


def raises(exc: Exception):
    async def h(_):
        raise exc
    return h


PAIR = ["route-corridor", "dwell-time"]


# --- happy path ---------------------------------------------------------------------

def test_calls_each_detector_at_its_service_with_the_request():
    votes, seen = run({"detector-route": answers(vote("problem")), "detector-dwell": answers(vote())}, PAIR)
    assert sorted(h for h, _ in seen) == ["detector-dwell", "detector-route"]
    assert all(body == REQUEST for _, body in seen)
    assert [v["verdict"] for v in votes] == ["problem", "normal"]


def test_votes_keep_the_order_detectors_were_asked_in():
    behaviours = {"detector-route": answers(vote("problem"), delay_s=0.05), "detector-dwell": answers(vote())}
    votes, _ = run(behaviours, PAIR)
    assert [v["detectorId"] for v in votes] == PAIR


def test_our_ids_and_latency_win_over_the_detectors_own():
    wrong = vote(detectorId="something-else", requestId="T-999#1", latencyMs=99999)
    votes, _ = run({"detector-route": answers(wrong), "detector-dwell": answers(vote())}, PAIR)
    assert votes[0]["detectorId"] == "route-corridor"
    assert votes[0]["requestId"] == "T-001#17"
    assert votes[0]["latencyMs"] < 1000


def test_detectors_are_called_in_parallel():
    slow = {h: answers(vote(), delay_s=0.1) for h in ("detector-route", "detector-heading", "detector-dwell", "detector-progress")}
    start = time.perf_counter()
    votes, _ = run(slow, ["route-corridor", "heading-speed", "dwell-time", "route-progress"], timeout_s=1)
    elapsed = time.perf_counter() - start
    assert len(votes) == 4 and all(v["verdict"] == "normal" for v in votes)
    assert elapsed < 0.3   # ~0.1 s in parallel, not 0.4 s one after another


def test_a_detector_that_abstains_itself_is_kept():
    votes, _ = run({"detector-route": answers(vote("abstain", 0.0)), "detector-dwell": answers(vote())}, PAIR)
    assert votes[0]["verdict"] == "abstain"
    assert votes[0]["reason"] == VOTE["reason"]   # its own reason, not one we made up


# --- failures become abstains ----------------------------------------------------------------

def test_slow_detector_times_out_as_abstain_without_holding_up_the_other():
    behaviours = {"detector-route": answers(vote("problem"), delay_s=1.0), "detector-dwell": answers(vote())}
    start = time.perf_counter()
    votes, _ = run(behaviours, PAIR, timeout_s=0.1)
    assert time.perf_counter() - start < 0.5
    assert votes[0]["verdict"] == "abstain"
    assert votes[0]["reason"] == "timeout"
    assert votes[0]["confidence"] == 0.0
    assert votes[1]["verdict"] == "normal"


def test_http_error_is_abstain():
    votes, _ = run({"detector-route": answers({}, status=500), "detector-dwell": answers(vote())}, PAIR)
    assert votes[0]["verdict"] == "abstain"
    assert votes[0]["reason"] == "error: HTTP 500"


def test_connection_failure_is_abstain():
    down = raises(httpx.ConnectError("connection refused"))
    votes, _ = run({"detector-route": down, "detector-dwell": answers(vote())}, PAIR)
    assert votes[0]["verdict"] == "abstain"
    assert votes[0]["reason"] == "error: ConnectError"


def test_transport_timeout_is_abstain():
    votes, _ = run({"detector-route": raises(httpx.ReadTimeout("slow")), "detector-dwell": answers(vote())}, PAIR)
    assert votes[0]["reason"] == "timeout"


def test_invalid_verdict_is_abstain():
    votes, _ = run({"detector-route": answers(vote("maybe")), "detector-dwell": answers(vote())}, PAIR)
    assert votes[0]["verdict"] == "abstain"
    assert votes[0]["reason"] == "error: ValueError"


def test_non_json_answer_is_abstain():
    async def html(_):
        return httpx.Response(200, text="<html>oops</html>")
    votes, _ = run({"detector-route": html, "detector-dwell": answers(vote())}, PAIR)
    assert votes[0]["verdict"] == "abstain"


def test_json_that_is_not_an_object_is_abstain():
    votes, _ = run({"detector-route": answers(["problem"]), "detector-dwell": answers(vote())}, PAIR)
    assert votes[0]["verdict"] == "abstain"


def test_all_detectors_failing_still_returns_a_vote_each():
    down = raises(httpx.ConnectError("down"))
    votes, _ = run({"detector-route": down, "detector-dwell": down}, PAIR)
    assert [v["verdict"] for v in votes] == ["abstain", "abstain"]
    assert [v["detectorId"] for v in votes] == PAIR


def test_abstain_vote_has_the_contract_fields():
    votes, _ = run({"detector-route": raises(httpx.ConnectError("down")), "detector-dwell": answers(vote())}, PAIR)
    assert set(votes[0]) == set(VOTE)
    assert votes[0]["requestId"] == "T-001#17"
