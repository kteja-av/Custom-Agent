"""OpenAI-compatible ModelClient (FR-3, FR-15, LLD 3.8).

ADR-13 was settled by probing the configured endpoint rather than by preference:
it is a LiteLLM proxy speaking the OpenAI wire format, fronting bedrock, azure,
openai and vertex model families behind one schema. Switching provider is
therefore a model-id change (NFR-1) -- honest caveat: that proves model
agnosticism, not wire-format agnosticism. The second wire format is what
Phase 0/1 actually proves, and it lands as a sibling of this module.

This is the ONLY module allowed to know what a provider's JSON looks like.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

from ..config import REDACTED, Secret
from ..errors import (
    AgentSDKError,
    ModelError,
    ModelProviderUnavailable,
    ModelRateLimited,
    ModelTimeout,
)
from ..model import ModelRequest, ModelResponse, StopReason, Usage
from ..primitives import Message, Role, ToolCall

_STOP_REASONS = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_CALLS,
    "function_call": StopReason.TOOL_CALLS,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.CONTENT_FILTER,
}


@dataclass(frozen=True)
class RetryPolicy:
    """LLD 4.5: timeouts and rate limits are transient and safe to retry -- no
    side effect has occurred. Every other ModelError propagates immediately."""

    attempts: int = 3  # one initial try plus two retries
    backoff_seconds: float = 0.5
    multiplier: float = 2.0


class OpenAICompatibleModelClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | Secret,
        model: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
        retry: RetryPolicy | None = None,
        max_tokens: int | None = 1024,
    ) -> None:
        self._base_url = base_url if base_url.endswith("/") else base_url + "/"
        self._api_key = api_key if isinstance(api_key, Secret) else Secret(api_key)
        self._model = model
        self._timeout = timeout
        self._retry = retry or RetryPolicy()
        self._max_tokens = max_tokens
        self._client = client
        self._owns_client = client is None

    # --- lifecycle ----------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> OpenAICompatibleModelClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        # Never render the key (NFR-4).
        return f"OpenAICompatibleModelClient(model={self._model!r}, base_url={self._base_url!r})"

    def _describe_failure(self, exc: BaseException) -> str:
        """Render a caught exception for a ModelError message, unconditionally.

        Every step here can fail on a hostile object: `str(exc)` may raise or
        return a non-string, `type(exc).__name__` may be overridden by a
        metaclass, and `_redact` touches both. Each is attempted separately and
        falls back, and the whole thing sits inside a final guard that returns a
        fixed string. A boundary whose error path can raise is not a boundary.
        """
        try:
            # str(exc) is the realistic failure -- a hostile __str__ raises, or
            # returns a non-string. Guarded here rather than by the outer catch
            # so the exception type is still named in the message.
            try:
                detail = str(exc)
                if not isinstance(detail, str):
                    detail = ""
            except Exception:  # noqa: BLE001
                detail = "<unrenderable>"
            # type(exc).__name__ can also raise (a metaclass may define it as a
            # property), and _redact touches the detail again. Both are covered
            # by the outer catch below rather than by their own handlers: an
            # inner guard there is unreachable dead code, since the outer one
            # already keeps the method total.
            name = type(exc).__name__
            detail = self._redact(detail, 200)
            return (
                f"model adapter failed: {name}: {detail}"
                if detail
                else f"model adapter failed: {name}"
            )
        except Exception:  # noqa: BLE001 - the last line of defence
            return "model adapter failed: unrenderable exception"

    def _redact(self, text: str, limit: int | None = None) -> str:
        """Strip the credential from anything headed for an exception message.

        Provider error bodies are copied into ModelError messages, and those
        messages land in a persisted RunFailed payload (FR-10). A gateway that
        echoes the Authorization header into a 4xx body would otherwise write
        the key straight into the database.

        ORDER MATTERS: redact the whole text, THEN truncate. Truncating first
        cuts a key that straddles the boundary in half, so `replace` no longer
        matches it and a recoverable fragment survives into the payload.

        Limit: literal replacement cannot see a URL-encoded, base64 or
        line-wrapped rendering of the key. Those are out of reach of this
        defence, not silently handled by it.
        """
        key = self._api_key.reveal()
        redacted = text.replace(key, REDACTED) if key else text
        return redacted[:limit] if limit is not None else redacted

    def _error_text(self, response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return self._redact(response.text, 300)
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            return self._redact(str(error.get("message", "")), 300)
        return self._redact(str(error if error is not None else body), 300)

    # --- the interface ------------------------------------------------------

    async def send(self, request: ModelRequest) -> ModelResponse:
        """The total boundary.

        The invariant is that only AgentSDKError subclasses leave this method.
        Three review rounds each found one more exception type escaping
        (JSONDecodeError, then TypeError from an unhashable dict key, then
        OverflowError from int(float('inf'))) because the defence was an
        enumeration of known failures inside _parse. An enumeration cannot be
        complete; the guards below still exist because a specific diagnosis
        beats a generic one, but this wrapper is what makes the invariant TOTAL
        rather than aspirational.

        BaseException deliberately passes through: asyncio.CancelledError,
        KeyboardInterrupt and SystemExit are control flow, not provider faults,
        and swallowing them would break cancellation.

        Cost: a genuine bug in this adapter now surfaces as a ModelError. The
        original type name is kept in the message and the cause is chained, so
        nothing is lost for debugging.
        """
        try:
            return await self._send(request)
        except AgentSDKError:
            raise
        except Exception as exc:
            # Describing the exception must not itself be able to fail: str(exc)
            # runs inside this handler, and if it raises, that secondary
            # exception leaves the handler with nothing behind it to catch. See
            # _describe_failure -- it is guaranteed to return a string.
            raise ModelError(self._describe_failure(exc)) from exc

    async def _send(self, request: ModelRequest) -> ModelResponse:
        payload = self.build_payload(request)
        url = urljoin(self._base_url, "v1/chat/completions")
        delay = self._retry.backoff_seconds

        for attempt in range(1, self._retry.attempts + 1):
            try:
                return self._parse(await self._post(url, payload))
            except (ModelTimeout, ModelRateLimited):
                # Transient only. ModelProviderUnavailable and every other
                # ModelError deliberately fall through and propagate.
                if attempt == self._retry.attempts:
                    raise
                await asyncio.sleep(delay)
                delay *= self._retry.multiplier
        raise AssertionError("unreachable")  # pragma: no cover

    async def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http().post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key.reveal()}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.TimeoutException as exc:
            raise ModelTimeout(self._redact(str(exc))) from exc
        # httpx.StreamError descends from RuntimeError, NOT from HTTPError, so
        # catching HTTPError alone lets it escape this boundary.
        except (httpx.HTTPError, httpx.StreamError) as exc:
            raise ModelProviderUnavailable(self._redact(str(exc))) from exc

        if response.status_code == 429:
            raise ModelRateLimited(self._error_text(response))
        if response.status_code >= 500:
            raise ModelProviderUnavailable(self._error_text(response))
        if response.status_code >= 400:
            # A raw provider exception must never escape this boundary.
            raise ModelError(f"{response.status_code}: {self._error_text(response)}")

        # A 2xx does not guarantee JSON. This deployment sits behind an Envoy
        # layer that can return a non-JSON body, so an unguarded .json() here
        # would leak a JSONDecodeError past the adapter and leave AgentLoop
        # unable to classify it.
        try:
            body = response.json()
        except (ValueError, RecursionError) as exc:
            raise ModelError(
                f"provider returned a non-JSON body ({response.status_code}): "
                f"{self._redact(response.text, 200)!r}"
            ) from exc
        if not isinstance(body, dict):
            raise ModelError(
                f"provider returned {type(body).__name__}, expected a JSON object"
            )
        return body

    # --- translation: canonical -> provider ---------------------------------

    def build_payload(self, request: ModelRequest) -> dict[str, Any]:
        wire: list[dict[str, Any]] = []
        if request.instructions:
            wire.append({"role": "system", "content": request.instructions})

        for message in request.messages:
            if message.role is Role.TOOL:
                # OpenAI wants one tool message per tool_call_id, so a single
                # canonical tool Message fans out into several wire messages.
                for result in message.tool_results:
                    wire.append(
                        {
                            "role": "tool",
                            "tool_call_id": result.tool_call_id,
                            "content": result.content,
                        }
                    )
                continue

            entry: dict[str, Any] = {"role": message.role.value, "content": message.content}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in message.tool_calls
                ]
            wire.append(entry)

        payload: dict[str, Any] = {
            "model": request.model_settings.get("model", self._model),
            "messages": wire,
        }
        if request.tools:
            payload["tools"] = list(request.tools)
        max_tokens = request.model_settings.get("max_tokens", self._max_tokens)
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        for key in ("temperature", "top_p", "stop"):
            if key in request.model_settings:
                payload[key] = request.model_settings[key]
        return payload

    # --- translation: provider -> canonical ---------------------------------

    def _parse(self, body: dict[str, Any]) -> ModelResponse:
        # Every shape assumption below is checked. A provider that returns
        # well-formed JSON of the wrong shape must still come out of this
        # component as a ModelError, never as an AttributeError or TypeError.
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelError("provider returned no choices")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ModelError(f"provider returned a {type(choice).__name__} choice, expected an object")
        raw = choice.get("message")
        if not isinstance(raw, dict):
            raw = {}

        tool_calls = []
        raw_calls = raw.get("tool_calls")
        for item in raw_calls if isinstance(raw_calls, list) else []:
            if not isinstance(item, dict):
                continue
            function = item.get("function")
            function = function if isinstance(function, dict) else {}
            arguments, arguments_error = _decode_arguments(function.get("arguments"))
            tool_calls.append(
                ToolCall(
                    id=str(item.get("id") or ""),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                    arguments_error=arguments_error,
                )
            )

        content = raw.get("content")
        if content is not None and not isinstance(content, str):
            # Message.content is typed str | None; structured content parts get
            # serialised rather than smuggled through as a list.
            content = json.dumps(content, default=str)

        message = Message(
            role=Role.ASSISTANT,
            content=content,
            tool_calls=tuple(tool_calls),
        )
        usage = body.get("usage")
        usage = usage if isinstance(usage, dict) else {}

        # dict.get() requires a HASHABLE key, so looking up an unchecked value
        # in _STOP_REASONS is itself a shape assumption: a list or dict
        # finish_reason raises TypeError: unhashable type, which would escape
        # this boundary as a non-SDK exception.
        finish_reason = choice.get("finish_reason")
        stop_reason = (
            _STOP_REASONS.get(finish_reason, StopReason.OTHER)
            if isinstance(finish_reason, str)
            else StopReason.OTHER
        )

        response_id = body.get("id")
        return ModelResponse(
            message=message,
            stop_reason=stop_reason,
            # Not coerced here: `Usage` coerces every field it is given, so
            # this adapter cannot forget to and no future adapter has to
            # remember. A provider sending a string token count still cannot
            # make the dataclass lie.
            usage=Usage(
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
            ),
            provider_response_id=str(response_id) if response_id is not None else None,
            provider_metadata={
                "model": body.get("model"),
                "finish_reason": finish_reason,
            },
        )


def _decode_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Tool arguments arrive as a JSON *string*.

    Returns (arguments, error). Malformed JSON must not crash the loop, but it
    must not silently become `{}` either: a tool whose schema declares no
    required properties would accept `{}` and execute, turning garbled model
    output into a successful no-argument call. The error travels on the ToolCall
    so ToolExecutor step 2 rejects it explicitly and the model gets a
    correctable error back.
    """
    if isinstance(raw, dict):
        return raw, None
    if raw is None or raw == "":
        return {}, None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, RecursionError) as exc:
        # RecursionError descends from RuntimeError, not ValueError: deeply
        # nested argument JSON must become a correctable tool error, not blow
        # up an otherwise valid response.
        return {}, f"{type(exc).__name__}: {exc}"
    if not isinstance(decoded, dict):
        return {}, f"expected a JSON object, got {type(decoded).__name__}"
    return decoded, None
