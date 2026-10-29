"""Shared environment loading for web and stdio entrypoints."""

import os
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def load_environment() -> None:
    # Explicit process variables win; .env.local takes precedence over .env.
    values = {**dotenv_values(ROOT / ".env"), **dotenv_values(ROOT / ".env.local")}
    for name, value in values.items():
        if value is not None:
            os.environ.setdefault(name, value)


def mcp_allowed_origins(base_url: str) -> list[str]:
    """Exact browser origins; MCP still requires bearer auth and a trusted Host."""
    from urllib.parse import urlparse
    origins = [base_url, "https://chatgpt.com", "https://chat.openai.com"]
    origins.extend(value.strip() for value in os.environ.get("CLASSROOM_MCP_ALLOWED_ORIGINS", "").split(",") if value.strip())
    for origin in origins:
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or "*" in origin:
            raise ValueError("CLASSROOM_MCP_ALLOWED_ORIGINS에는 경로 없는 정확한 Origin을 설정해 주세요.")
    return list(dict.fromkeys(origins))
