# Synthetic Data

All data here is fabricated. It contains no real patient information.

These files are **mock-server fixtures**, not API responses. The mock server reads
them and returns objects that match `../openapi/scheduling-api.yaml`. If you write
your own mock, do the same: the OpenAPI contract is the source of truth for what
your application sees.

## Fixture-Only Fields

Two fields exist in the fixtures but are **not** part of the API contract, and the
mock server must not return them:

| Field | Files | Purpose |
| --- | --- | --- |
| `daysFromToday` + `time` | `slots.json`, `appointments.json` | The server materializes `startTime` at request time as `(today + daysFromToday)T<time>:00-05:00`. This keeps the fixtures from going stale, so slots are always in the near future no matter when you run the assignment. |
| `conflictOnBooking` | `slots.json` | Marks a slot that looks available in `/availability` but fails with `409` when booked. It simulates a race with another booking channel. Your application never sees this field — it just gets a `409`. |

The timezone offset is a fixed `-05:00`. There is no daylight-saving logic. That is
deliberate: this is a prototype fixture, not a calendar system.

## Files

- `patients.json` — 5 patients. `pat_1003` and `pat_1004` deliberately share the same
  name, date of birth, and phone number. They differ only by zip code. This is what
  drives the "multiple matches" scenario.
- `providers.json` — 3 providers. Note that **no provider serves `lakeside`**, and the
  only dermatologist is at `uptown`. This is what drives the "no availability" scenario.
- `slots.json` — 4 bookable slots, one of which is the conflict slot described above.
- `appointments.json` — 2 pre-existing appointments, for `pat_1001` and `pat_1002`.
