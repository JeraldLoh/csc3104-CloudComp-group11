# Runs the Alert Service on this laptop against the local kind cluster.
# Needs both port-forwards running (Redis 6379, LocalStack 4566).
$env:REDIS_URL = "redis://localhost:6379/0"
$env:AWS_ENDPOINT_URL = "http://localhost:4566"
$env:AWS_ACCESS_KEY_ID = "csc3104"        # LocalStack accepts any value
$env:AWS_SECRET_ACCESS_KEY = "csc3104"
$env:AWS_DEFAULT_REGION = "ap-southeast-1"
$env:CREATE_RESOURCES = "true"
& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn app.main:app --reload --port 8005 --app-dir $PSScriptRoot
