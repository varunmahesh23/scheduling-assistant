"""Thin client for the scheduling API (stdlib only).

This is the *tool boundary*: the agent never builds URLs or touches HTTP. Every
call is timed and traced with redacted parameters, and failures surface as one
exception type (ApiError) so the workflow code can reason about them.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from tracing import Tracer, redact_params, scrub


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__("{} {}: {}".format(status, code, message))
        self.status, self.code, self.message = status, code, message

    @property
    def is_outage(self) -> bool:
        """Downstream unavailable (5xx) or unreachable (status 0)."""
        return self.status == 0 or self.status >= 500


class SchedulingClient:
    def __init__(self, base_url: str, tracer: Tracer, mock_scenario: str | None = None,
                 outage_affects_handoffs: bool = False, timeout: float = 5.0, get_retries: int = 1):
        self.base_url = base_url.rstrip("/")
        self.tracer = tracer
        self.mock_scenario = mock_scenario
        self.outage_affects_handoffs = outage_affects_handoffs
        self.timeout = timeout
        self.get_retries = get_retries
        self.calls: list[dict] = []  # redacted call log (also used by the eval harness)

    # -- low level --------------------------------------------------------
    def _request(self, name, method, path, params=None, body=None, trace_path=None,
                 retries=0, send_mock_header=True):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
        headers = {"Content-Type": "application/json"}
        if self.mock_scenario and send_mock_header:
            headers["X-Mock-Scenario"] = self.mock_scenario
        data = json.dumps(body).encode() if body is not None else None

        attempt = 0
        while True:
            started = time.perf_counter()
            status, payload, error_code = 0, {}, None
            try:
                req = urllib.request.Request(url, data=data, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status, payload = resp.status, json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as err:
                status = err.code
                try:
                    payload = json.loads(err.read() or b"{}")
                except ValueError:
                    payload = {}
                error_code = payload.get("code", "http_error")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                status, error_code, payload = 0, "network_error", {"message": "unreachable"}
            ms = round((time.perf_counter() - started) * 1000, 1)

            logged = {**(params or {}), **{k: v for k, v in (body or {}).items()
                                           if k in ("patientId", "slotId", "reason", "confirmed")}}
            rec = {"name": name, "method": method, "path": trace_path or path, "status": status,
                   "ms": ms, "attempt": attempt, "params": redact_params(logged)}
            if error_code:
                rec["error_code"] = error_code
                rec["error"] = scrub(payload.get("message", ""))
            self.calls.append(rec)
            self.tracer.event("api_call", **rec)

            if 200 <= status < 300:
                return payload
            err = ApiError(status, error_code or "error", payload.get("message", ""))
            # Only idempotent GETs are retried, and only on outage. Never retry a POST.
            if method == "GET" and err.is_outage and attempt < retries:
                attempt += 1
                time.sleep(0.2)
                continue
            raise err

    # -- tools ------------------------------------------------------------
    def search_patients(self, phone: str, dob: str) -> list:
        return self._request("search_patients", "GET", "/patients/search", {"phone": phone, "dob": dob},
                             retries=self.get_retries).get("matches", [])

    def patient_appointments(self, patient_id: str) -> list:
        return self._request("patient_appointments", "GET", "/patients/{}/appointments".format(patient_id),
                             trace_path="/patients/{patientId}/appointments",
                             retries=self.get_retries).get("appointments", [])

    def providers(self, specialty=None, location=None) -> list:
        return self._request("providers", "GET", "/providers", {"specialty": specialty, "location": location},
                             retries=self.get_retries).get("providers", [])

    def availability(self, patient_id, specialty, location=None) -> list:
        return self._request("availability", "GET", "/availability",
                             {"patientId": patient_id, "specialty": specialty, "location": location},
                             retries=self.get_retries).get("slots", [])

    def book(self, patient_id: str, slot_id: str, *, user_confirmed: bool) -> dict:
        # Defense in depth: even if the workflow had a bug, this tool refuses to
        # send confirmed=true unless the caller attests the user said yes.
        if user_confirmed is not True:
            raise ValueError("book() requires explicit user confirmation")
        return self._request("book", "POST", "/appointments",
                             body={"patientId": patient_id, "slotId": slot_id, "confirmed": True}
                             ).get("appointment", {})

    def handoff(self, reason: str, summary: str, patient_id: str | None = None) -> dict:
        body = {"reason": reason, "summary": summary}
        if patient_id:
            body["patientId"] = patient_id
        return self._request("handoff", "POST", "/handoffs", body=body,
                             send_mock_header=self.outage_affects_handoffs)
