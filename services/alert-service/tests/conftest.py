"""Tests run against the local cluster's LocalStack, so conditional writes behave like real DynamoDB.

Needs both port-forwards running (Redis for the live-alert tests):
    kubectl port-forward svc/cloud-localstack 4566:4566 -n csc3104
    kubectl port-forward svc/cloud-redis 6379:6379 -n csc3104
Each test uses its own random traveller ID, so tests don't see each other's incidents.
"""
import json
import os
import uuid
from pathlib import Path

# Must be set before the app is imported: its settings are read at import time.
os.environ.setdefault("AWS_ENDPOINT_URL", "http://localhost:4566")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "csc3104")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "csc3104")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")

import boto3  # noqa: E402
import pytest  # noqa: E402
import redis  # noqa: E402
from botocore.config import Config  # noqa: E402

from app.aws_resources import ensure_tables  # noqa: E402
from app.config import settings  # noqa: E402
from app.incidents import IncidentStore  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "examples"


@pytest.fixture(scope="session")
def dynamodb():
    client = boto3.client(
        "dynamodb",
        region_name=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
        # Enough connections for the concurrency test's threads; fail fast if LocalStack is down.
        config=Config(max_pool_connections=32, connect_timeout=3, retries={"max_attempts": 2}),
    )
    try:
        client.list_tables(Limit=1)
    except Exception as e:
        pytest.exit(f"LocalStack isn't reachable at {settings.aws_endpoint_url}. Is the port-forward running? ({e})", 1)
    ensure_tables(client)
    return client


@pytest.fixture(scope="session")
def sqs(dynamodb):  # dynamodb: checks LocalStack is reachable first
    return boto3.client("sqs", region_name=settings.aws_region, endpoint_url=settings.aws_endpoint_url)


@pytest.fixture(scope="session")
def redis_client():
    client = redis.Redis.from_url(settings.redis_url, socket_timeout=2, decode_responses=True)
    try:
        client.ping()
    except Exception as e:
        pytest.exit(f"Redis isn't reachable at {settings.redis_url}. Is the port-forward running? ({e})", 1)
    return client


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(dynamodb):
    return IncidentStore(dynamodb)


def load_example(name: str) -> dict:
    """A copy of contracts/examples/<name> for a traveller no other test uses."""
    data = json.loads((EXAMPLES / name).read_text())
    data["travellerId"] = f"U-test-{uuid.uuid4().hex[:8]}"
    return data


@pytest.fixture
def report() -> dict:
    return load_example("problem-report.json")


@pytest.fixture
def no_response_report() -> dict:
    return load_example("problem-report-no-response.json")


@pytest.fixture
def disruption() -> dict:
    return load_example("disruption.json")
