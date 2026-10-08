"""Interactive CLI:  python3 -m assistant"""
from __future__ import annotations

import argparse
import os
import sys

from agent import Assistant
from api import SchedulingClient
from nlu import OpenAINLU, RuleNLU
from tracing import Tracer

BANNER = ("Scheduling assistant (prototype, synthetic data). I can find providers, book appointments and look "
          "up your appointments. I can't give medical advice.\nType 'quit' to exit.\n")


def build(args) -> Assistant:
    tracer = Tracer(path=None if args.no_trace else args.trace_file, echo=args.debug)
    if args.trace_file and not args.no_trace:
        os.makedirs(os.path.dirname(os.path.abspath(args.trace_file)), exist_ok=True)
    api = SchedulingClient(args.api_url, tracer, mock_scenario=args.mock_scenario,
                           outage_affects_handoffs=args.outage_affects_handoffs)
    nlu = RuleNLU() if args.nlu == "rules" else OpenAINLU(tracer)
    if args.nlu == "llm" and not nlu.available:
        sys.exit("--nlu llm requires OPENAI_API_KEY to be set in the environment.")
    return Assistant(api, nlu, tracer)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="cli.py", description="Text-based appointment scheduling assistant.")
    ap.add_argument("--api-url", default=os.environ.get("SCHEDULING_API_URL", "http://localhost:4010"))
    ap.add_argument("--nlu", choices=["auto", "llm", "rules"], default="auto",
                    help="auto: OpenAI if OPENAI_API_KEY is set, else rules (default)")
    ap.add_argument("--mock-scenario", help="send X-Mock-Scenario header (e.g. api_failure) to the mock API")
    ap.add_argument("--outage-affects-handoffs", action="store_true",
                    help="also send the mock-scenario header on /handoffs (tests handoff-system-down path)")
    ap.add_argument("--trace-file", default="logs/trace.jsonl")
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--debug", action="store_true", help="echo trace events to stderr (useful for demos)")
    args = ap.parse_args(argv)

    bot = build(args)
    mode = "OpenAI + rules fallback" if isinstance(bot.nlu, OpenAINLU) and bot.nlu.available else "rules (offline)"
    print(BANNER + "[NLU: {}]\n".format(mode))
    interactive = sys.stdin.isatty()
    while True:
        try:
            line = input("you> " if interactive else "")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not interactive:
            print("you> " + line)
        if line.strip().lower() in {"quit", "exit", "/quit"}:
            break
        if not line.strip():
            continue
        print("bot> " + bot.respond(line) + "\n")


if __name__ == "__main__":
    main()
