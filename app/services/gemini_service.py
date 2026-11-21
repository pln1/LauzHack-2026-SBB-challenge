"""Google Gemini (google-genai) wrapper with Structured Outputs and a resilient fallback.

If GEMINI_API_KEY is missing, the quota is exhausted, the call times out or the response does
not validate, ``generate_decision`` returns ``None`` and the caller uses the deterministic
strategy output. After a quota / auth error the service backs off for GEMINI_COOLDOWN_SECONDS
so a live demo does not wait on a failing API for every click.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import get_settings

log = logging.getLogger("gemini")

LANG_NAMES = {"en": "English", "de": "German (Swiss standard German, use 'ss' not 'ß')", "fr": "French", "it": "Italian"}


class TripContextPatch(BaseModel):
    origin: str | None = Field(default=None, description="Origin station if the passenger stated it, else null")
    destination: str | None = Field(default=None, description="Destination station if stated, else null")
    ticket_type: Literal["SUPERSAVER", "POINT_TO_POINT", "GA", "HALF_FARE", "DAY_PASS", "OTHER"] | None = None
    urgency: Literal["LOW", "MEDIUM", "HIGH"] | None = None
    expected_delay_minutes: int | None = Field(default=None, description="Arrival delay in minutes if the passenger stated it")


class AssistantDecisionSchema(BaseModel):
    intent: Literal["missed_connection", "detour_route", "refund_compensation", "status_timeline", "general"]
    decision_title: str = Field(description="Short title of the action plan, max 70 characters")
    steps: list[str] = Field(description="2 to 5 numbered, concrete, actionable steps for the passenger")
    summary_text: str = Field(description="One or two empathetic sentences summarising the recommendation")
    trip_context_patch: TripContextPatch


SYSTEM_INSTRUCTION = """You are the SBB Disruption Companion, a calm and empathetic assistant for rail passengers in
Switzerland during a disruption. You turn operational data into the passenger's best next move.

Hard rules:
1. Answer ONLY in {language}. Station names stay as given.
2. Use only facts from INCIDENT, UPDATES, OPERATIONAL_LOG and DETERMINISTIC_PLAN. Never invent train
   departure times, platforms, train numbers or durations. If exact times are needed, tell the passenger
   to use the SBB timetable link shown below your answer.
3. SBB policy facts in DETERMINISTIC_PLAN are authoritative: keep refund percentages, ticket-validity
   rules and forecast warnings exactly as stated there.
4. If the forecast is uncertain (forecast_warning = true), say clearly that the end time may move and
   advise against waiting at an intermediate station without a backup plan.
5. Operational logs are internal staff notes: use them only to explain practical consequences
   (e.g. where replacement buses stop, which lines are rerouted). Never mention staff, names, phone
   numbers or internal systems.
6. Be brief: 2 to 4 steps, each a short imperative sentence (max 25 words); summary_text max 30 words. You may use **bold** for key words.
7. trip_context_patch: fill only fields the passenger explicitly mentioned in PASSENGER_MESSAGE; else null.
8. intent: the scenario that best fits the passenger's message (or the requested scenario).
"""

VARIANTS = ("schema", "jsonschema", "json")  # Pydantic response_schema | raw JSON schema | JSON mime type + schema in the prompt
JSON_HINT = ('Reply with ONE JSON object only: {"intent": "missed_connection|detour_route|refund_compensation|status_timeline|general", '
             '"decision_title": str, "steps": [str, ...], "summary_text": str, "trip_context_patch": {"origin": str|null, '
             '"destination": str|null, "ticket_type": "SUPERSAVER|POINT_TO_POINT|GA|HALF_FARE|DAY_PASS|OTHER|null", '
             '"urgency": "LOW|MEDIUM|HIGH|null", "expected_delay_minutes": int|null}}')


def build_prompt(context: dict, passenger_message: str | None, scenario: str | None, json_hint: bool = False) -> str:
    dump = lambda x: json.dumps(x, ensure_ascii=False, default=str)  # noqa: E731
    parts = [
        f"REQUESTED_SCENARIO: {scenario or 'infer from message'}",
        "INCIDENT:\n" + dump(context.get("incident", {})),
        "UPDATES (oldest first):\n" + dump(context.get("updates", [])),
        "OPERATIONAL_LOG (sanitized, newest first):\n" + dump(context.get("operational_log", [])),
        "PUBLIC_MESSAGES:\n" + dump(context.get("messages", [])),
        "TRIP_CONTEXT:\n" + dump(context.get("trip_context", {})),
        "RECENT_CHAT:\n" + dump(context.get("chat", [])),
        "DETERMINISTIC_PLAN (authoritative policy facts):\n" + dump(context.get("deterministic", {})),
        f"PASSENGER_MESSAGE: {passenger_message or '(scenario button pressed, no free text)'}",
    ]
    if json_hint:
        parts.append(JSON_HINT)
    return "\n\n".join(parts)


class GeminiService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._client = None
        self._disabled_until = 0.0
        self.last_error: str | None = None
        self.last_success_at: float | None = None
        # The model actually used. Starts from GEMINI_MODEL; if Google answers 404 "use models/<x>", we switch to <x>.
        self.model: str = (self.settings.gemini_model or "gemini-3.8-flash").strip()
        self._model_bad_until: dict[str, float] = {}  # model -> monotonic time until which we skip it
        self.last_model_used: str | None = None

    @property
    def attempt_timeout(self) -> float:
        # The SDK sends this as a server deadline and the Gemini API rejects anything under 10 s (400 INVALID_ARGUMENT).
        return max(10.0, float(self.settings.gemini_attempt_timeout_seconds))

    @property
    def configured(self) -> bool:
        return bool(self.settings.gemini_api_key)

    @property
    def available(self) -> bool:
        return self.configured and time.monotonic() >= self._disabled_until

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "model": self.model,
            "fallbackModel": self.settings.gemini_fallback_model or None,
            "lastModelUsed": self.last_model_used,
            "timeoutSeconds": self.settings.gemini_timeout_seconds,
            "attemptTimeoutSeconds": self.attempt_timeout,
            "thinkingLevel": self.settings.gemini_thinking_level,
            "available": self.available,
            "cooldownSeconds": max(0, round(self._disabled_until - time.monotonic())),
            "lastError": self.last_error,
        }

    def _get_client(self):
        if self._client is None:
            from google import genai  # imported lazily so the app starts without the SDK configured

            from google.genai import types

            # HttpOptions.timeout is in MILLISECONDS. SDK retries are off: we retry and switch models ourselves.
            http = types.HttpOptions(timeout=int(self.attempt_timeout * 1000),
                                     retry_options=types.HttpRetryOptions(attempts=1))
            self._client = genai.Client(api_key=self.settings.gemini_api_key, http_options=http)
        return self._client

    def _config(self, model_name: str, lang: str, variant: str | None = None, thinking: bool = True):
        """Build the request config. Gemini 3 wants its default temperature and a thinking *level*;
        Gemini 2.x uses a thinking *budget* (0 = off for flash)."""
        from google.genai import types

        model = model_name.lower()
        kwargs: dict = {
            "system_instruction": SYSTEM_INSTRUCTION.format(language=LANG_NAMES.get(lang, "English")),
            "response_mime_type": "application/json",
            "max_output_tokens": self.settings.gemini_max_output_tokens,
        }
        variant = variant or self.settings.gemini_output_mode
        if variant == "jsonschema":
            kwargs["response_json_schema"] = AssistantDecisionSchema.model_json_schema()
        elif variant != "json":
            kwargs["response_schema"] = AssistantDecisionSchema
        if model.startswith("gemini-2"):
            kwargs["temperature"] = 0.3
            if thinking:
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0 if "flash" in model else 512)
        else:
            level = (self.settings.gemini_thinking_level or "").strip().upper()
            if level and thinking:
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=level)
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _parse(resp) -> AssistantDecisionSchema:
        parsed = getattr(resp, "parsed", None)
        if isinstance(parsed, AssistantDecisionSchema):
            return parsed
        if parsed is not None:
            return AssistantDecisionSchema.model_validate(parsed)
        text = resp.text or ""
        if not text.strip():
            cand = (resp.candidates or [None])[0]
            reason = getattr(cand, "finish_reason", None)
            usage = getattr(resp, "usage_metadata", None)
            raise ValueError(f"empty response (finish_reason={reason}, usage={usage})")
        return AssistantDecisionSchema.model_validate_json(text)

    async def generate_decision(self, *, lang: str, context: dict, passenger_message: str | None,
                                scenario: str | None) -> AssistantDecisionSchema | None:
        if not self.available:
            return None
        st = self.settings
        started = time.monotonic()
        deadline = started + st.gemini_timeout_seconds  # one overall budget for retries and the fallback model
        candidates = [self.model]
        for fb in (st.gemini_fallback_model or "").split(","):
            fb = fb.strip()
            if fb and fb not in candidates:
                candidates.append(fb)
        now = time.monotonic()
        ordered = [m for m in candidates if self._model_bad_until.get(m, 0) <= now] or candidates[:1]
        for model in ordered:
            variant = st.gemini_output_mode if st.gemini_output_mode in VARIANTS else "schema"
            thinking = True
            attempt = 0
            while attempt <= st.gemini_retries:
                remaining = deadline - time.monotonic()
                if remaining < 4:  # not enough time left for a useful call
                    break
                try:
                    resp = await asyncio.wait_for(
                        self._get_client().aio.models.generate_content(
                            model=model, contents=build_prompt(context, passenger_message, scenario, variant == "json"),
                            config=self._config(model, lang, variant, thinking)),
                        timeout=min(remaining, self.attempt_timeout + 1),
                    )
                    parsed = self._parse(resp)
                    self.last_error, self.last_success_at, self.last_model_used = None, time.time(), model
                    self._model_bad_until.pop(model, None)
                    log.info("gemini ok", extra={"model": model, "attempt": attempt + 1,
                                                 "latency_ms": int((time.monotonic() - started) * 1000),
                                                 "intent": parsed.intent, "output": variant})
                    return parsed
                except Exception as exc:  # noqa: BLE001 - any SDK / network / validation error
                    kind = "TimeoutError" if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) else type(exc).__name__
                    msg = str(exc) or repr(exc)
                    self.last_error = f"{model}: {kind}: {msg[:300]}"
                    info = {"model": model, "attempt": attempt + 1, "error_type": kind, "error": msg[:400],
                            "elapsed_s": round(time.monotonic() - started, 1)}
                    if any(k in msg for k in ("401", "403", "PERMISSION_DENIED", "API key", "API_KEY")):
                        log.warning("gemini auth error; using deterministic fallback", extra=info)
                        self._disabled_until = time.monotonic() + st.gemini_cooldown_seconds
                        return None
                    if "404" in msg or "NOT_FOUND" in msg:  # model retired / not available for this key: next model
                        log.warning("gemini model not available; trying next model", extra=info)
                        self._model_bad_until[model] = time.monotonic() + 600
                        break
                    if thinking and "400" in msg and "think" in msg.lower():
                        thinking = False  # this model does not accept our thinking setting: retry with its default
                        log.warning("gemini rejected thinking config; retrying without it", extra=info)
                        continue
                    if variant != "json" and "400" in msg and "schema" in msg.lower():
                        variant = "jsonschema" if variant == "schema" else "json"
                        log.warning("gemini rejected the response schema; retrying with a simpler output mode",
                                    extra={**info, "next_output": variant})
                        continue
                    # Timeouts are NOT retried on the same model: the next model gets the remaining time instead.
                    timed_out = kind in ("TimeoutError", "ReadTimeout", "ConnectTimeout") or "timed out" in msg.lower()
                    transient = not timed_out and (any(k in msg for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                                                                          "500", "INTERNAL", "502", "504", "DEADLINE"))
                                                   or kind in ("ConnectError", "ValueError"))
                    attempt += 1
                    if transient and attempt <= st.gemini_retries:
                        delay = st.gemini_retry_backoff_seconds * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
                        if time.monotonic() + delay < deadline - 2:
                            log.warning("gemini transient error; retrying", extra={**info, "retry_in_s": round(delay, 1)})
                            await asyncio.sleep(delay)
                            continue
                    log.warning("gemini model failed; trying next model" if model != ordered[-1] else
                                "gemini call failed; using deterministic fallback", extra=info)
                    # skip an overloaded / over-quota model for a short while so the next click goes straight to the fallback
                    self._model_bad_until[model] = time.monotonic() + (60 if "429" in msg or "RESOURCE_EXHAUSTED" in msg else 30)
                    break
        if len(ordered) == len(candidates) and all(self._model_bad_until.get(m, 0) > time.monotonic() for m in candidates):
            self._disabled_until = time.monotonic() + min(st.gemini_cooldown_seconds, 15)  # every model is struggling
        return None


_service: GeminiService | None = None


def get_gemini_service() -> GeminiService:
    global _service
    if _service is None:
        _service = GeminiService()
    return _service
