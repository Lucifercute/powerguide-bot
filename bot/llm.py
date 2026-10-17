import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import httpx
from google import genai
from google.genai import errors, types

log = logging.getLogger(__name__)

RETRY_DELAYS = (3, 10)  # seconds before 2nd and 3rd attempt on the same model
# Image models on Vertex have no per-project quota: they share Google's capacity with all
# customers (Dynamic Shared Quota), and a 429 there passes in about a minute. Waiting that long
# is better than falling back to the pricier Pro model, which also draws in a slightly different style.
IMAGE_RETRY_DELAYS = (30, 60)
RETRYABLE_CODES = {429, 500, 502, 503, 504}

T = TypeVar("T")


class LLMError(Exception):
    """All configured models failed."""

    def __init__(self, message: str, quota_exceeded: bool):
        super().__init__(message)
        self.quota_exceeded = quota_exceeded


@dataclass(frozen=True)
class Image:
    data: bytes
    mime_type: str


# Name prefix of models called through Vertex AI: they are paid from the Google Cloud credits.
VERTEX_PREFIX = "vertex/"
# A 429 that asks to wait longer than this is a daily quota, not a per-minute one:
# retrying the same model is pointless, so the next model is tried at once.
MAX_QUOTA_WAIT_SECONDS = 60
_RETRY_IN = re.compile(r"retry in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", re.IGNORECASE)


@dataclass(frozen=True)
class _Target:
    client: genai.Client
    model: str
    name: str  # shown in logs and saved in the DB: the model, "vertex/" + model for paid ones


def _vertex_client(api_key: str) -> genai.Client:
    return genai.Client(
        vertexai=True,
        api_key=api_key,
        location="global",
        http_options=types.HttpOptions(timeout=180_000),  # ms
    )


class TextLLM:
    """Gemini text generation with retries and fallback across a list of models:
    first the free AI Studio models, then (optionally) paid Vertex AI models as the last resort."""

    def __init__(
        self, api_key: str, models: list[str], vertex_api_key: str | None = None, vertex_models: list[str] = ()
    ):
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=180_000),  # ms
        )
        self.targets = [_Target(self.client, m, m) for m in models]
        # The File API belongs to the free AI Studio key: only its models can read uploaded files.
        self.can_upload = bool(models)
        if vertex_api_key and vertex_models:
            vertex = _vertex_client(vertex_api_key)
            self.targets += [_Target(vertex, m, VERTEX_PREFIX + m) for m in vertex_models]
        if not self.targets:
            raise ValueError("no text models configured: set GEMINI_TEXT_MODELS and/or VERTEX_TEXT_MODELS")

    async def generate(
        self, contents: types.ContentListUnion, config: types.GenerateContentConfig | None = None
    ) -> tuple[str, str]:
        """Return (text, name of the model that produced it).

        contents is a prompt string, or a list of parts, e.g. [image Part, prompt]."""
        return await _generate_with_fallback(self.targets, contents, config, lambda r: r.text or None)

    async def upload(self, path: Path, mime_type: str) -> types.Part:
        """Upload a file to the Gemini File API (kept there 48 hours) and return a Part
        pointing to it: for files too big to send inside the request. Raises LLMError.
        Only AI Studio models can read such a Part; Vertex models will reject the request."""
        try:
            file = await self.client.aio.files.upload(file=path, config=types.UploadFileConfig(mime_type=mime_type))
        except (errors.APIError, httpx.TransportError) as e:
            raise LLMError(f"upload failed: {e}", quota_exceeded=False) from e
        return types.Part.from_uri(file_uri=file.uri, mime_type=mime_type)


class ImageLLM:
    """Image generation on Vertex AI (paid from Google Cloud trial credits: the free
    Gemini API key has no image models). Same retries and model fallback as TextLLM."""

    def __init__(self, api_key: str, models: list[str]):
        client = _vertex_client(api_key)
        self.targets = [_Target(client, m, m) for m in models]

    async def generate(self, contents: types.ContentListUnion, aspect_ratio: str) -> tuple[Image, str]:
        """Return (image, name of the model that produced it)."""
        config = types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
        )
        return await _generate_with_fallback(
            self.targets, contents, config, _first_image, retry_delays=IMAGE_RETRY_DELAYS
        )


async def _generate_with_fallback(
    targets: list[_Target],
    contents: types.ContentListUnion,
    config: types.GenerateContentConfig | None,
    extract: Callable[[types.GenerateContentResponse], T | None],
    retry_delays: tuple[int, ...] = RETRY_DELAYS,
) -> tuple[T, str]:
    """Try each model in order with retries; return (extract(response), model name).

    A response that extract() turns into None (empty text, no image, blocked by
    safety filters) is not retried on the same model: the next model is tried."""
    last_error: Exception | None = None
    for target in targets:
        for attempt in range(len(retry_delays) + 1):
            try:
                response = await target.client.aio.models.generate_content(
                    model=target.model, contents=contents, config=config
                )
            except errors.APIError as e:
                last_error = e
                log.warning("Gemini %s attempt %d failed: %s %s", target.name, attempt + 1, e.code, _first_line(e))
                if e.code not in RETRYABLE_CODES or attempt == len(retry_delays) or _is_long_quota(e):
                    break  # give up on this model, try the next one
                await asyncio.sleep(retry_delays[attempt])
                continue
            except httpx.TransportError as e:  # network errors and timeouts
                last_error = e
                log.warning("Gemini %s attempt %d network error: %r", target.name, attempt + 1, e)
                if attempt == len(retry_delays):
                    break
                await asyncio.sleep(retry_delays[attempt])
                continue
            result = extract(response)
            if result is not None:
                log.info("Gemini %s ok on attempt %d, %s", target.name, attempt + 1, _usage(response))
                return result, target.name
            log.warning(
                "Gemini %s returned an empty result (finish reason: %s)", target.name, _finish_reason(response)
            )
            break
    raise LLMError(
        f"all models failed, last error: {last_error}",
        quota_exceeded=isinstance(last_error, errors.APIError) and last_error.code == 429,
    )


def _is_long_quota(error: errors.APIError) -> bool:
    """429 with "Please retry in 13h44m34s"-style hint longer than MAX_QUOTA_WAIT_SECONDS."""
    if error.code != 429:
        return False
    match = _RETRY_IN.search(error.message or "")
    if match is None:
        return False
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds) > MAX_QUOTA_WAIT_SECONDS


def _first_line(error: errors.APIError) -> str:
    return (error.message or "").strip().splitlines()[0] if error.message else ""


def _usage(response: types.GenerateContentResponse) -> str:
    """Token counts for cost estimates: input, output (text or image) and thinking."""
    usage = response.usage_metadata
    if usage is None:
        return "tokens: unknown"
    return (
        f"tokens: in={usage.prompt_token_count or 0} out={usage.candidates_token_count or 0} "
        f"thinking={usage.thoughts_token_count or 0}"
    )


def _first_image(response: types.GenerateContentResponse) -> Image | None:
    for candidate in response.candidates or []:
        for part in (candidate.content.parts if candidate.content else None) or []:
            if part.inline_data and part.inline_data.data:
                return Image(part.inline_data.data, part.inline_data.mime_type or "image/png")
    return None


def _finish_reason(response: types.GenerateContentResponse) -> str | None:
    if response.candidates:
        return str(response.candidates[0].finish_reason)
    return None
