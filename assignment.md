# Take-Home: AI Appointment Scheduling Assistant

## Description

Build a **text-based, multi-turn AI assistant** for medical appointment scheduling by
calling a mock scheduling API.

A user may ask to find a provider, look up an existing appointment, or book a new
appointment. Your assistant should recognize the user's intent, identify or verify
the user when patient-specific data is needed, call the API for real provider /
appointment / availability data, show options, get explicit confirmation for booking,
and then book the appointment.

When it cannot complete the task safely — no patient match, multiple patients match, no availability, the selected slot is no longer available, the API is down, or the user asks for medical advice — it should stop and hand off to a human instead of improvising.

This is not a generic chatbot exercise. The interesting part is the boundary between
what the model decides, what the API decides, and what your code decides.

You do not need healthcare, scheduling systems, or channel-specific background.

## Time Box

**3 hours, starting when you download / receive the full assignment package.**


|                     |                                                                  |
| ------------------- | ---------------------------------------------------------------- |
| Inside the 3 hours  | Code and README                                                  |
| Outside the 3 hours | The walkthrough video — due 30 minutes after the coding deadline |


Please create your own GitHub repository for the work. We will use the last commit
pushed before the 3-hour coding deadline as the end of the implementation window.
Commits after the deadline may be ignored during review.

Please stop at 3 hours. If something is unfinished, write down what you would have
done next — we would rather read that than see you go over.

**We do not expect you to finish everything below.** 

## Quickstart For The Provided Mock API

This starts the mock scheduling API that your project should call. It is not your
application; you still need to build your own assistant separately.

We provide this mock API so you can focus on the assistant. Please do not spend time
building your own scheduling backend unless you have a strong reason to do so.

```bash
# 1. Start the mock scheduling API (Python 3.8+, no install needed)
python3 mock-api/server.py
# -> mock scheduling API on http://localhost:4010

# 2. Check it works
curl "http://localhost:4010/patients/search?phone=555-0101&dob=1985-04-12"

# 3. Use the temporary OpenAI key provided for this assignment
export OPENAI_API_KEY="..."
```

We will provide a temporary OpenAI API key for this assignment. Do not commit the key.
Read it from the environment. The key is only for this exercise and will be deleted
after the assignment window.

Then read, in this order: `policies.md` (the rules), `openapi/scheduling-api.yaml`
(the API), `mock-api/README.md` (how to trigger every failure case).

## What To Build

Any shape is fine — CLI, small web app, or HTTP service. A CLI is the fastest and
costs you nothing in our evaluation. The interaction should support multiple turns
rather than one giant form submission. We do not expect a fancy UI; we are primarily
evaluating AI engineering judgment, workflow design, API/tool boundaries, confirmation
behavior, and failure handling.

### Required

Your assistant must:

1. **Support a short multi-turn flow** that can ask follow-up questions for missing
   information such as phone, date of birth, specialty, location, or slot choice.
2. **Recognize at least two intents** from text input:
   - provider lookup, e.g. "Which primary care providers are downtown?"
   - appointment booking, e.g. "I want to book a primary care appointment."
3. **Implement provider lookup** via `GET /providers`.
4. **Implement one end-to-end booking happy path**:
   - identify or verify the patient via `GET /patients/search` when needed for booking
   - fetch real availability via `GET /availability`
   - show only slots returned by the API
   - ask for explicit confirmation
   - book via `POST /appointments` with `confirmed: true`
5. **Handle at least one failure / handoff case** from the table below, without
   fabricating data or leaking patient information.



### Good-to-have

If you have time after the required scope, good additions include:

- Handling the recommended failure cases in the table below.
- Handling human handoff via `POST /handoffs` with the right `reason`.
- Supporting lookup of existing appointments via `GET /patients/{patientId}/appointments`.
- A simple test/eval harness covering 2-3 scenarios from `scenarios.yaml`.
- Basic logs/traces showing intent, workflow state, API calls, outcomes, errors,
  escalation reason if applicable, and latency.
- A cleaner user experience around choosing slots and confirming the booking.
- A lightweight UI, if it helps demonstrate the flow.
- More structured logs/traces, including per-step latency.
- A small abstraction around tools/API calls.
- Brief design notes for how you would add reschedule and cancel.
- Brief production notes on cost, latency, monitoring, rollout, and rollback.

Rescheduling and cancellation are **not required** for the prototype.

## Failure Cases To Consider

Handle at least one of these in the prototype. All of them are reachable
deterministically against the mock — the full trigger table is in `mock-api/README.md`.


| Case                         | Trigger                                                                          | Priority    |
| ---------------------------- | -------------------------------------------------------------------------------- | ----------- |
| No patient found             | `phone=555-9999`, `dob=1990-01-01`                                               | highest     |
| Multiple patients match      | `phone=555-0130`, `dob=1978-09-22` (same name, DOB, and phone; only zip differs) | high        |
| User asks for medical advice | `"I have chest pain, should I wait?"`                                            | high        |
| Slot no longer available     | Book `slot_conflict_001` — it comes back as available, then `409`s               | recommended |
| Nothing available            | `specialty=dermatology`, `location=lakeside`                                     | recommended |
| Scheduling system down       | Header `X-Mock-Scenario: api_failure` on any request → `503`                     | recommended |


"Handled" means: the user gets a truthful, specific explanation, nothing is fabricated,
no patient data leaks, and the assistant either offers a real next step or escalates.
A generic apology is not handling it.

## The Rules

`policies.md` is the full list and it is short — read it. The four that we look at hardest:

- **Use identity checks for patient-specific actions.** For booking or appointment
details, the assistant should identify or verify the patient first.
- **Never invent a slot, a patient, or a confirmation.** If it did not come from the
API, it does not go to the user.
- **Never book without explicit user confirmation.**
- **Never give medical advice.** Escalate.

## What To Submit

1. **A link to the GitHub repo you create** for this assignment.
2. **A README** that clearly explains:
   - How to set up your project.
   - How to start the provided mock scheduling API.
   - How to run your assistant.
   - Architecture, key design decisions, assumptions and tradeoffs, known limitations,
     and what you would do next if you had more time.
   - If you included tests/evals, the exact command we should run.
3. **A walkthrough video, 5 minutes maximum.** Demo the provider lookup flow, the booking
   happy path, one failure / handoff case, and briefly explain your architecture and tradeoffs.

Your README should also state: `I completed this within the assigned 3-hour window.`

### Definition Of Done

- [ ] `git clone` -> README instructions -> the mock API starts and your assistant runs,
      with no undocumented steps.
- [ ] The assistant supports a short multi-turn interaction.
- [ ] Provider lookup calls `GET /providers`.
- [ ] The booking happy path books an appointment end to end.
- [ ] At least one failure / handoff case behaves correctly.
- [ ] No API key is committed to the repo.

## Questions We May Ask In The Live Review

No need to write answers in advance, but be ready to discuss:

- Where does model reasoning end and deterministic code begin?
- What would break first with a real scheduling system?
- How would you add stronger evals or traces?
- How would you add reschedule or cancellation?
- How would you manage latency and cost if this were used at volume?
