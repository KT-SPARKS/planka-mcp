"""Environment-driven configuration. Nothing here is ever exposed to the model."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _csv(name: str) -> list[str]:
    raw = os.getenv(name) or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    base_url: str
    api_key: str | None = None
    email: str | None = None
    password: str | None = None
    board_ids: list[str] = field(default_factory=list)
    board_types: list[str] = field(default_factory=lambda: ["project"])
    status_lists: dict[str, Any] = field(default_factory=dict)
    blocked_labels: list[str] = field(default_factory=list)
    require_deps_met: bool = False
    allow_reopen: bool = False
    act_as: str | None = None
    allow_user_admin: bool = False
    timeout: float = 20.0
    user_agent: str = "planka-mcp/0.1"
    transport: str = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = 8000

    @property
    def api_root(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/api") else base + "/api"

    def board_allowed(self, board_id: str | None) -> bool:
        return not self.board_ids or (board_id in self.board_ids)


def load_config() -> Config:
    base_url = os.getenv("PLANKA_BASE_URL", "").strip()
    if not base_url:
        raise ConfigError("PLANKA_BASE_URL is not set (see .env.example)")

    api_key = (os.getenv("PLANKA_API_KEY") or "").strip() or None
    email = (os.getenv("PLANKA_EMAIL") or "").strip() or None
    password = os.getenv("PLANKA_PASSWORD") or None
    if not api_key and not (email and password):
        raise ConfigError(
            "No credentials: set PLANKA_API_KEY, or PLANKA_EMAIL + PLANKA_PASSWORD"
        )

    raw_status = (os.getenv("PLANKA_STATUS_LISTS") or "").strip()
    status_lists: dict[str, Any] = {}
    if raw_status:
        try:
            status_lists = json.loads(raw_status)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"PLANKA_STATUS_LISTS is not valid JSON: {exc}") from exc
        if not isinstance(status_lists, dict):
            raise ConfigError("PLANKA_STATUS_LISTS must be a JSON object")

    act_as = (os.getenv("PLANKA_ACT_AS") or "").strip().lower() or None
    if act_as and act_as not in ("guest", "worker", "editor", "admin"):
        raise ConfigError(
            f"PLANKA_ACT_AS must be guest, worker, editor or admin (got '{act_as}')"
        )

    try:
        timeout = float(os.getenv("PLANKA_TIMEOUT") or 20)
        http_port = int(os.getenv("PLANKA_HTTP_PORT") or 8000)
    except ValueError as exc:
        raise ConfigError(f"Invalid numeric setting: {exc}") from exc

    return Config(
        base_url=base_url,
        api_key=api_key,
        email=email,
        password=password,
        board_ids=_csv("PLANKA_BOARD_IDS"),
        board_types=_csv("PLANKA_BOARD_TYPES") or ["project"],
        status_lists=status_lists,
        blocked_labels=[b.lower() for b in _csv("PLANKA_BLOCKED_LABELS")],
        require_deps_met=_bool("PLANKA_REQUIRE_DEPS_MET"),
        allow_reopen=_bool("PLANKA_ALLOW_REOPEN"),
        act_as=act_as,
        allow_user_admin=_bool("PLANKA_ALLOW_USER_ADMIN"),
        timeout=timeout,
        user_agent=os.getenv("PLANKA_USER_AGENT") or "planka-mcp/0.1",
        transport=(os.getenv("PLANKA_TRANSPORT") or "stdio").strip().lower(),
        http_host=os.getenv("PLANKA_HTTP_HOST") or "127.0.0.1",
        http_port=http_port,
    )
