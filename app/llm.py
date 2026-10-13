"""Azure OpenAI chat model (function calling)."""
from __future__ import annotations

from typing import Any

from openai import AsyncAzureOpenAI

from .config import Settings


class AzureOpenAIModel:
    def __init__(self, settings: Settings) -> None:
        kwargs: dict[str, Any] = {
            "azure_endpoint": settings.openai_endpoint,
            "api_version": settings.openai_api_version,
        }
        if settings.openai_api_key:
            kwargs["api_key"] = settings.openai_api_key
        else:
            # Managed identity in App Service; az login on a developer machine.
            # Needs the "Cognitive Services OpenAI User" role on the resource.
            from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

            kwargs["azure_ad_token_provider"] = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
            )
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
