"""Azure OpenAI / Foundry chat model (function calling).

By default it uses the Azure OpenAI **v1** API (``<endpoint>/openai/v1``), which works
with current models without choosing a dated ``api-version``. Set
``AZURE_OPENAI_API_VERSION`` to a date (e.g. ``2024-10-21``) to use the classic API.
"""
from __future__ import annotations

from typing import Any

from openai import AsyncAzureOpenAI, AsyncOpenAI

from .config import Settings

_SCOPE = "https://cognitiveservices.azure.com/.default"


def _base_endpoint(endpoint: str) -> str:
    """Accept the resource endpoint with or without a trailing /openai[/v1]."""
    e = endpoint.strip().rstrip("/")
    for suffix in ("/openai/v1", "/openai"):
        if e.endswith(suffix):
            e = e[: -len(suffix)]
    return e


def _token_provider():
    # Managed identity in App Service; az login on a developer machine.
    # Needs the "Cognitive Services OpenAI User" role on the resource.
    from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

    return get_bearer_token_provider(DefaultAzureCredential(), _SCOPE)


class AzureOpenAIModel:
    def __init__(self, settings: Settings) -> None:
        endpoint = _base_endpoint(settings.openai_endpoint)
        version = (settings.openai_api_version or "v1").strip()
        if version.lower() in ("v1", "latest"):
            self._client: AsyncOpenAI = AsyncOpenAI(
                base_url=f"{endpoint}/openai/v1/",
                api_key=settings.openai_api_key or _token_provider(),
            )
        else:
            kwargs: dict[str, Any] = {"azure_endpoint": endpoint, "api_version": version}
            if settings.openai_api_key:
                kwargs["api_key"] = settings.openai_api_key
            else:
                kwargs["azure_ad_token_provider"] = _token_provider()
            self._client = AsyncAzureOpenAI(**kwargs)
        self._deployment = settings.openai_deployment

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        response = await self._client.chat.completions.create(
            model=self._deployment,
            messages=messages,  # type: ignore[arg-type]
            tools=tools or None,  # type: ignore[arg-type]
        )
        msg = response.choices[0].message
        calls = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.function.name, "arguments": c.function.arguments},
            }
            for c in (msg.tool_calls or [])
            if getattr(c, "function", None) is not None
        ]
        return {"content": msg.content, "tool_calls": calls}
