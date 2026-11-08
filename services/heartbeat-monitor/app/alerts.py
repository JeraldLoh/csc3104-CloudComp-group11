"""Sends problem-report-no-response.json to the Alert Service (contracts/api.md: POST /internal/problems)."""
import httpx


class AlertClient:
    def __init__(self, base_url: str, timeout_s: float):
        self.http = httpx.Client(base_url=base_url, timeout=timeout_s)

    def report(self, report: dict) -> dict:
        """Returns {"incidentId": ..., "created": bool}. Raises if the Alert Service didn't accept it,
        so the caller doesn't mark the silence as reported and tries again on the next tick."""
        resp = self.http.post("/internal/problems", json=report)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self.http.close()
