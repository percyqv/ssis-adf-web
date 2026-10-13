"""Configuration from environment variables (App Service "Application settings")."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _list(value: str | None, default: str) -> list[str]:
    return [v.strip() for v in (value or default).split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    # MCP server (ssis-adf-agent running in Azure Container Apps)
    mcp_url: str = ""
    mcp_api_key: str = ""

    # Azure OpenAI (or Azure AI Foundry) chat model
    openai_endpoint: str = ""
    openai_deployment: str = ""
    openai_api_version: str = "v1"  # Azure OpenAI v1 API; or a date for the classic API
    openai_api_key: str = ""  # empty → Microsoft Entra ID (managed identity / az login)

    # Storage Account used for direct upload / list / download
    storage_account: str = ""
    allowed_containers: list[str] = field(default_factory=list)

    # Defaults shown in the settings panel
    default_input: str = "az://ssis-packages/Finanzas"
    default_output: str = "az://adf-artifacts/Finanzas"

    # Tools the chat agent may use (deploy_to_adf is excluded on purpose)
    allowed_tools: list[str] = field(default_factory=list)
    max_agent_steps: int = 8
    max_upload_mb: int = 20

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        return cls(
            mcp_url=env("MCP_URL", "").rstrip("/"),
            mcp_api_key=env("MCP_API_KEY", ""),
            openai_endpoint=env("AZURE_OPENAI_ENDPOINT", ""),
            openai_deployment=env("AZURE_OPENAI_DEPLOYMENT", ""),
            openai_api_version=env("AZURE_OPENAI_API_VERSION", "v1"),
            openai_api_key=env("AZURE_OPENAI_API_KEY", ""),
            storage_account=env("AZURE_STORAGE_ACCOUNT", ""),
            allowed_containers=_list(env("ALLOWED_CONTAINERS"), "ssis-packages,adf-artifacts"),
            default_input=env("DEFAULT_INPUT", "az://ssis-packages/Finanzas"),
            default_output=env("DEFAULT_OUTPUT", "az://adf-artifacts/Finanzas"),
            allowed_tools=_list(
                env("ALLOWED_TOOLS"),
                "scan_ssis_packages,analyze_ssis_package,convert_ssis_package,"
                "validate_adf_artifacts",
            ),
            max_agent_steps=int(env("MAX_AGENT_STEPS", "8")),
            max_upload_mb=int(env("MAX_UPLOAD_MB", "20")),
        )

    def missing(self) -> list[str]:
        """Names of required settings that are not configured."""
        required = {
            "MCP_URL": self.mcp_url,
            "MCP_API_KEY": self.mcp_api_key,
            "AZURE_OPENAI_ENDPOINT": self.openai_endpoint,
            "AZURE_OPENAI_DEPLOYMENT": self.openai_deployment,
            "AZURE_STORAGE_ACCOUNT": self.storage_account,
        }
        return [name for name, value in required.items() if not value]
