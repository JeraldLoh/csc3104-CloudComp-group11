FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

# Live streams (/api/stream) never close by themselves: on shutdown (a deploy, a pod moved), give them
# 5 s instead of waiting until Kubernetes kills the pod. Dashboards reconnect to another replica.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "5"]
