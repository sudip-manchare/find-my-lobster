"""LangChain agent wired to Gemini (via OpenRouter) with DatingOpenClaw tools.

Install:
    pip install -U langchain langchain-openai

Env:
    OPENROUTER_API_KEY=...
    DATINGOPENCLAW_API_KEY=...   # optional for unauthenticated calls only
    DATINGOPENCLAW_BASE_URL=https://datingopenclaw.com/api
"""

from __future__ import annotations

import os
from typing import Any

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

from datingopenclaw_client import DatingOpenClawClient


def build_gemini_llm(
    *,
    model: str = "google/gemini-3-flash-preview",
    temperature: float = 0.2,
) -> ChatOpenAI:
    """Create Gemini chat model through OpenRouter's OpenAI-compatible API."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is required")

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=temperature,
        default_headers={
            "HTTP-Referer": "https://datingopenclaw.com",
            "X-Title": "datingopenclaw-langchain-agent",
        },
    )


def build_dating_tools(client: DatingOpenClawClient) -> list:
    """Create one tool per DatingOpenClaw request in DOCS.json."""

    def get_docs() -> dict[str, Any]:
        """GET /api/docs."""
        return client.get_docs()

    def register_agent(display_name: str, profile: dict[str, Any], contact: dict[str, Any]) -> dict[str, Any]:
        """POST /api/agents/register. Register a new user profile and contact info."""
        return client.register_agent(display_name=display_name, profile=profile, contact=contact)

    def get_my_profile() -> dict[str, Any]:
        """GET /api/agents/profile. Get your own profile."""
        return client.get_my_profile()

    def update_my_profile(profile_patch: dict[str, Any]) -> dict[str, Any]:
        """PUT /api/agents/profile. Partial update for your profile fields."""
        return client.update_my_profile(profile_patch)

    def list_profiles(
        gender: str | None = None,
        age_min: int | None = None,
        age_max: int | None = None,
        city: str | None = None,
        country: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        """GET /api/agents/profiles. Browse profiles with filters and pagination."""
        return client.list_profiles(
            gender=gender,
            age_min=age_min,
            age_max=age_max,
            city=city,
            country=country,
            page=page,
            limit=limit,
        )

    def create_negotiation(target_id: str, intro_message: str | None = None) -> dict[str, Any]:
        """POST /api/negotiations. Send a negotiation request to another user."""
        return client.create_negotiation(target_id=target_id, intro_message=intro_message)

    def list_negotiations(type: str = "all", status: str | None = None, page: int = 1, limit: int = 20) -> dict[str, Any]:
        """GET /api/negotiations. List negotiations by type/status with pagination."""
        return client.list_negotiations(type=type, status=status, page=page, limit=limit)

    def get_negotiation(negotiation_id: str) -> dict[str, Any]:
        """GET /api/negotiations/:id. Get a specific negotiation."""
        return client.get_negotiation(negotiation_id)

    def accept_negotiation(negotiation_id: str) -> dict[str, Any]:
        """POST /api/negotiations/:id/accept. Accept a negotiation request."""
        return client.accept_negotiation(negotiation_id)

    def reject_negotiation(negotiation_id: str) -> dict[str, Any]:
        """POST /api/negotiations/:id/reject. Reject a negotiation request."""
        return client.reject_negotiation(negotiation_id)

    def list_messages(negotiation_id: str, after: str | None = None) -> dict[str, Any]:
        """GET /api/negotiations/:id/messages. Fetch messages, optionally after timestamp."""
        return client.list_messages(negotiation_id, after=after)

    def send_message(negotiation_id: str, content: str) -> dict[str, Any]:
        """POST /api/negotiations/:id/messages. Send a message in a negotiation."""
        return client.send_message(negotiation_id, content=content)

    def submit_result(negotiation_id: str, decision: str, reason: str | None = None) -> dict[str, Any]:
        """POST /api/negotiations/:id/result. Submit match/no_match decision."""
        return client.submit_result(negotiation_id, decision=decision, reason=reason)

    def get_contact(negotiation_id: str) -> dict[str, Any]:
        """GET /api/negotiations/:id/contact. Get contact info after mutual match."""
        return client.get_contact(negotiation_id)

    return [
        get_docs,
        register_agent,
        get_my_profile,
        update_my_profile,
        list_profiles,
        create_negotiation,
        list_negotiations,
        get_negotiation,
        accept_negotiation,
        reject_negotiation,
        list_messages,
        send_message,
        submit_result,
        get_contact,
    ]


def build_dating_agent(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
):
    """Construct LangChain agent with Gemini LLM and DatingOpenClaw endpoint tools."""
    resolved_base_url = base_url or os.getenv("DATINGOPENCLAW_BASE_URL", "https://datingopenclaw.com/api")
    resolved_api_key = api_key or os.getenv("DATINGOPENCLAW_API_KEY")

    client = DatingOpenClawClient(base_url=resolved_base_url, api_key=resolved_api_key)
    llm = build_gemini_llm()
    tools = build_dating_tools(client)

    return create_agent(
        model=llm,
        tools=tools,
        system_prompt=(
            "You are a DatingOpenClaw agent assistant. Use tools to interact with the API. "
            "Before submitting negotiation results, confirm explicit human approval."
        ),
    )


if __name__ == "__main__":
    agent = build_dating_agent()
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Call get_docs and summarize the available endpoints.",
                }
            ]
        }
    )
    print(result)
