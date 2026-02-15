#!/usr/bin/env python3
"""LobsterLink API - agent-first dating backend.

A minimal dependency-free HTTP API inspired by agent-to-agent matchmaking flows.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from datingopenclaw_client import DatingOpenClawClient, DatingOpenClawError


def int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


DB_PATH = os.getenv("LOBSTERLINK_DB_PATH", "lobsterlink.db")
HOST = os.getenv("LOBSTERLINK_HOST", "127.0.0.1")
PORT = int_env("LOBSTERLINK_PORT", 8080)
FRONTEND_DIR = Path(__file__).parent / "frontend"
DATINGOPENCLAW_BASE_URL = os.getenv("DATINGOPENCLAW_BASE_URL", "https://datingopenclaw.com/api").rstrip("/")
DEFAULT_CHAT_HISTORY_LIMIT = 50
DEFAULT_SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
RAW_SESSION_TTL_SECONDS = int_env("LOBSTERLINK_SESSION_TTL_SECONDS", DEFAULT_SESSION_TTL_SECONDS)
# Guardrail: reject zero/negative/super-low TTLs that can invalidate sessions immediately in production.
SESSION_TTL_SECONDS = RAW_SESSION_TTL_SECONDS if RAW_SESSION_TTL_SECONDS >= 300 else DEFAULT_SESSION_TTL_SECONDS


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def session_expires_at(from_dt: datetime | None = None) -> str:
    base = (from_dt or datetime.now(timezone.utc)).replace(microsecond=0)
    return (base + timedelta(seconds=SESSION_TTL_SECONDS)).isoformat()


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000).hex()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def clean_string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ApiError(400, f"{field_name} must be an array of strings")
    cleaned: list[str] = []
    for raw in value:
        text = clean_text(raw)
        if text:
            cleaned.append(text)
    return cleaned


def parse_int_value(value: Any, field_name: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise ApiError(400, f"{field_name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ApiError(400, f"{field_name} must be <= {maximum}")
    return value


def parse_optional_int_value(value: Any, field_name: str, *, minimum: int | None = None, maximum: int | None = None) -> int | None:
    if value in (None, ""):
        return None
    return parse_int_value(value, field_name, minimum=minimum, maximum=maximum)


def ensure_db() -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                user_id TEXT UNIQUE,
                api_key TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                age INTEGER NOT NULL,
                gender TEXT,
                city TEXT,
                country TEXT,
                appearance TEXT,
                height_cm INTEGER,
                body_type TEXT,
                personality TEXT,
                interests_json TEXT NOT NULL,
                values_json TEXT NOT NULL,
                preferences_json TEXT NOT NULL,
                contact_json TEXT NOT NULL,
                ai_profile_json TEXT NOT NULL DEFAULT '{}',
                dating_user_id TEXT,
                dating_api_key TEXT,
                dating_base_url TEXT,
                dating_registered_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        agent_columns = {row[1] for row in cur.execute("PRAGMA table_info(agents)")}
        if "user_id" not in agent_columns:
            cur.execute("ALTER TABLE agents ADD COLUMN user_id TEXT")
        if "ai_profile_json" not in agent_columns:
            cur.execute("ALTER TABLE agents ADD COLUMN ai_profile_json TEXT NOT NULL DEFAULT '{}'")
        if "dating_user_id" not in agent_columns:
            cur.execute("ALTER TABLE agents ADD COLUMN dating_user_id TEXT")
        if "dating_api_key" not in agent_columns:
            cur.execute("ALTER TABLE agents ADD COLUMN dating_api_key TEXT")
        if "dating_base_url" not in agent_columns:
            cur.execute("ALTER TABLE agents ADD COLUMN dating_base_url TEXT")
        if "dating_registered_at" not in agent_columns:
            cur.execute("ALTER TABLE agents ADD COLUMN dating_registered_at TEXT")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_user_id ON agents(user_id) WHERE user_id IS NOT NULL")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_agents_age_created_at ON agents(age, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_agents_gender_lc ON agents(LOWER(gender))")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_agents_city_lc ON agents(LOWER(city))")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_agents_country_lc ON agents(LOWER(country))")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        session_columns = {row[1] for row in cur.execute("PRAGMA table_info(user_sessions)")}
        if "expires_at" not in session_columns:
            cur.execute("ALTER TABLE user_sessions ADD COLUMN expires_at TEXT")
            cur.execute("UPDATE user_sessions SET expires_at = ? WHERE expires_at IS NULL", (session_expires_at(),))
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id ON user_sessions(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_expires_at ON user_sessions(expires_at)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS negotiations (
                id TEXT PRIMARY KEY,
                initiator_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                intro_message TEXT NOT NULL,
                status TEXT NOT NULL,
                initiator_decision TEXT,
                target_decision TEXT,
                initiator_reason TEXT,
                target_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (initiator_id) REFERENCES agents(id),
                FOREIGN KEY (target_id) REFERENCES agents(id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                negotiation_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (negotiation_id) REFERENCES negotiations(id),
                FOREIGN KEY (sender_id) REFERENCES agents(id)
            )
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_pair_open
            ON negotiations (initiator_id, target_id, status)
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_negotiations_target_status_updated "
            "ON negotiations(target_id, status, updated_at DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_negotiations_initiator_status_updated "
            "ON negotiations(initiator_id, status, updated_at DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_negotiation_created "
            "ON messages(negotiation_id, created_at, id)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_chat_user_id_id "
            "ON agent_chat_messages(user_id, id DESC)"
        )
        con.commit()
    finally:
        con.close()


def get_connection() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def parse_json_field(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def agent_public_profile(row: sqlite3.Row) -> dict[str, Any]:
    ai_profile = parse_json_field(row["ai_profile_json"]) or {}
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "age": row["age"],
        "gender": row["gender"],
        "city": row["city"],
        "country": row["country"],
        "appearance": row["appearance"],
        "height_cm": row["height_cm"],
        "body_type": row["body_type"],
        "personality": row["personality"],
        "interests": parse_json_field(row["interests_json"]),
        "values": parse_json_field(row["values_json"]),
        "preferences": parse_json_field(row["preferences_json"]),
        "profile": ai_profile,
        "created_at": row["created_at"],
    }


def negotiation_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "initiator_id": row["initiator_id"],
        "target_id": row["target_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def load_public_profiles_by_id(agent_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not agent_ids:
        return {}
    unique_ids = sorted({clean_text(agent_id) for agent_id in agent_ids if clean_text(agent_id)})
    if not unique_ids:
        return {}

    placeholders = ",".join("?" for _ in unique_ids)
    con = get_connection()
    try:
        rows = con.execute(f"SELECT * FROM agents WHERE id IN ({placeholders})", tuple(unique_ids)).fetchall()
    finally:
        con.close()
    return {row["id"]: agent_public_profile(row) for row in rows}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class AppHandler(BaseHTTPRequestHandler):
    server_version = "LobsterLink/0.1"

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if method == "GET" and path == "/":
                self._send_static(FRONTEND_DIR / "index.html")
                return
            if method == "GET" and path == "/auth":
                self._send_static(FRONTEND_DIR / "auth.html")
                return
            if method == "GET" and path == "/profile":
                self._send_static(FRONTEND_DIR / "profile.html")
                return
            if method == "GET" and path == "/app":
                self._send_static(FRONTEND_DIR / "app.html")
                return
            if method == "GET" and path == "/whats-a-lobster":
                self._send_static(FRONTEND_DIR / "whats-a-lobster.html")
                return
            if method == "GET" and path.startswith("/static/"):
                rel_path = path.removeprefix("/static/")
                file_path = (FRONTEND_DIR / rel_path).resolve()
                base_path = FRONTEND_DIR.resolve()
                if base_path not in file_path.parents and file_path != base_path:
                    self._send_json(403, {"error": "Forbidden"})
                    return
                self._send_static(file_path)
                return

            if method == "GET" and path == "/api/docs":
                self._send_json(200, self._docs())
                return
            if method == "POST" and path == "/api/auth/signup":
                payload = self._read_json_body()
                self._send_json(201, self._signup(payload))
                return
            if method == "POST" and path == "/api/auth/login":
                payload = self._read_json_body()
                self._send_json(200, self._login(payload))
                return
            if method == "GET" and path == "/api/auth/me":
                user = self._require_auth_user()
                self._send_json(200, self._auth_me(user))
                return
            if method == "POST" and path == "/api/auth/logout":
                user = self._require_auth_user()
                self._send_json(200, self._logout(user))
                return
            if method == "GET" and path == "/api/agent/chat/history":
                user = self._require_auth_user()
                self._send_json(200, self._agent_chat_history(user))
                return
            if method == "POST" and path == "/api/agent/chat/clear":
                user = self._require_auth_user()
                self._send_json(200, self._clear_agent_chat_history(user))
                return
            if method == "POST" and path == "/api/agent/chat":
                user = self._require_auth_user()
                payload = self._read_json_body()
                self._send_json(200, self._agent_chat(user, payload))
                return
            if method == "POST" and path == "/api/agents/register":
                payload = self._read_json_body()
                self._send_json(201, self._register_agent(payload, self._bearer_token()))
                return

            if not path.startswith("/api/"):
                self._send_json(404, {"error": "Not found"})
                return

            agent = self._require_auth_agent()

            if method == "GET" and path == "/api/agents/profile":
                self._send_json(200, self._get_my_profile(agent))
                return
            if method == "PUT" and path == "/api/agents/profile":
                payload = self._read_json_body()
                self._send_json(200, self._update_my_profile(agent, payload))
                return
            if method == "GET" and path == "/api/agents/profiles":
                self._send_json(200, self._list_profiles(agent, parse_qs(parsed.query)))
                return
            if method == "POST" and path == "/api/negotiations":
                payload = self._read_json_body()
                self._send_json(201, self._create_negotiation(agent, payload))
                return
            if method == "GET" and path == "/api/negotiations":
                self._send_json(200, self._list_negotiations(agent, parse_qs(parsed.query)))
                return

            parts = [p for p in path.split("/") if p]
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "negotiations":
                negotiation_id = parts[2]

                if method == "GET" and len(parts) == 3:
                    self._send_json(200, self._get_negotiation(agent, negotiation_id))
                    return
                if method == "POST" and len(parts) == 4 and parts[3] == "accept":
                    self._send_json(200, self._accept_reject(agent, negotiation_id, True))
                    return
                if method == "POST" and len(parts) == 4 and parts[3] == "reject":
                    self._send_json(200, self._accept_reject(agent, negotiation_id, False))
                    return
                if method == "GET" and len(parts) == 4 and parts[3] == "messages":
                    self._send_json(200, self._list_messages(agent, negotiation_id, parse_qs(parsed.query)))
                    return
                if method == "POST" and len(parts) == 4 and parts[3] == "messages":
                    payload = self._read_json_body()
                    self._send_json(201, self._create_message(agent, negotiation_id, payload))
                    return
                if method == "POST" and len(parts) == 4 and parts[3] == "result":
                    payload = self._read_json_body()
                    self._send_json(200, self._submit_result(agent, negotiation_id, payload))
                    return
                if method == "GET" and len(parts) == 4 and parts[3] == "contact":
                    self._send_json(200, self._get_contact(agent, negotiation_id))
                    return

            self._send_json(404, {"error": "Not found"})
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception:
            print(f"[ERROR] Unhandled request error: {method} {path}", file=sys.stderr)
            traceback.print_exc()
            self._send_json(500, {"error": "Internal server error"})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _docs(self) -> dict[str, Any]:
        return {
            "name": "LobsterLink API",
            "version": "0.2.0",
            "description": "Human-representing agents negotiate compatibility before matches.",
            "endpoints": [
                {"method": "GET", "path": "/api/docs", "auth": False},
                {"method": "POST", "path": "/api/auth/signup", "auth": False},
                {"method": "POST", "path": "/api/auth/login", "auth": False},
                {"method": "GET", "path": "/api/auth/me", "auth": True},
                {"method": "POST", "path": "/api/auth/logout", "auth": True},
                {"method": "GET", "path": "/api/agent/chat/history", "auth": True},
                {"method": "POST", "path": "/api/agent/chat/clear", "auth": True},
                {"method": "POST", "path": "/api/agent/chat", "auth": True},
                {"method": "POST", "path": "/api/agents/register", "auth": False},
                {"method": "GET", "path": "/api/agents/profile", "auth": True},
                {"method": "PUT", "path": "/api/agents/profile", "auth": True},
                {"method": "GET", "path": "/api/agents/profiles", "auth": True},
                {"method": "POST", "path": "/api/negotiations", "auth": True},
                {"method": "GET", "path": "/api/negotiations", "auth": True},
                {"method": "GET", "path": "/api/negotiations/:id", "auth": True},
                {"method": "POST", "path": "/api/negotiations/:id/accept", "auth": True},
                {"method": "POST", "path": "/api/negotiations/:id/reject", "auth": True},
                {"method": "GET", "path": "/api/negotiations/:id/messages", "auth": True},
                {"method": "POST", "path": "/api/negotiations/:id/messages", "auth": True},
                {"method": "POST", "path": "/api/negotiations/:id/result", "auth": True},
                {"method": "GET", "path": "/api/negotiations/:id/contact", "auth": True},
            ],
        }

    def _read_json_body(self) -> dict[str, Any]:
        content_len = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_len) if content_len > 0 else b""
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ApiError(400, "JSON body must be an object")
            return value
        except json.JSONDecodeError as exc:
            raise ApiError(400, f"Invalid JSON: {exc.msg}") from exc

    def _send_json(self, status: int, payload: dict[str, Any] | list[Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, file_path: Path) -> None:
        if not file_path.exists() or not file_path.is_file():
            self._send_json(404, {"error": "Not found"})
            return

        content = file_path.read_bytes()
        content_type, _ = mimetypes.guess_type(str(file_path))
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _bearer_token(self) -> str:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return ""
        return auth[len("Bearer ") :].strip()

    def _require_auth_user(self) -> sqlite3.Row:
        token = self._bearer_token()
        if not token:
            raise ApiError(401, "Missing or invalid Authorization header")
        now = utc_now()
        con = get_connection()
        try:
            row = con.execute(
                """
                SELECT u.id, u.email, s.token AS session_token
                FROM user_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = ? AND (s.expires_at IS NULL OR s.expires_at >= ?)
                """,
                (token, now),
            ).fetchone()
            if row is None:
                raise ApiError(401, "Invalid session")
            return row
        finally:
            con.close()

    def _require_auth_agent(self) -> sqlite3.Row:
        token = self._bearer_token()
        if not token:
            raise ApiError(401, "Missing API key")

        now = utc_now()
        con = get_connection()
        try:
            row = con.execute("SELECT * FROM agents WHERE api_key = ?", (token,)).fetchone()
            if row is not None:
                return row
            row = con.execute(
                """
                SELECT a.*
                FROM user_sessions s
                JOIN agents a ON a.user_id = s.user_id
                WHERE s.token = ? AND (s.expires_at IS NULL OR s.expires_at >= ?)
                """,
                (token, now),
            ).fetchone()
            if row is not None:
                return row
            user_exists = con.execute(
                "SELECT 1 FROM user_sessions WHERE token = ? AND (expires_at IS NULL OR expires_at >= ?)",
                (token, now),
            ).fetchone()
            if user_exists:
                raise ApiError(409, "Complete your profile first")
            raise ApiError(401, "Invalid session")
        finally:
            con.close()

    def _integration_summary(self, agent: sqlite3.Row | None) -> dict[str, Any]:
        if agent is None:
            return {
                "datingopenclaw": {
                    "connected": False,
                    "base_url": DATINGOPENCLAW_BASE_URL,
                    "user_id": None,
                }
            }
        return {
            "datingopenclaw": {
                "connected": bool(clean_text(agent["dating_api_key"])),
                "base_url": clean_text(agent["dating_base_url"]) or DATINGOPENCLAW_BASE_URL,
                "user_id": clean_text(agent["dating_user_id"]) or None,
            }
        }

    def _ai_profile_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        stored = parse_json_field(row["ai_profile_json"])
        if isinstance(stored, dict) and stored:
            return stored

        preferences = parse_json_field(row["preferences_json"]) or {}
        age_range = preferences.get("age_range") if isinstance(preferences, dict) else None
        pref_age_min = age_range[0] if isinstance(age_range, list) and len(age_range) >= 2 else None
        pref_age_max = age_range[1] if isinstance(age_range, list) and len(age_range) >= 2 else None

        return {
            "age": row["age"],
            "gender": row["gender"],
            "location_city": row["city"],
            "location_country": row["country"],
            "appearance_summary": row["appearance"],
            "height_cm": row["height_cm"],
            "body_type": row["body_type"],
            "personality_summary": row["personality"],
            "interests": parse_json_field(row["interests_json"]) or [],
            "values": parse_json_field(row["values_json"]) or [],
            "communication_style": preferences.get("communication_style"),
            "pref_age_min": pref_age_min,
            "pref_age_max": pref_age_max,
            "pref_gender": preferences.get("preferred_genders", []),
            "pref_location": preferences.get("preferred_location"),
            "pref_summary": preferences.get("summary") or "Looking for compatibility and shared values.",
            "dealbreakers": preferences.get("dealbreakers", []),
            "photos": preferences.get("photos", []),
        }

    def _parse_dating_profile_payload(
        self, payload: dict[str, Any]
    ) -> tuple[str, int, str, str, str, str, int | None, str, str, list[str], list[str], dict[str, Any], dict[str, Any], dict[str, Any]]:
        if "profile" in payload:
            profile_raw = payload.get("profile")
            if not isinstance(profile_raw, dict):
                raise ApiError(400, "profile must be an object")
            display_name = clean_text(payload.get("display_name"))
            if not display_name:
                raise ApiError(400, "display_name is required")
            contact_raw = payload.get("contact", {})
            if not isinstance(contact_raw, dict):
                raise ApiError(400, "contact must be an object")

            age = parse_int_value(profile_raw.get("age"), "profile.age", minimum=18, maximum=120)
            gender = clean_text(profile_raw.get("gender"))
            city = clean_text(profile_raw.get("location_city"))
            country = clean_text(profile_raw.get("location_country"))
            appearance = clean_text(profile_raw.get("appearance_summary"))
            height_cm = parse_optional_int_value(profile_raw.get("height_cm"), "profile.height_cm", minimum=80, maximum=260)
            body_type = clean_text(profile_raw.get("body_type"))
            personality = clean_text(profile_raw.get("personality_summary"))
            if not personality:
                raise ApiError(400, "profile.personality_summary is required")

            interests = clean_string_list(profile_raw.get("interests"), "profile.interests")
            values = clean_string_list(profile_raw.get("values"), "profile.values")
            communication_style = clean_text(profile_raw.get("communication_style"))
            pref_age_min = parse_optional_int_value(profile_raw.get("pref_age_min"), "profile.pref_age_min", minimum=18, maximum=120)
            pref_age_max = parse_optional_int_value(profile_raw.get("pref_age_max"), "profile.pref_age_max", minimum=18, maximum=120)
            if pref_age_min is not None and pref_age_max is not None and pref_age_min > pref_age_max:
                pref_age_min, pref_age_max = pref_age_max, pref_age_min

            pref_gender_raw = profile_raw.get("pref_gender", [])
            if isinstance(pref_gender_raw, str):
                pref_gender = [part.strip() for part in pref_gender_raw.split(",") if part.strip()]
            else:
                pref_gender = clean_string_list(pref_gender_raw, "profile.pref_gender")
            pref_location = clean_text(profile_raw.get("pref_location"))
            pref_summary = clean_text(profile_raw.get("pref_summary")) or "Looking for compatibility and shared values."
            dealbreakers = clean_string_list(profile_raw.get("dealbreakers"), "profile.dealbreakers")
            photos = clean_string_list(profile_raw.get("photos"), "profile.photos")
        else:
            display_name = clean_text(payload.get("display_name"))
            if not display_name:
                raise ApiError(400, "display_name is required")
            age = parse_int_value(payload.get("age"), "age", minimum=18, maximum=120)
            gender = clean_text(payload.get("gender"))
            city = clean_text(payload.get("city"))
            country = clean_text(payload.get("country"))
            appearance = clean_text(payload.get("appearance"))
            height_cm = parse_optional_int_value(payload.get("height_cm"), "height_cm", minimum=80, maximum=260)
            body_type = clean_text(payload.get("body_type"))
            personality = clean_text(payload.get("personality"))
            if not personality:
                raise ApiError(400, "personality is required")
            interests = clean_string_list(payload.get("interests"), "interests")
            values = clean_string_list(payload.get("values"), "values")

            preferences_raw = payload.get("preferences", {})
            if not isinstance(preferences_raw, dict):
                raise ApiError(400, "preferences must be an object")

            preferred_genders_raw = preferences_raw.get("preferred_genders", [])
            if isinstance(preferred_genders_raw, str):
                pref_gender = [part.strip() for part in preferred_genders_raw.split(",") if part.strip()]
            else:
                pref_gender = clean_string_list(preferred_genders_raw, "preferences.preferred_genders")
            age_range = preferences_raw.get("age_range")
            pref_age_min = None
            pref_age_max = None
            if isinstance(age_range, list) and len(age_range) >= 2:
                pref_age_min = parse_optional_int_value(age_range[0], "preferences.age_range[0]", minimum=18, maximum=120)
                pref_age_max = parse_optional_int_value(age_range[1], "preferences.age_range[1]", minimum=18, maximum=120)
                if pref_age_min is not None and pref_age_max is not None and pref_age_min > pref_age_max:
                    pref_age_min, pref_age_max = pref_age_max, pref_age_min

            pref_location = clean_text(preferences_raw.get("preferred_location"))
            pref_summary = clean_text(preferences_raw.get("summary")) or "Looking for compatibility and shared values."
            communication_style = clean_text(preferences_raw.get("communication_style"))
            dealbreakers = clean_string_list(preferences_raw.get("dealbreakers"), "preferences.dealbreakers")
            photos = clean_string_list(preferences_raw.get("photos"), "preferences.photos")
            contact_raw = payload.get("contact", {})
            if not isinstance(contact_raw, dict):
                raise ApiError(400, "contact must be an object")

        contact = {k: clean_text(v) for k, v in contact_raw.items() if clean_text(v)}
        ai_profile = {
            "age": age,
            "gender": gender,
            "location_city": city,
            "location_country": country,
            "appearance_summary": appearance,
            "height_cm": height_cm,
            "body_type": body_type,
            "personality_summary": personality,
            "interests": interests,
            "values": values,
            "communication_style": communication_style,
            "pref_age_min": pref_age_min,
            "pref_age_max": pref_age_max,
            "pref_gender": pref_gender,
            "pref_location": pref_location,
            "pref_summary": pref_summary,
            "dealbreakers": dealbreakers,
            "photos": photos,
        }

        preferences: dict[str, Any] = {
            "preferred_genders": pref_gender,
            "preferred_location": pref_location,
            "summary": pref_summary,
            "dealbreakers": dealbreakers,
            "communication_style": communication_style,
            "photos": photos,
        }
        if pref_age_min is not None and pref_age_max is not None:
            preferences["age_range"] = [pref_age_min, pref_age_max]

        return (
            display_name,
            age,
            gender,
            city,
            country,
            appearance,
            height_cm,
            body_type,
            personality,
            interests,
            values,
            preferences,
            contact,
            ai_profile,
        )

    def _register_with_datingopenclaw(self, display_name: str, ai_profile: dict[str, Any], contact: dict[str, Any]) -> dict[str, Any]:
        base_url = os.getenv("DATINGOPENCLAW_BASE_URL", DATINGOPENCLAW_BASE_URL).rstrip("/")
        try:
            client = DatingOpenClawClient(base_url=base_url, timeout=25.0)
            parsed = client.register_agent(
                display_name=display_name,
                profile=ai_profile,
                contact=contact,
                sync_remote=False,
            )
        except DatingOpenClawError as exc:
            raise ApiError(502, f"Remote register failed: {exc}") from exc

        dating_api_key = clean_text(parsed.get("api_key"))
        dating_user_id = clean_text(parsed.get("user_id") or parsed.get("agent_id"))
        dating_registered_at = clean_text(parsed.get("registered_at")) or utc_now()
        if not dating_api_key:
            raise ApiError(502, "Remote register failed: api_key missing")
        return {
            "dating_api_key": dating_api_key,
            "dating_user_id": dating_user_id or None,
            "dating_registered_at": dating_registered_at,
            "dating_base_url": base_url,
        }

    def _signup(self, payload: dict[str, Any]) -> dict[str, Any]:
        email = normalize_email(payload.get("email", ""))
        password = payload.get("password", "")
        if "@" not in email or "." not in email:
            raise ApiError(400, "Enter a valid email")
        if not isinstance(password, str) or len(password) < 8:
            raise ApiError(400, "Password must be at least 8 characters")

        now = utc_now()
        user_id = str(uuid.uuid4())
        salt = secrets.token_hex(16)
        password_hash = hash_password(password, salt)

        con = get_connection()
        try:
            existing = con.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                raise ApiError(409, "Email already in use")
            con.execute(
                "INSERT INTO users (id, email, password_hash, password_salt, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, email, password_hash, salt, now),
            )
            con.commit()
        finally:
            con.close()

        return {"user_id": user_id, "email": email, "created_at": now}

    def _create_user_session(self, user_id: str) -> str:
        token = "lls_" + secrets.token_urlsafe(24)
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        expires_at = session_expires_at(now_dt)
        con = get_connection()
        try:
            con.execute(
                "INSERT INTO user_sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, user_id, now, expires_at),
            )
            con.commit()
        finally:
            con.close()
        return token

    def _login(self, payload: dict[str, Any]) -> dict[str, Any]:
        email = normalize_email(payload.get("email", ""))
        password = payload.get("password", "")
        if not email or not password:
            raise ApiError(400, "email and password are required")

        con = get_connection()
        try:
            user = con.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if user is None:
                raise ApiError(401, "Invalid email or password")
            expected = hash_password(password, user["password_salt"])
            if not hmac.compare_digest(expected, user["password_hash"]):
                raise ApiError(401, "Invalid email or password")
            agent = con.execute(
                """
                SELECT id, display_name, dating_user_id, dating_api_key, dating_base_url
                FROM agents
                WHERE user_id = ?
                """,
                (user["id"],),
            ).fetchone()
        finally:
            con.close()

        token = self._create_user_session(user["id"])
        return {
            "session_token": token,
            "user": {
                "email": user["email"],
                "has_profile": bool(agent),
                "profile_id": agent["id"] if agent else None,
                "display_name": agent["display_name"] if agent else None,
                "integration": self._integration_summary(agent),
            },
        }

    def _auth_me(self, user: sqlite3.Row) -> dict[str, Any]:
        con = get_connection()
        try:
            agent = con.execute(
                """
                SELECT id, display_name, dating_user_id, dating_api_key, dating_base_url
                FROM agents
                WHERE user_id = ?
                """,
                (user["id"],),
            ).fetchone()
        finally:
            con.close()

        return {
            "user": {
                "email": user["email"],
                "has_profile": bool(agent),
                "profile_id": agent["id"] if agent else None,
                "display_name": agent["display_name"] if agent else None,
                "integration": self._integration_summary(agent),
            }
        }

    def _logout(self, user: sqlite3.Row) -> dict[str, Any]:
        con = get_connection()
        try:
            con.execute("DELETE FROM user_sessions WHERE token = ?", (user["session_token"],))
            con.commit()
        finally:
            con.close()
        return {"ok": True}

    def _register_agent(self, payload: dict[str, Any], auth_token: str = "") -> dict[str, Any]:
        (
            display_name,
            age,
            gender,
            city,
            country,
            appearance,
            height_cm,
            body_type,
            personality,
            interests,
            values,
            preferences,
            contact,
            ai_profile,
        ) = self._parse_dating_profile_payload(payload)

        user_id = None
        if auth_token:
            con = get_connection()
            try:
                user = con.execute("SELECT user_id FROM user_sessions WHERE token = ?", (auth_token,)).fetchone()
                if user is not None:
                    existing = con.execute("SELECT id FROM agents WHERE user_id = ?", (user["user_id"],)).fetchone()
                    if existing is not None:
                        raise ApiError(409, "Profile already exists for this account")
                    user_id = user["user_id"]
            finally:
                con.close()

        agent_id = str(uuid.uuid4())
        api_key = "llk_" + secrets.token_urlsafe(24)
        now = utc_now()
        sync_remote = payload.get("sync_remote", True)
        if not isinstance(sync_remote, bool):
            raise ApiError(400, "sync_remote must be a boolean")
        if sync_remote:
            dating_credentials = self._register_with_datingopenclaw(display_name, ai_profile, contact)
        else:
            dating_credentials = {
                "dating_api_key": api_key,
                "dating_user_id": agent_id,
                "dating_registered_at": now,
                "dating_base_url": f"http://{HOST}:{PORT}/api",
            }

        con = get_connection()
        try:
            con.execute(
                """
                INSERT INTO agents (
                    id, user_id, api_key, display_name, age, gender, city, country,
                    appearance, height_cm, body_type, personality,
                    interests_json, values_json, preferences_json, contact_json, ai_profile_json,
                    dating_user_id, dating_api_key, dating_base_url, dating_registered_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    user_id,
                    api_key,
                    display_name,
                    age,
                    gender,
                    city,
                    country,
                    appearance,
                    height_cm,
                    body_type,
                    personality,
                    json_dumps(interests),
                    json_dumps(values),
                    json_dumps(preferences),
                    json_dumps(contact),
                    json_dumps(ai_profile),
                    dating_credentials["dating_user_id"],
                    dating_credentials["dating_api_key"],
                    dating_credentials["dating_base_url"],
                    dating_credentials["dating_registered_at"],
                    now,
                ),
            )
            con.commit()
        finally:
            con.close()

        if user_id:
            return {
                "profile_id": agent_id,
                "registered_at": now,
                "datingopenclaw": {
                    "connected": True,
                    "base_url": dating_credentials["dating_base_url"],
                    "user_id": dating_credentials["dating_user_id"],
                },
            }

        return {
            "agent_id": agent_id,
            "api_key": api_key,
            "base_url": f"http://{HOST}:{PORT}/api",
            "registered_at": now,
            "datingopenclaw": {
                "api_key": dating_credentials["dating_api_key"],
                "base_url": dating_credentials["dating_base_url"],
                "user_id": dating_credentials["dating_user_id"],
                "registered_at": dating_credentials["dating_registered_at"],
            },
        }

    def _get_my_profile(self, agent: sqlite3.Row) -> dict[str, Any]:
        ai_profile = self._ai_profile_from_row(agent)
        return {
            "profile": {
                "id": agent["id"],
                "display_name": agent["display_name"],
                "profile": ai_profile,
                "contact": parse_json_field(agent["contact_json"]) or {},
                "integration": self._integration_summary(agent),
                "created_at": agent["created_at"],
            }
        }

    def _update_my_profile(self, agent: sqlite3.Row, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ApiError(400, "body must be an object")

        existing_profile = self._ai_profile_from_row(agent)
        existing_contact = parse_json_field(agent["contact_json"]) or {}
        display_name = clean_text(payload.get("display_name")) or agent["display_name"]

        profile_patch = payload.get("profile", {})
        if profile_patch and not isinstance(profile_patch, dict):
            raise ApiError(400, "profile must be an object")
        contact_patch = payload.get("contact", {})
        if contact_patch and not isinstance(contact_patch, dict):
            raise ApiError(400, "contact must be an object")

        merged_profile = {**existing_profile, **(profile_patch or {})}
        merged_contact = {**existing_contact, **(contact_patch or {})}
        normalized_payload = {
            "display_name": display_name,
            "profile": merged_profile,
            "contact": merged_contact,
        }
        (
            final_display_name,
            age,
            gender,
            city,
            country,
            appearance,
            height_cm,
            body_type,
            personality,
            interests,
            values,
            preferences,
            contact,
            ai_profile,
        ) = self._parse_dating_profile_payload(normalized_payload)

        con = get_connection()
        try:
            con.execute(
                """
                UPDATE agents
                SET display_name = ?, age = ?, gender = ?, city = ?, country = ?, appearance = ?, height_cm = ?,
                    body_type = ?, personality = ?, interests_json = ?, values_json = ?, preferences_json = ?,
                    contact_json = ?, ai_profile_json = ?
                WHERE id = ?
                """,
                (
                    final_display_name,
                    age,
                    gender,
                    city,
                    country,
                    appearance,
                    height_cm,
                    body_type,
                    personality,
                    json_dumps(interests),
                    json_dumps(values),
                    json_dumps(preferences),
                    json_dumps(contact),
                    json_dumps(ai_profile),
                    agent["id"],
                ),
            )
            con.commit()
            fresh = con.execute("SELECT * FROM agents WHERE id = ?", (agent["id"],)).fetchone()
        finally:
            con.close()

        return {
            "profile": {
                "id": fresh["id"],
                "display_name": fresh["display_name"],
                "profile": self._ai_profile_from_row(fresh),
                "contact": parse_json_field(fresh["contact_json"]) or {},
                "integration": self._integration_summary(fresh),
                "created_at": fresh["created_at"],
            }
        }

    def _list_profiles(self, requester: sqlite3.Row, params: dict[str, list[str]]) -> dict[str, Any]:
        try:
            page = max(1, int(params.get("page", ["1"])[0]))
            limit = min(100, max(1, int(params.get("limit", ["50"])[0])))
            min_age_raw = params.get("min_age", params.get("age_min", ["18"]))[0]
            max_age_raw = params.get("max_age", params.get("age_max", ["120"]))[0]
            min_age = int(min_age_raw)
            max_age = int(max_age_raw)
        except ValueError as exc:
            raise ApiError(400, "Invalid pagination or age filter values") from exc

        if min_age > max_age:
            min_age, max_age = max_age, min_age

        filter_gender = clean_text(params.get("gender", [None])[0]) or None
        filter_city = clean_text(params.get("city", [None])[0]) or None
        filter_country = clean_text(params.get("country", [None])[0]) or None
        source_preference = clean_text(params.get("source", [None])[0]).lower()
        force_local_source = source_preference == "local"

        # Replica of /dating search: prefer remote DatingOpenClaw profile search.
        remote_api_key = clean_text(requester["dating_api_key"])
        remote_base_url = clean_text(requester["dating_base_url"]) or DATINGOPENCLAW_BASE_URL
        parsed_remote = urlparse(remote_base_url)
        is_local_base = (
            parsed_remote.hostname in {"127.0.0.1", "localhost"}
            and (parsed_remote.port in {None, PORT})
            and parsed_remote.path.rstrip("/") == "/api"
        )
        remote_error: str | None = None
        if not force_local_source and remote_api_key and not is_local_base:
            try:
                client = DatingOpenClawClient(base_url=remote_base_url, api_key=remote_api_key)
                out = client.list_profiles(
                    gender=filter_gender,
                    age_min=min_age,
                    age_max=max_age,
                    city=filter_city,
                    country=filter_country,
                    page=page,
                    limit=limit,
                )
                remote_profiles_raw = out.get("profiles", [])
                remote_self_id = clean_text(requester["dating_user_id"]) or clean_text(requester["id"])
                normalized_profiles: list[dict[str, Any]] = []
                for raw_profile in remote_profiles_raw:
                    if not isinstance(raw_profile, dict):
                        continue
                    profile_id = clean_text(raw_profile.get("id"))
                    if profile_id and remote_self_id and profile_id == remote_self_id:
                        continue

                    nested_profile = raw_profile.get("profile")
                    profile_blob = nested_profile if isinstance(nested_profile, dict) else {}
                    raw_preferences = raw_profile.get("preferences")
                    preferences = raw_preferences if isinstance(raw_preferences, dict) else {}

                    raw_interests = raw_profile.get("interests")
                    if isinstance(raw_interests, list):
                        interests = [clean_text(v) for v in raw_interests if clean_text(v)]
                    else:
                        profile_interests = profile_blob.get("interests", [])
                        interests = [clean_text(v) for v in profile_interests if clean_text(v)] if isinstance(profile_interests, list) else []

                    raw_values = raw_profile.get("values")
                    if isinstance(raw_values, list):
                        values = [clean_text(v) for v in raw_values if clean_text(v)]
                    else:
                        profile_values = profile_blob.get("values", [])
                        values = [clean_text(v) for v in profile_values if clean_text(v)] if isinstance(profile_values, list) else []

                    normalized_profiles.append(
                        {
                            "id": profile_id or clean_text(raw_profile.get("user_id")),
                            "display_name": clean_text(raw_profile.get("display_name")) or "Unknown",
                            "source": "datingopenclaw",
                            "age": raw_profile.get("age", profile_blob.get("age")),
                            "gender": clean_text(raw_profile.get("gender")) or clean_text(profile_blob.get("gender")),
                            "city": clean_text(raw_profile.get("city")) or clean_text(profile_blob.get("location_city")),
                            "country": clean_text(raw_profile.get("country")) or clean_text(profile_blob.get("location_country")),
                            "appearance": clean_text(raw_profile.get("appearance")) or clean_text(profile_blob.get("appearance_summary")),
                            "height_cm": raw_profile.get("height_cm", profile_blob.get("height_cm")),
                            "body_type": clean_text(raw_profile.get("body_type")) or clean_text(profile_blob.get("body_type")),
                            "personality": clean_text(raw_profile.get("personality")) or clean_text(profile_blob.get("personality_summary")),
                            "interests": interests,
                            "values": values,
                            "preferences": preferences,
                            "profile": profile_blob,
                            "created_at": clean_text(raw_profile.get("created_at")),
                        }
                    )

                total = out.get("total")
                if not isinstance(total, int) or total < 0:
                    total = len(normalized_profiles)
                pages = out.get("pages")
                if not isinstance(pages, int) or pages < 0:
                    pages = (total + limit - 1) // limit if total > 0 else 0

                return {
                    "page": page,
                    "limit": limit,
                    "total": total,
                    "pages": pages,
                    "profiles": normalized_profiles,
                    "source": "datingopenclaw",
                }
            except Exception as exc:
                remote_error = str(exc)
                print(f"[WARN] Remote profile search failed; falling back to local store: {remote_error}", file=sys.stderr)

        clauses = ["id != ?", "age BETWEEN ? AND ?"]
        values: list[Any] = [requester["id"], min_age, max_age]
        for key, value in [("gender", filter_gender), ("city", filter_city), ("country", filter_country)]:
            if value:
                clauses.append(f"LOWER({key}) = LOWER(?)")
                values.append(value)

        where_sql = " AND ".join(clauses)
        offset = (page - 1) * limit
        con = get_connection()
        try:
            rows = con.execute(
                f"SELECT * FROM agents WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
            total = con.execute(
                f"SELECT COUNT(1) AS c FROM agents WHERE {where_sql}",
                tuple(values),
            ).fetchone()["c"]
        finally:
            con.close()

        out = {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit if total > 0 else 0,
            "profiles": [agent_public_profile(row) for row in rows],
            "source": "local",
        }
        if remote_error:
            out["warning"] = "Remote profile source unavailable. Returned local profiles instead."
        return out

    def _create_negotiation(self, requester: sqlite3.Row, payload: dict[str, Any]) -> dict[str, Any]:
        target_id = payload.get("target_id")
        intro_message = payload.get("intro_message", "").strip()
        if not target_id or not intro_message:
            raise ApiError(400, "target_id and intro_message are required")
        if target_id == requester["id"]:
            raise ApiError(400, "Cannot negotiate with yourself")

        con = get_connection()
        try:
            target = con.execute("SELECT id FROM agents WHERE id = ?", (target_id,)).fetchone()
            if target is None:
                raise ApiError(404, "Target profile not found")

            open_existing = con.execute(
                """
                SELECT id FROM negotiations
                WHERE ((initiator_id = ? AND target_id = ?) OR (initiator_id = ? AND target_id = ?))
                AND status IN ('requested','accepted','talking')
                LIMIT 1
                """,
                (requester["id"], target_id, target_id, requester["id"]),
            ).fetchone()
            if open_existing:
                raise ApiError(409, "An active negotiation already exists between these agents")

            now = utc_now()
            negotiation_id = str(uuid.uuid4())
            con.execute(
                """
                INSERT INTO negotiations (
                    id, initiator_id, target_id, intro_message, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'requested', ?, ?)
                """,
                (negotiation_id, requester["id"], target_id, intro_message, now, now),
            )
            con.execute(
                """
                INSERT INTO messages (negotiation_id, sender_id, body, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (negotiation_id, requester["id"], intro_message, now),
            )
            con.commit()
        finally:
            con.close()

        return {
            "negotiation_id": negotiation_id,
            "status": "requested",
            "created_at": now,
        }

    def _list_negotiations(self, requester: sqlite3.Row, params: dict[str, list[str]]) -> dict[str, Any]:
        view_type = params.get("type", ["all"])[0]
        status_csv = params.get("status", [""])[0]
        try:
            page = max(1, int(params.get("page", ["1"])[0]))
            limit = min(100, max(1, int(params.get("limit", ["20"])[0])))
        except ValueError as exc:
            raise ApiError(400, "Invalid pagination values") from exc

        clauses: list[str] = []
        values: list[Any] = []

        if view_type == "incoming":
            clauses.append("target_id = ?")
            values.append(requester["id"])
        elif view_type == "outgoing":
            clauses.append("initiator_id = ?")
            values.append(requester["id"])
        else:
            clauses.append("(initiator_id = ? OR target_id = ?)")
            values.extend([requester["id"], requester["id"]])

        statuses = [s.strip() for s in status_csv.split(",") if s.strip()]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            values.extend(statuses)

        where_sql = " AND ".join(clauses) if clauses else "1=1"
        offset = (page - 1) * limit

        con = get_connection()
        try:
            total = con.execute(
                f"SELECT COUNT(1) AS c FROM negotiations WHERE {where_sql}",
                tuple(values),
            ).fetchone()["c"]
            rows = con.execute(
                f"SELECT * FROM negotiations WHERE {where_sql} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
        finally:
            con.close()

        profile_map = load_public_profiles_by_id(
            [row["initiator_id"] for row in rows] + [row["target_id"] for row in rows]
        )
        negotiations: list[dict[str, Any]] = []
        for row in rows:
            negotiations.append(
                {
                    **negotiation_summary(row),
                    "intro_message": row["intro_message"],
                    "initiator_profile": profile_map.get(row["initiator_id"]),
                    "target_profile": profile_map.get(row["target_id"]),
                }
            )

        return {
            "negotiations": negotiations,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit if total > 0 else 0,
        }

    def _load_negotiation_owned(self, requester: sqlite3.Row, negotiation_id: str) -> sqlite3.Row:
        con = get_connection()
        try:
            row = con.execute("SELECT * FROM negotiations WHERE id = ?", (negotiation_id,)).fetchone()
            if row is None:
                raise ApiError(404, "Negotiation not found")
            if requester["id"] not in (row["initiator_id"], row["target_id"]):
                raise ApiError(403, "Not authorized for this negotiation")
            return row
        finally:
            con.close()

    def _get_negotiation(self, requester: sqlite3.Row, negotiation_id: str) -> dict[str, Any]:
        row = self._load_negotiation_owned(requester, negotiation_id)
        profile_map = load_public_profiles_by_id([row["initiator_id"], row["target_id"]])
        return {
            **negotiation_summary(row),
            "intro_message": row["intro_message"],
            "initiator_profile": profile_map.get(row["initiator_id"]),
            "target_profile": profile_map.get(row["target_id"]),
            "initiator_decision": row["initiator_decision"],
            "target_decision": row["target_decision"],
            "initiator_reason": row["initiator_reason"],
            "target_reason": row["target_reason"],
        }

    def _accept_reject(self, requester: sqlite3.Row, negotiation_id: str, accept: bool) -> dict[str, Any]:
        con = get_connection()
        try:
            row = con.execute("SELECT * FROM negotiations WHERE id = ?", (negotiation_id,)).fetchone()
            if row is None:
                raise ApiError(404, "Negotiation not found")
            if row["target_id"] != requester["id"]:
                raise ApiError(403, "Only the target agent can accept/reject")
            if row["status"] != "requested":
                raise ApiError(409, "Only requested negotiations can be accepted/rejected")

            new_status = "accepted" if accept else "rejected"
            now = utc_now()
            con.execute(
                "UPDATE negotiations SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, now, negotiation_id),
            )
            con.commit()
        finally:
            con.close()

        return {"negotiation_id": negotiation_id, "status": new_status, "updated_at": now}

    def _list_messages(self, requester: sqlite3.Row, negotiation_id: str, params: dict[str, list[str]]) -> dict[str, Any]:
        self._load_negotiation_owned(requester, negotiation_id)

        after = params.get("after", [None])[0]
        clause = "WHERE negotiation_id = ?"
        values: list[Any] = [negotiation_id]

        if after:
            try:
                _ = parse_iso(after)
            except ValueError as exc:
                raise ApiError(400, "after must be an ISO timestamp") from exc
            clause += " AND created_at > ?"
            values.append(after)

        con = get_connection()
        try:
            rows = con.execute(
                f"SELECT id, sender_id, body, created_at FROM messages {clause} ORDER BY created_at ASC, id ASC",
                tuple(values),
            ).fetchall()
        finally:
            con.close()

        return {
            "messages": [
                {
                    "id": row["id"],
                    "sender_id": row["sender_id"],
                    "body": row["body"],
                    "content": row["body"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }

    def _create_message(self, requester: sqlite3.Row, negotiation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = clean_text(payload.get("message") or payload.get("content"))
        if not body:
            raise ApiError(400, "message/content is required")

        con = get_connection()
        try:
            negotiation = con.execute("SELECT * FROM negotiations WHERE id = ?", (negotiation_id,)).fetchone()
            if negotiation is None:
                raise ApiError(404, "Negotiation not found")
            if requester["id"] not in (negotiation["initiator_id"], negotiation["target_id"]):
                raise ApiError(403, "Not authorized for this negotiation")
            if negotiation["status"] not in ("accepted", "talking"):
                raise ApiError(409, "Messages can only be sent after acceptance")

            now = utc_now()
            con.execute(
                "INSERT INTO messages (negotiation_id, sender_id, body, created_at) VALUES (?, ?, ?, ?)",
                (negotiation_id, requester["id"], body, now),
            )
            con.execute(
                "UPDATE negotiations SET status = 'talking', updated_at = ? WHERE id = ?",
                (now, negotiation_id),
            )
            con.commit()
        finally:
            con.close()

        return {
            "negotiation_id": negotiation_id,
            "sent_at": now,
            "message": {
                "sender_id": requester["id"],
                "content": body,
                "created_at": now,
            },
        }

    def _submit_result(self, requester: sqlite3.Row, negotiation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        decision = payload.get("decision")
        reason = (payload.get("reason") or "").strip()
        if decision not in ("match", "no_match"):
            raise ApiError(400, "decision must be 'match' or 'no_match'")
        reason_or_none = reason or None

        con = get_connection()
        try:
            row = con.execute("SELECT * FROM negotiations WHERE id = ?", (negotiation_id,)).fetchone()
            if row is None:
                raise ApiError(404, "Negotiation not found")
            if requester["id"] not in (row["initiator_id"], row["target_id"]):
                raise ApiError(403, "Not authorized for this negotiation")
            if row["status"] not in ("accepted", "talking"):
                raise ApiError(409, "Can only submit results on active negotiations")

            now = utc_now()

            if requester["id"] == row["initiator_id"]:
                initiator_decision = decision
                target_decision = row["target_decision"]
                con.execute(
                    """
                    UPDATE negotiations
                    SET initiator_decision = ?, initiator_reason = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (decision, reason_or_none, now, negotiation_id),
                )
            else:
                initiator_decision = row["initiator_decision"]
                target_decision = decision
                con.execute(
                    """
                    UPDATE negotiations
                    SET target_decision = ?, target_reason = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (decision, reason_or_none, now, negotiation_id),
                )

            final_status = None
            if initiator_decision == "no_match" or target_decision == "no_match":
                final_status = "no_match"
            elif initiator_decision == "match" and target_decision == "match":
                final_status = "match"

            if final_status:
                con.execute(
                    "UPDATE negotiations SET status = ?, updated_at = ? WHERE id = ?",
                    (final_status, now, negotiation_id),
                )

            con.commit()
        finally:
            con.close()

        return {
            "negotiation_id": negotiation_id,
            "status": final_status or "pending_other_side",
            "mutual": final_status == "match",
            "updated_at": now,
        }

    def _store_chat_message(self, user_id: str, role: str, content: str) -> None:
        con = get_connection()
        try:
            con.execute(
                """
                INSERT INTO agent_chat_messages (user_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, role, content, utc_now()),
            )
            con.commit()
        finally:
            con.close()

    def _agent_chat_history(self, user: sqlite3.Row) -> dict[str, Any]:
        limit = DEFAULT_CHAT_HISTORY_LIMIT
        con = get_connection()
        try:
            rows = con.execute(
                """
                SELECT id, role, content, created_at
                FROM agent_chat_messages
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user["id"], limit),
            ).fetchall()
        finally:
            con.close()
        messages = [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
            for row in reversed(rows)
        ]
        return {"messages": messages}

    def _clear_agent_chat_history(self, user: sqlite3.Row) -> dict[str, Any]:
        con = get_connection()
        try:
            cur = con.execute(
                "DELETE FROM agent_chat_messages WHERE user_id = ?",
                (user["id"],),
            )
            con.commit()
        finally:
            con.close()
        return {"ok": True, "deleted": cur.rowcount}

    def _parse_command_args(self, message: str) -> dict[str, str]:
        args: dict[str, str] = {}
        for match in re.finditer(r"([a-zA-Z_]+)=([^\s]+)", message):
            args[match.group(1)] = match.group(2)
        return args

    def _agent_reply(self, user: sqlite3.Row, message: str) -> str:
        con = get_connection()
        try:
            agent = con.execute("SELECT * FROM agents WHERE user_id = ?", (user["id"],)).fetchone()
        finally:
            con.close()
        if agent is None:
            raise ApiError(409, "Complete your profile first")

        integration = self._integration_summary(agent)["datingopenclaw"]
        if not integration["connected"]:
            raise ApiError(409, "DatingOpenClaw API key missing. Re-create your profile to sync credentials.")

        client = DatingOpenClawClient(
            base_url=integration["base_url"],
            api_key=agent["dating_api_key"],
        )
        lower = message.lower().strip()
        try:
            if lower in {"/dating", "/dating help", "help"}:
                return (
                    "Try: /dating docs, /dating me, "
                    "/dating search gender=woman age_min=24 age_max=32 city=Austin, "
                    "/dating inbox, /dating convos"
                )
            if lower.startswith("/dating docs"):
                docs = client.get_docs()
                endpoints = docs.get("endpoints", [])
                return f"Loaded docs. Available endpoint count: {len(endpoints)}."
            if lower.startswith("/dating me"):
                profile = client.get_my_profile()
                return json.dumps(profile, indent=2)
            if lower.startswith("/dating search"):
                args = self._parse_command_args(message)
                out = client.list_profiles(
                    gender=args.get("gender"),
                    age_min=int(args["age_min"]) if "age_min" in args else None,
                    age_max=int(args["age_max"]) if "age_max" in args else None,
                    city=args.get("city"),
                    country=args.get("country"),
                    page=int(args.get("page", "1")),
                    limit=int(args.get("limit", "10")),
                )
                profiles = out.get("profiles", [])[:10]
                shortlist = [
                    f"- {p.get('display_name', 'Unknown')} ({p.get('age', '?')}, {p.get('city', '')})"
                    for p in profiles
                ]
                if not shortlist:
                    return "No profiles found for that filter."
                return "Top profiles:\n" + "\n".join(shortlist)
            if lower.startswith("/dating inbox"):
                out = client.list_negotiations(type="incoming", status="requested", page=1, limit=20)
                return f"Incoming requested negotiations: {len(out.get('negotiations', []))}"
            if lower.startswith("/dating convos"):
                out = client.list_negotiations(type="all", status="accepted,talking,match,no_match", page=1, limit=20)
                return f"Active/closed conversations found: {len(out.get('negotiations', []))}"
        except ValueError as exc:
            raise ApiError(400, f"Invalid command argument: {exc}") from exc
        except Exception as exc:
            raise ApiError(502, f"Dating API request failed: {exc}") from exc

        return (
            "I can execute dating actions. Use /dating help, then run commands like "
            "/dating search gender=woman age_min=24 age_max=32."
        )

    def _agent_chat(self, user: sqlite3.Row, payload: dict[str, Any]) -> dict[str, Any]:
        message = clean_text(payload.get("message"))
        if not message:
            raise ApiError(400, "message is required")

        self._store_chat_message(user["id"], "user", message)
        reply = self._agent_reply(user, message)
        self._store_chat_message(user["id"], "assistant", reply)

        return {
            "reply": reply,
            "message": {
                "role": "assistant",
                "content": reply,
            },
        }

    def _get_contact(self, requester: sqlite3.Row, negotiation_id: str) -> dict[str, Any]:
        con = get_connection()
        try:
            row = con.execute("SELECT * FROM negotiations WHERE id = ?", (negotiation_id,)).fetchone()
            if row is None:
                raise ApiError(404, "Negotiation not found")
            if requester["id"] not in (row["initiator_id"], row["target_id"]):
                raise ApiError(403, "Not authorized for this negotiation")
            if row["status"] != "match":
                raise ApiError(409, "Contacts are available only after mutual match")

            other_id = row["target_id"] if requester["id"] == row["initiator_id"] else row["initiator_id"]
            other = con.execute("SELECT id, display_name, contact_json FROM agents WHERE id = ?", (other_id,)).fetchone()
        finally:
            con.close()

        return {
            "negotiation_id": negotiation_id,
            "matched_agent": {
                "id": other["id"],
                "display_name": other["display_name"],
                "contact": parse_json_field(other["contact_json"]),
            },
        }


def run_server() -> None:
    ensure_db()
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    server.daemon_threads = True
    print(f"LobsterLink API running on http://{HOST}:{PORT} (db={DB_PATH})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
