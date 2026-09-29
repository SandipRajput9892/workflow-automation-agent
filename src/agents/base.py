"""Thin wrapper around the Anthropic SDK shared by all agents.

One primitive covers everything the agents need: structured(), a single call
whose response is parsed into a Pydantic model (supervisor, planner, executor
input repair, reflection). Tools themselves are invoked directly by code.

generate_validated() layers semantic validation on top: it re-asks Claude with
a corrective prompt until the output passes the caller's checks.

Agents depend on this interface, not on the SDK, so tests can pass a fake.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Protocol, TypeVar

import anthropic
import httpx
from pydantic import BaseModel, ValidationError

from src.config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")

# fallbacks="default" routes a refused request to a suitable fallback model
# server-side, chosen by refusal category.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


class StructuredOutputError(LLMError):
    """Claude's response could not be parsed/validated into the requested schema.

    Unlike a refusal, this is usually fixable by asking again with the
    validation errors, so callers may retry with a corrective prompt."""


class ValidationFailed(LLMError):
    """Claude's output still failed validation after every allowed attempt."""

    def __init__(self, message: str, problems: list[str]):
        super().__init__(message)
        self.problems = problems


class LLM(Protocol):
    def structured(self, system: str, prompt: str, schema: type[T]) -> T: ...


def generate_validated(
    llm: LLM,
    system: str,
    prompt: str,
    schema: type[T],
    validate: Callable[[T], tuple[R | None, list[str]]],
    max_attempts: int,
    what: str,
) -> R:
    """Ask for `schema`, then check it with `validate`, which returns
    (value, problems). On unparseable output or any problems, re-ask with a
    corrective prompt listing them, up to `max_attempts` calls in total.

    Raises ValidationFailed if no attempt passes. Other LLMErrors (e.g. a
    refusal) propagate immediately, since asking again won't help."""
    current = prompt
    problems: list[str] = []
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            output = llm.structured(system, current, schema)
        except StructuredOutputError as exc:
            previous = None  # the SDK doesn't hand back the unparseable text
            problems = [str(exc)]
        else:
            value, problems = validate(output)
            if not problems:
                if attempt > 1:
                    logger.info("%s valid after %d attempts", what, attempt)
                return value  # type: ignore[return-value]
            previous = output.model_dump_json(indent=2)

        logger.warning("%s attempt %d/%d rejected: %s", what, attempt, max_attempts, "; ".join(problems))
        current = corrective_prompt(prompt, previous, problems, what)

    raise ValidationFailed(f"No valid {what} after {max_attempts} attempts: {'; '.join(problems)}", problems)


def corrective_prompt(base_prompt: str, previous: str | None, problems: list[str], what: str) -> str:
    # Appended after the original prompt (not replacing it) so Claude keeps full
    # context and the prompt prefix stays cacheable across attempts.
    parts = [base_prompt]
    if previous:
        parts.append(f"<previous_attempt>\n{previous}\n</previous_attempt>")
    parts.append(
        f"Your previous {what} was rejected because of these problems:\n"
        + "\n".join(f"- {p}" for p in problems)
        + f"\n\nReturn a corrected, complete {what} that fixes every problem listed. "
        "Keep the parts that were already correct."
    )
    return "\n\n".join(parts)


class ClaudeLLM:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None):
        self.settings = settings
        self.client = client or anthropic.Anthropic(max_retries=3)

    def _extra(self) -> dict[str, Any]:
        if not self.settings.enable_refusal_fallback:
            return {}
        return {"betas": [FALLBACK_BETA], "fallbacks": "default"}

    def _messages(self):
        return self.client.beta.messages if self.settings.enable_refusal_fallback else self.client.messages

    @staticmethod
    def _check_stop(response: Any) -> None:
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMError(f"Claude declined the request ({getattr(details, 'category', None)})")
        if response.stop_reason == "max_tokens":
            raise LLMError("Response hit max_tokens before completing")

    def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        try:
            response = self._messages().parse(
                model=self.settings.claude_model,
                max_tokens=self.settings.claude_max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                **self._extra(),
            )
        except ValidationError as exc:
            # The SDK validates the JSON text against the schema inside parse();
            # malformed or truncated JSON surfaces here.
            errors = "; ".join(f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in exc.errors())
            raise StructuredOutputError(f"Response is not a valid {schema.__name__}: {errors}") from exc
        self._check_stop(response)
        if response.parsed_output is None:
            raise StructuredOutputError(f"Response contained no {schema.__name__} JSON")
        return response.parsed_output


class GroqLLM:
    """Groq via its OpenAI-compatible chat completions API, in JSON mode.

    JSON mode only guarantees syntactically valid JSON, so the schema goes in
    the system prompt and the reply is validated here with Pydantic."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.groq_api_key:
            raise LLMError("LLM_PROVIDER=groq but GROQ_API_KEY is not set (add it to .env)")
        self.settings = settings
        self.client = client or httpx.Client(
            base_url=settings.groq_base_url,
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            timeout=120,
        )

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(4):
            try:
                response = self.client.post("/chat/completions", json=payload)
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise LLMError(f"Groq request failed: {exc}") from exc
                time.sleep(2**attempt)
                continue
            if response.status_code in (429, 500, 502, 503) and attempt < 3:
                # Rate limits are common on Groq's free tier; honour retry-after.
                delay = float(response.headers.get("retry-after") or 2**attempt)
                logger.warning("Groq returned %d, retrying in %.0fs", response.status_code, delay)
                time.sleep(min(delay, 30))
                continue
            if response.status_code >= 400:
                raise LLMError(f"Groq API error {response.status_code}: {response.text[:500]}")
            return response.json()
        raise LLMError("Groq request failed after retries")

    def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        json_schema = json.dumps(schema.model_json_schema())
        system_with_schema = (
            f"{system}\n\nRespond with a single JSON object (no prose, no markdown) that is a valid "
            f"instance of this JSON Schema:\n{json_schema}"
        )
        data = self._post(
            {
                "model": self.settings.groq_model,
                "max_tokens": self.settings.groq_max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_with_schema},
                    {"role": "user", "content": prompt},
                ],
            }
        )
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise LLMError("Response hit max_tokens before completing")
        content = choice["message"].get("content") or ""
        try:
            return schema.model_validate_json(content)
        except ValidationError as exc:
            errors = "; ".join(f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in exc.errors())
            raise StructuredOutputError(f"Response is not a valid {schema.__name__}: {errors}") from exc


def build_llm(settings: Settings) -> LLM:
    """The LLM client selected by LLM_PROVIDER."""
    if settings.llm_provider == "groq":
        return GroqLLM(settings)
    return ClaudeLLM(settings)
