"""Open the Operation Workbench in process: its settings, store, vault and
`WorkbenchService`, started without workers (nothing here claims a task)."""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WORKBENCH = Path(os.environ.get("OPERATION_WORKBENCH_DIR", r"C:\Users\itsme\git\operation-workbench"))

#: The URLs the Workbench may open: both platforms' APIs and MCP servers, the
#: plan directory's MCP server, and TRACE's (Platform A's screens). The
#: Workbench's .env lists them too, so its running API agrees.
ALLOWLIST = (
    "http://127.0.0.1:8201/,http://127.0.0.1:8202/,http://127.0.0.1:8211/,http://127.0.0.1:8212/,"
    "http://127.0.0.1:8213/,http://127.0.0.1:8712/"
)


@dataclass
class Actor:
    """Who the writes are recorded as: the workspace's first administrator."""

    user_id: str
    email: str
    workspace_id: str


@asynccontextmanager
async def workbench():
    sys.path.insert(0, str(WORKBENCH / "backend"))
    os.environ["CONNECTOR_HTTP_ALLOWLIST"] = ALLOWLIST
    # Nothing here calls a model, so the provider only has to load; this
    # process's environment has no Claude API key, and must not need one.
    os.environ["LLM_PROVIDER"] = "bedrock"
    os.environ["LANGSMITH_TRACING"] = "false"

    from ai.llm import ModelPool
    from infra.config import get_settings
    from infra.credentials import Vault
    from infra.storage import build_storage
    from infra.store import Store
    from worker.workbench.service import WorkbenchService

    settings = get_settings()
    store = Store(settings)
    await store.connect()
    service = WorkbenchService(
        settings, store, vault=Vault.from_settings(settings), models=ModelPool(settings), storage=build_storage(settings)
    )
    await service.start(workers=False)
    try:
        yield service, await _admin(store, settings)
    finally:
        await service.stop()
        await store.close()


async def _admin(store: Any, settings: Any) -> Actor:
    from sqlalchemy import text

    async with store.sessions() as db:
        row = (
            await db.execute(
                text(
                    f'select id, email, workspace_id from "{settings.db_schema}".users '
                    "where role = 'admin' order by created_at limit 1"
                )
            )
        ).first()
    if row is None:
        raise SystemExit("The Workbench has no administrator yet; start its API once so it creates one.")
    return Actor(user_id=row.id, email=row.email, workspace_id=row.workspace_id)
