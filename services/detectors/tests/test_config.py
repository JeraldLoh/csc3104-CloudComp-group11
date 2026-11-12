import pytest
from pydantic import ValidationError

from detectors.config import Settings


def test_redis_timeout_defaults_to_200_ms_and_reads_the_env():
    assert Settings().redis_timeout_s == 0.2
    assert Settings.from_env({"REDIS_TIMEOUT_S": "0.35"}).redis_timeout_s == 0.35
    with pytest.raises(ValidationError):
        Settings.from_env({"REDIS_TIMEOUT_S": "0"})
