"""Scenario evals. Each test scripts a conversation against a *fresh* mock API process and asserts
on (a) what the user was told and (b) what the assistant actually called on the API.

Run:  python3 -m unittest discover -s tests -v        (no API key or network needed)
"""
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from agent import Assistant  # noqa: E402
from api import SchedulingClient  # noqa: E402
from nlu import OpenAINLU, RuleNLU  # noqa: E402
from tracing import Tracer  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Harness:
    def __init__(self, mock_scenario=None, outage_affects_handoffs=False):
        self.port = free_port()
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py"), "--port",
                                      str(self.port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = "http://127.0.0.1:{}".format(self.port)
        for _ in range(50):
            try:
                urllib.request.urlopen(url + "/providers", timeout=0.5)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        self.tracer = Tracer()
        self.api = SchedulingClient(url, self.tracer, mock_scenario=mock_scenario,
                                    outage_affects_handoffs=outage_affects_handoffs, get_retries=0)
        self.bot = Assistant(self.api, RuleNLU(), self.tracer)

    def say(self, *messages):
        reply = ""
        for m in messages:
            reply = self.bot.respond(m)
        return reply

    def calls(self, name=None):
        return [c for c in self.api.calls if name is None or c["name"] == name]

    def handoff_reasons(self):
        return [c["params"].get("reason") for c in self.calls("handoff")]

    def close(self):
        self.proc.terminate()
        self.proc.wait()


class Base(unittest.TestCase):
    def make(self, **kw):
        h = Harness(**kw)
        self.addCleanup(h.close)
        return h


class TestProviderLookup(Base):
    def test_provider_lookup_needs_no_identity(self):
        h = self.make()
        reply = h.say("Which primary care providers are available downtown?")
        self.assertIn("Dr. Elena Brooks", reply)
        self.assertIn("Dr. Marcus King", reply)
        self.assertNotIn("Priya Shah", reply)  # dermatologist must not appear
        self.assertEqual([c["name"] for c in h.calls()], ["providers"])  # no patient search, nothing else


class TestBooking(Base):
    def test_happy_path_requires_confirmation_before_post(self):
        h = self.make()
        h.say("I want to book a primary care appointment")
        reply = h.say("downtown", "My number is 555-0101 and my DOB is 1985-04-12")
        self.assertIn("1.", reply)
        self.assertEqual(len(h.calls("search_patients")), 1)
        self.assertEqual(len(h.calls("availability")), 1)
        reply = h.say("1")
        self.assertIn("Shall I book", reply)
        self.assertEqual(h.calls("book"), [], "must not POST /appointments before the user confirms")
        reply = h.say("yes")
        self.assertIn("You're booked", reply)
        book = h.calls("book")
        self.assertEqual(len(book), 1)
        self.assertEqual(book[0]["status"], 201)
        self.assertIn("appt_", reply)  # confirmation number came from the API

    def test_only_api_slots_are_shown(self):
        h = self.make()
        h.say("book primary care downtown", "555-0101 1985-04-12")
        shown = h.bot.s.offered
        api_slots = h.api.availability("pat_1001", "primary_care", "downtown")
        self.assertEqual({x["slotId"] for x in shown}, {x["slotId"] for x in api_slots})

    def test_ambiguous_confirmation_never_books(self):
        h = self.make()
        h.say("book primary care downtown", "555-0101 1985-04-12", "1")
        for utterance in ("maybe", "sounds good", "yes but a different time", "ok"):
            h.say(utterance)
            self.assertEqual(h.calls("book"), [], utterance)
        h.say("no")
        self.assertEqual(h.calls("book"), [])
        self.assertEqual(h.bot.s.state, "pick_slot")

    def test_booked_slot_cannot_be_rebooked_by_stray_yes(self):
        h = self.make()
        h.say("book primary care downtown", "555-0101 1985-04-12", "1", "yes")
        h.say("yes")
        self.assertEqual(len(h.calls("book")), 1)

    def test_slot_conflict(self):
        h = self.make()
        h.say("book primary care downtown", "555-0101 1985-04-12")
        reply = h.say("slot_conflict_001", "yes")
        self.assertEqual(h.calls("book")[0]["status"], 409)
        self.assertIn("nothing has been booked", reply)
        self.assertNotIn("You're booked", reply)
        self.assertNotIn("slot_conflict_001", [x["slotId"] for x in h.bot.s.offered])  # trap slot filtered out
        self.assertIn("1.", reply)  # remaining real slots are offered

    def test_no_availability_offers_real_alternative_then_handoff(self):
        h = self.make()
        reply = h.say("book a dermatology appointment at lakeside", "555-0102 1992-06-03")
        self.assertIn("don't see any open dermatology", reply)
        self.assertIn("uptown", reply)  # from /providers, not invented
        self.assertEqual(h.calls("book"), [])
        reply = h.say("connect me to a person")
        self.assertEqual(h.handoff_reasons(), ["no_availability"])
        self.assertIn("handoff_", reply)


class TestIdentity(Base):
    def test_no_patient_match_retries_then_hands_off_without_leaking(self):
        h = self.make()
        h.say("I'd like to book an appointment")
        reply = h.say("primary care", "downtown", "555-9999 1990-01-01")
        self.assertIn("couldn't find", reply)
        self.assertEqual(h.handoff_reasons(), [])  # first miss: ask for more info
        reply = h.say("555-9999 1990-01-01")
        self.assertEqual(h.handoff_reasons(), ["identity_unclear"])
        self.assertEqual(h.calls("availability"), [])
        self.assertEqual(h.calls("book"), [])
        self.assertIn("handoff_", reply)

    def test_multiple_matches_asks_zip_and_leaks_nothing(self):
        h = self.make()
        reply = h.say("Look up my appointment", "555-0130 1978-09-22")
        self.assertIn("ZIP", reply)
        for leaked in ("Avery", "Patel", "70115", "70005", "pat_100"):
            self.assertNotIn(leaked, reply)
        self.assertEqual(h.calls("patient_appointments"), [])
        reply = h.say("70115")
        self.assertIn("confirmed", reply)
        self.assertEqual(len(h.calls("patient_appointments")), 1)

    def test_multiple_matches_wrong_zip_hands_off(self):
        h = self.make()
        h.say("Look up my appointment", "555-0130 1978-09-22", "99999", "11111")
        self.assertEqual(h.handoff_reasons(), ["identity_unclear"])
        self.assertEqual(h.calls("patient_appointments"), [])

    def test_my_appointments(self):
        h = self.make()
        reply = h.say("what are my upcoming appointments?", "555-0101 1985-04-12")
        self.assertIn("primary care with Dr. Elena Brooks", reply)


class TestHandoffs(Base):
    def test_medical_advice_escalates_without_advice(self):
        h = self.make()
        reply = h.say("I have chest pain, should I wait?")
        self.assertEqual(h.handoff_reasons(), ["medical_advice"])
        self.assertIn("911", reply)
        self.assertEqual([c["name"] for c in h.calls()], ["handoff"])  # no patient/availability lookups
        self.assertNotIn("wait", reply.lower().replace("wait for", ""))

    def test_medical_advice_mid_booking_flow(self):
        h = self.make()
        h.say("book primary care downtown", "555-0101 1985-04-12")
        h.say("Is this rash serious? What should I take?")
        self.assertEqual(h.handoff_reasons(), ["medical_advice"])
        self.assertEqual(h.bot.s.patient_id, None, "session must be cleared after handoff")

    def test_symptom_mention_in_booking_request_is_not_advice(self):
        h = self.make()
        reply = h.say("I have a rash and want to book a dermatologist")
        self.assertEqual(h.handoff_reasons(), [])
        self.assertIn("verify", reply)

    def test_api_failure_creates_handoff(self):
        h = self.make(mock_scenario="api_failure")
        reply = h.say("book a primary care appointment downtown", "555-0101 1985-04-12")
        self.assertIn("isn't responding", reply)
        self.assertIn("nothing has been booked", reply)
        self.assertEqual(h.handoff_reasons(), ["api_failure"])
        self.assertEqual([c["status"] for c in h.calls("handoff")], [201])

    def test_api_failure_and_handoff_down_is_reported_honestly(self):
        h = self.make(mock_scenario="api_failure", outage_affects_handoffs=True)
        reply = h.say("book a primary care appointment downtown", "555-0101 1985-04-12")
        self.assertIn("nothing has been sent", reply)
        self.assertNotIn("handoff_", reply)  # no fabricated reference

    def test_user_requests_human(self):
        h = self.make()
        h.say("can I talk to a real person?")
        self.assertEqual(h.handoff_reasons(), ["user_requested"])

    def test_unsupported_specialty(self):
        h = self.make()
        h.say("I need to book a cardiology appointment")
        self.assertEqual(h.handoff_reasons(), ["unsupported_request"])


class TestPolicyAndObservability(Base):
    def test_trace_contains_no_sensitive_identifiers(self):
        h = self.make()
        h.say("book primary care downtown", "my phone is 555-0101, dob 1985-04-12", "1", "yes")
        blob = json.dumps(h.tracer.events) + json.dumps(h.api.calls)
        for secret in ("555-0101", "1985-04-12", "Maya", "Chen", "70112", "pat_1001"):
            self.assertNotIn(secret, blob)
        kinds = {e["kind"] for e in h.tracer.events}
        self.assertTrue({"api_call", "turn", "identity", "booking"} <= kinds)
        self.assertTrue(all("ms" in e for e in h.tracer.events if e["kind"] == "api_call"))

    def test_handoff_summary_has_no_identifiers(self):
        h = self.make()
        h.say("I have chest pain, should I wait?")
        # summary is code-generated; verify the call log has no free-text user content
        self.assertNotIn("chest", json.dumps(h.api.calls))

    def test_book_tool_refuses_without_confirmation(self):
        h = self.make()
        with self.assertRaises(ValueError):
            h.api.book("pat_1001", "slot_4001", user_confirmed=False)
        self.assertEqual(h.calls("book"), [])


class _StubOpenAI(http.server.BaseHTTPRequestHandler):
    seen = []
    mode = "ok"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _StubOpenAI.seen.append({"body": body, "auth": self.headers.get("Authorization")})
        if _StubOpenAI.mode == "bad_json":
            content = "not json"
        elif _StubOpenAI.mode == "bad_enum":
            content = json.dumps({"intent": "hack", "specialty": None, "location": None})
        else:
            content = json.dumps({"intent": "book", "specialty": "dermatology", "location": "uptown"})
        raw = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class TestOpenAINLU(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubOpenAI)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:{}/v1".format(cls.srv.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _StubOpenAI.seen, _StubOpenAI.mode = [], "ok"
        self.nlu = OpenAINLU(Tracer(), api_key="test-key", base_url=self.url)

    def test_llm_result_used_and_identifiers_redacted_before_sending(self):
        p = self.nlu.parse("my skin is bad, book me. phone 555-0101 born 1985-04-12 zip 70112")
        self.assertEqual((p.intent, p.specialty, p.location, p.source), ("book", "dermatology", "uptown", "llm"))
        sent = json.dumps(_StubOpenAI.seen[0]["body"])
        for secret in ("555-0101", "1985-04-12", "70112"):
            self.assertNotIn(secret, sent)
        self.assertEqual(_StubOpenAI.seen[0]["auth"], "Bearer test-key")

    def test_bad_model_output_falls_back_to_rules(self):
        for mode in ("bad_json", "bad_enum"):
            _StubOpenAI.mode = mode
            p = self.nlu.parse("Which primary care providers are downtown?")
            self.assertEqual((p.intent, p.specialty, p.location), ("provider_lookup", "primary_care", "downtown"))
            self.assertEqual(p.source, "rules(fallback)")

    def test_no_key_means_rules_and_no_network(self):
        p = OpenAINLU(Tracer(), api_key="", base_url=self.url).parse("book dermatology")
        self.assertEqual(p.source, "rules")
        self.assertEqual(_StubOpenAI.seen, [])


if __name__ == "__main__":
    unittest.main()
