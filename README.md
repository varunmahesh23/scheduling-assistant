# AI Appointment Scheduling Assistant (take-home prototype)

A text-based, multi-turn assistant that helps patients find providers, book appointments (with explicit
confirmation) and look up existing appointments, using the provided mock scheduling API. When it can't proceed
safely it hands off to a human instead of improvising.

<!-- Keep the next line only if it is true for you: -->
I completed this within the assigned 3-hour window.

Python 3.8+, **standard library only** (no `pip install`). No database. No API key is needed to run or test it.

## Quick start

```bash
# terminal 1 - the provided mock scheduling API (state is in memory; restart to reset)
python3 server.py                  # http://localhost:4010

# terminal 2 - the assistant
python3 cli.py                        # offline rule-based NLU by default
python3 cli.py --debug                # also echo redacted trace events (nice for demos)

# optional: use an LLM for intent/slot understanding (key is read from the environment only, never committed)
export OPENAI_API_KEY="..."                 # optional OPENAI_MODEL (default gpt-4o-mini)
python3 cli.py --nlu llm              # --nlu auto (default) uses OpenAI if the key is set, else rules

# tests / evals (spawns its own mock servers on free ports; no key, no network)
python3 -m unittest test_scenarios -v
```

Useful flags: `--mock-scenario api_failure` (forces the 503 outage path on every scheduling call),
`--outage-affects-handoffs` (also fail `/handoffs`), `--trace-file logs/trace.jsonl`, `--no-trace`.
Piped input works too: `printf '%s\n' "Which primary care providers are downtown?" | python3 cli.py`.

### Try these (synthetic data)

| Flow | Say |
|---|---|
| Provider lookup | `Which primary care providers are downtown?` |
| Booking happy path | `I want to book a primary care appointment` -> `downtown` -> `555-0101 and 1985-04-12` -> `1` -> `yes` |
| Slot conflict | same as above, pick the 08:30 slot (`slot_conflict_001`), `yes` |
| No patient | phone `555-9999`, DOB `1990-01-01` (twice) |
| Multiple patients | `Look up my appointment`, `555-0130 1978-09-22`, then ZIP `70115` |
| No availability | `book dermatology at lakeside` with `555-0102 1992-06-03` |
| Medical advice | `I have chest pain, should I wait?` |
| API down | `python3 cli.py --mock-scenario api_failure`, then try to book |

## Architecture

```
user text
   |
   v
 safety.py  --- deterministic guard on EVERY turn: medical-advice / human-request  --> handoff
   |
 nlu.py     --- LLM (or rules): intent + specialty + location ONLY (identifiers redacted before sending)
   |
 agent.py   --- state machine: idle -> need_info -> [need_zip] -> pick_slot -> confirm -> booked
   |            (also no_availability). All wording is templated from API data.
   v
 api.py     --- tool boundary: typed calls, timing, retries (GET only), redacted tracing
   |
   v
 mock scheduling API
```

### Where model reasoning ends and deterministic code begins

| Decision | Owner | Why |
|---|---|---|
| Intent, "skin doctor" -> dermatology, "near the lake" -> lakeside | **LLM** (rules as fallback) | Open-ended language is what models are good at |
| Phone / DOB / ZIP extraction | Code (regex) | High-stakes, and means identifiers never need to leave the process |
| Medical-advice and "get me a human" detection | Code **OR** LLM | Safety net: a model miss can't route a symptom question into booking |
| Which question to ask next, when to hand off, identity resolution | Code | Policy, must be predictable and testable |
| Which slots may be shown | API (code filters out slots that already 409'd) | Never invent a slot |
| Slot selection | Code, matched against the list that was actually displayed | |
| **What counts as confirmation** | Code, strict whitelist (`yes`, `confirm`, `book it`...) | "maybe", "ok", "yes but later" never book. The LLM is never consulted |
| `confirmed: true` | `SchedulingClient.book(..., user_confirmed=True)` refuses otherwise | Defense in depth |
| Every sentence the user reads | Code templates filled with API data | The model cannot fabricate a slot, patient or confirmation number |

The LLM never sees API responses or patient data, and never writes replies. The cost is less "chatty"
phrasing; the benefit is that the policies in `policies.md` are enforced by construction rather than by prompt.

### Policy coverage

| Policy | How it's met |
|---|---|
| Verify before booking / appointment details | `search_patients` (phone + DOB) must return exactly one match before availability or appointments are touched |
| No match | One retry, then handoff `identity_unclear`. Nothing about any patient is revealed |
| Multiple matches | Asks for ZIP (the only differing field), resolves locally against the API's candidates, nothing shown; 2 failures -> handoff `identity_unclear` |
| Show options before booking / explicit confirmation | Offer list -> "To confirm: ... (yes/no)" -> POST only on a whitelisted yes |
| Slot gone (409) | Says nothing was booked, drops that slot (the mock keeps advertising it), re-queries, offers what's left or alternatives |
| Nothing available | Explains; suggests locations where `/providers` lists that specialty (labelled as not a guarantee); offers search or human (`no_availability`) |
| API down (503/unreachable) | One GET retry, never POST; plain explanation; handoff `api_failure`. If the handoff also fails it says so rather than inventing a reference |
| Medical advice | No advice, no triage, generic "call 911 if this may be an emergency", handoff `medical_advice`, session cleared |
| Unsupported specialty/location, user asks for a human | handoff `unsupported_request` / `user_requested` |
| No sensitive identifiers in logs | See Observability |

### Observability

Every run appends JSONL to `logs/trace.jsonl` (git-ignored): `turn` (intent, NLU source, state before/after, NLU and
total latency), `api_call` (name, path, status, latency, attempt, error code), `identity`, `booking`, `handoff`
(reason), `llm_call` (ok/error, latency). **Redaction:** phone -> `***-**01`, DOB/ZIP -> `<redacted>`, patientId -> salted
hash, user text is never logged (only its length), error strings are scrubbed. A test asserts none of the demo
patient's identifiers appear in traces. Handoff summaries are generated by code and carry no symptoms or identifiers.

### Tests / evals

`python3 -m unittest test_scenarios -v` runs 24 tests, mapped to the scenario ids in `scenarios.yaml`
(happy path, provider lookup, no patient, multiple patients, slot conflict, no availability, API failure,
medical advice) plus extra guards: ambiguous confirmations never book, a stray second "yes" doesn't rebook, no
leakage in replies or traces, only API slots are shown, LLM output validation, and fallback on LLM failure.
Each asserts both what the user was told and what was actually called on the API. I mutation-checked the key
guards (loosening confirmation, unfiltered conflict slot, raw phone in logs each make a test fail).

The OpenAI path is tested against a local stub server (request shape, redaction, validation, fallback). I could not
reach OpenAI from the environment where I built this, so run it once with a real key before relying on it.

## Key decisions & assumptions

- **Model is a classifier, not the agent.** Chosen because the brief is about the boundary between model, API and
  code. It makes behaviour testable and cheap (one small structured-output call per turn, ~no tokens of history).
- **Phone + DOB is weak identity.** It's what the API offers. I greet by first name only after a unique match and never show
  other fields. Production would add a second factor (OTP) before showing appointment details.
- **Identifiers are dropped from memory after the search** and never sent to the LLM (replaced by `[PHONE]`/`[DOB]`/`[ZIP]`).
- Fixture phones have no area code, so only the last 7 digits are used (`(504) 555-0101` -> `555-0101`).
- Max 2 identity attempts, then handoff. At most 5 slots are shown.
- "Any location" is allowed (the API treats location as optional).
- The emergency line says "911 (or your local emergency number)" because the fixtures are US-based. It is a generic safety
  message, not triage.
- The mock applies `X-Mock-Scenario: api_failure` to *every* request including `/handoffs`. By default I send it only to
  scheduling endpoints, treating the handoff service as separate, so the `api_failure` handoff can be shown;
  `--outage-affects-handoffs` tests the case where that fails too.
- After a handoff the session (including verified identity) is cleared.
- The brief (`assignment.md`) says 3 hours; the invitation email says 2. I followed the package.

## Known limitations

- Rule-based fallback is keyword-driven; unusual phrasing relies on the LLM. Specialty/location typos aren't handled.
- No date/time preferences (`startDate`/`endDate` are supported by the API but not exposed), no provider-name or
  time-of-day selection ("the 11am one"), no modality (virtual) filter.
- Single session, single patient, synchronous CLI; no persistence between runs.
- Verification is phone+DOB only (see above). Handoff context for the human agent is deliberately minimal.
- Only the last user message is sent to the LLM (plus what the assistant is awaiting), so long-range co-reference is out of scope.
- Not tested against a live OpenAI endpoint (see above).

## What I'd do next

1. **Live LLM eval set**: ~100 labelled utterances (typos, multi-intent, adversarial, symptom-adjacent) scored for
   intent/slot accuracy, with a medical-advice recall target near 100% and a false-positive budget.
2. **Tracing**: ship the JSONL to OpenTelemetry spans (one per turn, child span per API call), dashboards for
   handoff rate by reason, 409 rate, p95 latency, NLU fallback rate.
3. **Date / time / provider preferences** and an "earliest available" shortcut that still goes through confirmation.
4. **Stronger identity**: OTP to the phone on file; rate-limit lookups to resist enumeration.
5. **Voice/SMS channel adapters** around the same `Assistant.respond()`.

### Reschedule & cancel (design sketch)

Add intents `reschedule`/`cancel`. Both require verified identity, then `GET /patients/{id}/appointments` to show
*only that patient's* upcoming appointments and let the user choose one (code-matched, like slots). **Cancel:** state
`confirm_cancel` with the same strict yes/no gate and an irreversibility warning, then a new `DELETE /appointments/{id}`
(not in this API). **Reschedule:** reuse `availability` + `pick_slot`, then book-new-then-cancel-old, or an atomic
`PATCH` if the API offers one; the failure mode to design for is "new slot booked, cancel of old failed" (idempotency
keys + compensating action + handoff). Policy extras: cancellation windows/fees are API-decided, never assumed.

### Production notes

- **Cost/latency:** one `gpt-4o-mini`-class call per turn on ~150 tokens in/out (sub-second); rules short-circuit for
  slot picks / yes-no / identifiers so many turns need no model call at all (a cheap next step: skip the LLM when
  state is `pick_slot`/`confirm`/`need_zip`). Cache `/providers` (already per session). Timeouts: 8s LLM, 5s API.
- **Monitoring:** handoff reasons, `slot_conflict` rate, NLU fallback rate, identity-failure rate (enumeration signal).
- **Rollout:** shadow mode (assistant proposes, human books) -> a small share of traffic with handoff always one message
  away -> widen. **Rollback:** feature flag routes everything to the human queue; the assistant is stateless between sessions.
- **What breaks first with a real scheduler:** the TOCTOU gap between `availability` and `book` (held slots / short
  reservations), identity matching beyond exact phone+DOB, timezones/DST (the mock hard-codes -05:00), pagination, and
  rate limits. Booking is deliberately never auto-retried because the POST isn't idempotent.

## Layout

Everything is in one flat folder (no sub-folders, so it can be uploaded by drag and drop):

```
cli.py            entry point (python3 cli.py)
agent.py          workflow state machine
nlu.py            LLM + rule-based intent/slot understanding
extract.py        identifier extraction, slot choice, yes/no parsing
safety.py         deterministic medical-advice / human-request guards
api.py            tool boundary (scheduling API client)
tracing.py        redacted JSONL tracing
test_scenarios.py scenario evals
server.py + *.json  provided mock API and fixtures
scheduling-api.yaml, policies.md, assignment.md, scenarios.yaml, MOCK_API_README.md, DATA_README.md  <- provided package
```

The provided mock API and data are unchanged except that `server.py` reads the JSON fixtures from its own folder.
