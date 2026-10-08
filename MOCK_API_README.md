# Mock Scheduling API

The canonical API surface is `../openapi/scheduling-api.yaml`. A working reference
implementation of that contract ships with this package, so you do not have to build
one. **Use it.** Your 3 hours are for the assistant, not for the mock.

## Run It

```bash
python3 mock-api/server.py
# mock scheduling API on http://localhost:4010
```

Python 3.8+, standard library only. No `pip install`, no database, no config.
State is in memory — restart the server to reset bookings.

Options:

```bash
python3 mock-api/server.py --port 5000
python3 mock-api/server.py --log-file /tmp/mock-api.log
```

## Smoke Test

```bash
curl "http://localhost:4010/patients/search?phone=555-0101&dob=1985-04-12"
curl "http://localhost:4010/providers?specialty=primary_care&location=downtown"
curl "http://localhost:4010/availability?patientId=pat_1001&specialty=primary_care&location=downtown"
curl -X POST http://localhost:4010/appointments \
  -H 'Content-Type: application/json' \
  -d '{"patientId":"pat_1001","slotId":"slot_4001","confirmed":true}'
```

## How To Trigger Each Failure Case

Every failure your assistant must handle is reachable deterministically. You do not
need to mock anything yourself.

| Case | How to trigger | Response |
| --- | --- | --- |
| No patient match | `phone=555-9999&dob=1990-01-01` | `200` with `matches: []` |
| Multiple patient matches | `phone=555-0130&dob=1978-09-22` | `200` with two patients, same name/DOB/phone |
| Provider lookup | `/providers?specialty=primary_care&location=downtown` | `200` with matching providers |
| No availability | `specialty=dermatology&location=lakeside` | `200` with `slots: []` |
| Slot conflict | Book `slot_conflict_001` — it is returned as available by `/availability`, but always fails at booking | `409 slot_taken` |
| Booking without confirmation | `POST /appointments` with `confirmed: false` | `400 confirmation_required` |
| Downstream API outage | Send header `X-Mock-Scenario: api_failure` on **any** request | `503 downstream_unavailable` |
| Bad input | Omit a required parameter, or send an out-of-enum `specialty` / `location` / `reason` | `400` |

`slot_conflict_001` is a permanent trap: it fails every time, so conflict handling is
repeatable in your evals. A slot you successfully book becomes unavailable and returns
`409` on a second attempt.

## Request Log

The server prints one JSON line per request to stdout:

```json
{"ts": "2026-09-23T22:58:40", "method": "POST", "path": "/appointments", "status": 409, "durationMs": 0.4}
```

Useful when you want to see what your agent actually called, separately from your own
application traces.

## If You Would Rather Not Use It

You may write your own mock in any language, as long as it honors
`../openapi/scheduling-api.yaml` and uses the fixtures in `../data/`. See
`../data/README.md` for the two fixture-only fields your mock must not leak into
responses.

Contract-mock tools (Stoplight Prism, Mockoon) will start quickly but serve
**schema-generated examples, not the synthetic data in `../data/`**, and they will not
reproduce the conflict or outage behavior above. If you go that route, expect to add
the deterministic behavior yourself. For a 3-hour time box we recommend the reference
server instead.

## No Database

Do not set up Postgres, MySQL, SQLite migrations, or an ORM for this assignment.
In-memory state or JSON files are fine.
