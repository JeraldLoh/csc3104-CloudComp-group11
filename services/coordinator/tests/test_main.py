"""App startup and probes. Nothing is listening on the addresses used, so /ready must fail."""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app, resolve_queue_url


@pytest.fixture(autouse=True)
def offline_aws(monkeypatch):
    # Dummy credentials and a single attempt, so boto3 fails fast instead of searching/retrying.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")


def offline_settings(**kw) -> Settings:
    return Settings(**{"redis_url": "redis://localhost:6399/0", "aws_endpoint_url": "http://localhost:9",
                       "queue_url": "http://localhost:9/000000000000/careroute-checkins",
                       "consumer_enabled": False, **kw})


def test_starts_and_reports_health():
    with TestClient(create_app(offline_settings())) as c:
        assert c.get("/health").json() == {"status": "ok"}


def test_not_ready_lists_each_unreachable_dependency():
    with TestClient(create_app(offline_settings())) as c:
        r = c.get("/ready")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "not_ready"
    assert "redis" in r.json()["error"]["message"]
    assert "sqs" in r.json()["error"]["message"]


def test_starts_with_the_consumer_running_and_stops_cleanly():
    # The consumer can't reach SQS; it must keep retrying in the background, not crash the app.
    with TestClient(create_app(offline_settings(consumer_enabled=True))) as c:
        assert c.get("/health").status_code == 200


class FakeSqs:
    def __init__(self):
        self.calls = []

    def create_queue(self, QueueName, Attributes):
        self.calls.append(("create", QueueName))
        return {"QueueUrl": f"http://local/{QueueName}"}

    def get_queue_url(self, QueueName):
        self.calls.append(("get", QueueName))
        return {"QueueUrl": f"http://aws/{QueueName}"}


def test_queue_url_given_directly_is_used_as_is():
    sqs = FakeSqs()
    assert resolve_queue_url(sqs, Settings(queue_url="http://given/q")) == "http://given/q"
    assert sqs.calls == []


def test_queue_url_looked_up_by_name():
    sqs = FakeSqs()
    assert resolve_queue_url(sqs, Settings(queue_url=None, queue_name="careroute-checkins")) == "http://aws/careroute-checkins"
    assert sqs.calls == [("get", "careroute-checkins")]


def test_queue_created_only_when_asked():
    sqs = FakeSqs()
    url = resolve_queue_url(sqs, Settings(queue_url=None, queue_name="careroute-checkins", create_queue=True))
    assert url == "http://local/careroute-checkins"
    assert sqs.calls == [("create", "careroute-checkins")]
