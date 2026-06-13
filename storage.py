"""Azure Table Storage helpers for users and games.

Uses DefaultAzureCredential in production (Managed Identity on App Service)
and the Azurite dev connection string when AZURE_STORAGE_USE_AZURITE=true.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential


AZURITE_CONN_STRING = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"
)

LEVELS = {"beginner", "intermediate", "expert", "wizard"}
LEVEL_PAIRS = {"beginner": 18, "intermediate": 32, "expert": 50, "wizard": 72}
MAX_INT64 = (1 << 63) - 1


def _service() -> TableServiceClient:
    if os.environ.get("AZURE_STORAGE_USE_AZURITE") == "true":
        return TableServiceClient.from_connection_string(AZURITE_CONN_STRING)
    endpoint = os.environ.get("AZURE_STORAGE_TABLE_ENDPOINT")
    if not endpoint:
        raise RuntimeError("AZURE_STORAGE_TABLE_ENDPOINT is not set")
    return TableServiceClient(endpoint=endpoint, credential=DefaultAzureCredential())


def ensure_tables() -> None:
    svc = _service()
    svc.create_table_if_not_exists("users")
    svc.create_table_if_not_exists("games")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _user_partition(email_norm: str) -> str:
    return hashlib.sha1(email_norm.encode("utf-8")).hexdigest()[:2]


def _game_row_key(completed_at_ms: int) -> str:
    return f"{MAX_INT64 - completed_at_ms:020d}_{uuid.uuid4().hex}"


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- users ---

@dataclass
class User:
    email: str
    display_name: str
    providers: list[dict] = field(default_factory=list)
    created_at: str = ""
    last_login: str = ""

    @classmethod
    def from_entity(cls, e: dict) -> "User":
        providers_raw = e.get("providers") or "[]"
        try:
            providers = json.loads(providers_raw)
        except (ValueError, TypeError):
            providers = []
        return cls(
            email=e["RowKey"],
            display_name=e.get("display_name", ""),
            providers=providers,
            created_at=e.get("created_at", ""),
            last_login=e.get("last_login", ""),
        )


def get_user(email: str) -> User | None:
    email_norm = normalize_email(email)
    tbl = _service().get_table_client("users")
    try:
        entity = tbl.get_entity(_user_partition(email_norm), email_norm)
    except ResourceNotFoundError:
        return None
    return User.from_entity(entity)


def upsert_user_login(email: str, display_name: str, provider: str, subject: str) -> tuple[User, bool]:
    """Create or update a user on OAuth login.

    Returns (user, is_new) where is_new is True if this is the user's first login.
    """
    email_norm = normalize_email(email)
    tbl = _service().get_table_client("users")
    now = _iso_now()
    existing = get_user(email_norm)
    is_new = existing is None

    if existing:
        providers = existing.providers
        if not any(p.get("provider") == provider and p.get("subject") == subject for p in providers):
            providers.append({"provider": provider, "subject": subject})
        entity = {
            "PartitionKey": _user_partition(email_norm),
            "RowKey": email_norm,
            "display_name": display_name or existing.display_name,
            "providers": json.dumps(providers),
            "created_at": existing.created_at or now,
            "last_login": now,
        }
    else:
        entity = {
            "PartitionKey": _user_partition(email_norm),
            "RowKey": email_norm,
            "display_name": display_name or email_norm.split("@")[0],
            "providers": json.dumps([{"provider": provider, "subject": subject}]),
            "created_at": now,
            "last_login": now,
        }
    tbl.upsert_entity(entity)
    return User.from_entity(entity), is_new


# --- games ---

def record_game(
    email: str,
    *,
    level: str,
    moves: int,
    duration_ms: int,
    completed_at: str | None = None,
    client_version: str = "",
) -> dict:
    if level not in LEVELS:
        raise ValueError(f"invalid level: {level!r}")
    if moves < LEVEL_PAIRS[level]:
        raise ValueError(f"moves ({moves}) below the minimum for {level}")
    if duration_ms < 500:
        raise ValueError("duration_ms too low")
    email_norm = normalize_email(email)
    completed_at = completed_at or _iso_now()
    try:
        completed_at_ms = int(
            datetime.fromisoformat(completed_at.replace("Z", "+00:00")).timestamp() * 1000
        )
    except ValueError:
        completed_at_ms = int(time.time() * 1000)
    entity = {
        "PartitionKey": email_norm,
        "RowKey": _game_row_key(completed_at_ms),
        "level": level,
        "pairs": LEVEL_PAIRS[level],
        "moves": int(moves),
        "duration_ms": int(duration_ms),
        "completed_at": completed_at,
        "client_version": client_version,
    }
    tbl = _service().get_table_client("games")
    tbl.create_entity(entity)
    return entity


def list_games(email: str, limit: int = 20) -> list[dict]:
    email_norm = normalize_email(email)
    tbl = _service().get_table_client("games")
    rows = tbl.query_entities(
        query_filter="PartitionKey eq @pk",
        parameters={"pk": email_norm},
        results_per_page=limit,
    )
    out: list[dict] = []
    for row in rows:
        out.append({
            "level": row.get("level"),
            "moves": row.get("moves"),
            "duration_ms": row.get("duration_ms"),
            "completed_at": row.get("completed_at"),
        })
        if len(out) >= limit:
            break
    return out


def compute_stats(email: str) -> dict:
    """Return {level: {games, best_moves, best_duration_ms, total_moves, total_duration_ms}}."""
    email_norm = normalize_email(email)
    tbl = _service().get_table_client("games")
    stats = {lvl: {"games": 0, "best_moves": None, "best_duration_ms": None,
                   "total_moves": 0, "total_duration_ms": 0} for lvl in LEVELS}
    for row in tbl.query_entities(
        query_filter="PartitionKey eq @pk",
        parameters={"pk": email_norm},
    ):
        lvl = row.get("level")
        if lvl not in stats:
            continue
        s = stats[lvl]
        moves = int(row.get("moves", 0))
        dur = int(row.get("duration_ms", 0))
        s["games"] += 1
        s["total_moves"] += moves
        s["total_duration_ms"] += dur
        if s["best_moves"] is None or moves < s["best_moves"]:
            s["best_moves"] = moves
        if s["best_duration_ms"] is None or dur < s["best_duration_ms"]:
            s["best_duration_ms"] = dur
    return stats


def import_guest_games(email: str, games: Iterable[dict]) -> int:
    """Import an iterable of guest game records. Silently skips invalid rows."""
    n = 0
    for g in games:
        try:
            record_game(
                email,
                level=g["level"],
                moves=int(g["moves"]),
                duration_ms=int(g["duration_ms"]),
                completed_at=g.get("completed_at"),
                client_version=g.get("client_version", "guest-import"),
            )
            n += 1
        except (ValueError, KeyError, TypeError):
            continue
    return n
