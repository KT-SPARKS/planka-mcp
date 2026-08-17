"""Thin Planka REST client.

Everything the model must never see lives here: routes, headers, tokens,
pagination. Tools call these methods and get plain dicts back.

Tested against Planka 2.4.1 (the published swagger is 2.0.1 and lags reality,
e.g. `GET /users/me` exists, boards require a `type`).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from typing import Any

import httpx

from .config import Config

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BOARD_CACHE_TTL = 8.0  # seconds; claim/status always re-read the card itself


class PlankaError(Exception):
    """An API-level failure that tools turn into a clean error result."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class PlankaClient:
    def __init__(self, config: Config):
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = config.api_key
        self._token_is_api_key = bool(config.api_key)
        self._me: dict[str, Any] | None = None
        self._auth_lock = asyncio.Lock()
        self._board_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    # ---------- lifecycle ----------

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.api_root,
                timeout=self.config.timeout,
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "application/json",
                },
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- auth ----------

    async def _login(self) -> str:
        if self._token_is_api_key:
            if not self._token:
                raise PlankaError("PLANKA_API_KEY is empty")
            return self._token
        client = await self._http()
        try:
            resp = await client.post(
                "/access-tokens",
                json={
                    "emailOrUsername": self.config.email,
                    "password": self.config.password,
                },
            )
        except httpx.HTTPError as exc:
            raise PlankaError(f"Cannot reach Planka at {self.config.base_url}: {exc}") from exc
        if resp.status_code != 200:
            raise PlankaError(
                "Login failed - check PLANKA_EMAIL / PLANKA_PASSWORD",
                status=resp.status_code,
            )
        token = (resp.json() or {}).get("item")
        if not token:
            raise PlankaError("Login response contained no token", status=resp.status_code)
        self._token = token
        return token

    async def _ensure_token(self) -> str:
        if self._token:
            return self._token
        async with self._auth_lock:
            if not self._token:
                await self._login()
        return self._token  # type: ignore[return-value]

    def _auth_headers(self, token: str) -> dict[str, str]:
        """Planka takes an API key in `X-Api-Key`, but a login token as a bearer.
        Sending a key as a bearer token silently fails to authenticate."""
        if self._token_is_api_key:
            return {"X-Api-Key": token}
        return {"Authorization": f"Bearer {token}"}

    async def _reauth(self) -> None:
        if self._token_is_api_key:
            raise PlankaError("API key rejected by Planka (401)", status=401)
        async with self._auth_lock:
            self._token = None
            self._me = None
            await self._login()

    # ---------- request plumbing ----------

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_status: tuple[int, ...] = (),
    ) -> tuple[int, dict[str, Any]]:
        """Perform a request. Returns (status, body).

        Raises PlankaError for anything unexpected; statuses in `allow_status`
        are returned to the caller instead (used for 409 conflict handling).
        """
        client = await self._http()
        token = await self._ensure_token()
        retried_auth = False
        delay = 0.5

        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.request(
                    method,
                    path,
                    json=json_body,
                    params=params,
                    headers=self._auth_headers(token),
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES - 1:
                    raise PlankaError(f"Network error talking to Planka: {exc}") from exc
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 401 and not retried_auth:
                retried_auth = True
                await self._reauth()
                token = await self._ensure_token()
                continue

            if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                await asyncio.sleep(delay)
                delay *= 2
                continue

            body: dict[str, Any]
            try:
                body = resp.json() if resp.content else {}
            except (json.JSONDecodeError, ValueError):
                body = {"raw": resp.text[:400]}
            if not isinstance(body, dict):
                body = {"item": body}

            if resp.status_code in allow_status or resp.status_code < 300:
                return resp.status_code, body

            raise PlankaError(
                _describe(resp.status_code, body),
                status=resp.status_code,
                code=body.get("code"),
            )

        raise PlankaError("Planka request failed after retries")

    async def get(self, path: str, **kw: Any) -> dict[str, Any]:
        _, body = await self.request("GET", path, **kw)
        return body

    # ---------- identity ----------

    async def me(self) -> dict[str, Any]:
        if self._me is None:
            try:
                body = await self.get("/users/me")
                self._me = body.get("item") or {}
            except PlankaError:
                # Older builds have no /users/me; fall back to the JWT subject.
                user_id = _jwt_subject(self._token) if not self._token_is_api_key else None
                if not user_id:
                    raise
                body = await self.get(f"/users/{user_id}")
                self._me = body.get("item") or {}
        return self._me

    async def my_id(self) -> str:
        me = await self.me()
        user_id = me.get("id")
        if not user_id:
            raise PlankaError("Could not resolve the authenticated user's id")
        return str(user_id)

    # ---------- reads ----------

    async def projects(self) -> dict[str, Any]:
        return await self.get("/projects")

    async def board_ids(self) -> list[str]:
        """Board ids worth scanning for work.

        An explicit allowlist wins. Otherwise only boards of an accepted type are
        returned: Planka 2.4 boards are `project`, `library` or `collection`, and
        cards on a library/collection board are documents or references, not work.
        """
        if self.config.board_ids:
            return list(self.config.board_ids)
        body = await self.projects()
        boards = (body.get("included") or {}).get("boards") or []
        accepted = self.config.board_types
        return [
            str(b["id"])
            for b in boards
            if b.get("id") and (not accepted or (b.get("type") or "project") in accepted)
        ]

    async def board(self, board_id: str, *, fresh: bool = False) -> dict[str, Any]:
        """Full board graph: lists, cards, memberships, labels, task lists, tasks."""
        now = time.monotonic()
        if not fresh:
            hit = self._board_cache.get(board_id)
            if hit and now - hit[0] < BOARD_CACHE_TTL:
                return hit[1]
        body = await self.get(f"/boards/{board_id}")
        self._board_cache[board_id] = (now, body)
        return body

    def invalidate_board(self, board_id: str | None) -> None:
        if board_id:
            self._board_cache.pop(board_id, None)

    async def card(self, card_id: str) -> dict[str, Any]:
        return await self.get(f"/cards/{card_id}")

    async def comments(self, card_id: str) -> list[dict[str, Any]]:
        body = await self.get(f"/cards/{card_id}/comments")
        return body.get("items") or []

    # ---------- writes (no delete endpoints are wired on purpose) ----------

    async def add_membership(self, card_id: str, user_id: str) -> tuple[int, dict[str, Any]]:
        """POST card membership. 409 means the row already exists (unique on
        card+user) - that is the compare-and-swap the claim tool relies on."""
        return await self.request(
            "POST",
            f"/cards/{card_id}/card-memberships",
            json_body={"userId": user_id},
            allow_status=(409,),
        )

    async def remove_membership(self, card_id: str, user_id: str) -> None:
        await self.request(
            "DELETE",
            f"/cards/{card_id}/card-memberships/userId:{user_id}",
            allow_status=(404,),
        )

    async def move_card(self, card_id: str, list_id: str, position: float) -> dict[str, Any]:
        _, body = await self.request(
            "PATCH",
            f"/cards/{card_id}",
            json_body={"listId": list_id, "position": position},
        )
        return body.get("item") or {}

    async def create_comment(self, card_id: str, text: str) -> dict[str, Any]:
        _, body = await self.request(
            "POST", f"/cards/{card_id}/comments", json_body={"text": text}
        )
        return body.get("item") or {}

    async def create_card(self, list_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        _, body = await self.request(
            "POST", f"/lists/{list_id}/cards", json_body={"type": "project", **fields}
        )
        return body.get("item") or {}

    async def patch_card(self, card_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        _, body = await self.request("PATCH", f"/cards/{card_id}", json_body=fields)
        return body.get("item") or {}

    async def add_label(self, card_id: str, label_id: str) -> None:
        await self.request(
            "POST",
            f"/cards/{card_id}/card-labels",
            json_body={"labelId": label_id},
            allow_status=(409,),
        )

    async def create_task_list(
        self, card_id: str, name: str, position: float
    ) -> dict[str, Any]:
        _, body = await self.request(
            "POST",
            f"/cards/{card_id}/task-lists",
            json_body={"name": name, "position": position, "showOnFrontOfCard": True},
        )
        return body.get("item") or {}

    async def create_task(
        self,
        task_list_id: str,
        name: str | None,
        position: float,
        is_completed: bool = False,
        assignee_user_id: str | None = None,
        linked_card_id: str | None = None,
    ) -> dict[str, Any]:
        """A checklist item. `linkedCardId` makes it point at another card, which
        is how Planka models a dependency; `name` is optional in that case."""
        fields: dict[str, Any] = {"position": position, "isCompleted": is_completed}
        if name:
            fields["name"] = name
        if assignee_user_id:
            fields["assigneeUserId"] = assignee_user_id
        if linked_card_id:
            fields["linkedCardId"] = linked_card_id
        _, body = await self.request(
            "POST", f"/task-lists/{task_list_id}/tasks", json_body=fields
        )
        return body.get("item") or {}

    async def update_task(self, task_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        _, body = await self.request("PATCH", f"/tasks/{task_id}", json_body=fields)
        return body.get("item") or {}

    async def set_task_completed(self, task_id: str, completed: bool) -> dict[str, Any]:
        return await self.update_task(task_id, {"isCompleted": completed})

    # ---- structure: lists, labels, boards (the user-facing "projects") ----

    async def create_list(
        self,
        board_id: str,
        name: str,
        position: float,
        list_type: str = "active",
        color: str | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {"name": name, "position": position, "type": list_type}
        _, body = await self.request("POST", f"/boards/{board_id}/lists", json_body=fields)
        created = body.get("item") or {}
        # `color` is not accepted on create - only PATCH takes it, so set it after.
        if color and created.get("id"):
            created = await self.update_list(str(created["id"]), {"color": color})
        return created

    async def update_list(self, list_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        _, body = await self.request("PATCH", f"/lists/{list_id}", json_body=fields)
        return body.get("item") or {}

    async def delete_list(self, list_id: str) -> None:
        """Only ever called for a list the caller has proven to be empty."""
        await self.request("DELETE", f"/lists/{list_id}", allow_status=(404,))

    async def create_label(
        self, board_id: str, name: str, color: str, position: float
    ) -> dict[str, Any]:
        _, body = await self.request(
            "POST",
            f"/boards/{board_id}/labels",
            json_body={"name": name, "color": color, "position": position},
        )
        return body.get("item") or {}

    async def update_label(self, label_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        _, body = await self.request("PATCH", f"/labels/{label_id}", json_body=fields)
        return body.get("item") or {}

    async def delete_label(self, label_id: str) -> None:
        """Only ever called for a label proven to be on no cards."""
        await self.request("DELETE", f"/labels/{label_id}", allow_status=(404,))

    async def remove_label(self, card_id: str, label_id: str) -> None:
        await self.request(
            "DELETE",
            f"/cards/{card_id}/card-labels/labelId:{label_id}",
            allow_status=(404,),
        )

    async def update_container(self, project_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Update a Planka *project* - the container the user calls a board."""
        _, body = await self.request("PATCH", f"/projects/{project_id}", json_body=fields)
        return body.get("item") or {}

    async def create_container(self, name: str, container_type: str = "shared") -> dict[str, Any]:
        """Create a Planka *project* - the container the user calls a board.

        `type` is create-only: `private` carries an owner and accepts exactly one
        manager, `shared` accepts many. Requires an admin or projectOwner account.
        """
        fields: dict[str, Any] = {"name": name, "type": container_type}
        _, body = await self.request("POST", "/projects", json_body=fields)
        return body.get("item") or {}

    async def create_board(
        self, project_id: str, name: str, position: float, board_type: str = "project"
    ) -> dict[str, Any]:
        _, body = await self.request(
            "POST",
            f"/projects/{project_id}/boards",
            json_body={"name": name, "position": position, "type": board_type},
        )
        return body.get("item") or {}

    async def update_board(self, board_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        _, body = await self.request("PATCH", f"/boards/{board_id}", json_body=fields)
        return body.get("item") or {}

    # ---- people ----

    async def users(self) -> list[dict[str, Any]]:
        body = await self.get("/users")
        return body.get("items") or []

    async def add_board_membership(
        self, board_id: str, user_id: str, role: str, can_comment: bool | None = None
    ) -> tuple[int, dict[str, Any]]:
        fields: dict[str, Any] = {"userId": user_id, "role": role}
        if can_comment is not None:
            fields["canComment"] = can_comment
        return await self.request(
            "POST",
            f"/boards/{board_id}/board-memberships",
            json_body=fields,
            allow_status=(409,),
        )

    async def update_board_membership(
        self, membership_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        _, body = await self.request(
            "PATCH", f"/board-memberships/{membership_id}", json_body=fields
        )
        return body.get("item") or {}

    async def remove_board_membership(self, membership_id: str) -> None:
        await self.request(
            "DELETE", f"/board-memberships/{membership_id}", allow_status=(404,)
        )

    async def set_instance_role(self, user_id: str, role: str) -> dict[str, Any]:
        _, body = await self.request(
            "PATCH", f"/users/{user_id}", json_body={"role": role}
        )
        return body.get("item") or {}

    async def create_api_key(self, user_id: str) -> tuple[dict[str, Any], str | None]:
        """Mint an API key. Planka returns the value exactly once."""
        _, body = await self.request("POST", f"/users/{user_id}/api-key")
        included = body.get("included") or {}
        return body.get("item") or {}, included.get("apiKey")

    async def revoke_api_key(self, user_id: str) -> dict[str, Any]:
        """Clear a user's API key. `PATCH /users/{id}` accepts apiKey: null."""
        _, body = await self.request(
            "PATCH", f"/users/{user_id}", json_body={"apiKey": None}
        )
        return body.get("item") or {}

    async def create_user(self, fields: dict[str, Any]) -> dict[str, Any]:
        _, body = await self.request("POST", "/users", json_body=fields)
        return body.get("item") or {}

    async def add_project_manager(self, project_id: str, user_id: str) -> tuple[int, dict[str, Any]]:
        return await self.request(
            "POST",
            f"/projects/{project_id}/project-managers",
            json_body={"userId": user_id},
            allow_status=(409,),
        )

    async def project_graph(self) -> dict[str, Any]:
        """`GET /projects` carries projects, their boards, memberships and managers."""
        return await self.projects()

    async def attach_link(self, card_id: str, url: str, name: str) -> dict[str, Any]:
        """Attachments are multipart/form-data even for the link variant."""
        client = await self._http()
        token = await self._ensure_token()
        try:
            resp = await client.post(
                f"/cards/{card_id}/attachments",
                files={
                    "type": (None, "link"),
                    "url": (None, url),
                    "name": (None, name),
                },
                headers=self._auth_headers(token),
            )
        except httpx.HTTPError as exc:
            raise PlankaError(f"Network error attaching the link: {exc}") from exc
        try:
            body = resp.json() if resp.content else {}
        except (json.JSONDecodeError, ValueError):
            body = {}
        if resp.status_code >= 300:
            raise PlankaError(
                _describe(resp.status_code, body if isinstance(body, dict) else {}),
                status=resp.status_code,
            )
        return (body or {}).get("item") or {}


def _describe(status: int, body: dict[str, Any]) -> str:
    code = body.get("code")
    message = body.get("message") or ""
    problems = body.get("problems")
    parts = [p for p in (code, message) if p]
    if problems:
        parts.append("; ".join(str(p) for p in problems)[:300])
    if not parts:
        parts.append(f"HTTP {status}")
    if status == 403:
        parts.append("(the Planka account lacks rights for this board/action)")
    if status == 404:
        parts.append("(not found, or not visible to this account)")
    return " - ".join(str(p) for p in parts)


def _jwt_subject(token: str | None) -> str | None:
    if not token or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError, json.JSONDecodeError):
        return None
    sub = data.get("sub")
    return str(sub) if sub else None
