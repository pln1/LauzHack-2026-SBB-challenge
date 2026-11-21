"""Gemini connectivity and speed benchmark. Run inside the app container:

    docker compose exec app python scripts/check_gemini.py             # real-size request, all current Flash models
    docker compose exec app python scripts/check_gemini.py --tiny      # only a tiny "are you alive" call
    docker compose exec app python scripts/check_gemini.py --runs 2    # median of 2 runs
    docker compose exec app python scripts/check_gemini.py --models gemini-3.6-flash,gemini-3.5-flash-lite

For every model it sends the SAME kind of request the app sends (system instruction, a realistic prompt of the
same size, the structured-output schema, the configured thinking level) in three output modes:
  schema      Pydantic response_schema   (app default)
  jsonschema  raw JSON schema
  json        JSON mime type only, schema described in the prompt
and STREAMS the answer, so you see the time to the first token and the total time. Then it prints the fastest
combination as ready-to-paste .env lines.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.gemini_service import VARIANTS, AssistantDecisionSchema, build_prompt, get_gemini_service  # noqa: E402

SKIP_WORDS = ("image", "tts", "audio", "live", "embed", "vision", "robotics", "computer-use", "imagen", "veo", "learnlm", "gemma")

# A realistic, worst-case-sized context (about 6 KB of JSON, like the app sends).
_TXT = ("Unterbruch zwischen Cossonay-Penthalaz und Chavornay wegen Brand im Bahnhof. Reisende zwischen Genève-Aéroport, Genève, "
        "Morges, Lausanne und Biel/Bienne, Solothurn reisen via Bern. Ersatzbusse zwischen Cossonay-Penthalaz und Chavornay im Einsatz. ")
SAMPLE_CONTEXT = {
    "incident": {"title": "Cossonay-Penthalaz – Chavornay", "status": "ACTIVE", "reason": "Fire at the station",
                 "affected_lines": ["IC5", "IC51", "R1", "R2"], "affected_stations": ["Cossonay-Penthalaz", "Chavornay"],
                 "duration_type": "UNTIL_AT_LEAST", "forecast": "At least until 22.11. 00:10 (extension possible)",
                 "forecast_warning": True, "has_replacement_bus": True, "recommended_action": _TXT[:300],
                 "detour_summary": _TXT[:300], "version": 2},
    "updates": [{"version": i, "time": f"18:{10 + i}", "text": _TXT[:220], "source": "PUBLIC"} for i in range(1, 4)],
    "operational_log": [{"time": f"18:{20 + i}", "source": "KULT", "text": _TXT[:220]} for i in range(5)],
    "messages": [{"time": "18:05", "title": "Disruption Lausanne - Yverdon", "body": _TXT[:260]} for _ in range(2)],
    "trip_context": {"destination": "Chavornay", "ticket_type": "SUPERSAVER"},
    "chat": [{"sender": "USER", "content": "I have a Supersaver ticket to Chavornay, what should I do?"}] * 2,
    "deterministic": {"scenario": "detour_route", "title": "Detour route & replacement transport",
                      "steps": [_TXT[:200]] * 3, "policy_facts": ["Supersaver: the train binding is lifted during an official disruption."],
                      "forecast_warning": True},
}
SAMPLE_MESSAGE = "I have a Supersaver ticket to Chavornay, what should I do?"


def pick_models(results: dict[tuple[str, str], dict]) -> tuple[str | None, str | None, list[str]]:
    """Fastest OK (model, output mode) pair = main. Fallbacks = next fastest OK models (other names, max 2)."""
    ok = sorted((k for k, r in results.items() if r["ok"]), key=lambda k: results[k]["total"])
    if not ok:
        return None, None, []
    main, mode = ok[0]
    fbs: list[str] = []
    for m, _ in ok:
        if m != main and m not in fbs:
            fbs.append(m)
    return main, mode, fbs[:2]


async def one_call(client, svc, model: str, variant: str, tiny: bool, timeout: float) -> dict:
    cfg = svc._config(model, "en", variant)
    if tiny:
        contents = "Passenger: my train is cancelled. Give a 2-step plan."
    else:
        contents = build_prompt(SAMPLE_CONTEXT, SAMPLE_MESSAGE, None, variant == "json")
    t0 = time.monotonic()
    first = None
    text = ""
    try:
        async def run():
            nonlocal first, text
            stream = await client.aio.models.generate_content_stream(model=model, contents=contents, config=cfg)
            async for chunk in stream:
                piece = getattr(chunk, "text", None)
                if piece:
                    if first is None:
                        first = time.monotonic() - t0
                    text += piece
        await asyncio.wait_for(run(), timeout=timeout)
        AssistantDecisionSchema.model_validate_json(text)  # must be valid, schema-conforming JSON
        return {"ok": True, "first": first or 0.0, "total": time.monotonic() - t0, "error": ""}
    except Exception as exc:  # noqa: BLE001
        kind = "Timeout" if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) else type(exc).__name__
        return {"ok": False, "first": first or 0.0, "total": time.monotonic() - t0,
                "error": f"{kind}: {(str(exc) or repr(exc)).replace(chr(10), ' ')[:110]}"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--models", default="", help="comma separated; default: every current Flash model the key lists")
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds to wait per call in this benchmark")
    ap.add_argument("--tiny", action="store_true", help="tiny prompt instead of the realistic one")
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--include-old", action="store_true", help="also test Gemini 1.x / 2.x (retired for new keys)")
    args = ap.parse_args()

    svc = get_gemini_service()
    s = svc.settings
    key = s.gemini_api_key or ""
    print(f"configured: model={s.gemini_model} fallback={s.gemini_fallback_model or '-'} mode={s.gemini_mode} output={s.gemini_output_mode} "
          f"thinking={s.gemini_thinking_level} max_tokens={s.gemini_max_output_tokens} key={'set (' + key[:4] + '…)' if key else 'MISSING'}")
    if not key:
        return 1
    from google import genai
    from google.genai import types

    # Benchmark client: generous server deadline so a slow model shows its real time instead of a 504.
    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(args.timeout * 1000),
                                                                       retry_options=types.HttpRetryOptions(attempts=1)))
    names: list[str] = []
    if args.models:
        names = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        try:
            async for m in await client.aio.models.list():
                n = (m.name or "").removeprefix("models/")
                if "generateContent" in (getattr(m, "supported_actions", None) or []) and "flash" in n \
                        and not any(w in n for w in SKIP_WORDS) and (args.include_old or not n.startswith(("gemini-1", "gemini-2"))):
                    names.append(n)
        except Exception as exc:  # noqa: BLE001
            print(f"could not list models ({type(exc).__name__}: {str(exc)[:200]}); testing the configured ones")
        names = sorted(set(names)) or [x.strip() for x in [s.gemini_model, *(s.gemini_fallback_model or "").split(",")] if x.strip()]
    variants = [v for v in args.variants.split(",") if v in VARIANTS]
    print(f"{'tiny' if args.tiny else 'real-size'} request, {len(names)} model(s) x {len(variants)} output mode(s) x {args.runs} run(s), streaming\n")

    sem = asyncio.Semaphore(6)

    async def guarded(model, variant):
        async with sem:
            return await one_call(client, svc, model, variant, args.tiny, args.timeout)

    combos = [(m, v) for m in names for v in variants]
    all_runs = [await asyncio.gather(*(guarded(m, v) for m, v in combos)) for _ in range(args.runs)]
    results: dict[tuple[str, str], dict] = {}
    for i, key_ in enumerate(combos):
        rs = [r[i] for r in all_runs]
        good = [x for x in rs if x["ok"]]
        results[key_] = {"ok": bool(good), "total": statistics.median(x["total"] for x in good) if good else 1e9,
                         "first": statistics.median(x["first"] for x in good) if good else 0.0,
                         "error": next((x["error"] for x in rs if not x["ok"]), "")}
    print(f"  {'':5} {'model':<30} {'output':<11} {'first token':>11} {'total':>7}")
    for k in sorted(combos, key=lambda k: results[k]["total"]):
        r = results[k]
        if r["ok"]:
            print(f"  OK    {k[0]:<30} {k[1]:<11} {r['first']:>10.1f}s {r['total']:>6.1f}s")
        else:
            print(f"  FAIL  {k[0]:<30} {k[1]:<11} {r['error']}")

    main_m, mode, fbs = pick_models(results)
    print()
    if not main_m:
        print("No model answered. Check the key, the network, and the errors above (503 = overloaded, try again in a minute).")
        return 2
    print("Fastest combination. Paste into .env, then: docker compose up -d --force-recreate app")
    print(f"GEMINI_MODEL={main_m}")
    print(f"GEMINI_FALLBACK_MODEL={','.join(fbs)}" if fbs else "# no second working model found")
    print(f"GEMINI_OUTPUT_MODE={mode}")
    t = results[(main_m, mode)]["total"]
    print(f"\nExpected answer time: about {t:.0f}s. The app answers instantly from the SBB rules (GEMINI_MODE=async) and swaps in "
          f"the Gemini wording when it arrives.")
    if t > 20:
        print("Warning: even the fastest model needs more than 20 s right now: Google is probably overloaded; try again in a few minutes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
