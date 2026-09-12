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
from ..model import ModelRequest, ModelResponse, StopReason, Usage, token_count
from ..primitives import Message, Role, ToolCall

_STOP_REASONS = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_CALLS,
    "function_call": StopReason.TOOL_CALLS,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.CONTENT_FILTER,
}

# An error body is read only for the detail it adds to a message, so it is read
# within bounds: a body that never ends, or trickles in just under the read
# timeout, cannot hold up the classification or the retry that follows (FR-33).
_ERROR_BODY_LIMIT = 64_000


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
        error_body_timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url if base_url.endswith("/") else base_url + "/"
        self._api_key = api_key if isinstance(api_key, Secret) else Secret(api_key)
        self._model = model
        self._timeout = timeout
        self._retry = retry or RetryPolicy()
        self._max_tokens = max_tokens
        # The whole time an error body may take to read, however it arrives.
        self._error_body_timeout = error_body_timeout
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

    @property
    def default_model_id(self) -> str:
        """The model this client sends when a request names none (FR-32).

        Read by the Runner, so a run on the client's default records the model
        it actually used rather than NULL (KNOWLEDGE-862c2e9e).
        """
        return self._model

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

    def _scrub(self, value: Any) -> Any:
        """A copy of decoded JSON with the credential removed from every string,
        dictionary keys included (FR-33, NFR-4).

        Error bodies were always redacted; success bodies were not, on the
        reasoning that providers echo request context in errors and not in
        answers (ASSUMPTION-75110765). NFR-4 is absolute, so the asymmetry goes.
        This works on DECODED values, so a key the wire JSON-escaped is found
        too, which literal matching on the raw text would miss. It walks with an
        explicit stack, because a body may legally nest as deep as the decoder
        allows. The limit `_redact` states still applies: a base64, URL-encoded
        or line-wrapped rendering is not found.
        """
        key = self._api_key.reveal()
        if not key:
            return value
        holder = [value]
        stack: list[tuple[Any, Any]] = [(holder, 0)]
        while stack:
            container, slot = stack.pop()
            item = container[slot]
            if isinstance(item, str):
                container[slot] = item.replace(key, REDACTED)
            elif isinstance(item, dict):
                copied = {
                    (k.replace(key, REDACTED) if isinstance(k, str) else k): v
                    for k, v in item.items()
                }
                container[slot] = copied
                stack.extend((copied, k) for k in copied)
            elif isinstance(item, list):
                copied_list = list(item)
                container[slot] = copied_list
                stack.extend((copied_list, index) for index in range(len(copied_list)))
        return holder[0]

    async def _read_error_body(self, response: httpx.Response) -> str | None:
        """The body of an error response as text, or None when it cannot be read.

        An error's class is decided by its status line; the body only adds
        detail to the message. So ANY failure to read it -- a body that cannot
        be decoded as declared, a lying Content-Length, a truncated chunked
        body, a reset, a read timeout, an exception from a custom transport --
        leaves the status to speak for itself (FR-33). M9 round 1 guarded
        DecodingError alone and was rejected for it: the instance, not the
        class. Bounded in size and in total time, so a body that never ends, or
        trickles in just under the read timeout, cannot stall the retry.
        """
        chunks: list[bytes] = []

        async def read() -> None:
            size = 0
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= _ERROR_BODY_LIMIT:
                    break

        try:
            await asyncio.wait_for(read(), timeout=self._error_body_timeout)
            return b"".join(chunks)[:_ERROR_BODY_LIMIT].decode(
                response.encoding or "utf-8", errors="replace"
            )
        except Exception:  # noqa: BLE001 - see the docstring: the class, not a list
            return None
        finally:
            try:
                await response.aclose()
            except Exception:  # noqa: BLE001 - closing cannot change the status either
                pass

    def _error_text(self, status: int, body: str | None) -> str:
        """The message for an error response, redacted and bounded -- and TOTAL.

        This sits between an error status and the exception that status means,
        so it must never raise: before M9 a body nested past the recursion limit
        raised here, and a rate limit escaped as a generic ModelError that was
        never retried (ASSUMPTION-75110765).
        """
        if body is None:
            return f"HTTP {status}: the error body could not be read"
        try:
            try:
                parsed = json.loads(body)
            except Exception:  # noqa: BLE001 - not JSON, or nested too deep
                return self._redact(body, 300)
            error = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(error, dict):
                return self._redact(str(error.get("message", "")), 300)
            return self._redact(str(error if error is not None else parsed), 300)
        except Exception:  # noqa: BLE001 - the classification must survive the body
            return f"HTTP {status}: the error body could not be read"

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
        http = self._http()
        try:
            # Streamed, so the status is in hand before any body is read. A
            # plain post() reads the body inside the request, and a failure
            # there -- a body that cannot be decoded, a connection cut short --
            # was raised before anything looked at the status (FR-33).
            response = await http.send(
                http.build_request(
                    "POST",
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._api_key.reveal()}",
                        "Content-Type": "application/json",
                    },
                ),
                stream=True,
            )
        except httpx.TimeoutException as exc:
            raise ModelTimeout(self._redact(str(exc))) from exc
        # httpx.StreamError descends from RuntimeError, NOT from HTTPError, so
        # catching HTTPError alone lets it escape this boundary.
        except (httpx.HTTPError, httpx.StreamError) as exc:
            raise ModelProviderUnavailable(self._redact(str(exc))) from exc

        status = response.status_code
        if status >= 400:
            # The status line decides the class, and nothing about the body can
            # change it: the body is read for its message, within bounds, and a
            # body that cannot be read leaves the status to speak for itself.
            detail = self._error_text(status, await self._read_error_body(response))
            if status == 429:
                raise ModelRateLimited(detail)
            if status >= 500:
                raise ModelProviderUnavailable(detail)
            # A raw provider exception must never escape this boundary.
            raise ModelError(f"{status}: {detail}")

        body_readable = True
        try:
            await response.aread()
        except httpx.DecodingError:
            # A success whose body cannot be decoded as its headers declare.
            body_readable = False
        except httpx.TimeoutException as exc:
            raise ModelTimeout(self._redact(str(exc))) from exc
        except (httpx.HTTPError, httpx.StreamError) as exc:
            raise ModelProviderUnavailable(self._redact(str(exc))) from exc
        finally:
            await response.aclose()

        if not body_readable:
            # A malformed response, the same class as a non-JSON one below --
            # not an unavailable provider.
            raise ModelError(
                f"provider returned a {status} body that could not be "
                "decoded as its headers declare"
            )

        # A 2xx does not guarantee JSON. This deployment sits behind an Envoy
        # layer that can return a non-JSON body, so an unguarded .json() here
        # would leak a JSONDecodeError past the adapter and leave AgentLoop
        # unable to classify it.
        try:
            body = response.json()
        except (ValueError, RecursionError) as exc:
            raise ModelError(
                f"provider returned a non-JSON body ({status}): "
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
        # FR-28: only when a run set it. A model that rejects the parameter is
        # unaffected by runs that never asked for it.
        if request.model_settings.get("reasoning_effort") is not None:
            payload["reasoning_effort"] = request.model_settings["reasoning_effort"]
        for key in ("temperature", "top_p", "stop"):
            if key in request.model_settings:
                payload[key] = request.model_settings[key]
        return payload

    # --- translation: provider -> canonical ---------------------------------

    def _parse(self, body: dict[str, Any]) -> ModelResponse:
        # The credential leaves the body before anything is read from it, so no
        # field this parser reads -- or a later one adds -- can carry it into
        # model context, a row or an event (FR-33).
        body = self._scrub(body)

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
            # Arguments arrive as a JSON string INSIDE the body, so a key
            # escaped at that inner level only appears once they are decoded.
            arguments = self._scrub(arguments)
            if arguments_error is not None:
                arguments_error = self._redact(arguments_error)
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
        prompt_details = usage.get("prompt_tokens_details")
        prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
        completion_details = usage.get("completion_tokens_details")
        completion_details = completion_details if isinstance(completion_details, dict) else {}

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
                # FR-29. The gateway reports cache tokens in more than one
                # place -- OpenAI's detail object, Anthropic's top-level fields
                # -- and they are the same tokens either way, so the larger
                # report is taken, never the sum. token_count is Usage's own
                # coercion (INVARIANT-3c123c38), applied first so the
                # comparison is between ints.
                cache_read_tokens=max(
                    token_count(prompt_details.get("cached_tokens")),
                    token_count(usage.get("cache_read_input_tokens")),
                ),
                cache_write_tokens=max(
                    token_count(usage.get("cache_creation_input_tokens")),
                    token_count(prompt_details.get("cache_write_tokens")),
                    token_count(prompt_details.get("cache_creation_tokens")),
                ),
                reasoning_tokens=completion_details.get("reasoning_tokens"),
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
    # Storability is NOT checked here. ToolCall routes arguments no store can
    # hold onto arguments_error itself: 1e400 decodes to inf, and a JSON escape
    # for U+0000 decodes to a NUL. Both are valid RFC-8259 that JSONB refuses,
    # and doing it in the primitive means this adapter and every future one get
    # the behaviour without having to remember it.
    return decoded, None
