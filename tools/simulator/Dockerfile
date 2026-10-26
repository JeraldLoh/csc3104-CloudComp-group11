# load test image for k8s/local/load-test built with tools as the context
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "redis>=5,<7"

COPY . ./tools
RUN find ./tools -name __pycache__ -prune -exec rm -rf {} + && chmod -R a+rX /app

RUN useradd --create-home appuser
USER appuser

ENTRYPOINT ["python", "-m", "tools.simulator"]
CMD ["load", "--help"]
