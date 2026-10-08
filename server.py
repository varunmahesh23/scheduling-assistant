#!/usr/bin/env python3
"""Reference mock server for the Synthetic Scheduling API.

Implements ../openapi/scheduling-api.yaml over the fixtures in ../data/.
Python 3.8+ standard library only. No pip install, no database.

    python3 mock-api/server.py            # serves http://localhost:4010
    python3 mock-api/server.py --port 5000

State lives in memory. Restart the server to reset it.
"""

import argparse
import json
import sys
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DATA_DIR = Path(__file__).resolve().parent
TZ_OFFSET = "-05:00"

SPECIALTIES = ("primary_care", "dermatology")
LOCATIONS = ("downtown", "uptown", "lakeside")
HANDOFF_REASONS = (
    "user_requested",
    "identity_unclear",
    "unsupported_request",
    "medical_advice",
    "api_failure",
    "no_availability",
    "other",
)

# Fixture-only fields. These must never appear in an API response.
INTERNAL_FIELDS = ("daysFromToday", "time", "conflictOnBooking")


def load(name):
    with open(DATA_DIR / name) as fh:
        return json.load(fh)


def start_time(fixture):
    """Materialize an absolute startTime from the fixture's relative date."""
    day = date.today() + timedelta(days=fixture["daysFromToday"])
    return "{}T{}:00{}".format(day.isoformat(), fixture["time"], TZ_OFFSET)


def public(fixture):
    """Strip fixture-only fields and add the materialized startTime."""
    out = {k: v for k, v in fixture.items() if k not in INTERNAL_FIELDS}
    out["startTime"] = start_time(fixture)
    return out


class Store:
    """In-memory scheduling state, seeded from the fixtures."""

    def __init__(self):
        self.patients = load("patients.json")
        self.providers = load("providers.json")
        self.slots = load("slots.json")
        self.appointments = load("appointments.json")
        self.handoffs = []
        self._next_id = 1

    def patient(self, patient_id):
        return next((p for p in self.patients if p["patientId"] == patient_id), None)

    def slot(self, slot_id):
        return next((s for s in self.slots if s["slotId"] == slot_id), None)

    def new_id(self, prefix):
        self._next_id += 1
        return "{}_{}".format(prefix, 9000 + self._next_id)


class Error(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def require(params, *names):
    missing = [n for n in names if not params.get(n)]
    if missing:
        raise Error(400, "missing_parameter", "Missing required parameter(s): " + ", ".join(missing))


def one_of(value, allowed, name):
    if value not in allowed:
        raise Error(400, "invalid_parameter", "{} must be one of: {}".format(name, ", ".join(allowed)))


# --- Route handlers -------------------------------------------------------


def search_patients(store, query, _body):
    require(query, "phone", "dob")
    phone, dob = query["phone"], query["dob"]
    matches = [p for p in store.patients if p["phone"] == phone and p["dateOfBirth"] == dob]
    return 200, {"matches": matches}


def patient_appointments(store, patient_id):
    if not store.patient(patient_id):
        raise Error(404, "patient_not_found", "No patient with id {}".format(patient_id))
    found = [a for a in store.appointments if a["patientId"] == patient_id]
    return 200, {"appointments": [public(a) for a in found]}


def availability(store, query, _body):
    require(query, "patientId", "specialty")
    one_of(query["specialty"], SPECIALTIES, "specialty")
    if query.get("location"):
        one_of(query["location"], LOCATIONS, "location")
    if not store.patient(query["patientId"]):
        raise Error(400, "unknown_patient", "Unknown patientId. Search for the patient first.")

    found = []
    for slot in store.slots:
        if not slot.get("available"):
            continue
        if slot["specialty"] != query["specialty"]:
            continue
        if query.get("location") and slot["location"] != query["location"]:
            continue
        day = start_time(slot)[:10]
        if query.get("startDate") and day < query["startDate"]:
            continue
        if query.get("endDate") and day > query["endDate"]:
            continue
        found.append(public(slot))
    found.sort(key=lambda s: s["startTime"])
    return 200, {"slots": found}


def providers(store, query, _body):
    if query.get("specialty"):
        one_of(query["specialty"], SPECIALTIES, "specialty")
    if query.get("location"):
        one_of(query["location"], LOCATIONS, "location")

    found = []
    for provider in store.providers:
        if query.get("specialty") and provider["specialty"] != query["specialty"]:
            continue
        if query.get("location") and query["location"] not in provider["locations"]:
            continue
        found.append(provider)
    return 200, {"providers": found}


def book(store, _query, body):
    require(body, "patientId", "slotId")
    if body.get("confirmed") is not True:
        raise Error(400, "confirmation_required", "confirmed must be true, and only after explicit user confirmation.")
    if not store.patient(body["patientId"]):
        raise Error(400, "unknown_patient", "Unknown patientId.")

    slot = store.slot(body["slotId"])
    if slot is None:
        raise Error(400, "unknown_slot", "Unknown slotId. Search availability first.")
    if slot.get("conflictOnBooking"):
        raise Error(409, "slot_taken", "That slot was just taken by another booking channel.")
    if not slot.get("available"):
        raise Error(409, "slot_taken", "That slot is no longer available.")

    slot["available"] = False
    appointment = {
        "appointmentId": store.new_id("appt"),
        "patientId": body["patientId"],
        "providerId": slot["providerId"],
        "specialty": slot["specialty"],
        "location": slot["location"],
        "daysFromToday": slot["daysFromToday"],
        "time": slot["time"],
        "status": "scheduled",
    }
    store.appointments.append(appointment)
    return 201, {"appointment": public(appointment)}


def handoff(store, _query, body):
    require(body, "reason", "summary")
    one_of(body["reason"], HANDOFF_REASONS, "reason")
    record = {
        "handoffId": store.new_id("handoff"),
        "patientId": body.get("patientId"),
        "reason": body["reason"],
        "summary": body["summary"],
        "status": "queued",
    }
    store.handoffs.append(record)
    return 201, {"handoffId": record["handoffId"], "status": "queued"}


ROUTES = {
    ("GET", "/patients/search"): search_patients,
    ("GET", "/availability"): availability,
    ("GET", "/providers"): providers,
    ("POST", "/appointments"): book,
    ("POST", "/handoffs"): handoff,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    store = None
    log_fh = None

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        started = time.time()
        parsed = urlparse(self.path)
        status, payload = 500, {"code": "internal_error", "message": "unhandled"}
        try:
            status, payload = self.route(method, parsed)
        except Error as err:
            status, payload = err.status, {"code": err.code, "message": err.message}
        except Exception as err:  # noqa: BLE001 - mock server, surface the cause
            status, payload = 500, {"code": "internal_error", "message": str(err)}
        self.respond(status, payload)
        self.audit(method, parsed.path, status, (time.time() - started) * 1000)

    def route(self, method, parsed):
        # Deterministic fault injection: send `X-Mock-Scenario: api_failure`
        # on any request to exercise the downstream-outage path.
        if self.headers.get("X-Mock-Scenario") == "api_failure":
            raise Error(503, "downstream_unavailable", "Scheduling system is temporarily unavailable.")

        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        body = self.read_body() if method == "POST" else {}

        handler = ROUTES.get((method, parsed.path))
        if handler:
            return handler(self.store, query, body)

        parts = parsed.path.strip("/").split("/")
        if method == "GET" and len(parts) == 3 and parts[0] == "patients" and parts[2] == "appointments":
            return patient_appointments(self.store, parts[1])

        raise Error(404, "not_found", "No route for {} {}".format(method, parsed.path))

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise Error(400, "invalid_json", "Request body is not valid JSON.")
        if not isinstance(parsed, dict):
            raise Error(400, "invalid_json", "Request body must be a JSON object.")
        return parsed

    def respond(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def audit(self, method, path, status, ms):
        line = json.dumps(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "method": method,
                "path": path,
                "status": status,
                "durationMs": round(ms, 1),
            }
        )
        print(line, flush=True)
        if self.log_fh:
            self.log_fh.write(line + "\n")
            self.log_fh.flush()

    def log_message(self, *_args):
        """Silence the default stderr access log; audit() is the log."""


def main():
    parser = argparse.ArgumentParser(description="Reference mock scheduling API.")
    parser.add_argument("--port", type=int, default=4010)
    parser.add_argument("--log-file", help="Also append the JSON request log to this file.")
    args = parser.parse_args()

    Handler.store = Store()
    if args.log_file:
        Handler.log_fh = open(args.log_file, "a")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("mock scheduling API on http://localhost:{}".format(args.port), file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
