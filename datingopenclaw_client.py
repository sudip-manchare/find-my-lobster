"""Client methods for DatingOpenClaw API endpoints described in DOCS.json."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class DatingOpenClawError(Exception):
    """Raised when the DatingOpenClaw API request fails."""


@dataclass
class DatingOpenClawClient:
    """Thin API client with one method per documented request."""

    base_url: str = "https://datingopenclaw.com/api"
    api_key: str | None = None
    timeout: float = 20.0

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_path = path if path.startswith("/") else f"/{path}"
        url = f"{self.base_url.rstrip('/')}{clean_path}"
        if params:
            query = urlencode(params, doseq=True)
            if query:
                url = f"{url}?{query}"

        body = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        request = Request(url=url, data=body, headers=self._headers(), method=method)

        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.reason
            try:
                body_raw = exc.read().decode("utf-8")
                parsed = json.loads(body_raw)
                if isinstance(parsed, dict):
                    detail = parsed.get("error", detail)
                elif body_raw:
                    detail = body_raw
            except Exception:
                pass
            raise DatingOpenClawError(f"HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise DatingOpenClawError(f"Network error: {exc.reason}") from exc

        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DatingOpenClawError("Invalid JSON response from DatingOpenClaw API") from exc
        if not isinstance(parsed, dict):
            raise DatingOpenClawError("Unexpected API response format")
        return parsed

    def get_docs(self) -> dict[str, Any]:
        """GET /api/docs"""
        return self._request("GET", "/docs")

    def register_agent(
        self,
        *,
        display_name: str,
        profile: dict[str, Any],
        contact: dict[str, Any],
        sync_remote: bool = True,
    ) -> dict[str, Any]:
        """POST /api/agents/register"""
        payload = {
            "display_name": display_name,
            "profile": profile,
            "contact": contact,
            "sync_remote": sync_remote,
        }
        return self._request("POST", "/agents/register", json_body=payload)

    def get_my_profile(self) -> dict[str, Any]:
        """GET /api/agents/profile"""
        return self._request("GET", "/agents/profile")

    def update_my_profile(self, profile_patch: dict[str, Any]) -> dict[str, Any]:
        """PUT /api/agents/profile"""
        return self._request("PUT", "/agents/profile", json_body=profile_patch)

    def list_profiles(
        self,
        *,
        gender: str | None = None,
        age_min: int | None = None,
        age_max: int | None = None,
        city: str | None = None,
        country: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        """GET /api/agents/profiles"""
        params: dict[str, Any] = {"page": page, "limit": limit}
        if gender is not None:
            params["gender"] = gender
        if age_min is not None:
            params["age_min"] = age_min
        if age_max is not None:
            params["age_max"] = age_max
        if city is not None:
            params["city"] = city
        if country is not None:
            params["country"] = country
        return self._request("GET", "/agents/profiles", params=params)

    def create_negotiation(self, *, target_id: str, intro_message: str | None = None) -> dict[str, Any]:
        """POST /api/negotiations"""
        payload: dict[str, Any] = {"target_id": target_id}
        if intro_message is not None:
            payload["intro_message"] = intro_message
        return self._request("POST", "/negotiations", json_body=payload)

    def list_negotiations(
        self,
        *,
        type: str = "all",
        status: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        """GET /api/negotiations"""
        params: dict[str, Any] = {"type": type, "page": page, "limit": limit}
        if status is not None:
            params["status"] = status
        return self._request("GET", "/negotiations", params=params)

    def get_negotiation(self, negotiation_id: str) -> dict[str, Any]:
        """GET /api/negotiations/:id"""
        return self._request("GET", f"/negotiations/{negotiation_id}")

    def accept_negotiation(self, negotiation_id: str) -> dict[str, Any]:
        """POST /api/negotiations/:id/accept"""
        return self._request("POST", f"/negotiations/{negotiation_id}/accept")

    def reject_negotiation(self, negotiation_id: str) -> dict[str, Any]:
        """POST /api/negotiations/:id/reject"""
        return self._request("POST", f"/negotiations/{negotiation_id}/reject")

    def list_messages(self, negotiation_id: str, *, after: str | None = None) -> dict[str, Any]:
        """GET /api/negotiations/:id/messages"""
        params = {"after": after} if after else None
        return self._request("GET", f"/negotiations/{negotiation_id}/messages", params=params)

    def send_message(self, negotiation_id: str, *, content: str) -> dict[str, Any]:
        """POST /api/negotiations/:id/messages"""
        return self._request("POST", f"/negotiations/{negotiation_id}/messages", json_body={"content": content})

    def submit_result(
        self,
        negotiation_id: str,
        *,
        decision: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/negotiations/:id/result"""
        payload: dict[str, Any] = {"decision": decision}
        if reason is not None:
            payload["reason"] = reason
        return self._request("POST", f"/negotiations/{negotiation_id}/result", json_body=payload)

    def get_contact(self, negotiation_id: str) -> dict[str, Any]:
        """GET /api/negotiations/:id/contact"""
        return self._request("GET", f"/negotiations/{negotiation_id}/contact")
