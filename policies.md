# Assignment Policies

Use these rules when designing your prototype.

## Identity And Privacy

- The assistant should identify or verify the patient before showing patient-specific
appointment details or booking.
- If no patient is found, ask for more information or hand off to a human.
- If multiple patients match, ask a clarifying question before proceeding.
- Do not log full date of birth, phone number, or other sensitive identifiers in plaintext.
- Do not provide medical advice.

## Booking Rules

- The assistant must show available appointment options before booking.
- The assistant must receive explicit user confirmation before booking.
- The assistant must not invent appointment slots or patient data.
- If a selected slot is no longer available, explain the issue and offer another option or hand off.



## Human Handoff

Hand off to a human when:

- The user asks for a human.
- The request is outside supported scheduling scope.
- The assistant cannot confidently identify the patient for a patient-specific request.
- The downstream mock API is unavailable.
- The user asks for medical advice.
- The specialty, location, or appointment type is unsupported.



## Observability

Your prototype should record enough information to debug:

- Intent classification or workflow state.
- Tool/API calls made.
- API outcomes and errors.
- Escalation reason.
- Basic latency measurements.

Avoid logging secrets, OpenAI API keys, or sensitive identifiers.