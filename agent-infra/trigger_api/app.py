"""FastAPI application — agent trigger API.

Startup
-------
::

    uvicorn trigger_api.app:app --host 0.0.0.0 --port 8000

Environment variables (all optional except DISCORD_BOT_TOKEN when polling)
---------------------------------------------------------------------------
DISCORD_BOT_TOKEN          Bot token — required for Discord polling
DISCORD_GUILD_NAME         Default: arigato-mr-roboto
DISCORD_ADO_CHANNEL        ADO work-item channel      (default: trigger-agents-ado-work)
DISCORD_GENERAL_CHANNEL    General-purpose channel     (default: trigger-agents-general)
DISCORD_TRIGGER_CHANNEL    Deprecated alias for DISCORD_ADO_CHANNEL
DISCORD_POLL_SECONDS       Default: 10
ADO_WEBHOOK_SECRET         Shared secret for Azure DevOps service hooks (Basic auth password)
DEFAULT_REPO               Absolute path used when a trigger omits --repo
RUNNER_SCRIPT              Explicit path to run_headless.py
GENERAL_RUNNER_SCRIPT      Explicit path to run_general.py
BACKEND                    copilot | claude (auto-detected if unset)
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse

from .actions.run_workflow import RunWorkflowHandler
from .actions.general_run import GeneralRunHandler
from .adapters.azure_devops import parse_ado_webhook, verify_basic_auth
from .adapters.discord import DiscordPollerAdapter
from .models import (
    CancelResponse,
    HealthResponse,
    RunRecord,
    TriggerEvent,
)
from .run_store import RunStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_default_repo(hint: str | None) -> str:
    if hint:
        return str(Path(hint).resolve())
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return str(Path(__file__).resolve().parent.parent)


def _find_runner_script(default_repo: str) -> Path:
    candidate = Path(default_repo) / "agent-runner" / "run_headless.py"
    if candidate.exists():
        return candidate
    current = Path(__file__).resolve().parent
    for _ in range(6):
        c = current / "agent-runner" / "run_headless.py"
        if c.exists():
            return c
        current = current.parent
    return Path(default_repo) / "agent-runner" / "run_headless.py"


def _find_general_runner_script(default_repo: str) -> Path:
    """Locate ``run_general.py`` using the same heuristic as the ADO runner."""
    candidate = Path(default_repo) / "agent-runner" / "run_general.py"
    if candidate.exists():
        return candidate
    current = Path(__file__).resolve().parent
    for _ in range(6):
        c = current / "agent-runner" / "run_general.py"
        if c.exists():
            return c
        current = current.parent
    return Path(default_repo) / "agent-runner" / "run_general.py"


# ---------------------------------------------------------------------------
# App factory — keeps the app fully testable
# ---------------------------------------------------------------------------


def create_app(
    run_store: RunStore | None = None,
    action_registry: dict[str, Any] | None = None,
    discord_token: str | None = None,
    discord_guild_name: str | None = None,
    discord_trigger_channel: str | None = None,
    discord_ado_channel: str | None = None,
    discord_general_channel: str | None = None,
    discord_poll_seconds: int | None = None,
    ado_webhook_secret: str | None = None,
    default_repo: str | None = None,
    runner_script: Path | None = None,
    general_runner_script: Path | None = None,
    backend: str | None = None,
) -> FastAPI:
    """Return a configured FastAPI instance.

    All parameters default to environment-variable values when *None*.  Pass
    explicit values in tests to avoid reading env vars and to inject fakes.

    Discord channels:
        ``discord_ado_channel``     — ADO work-item triggers (default: ``trigger-agents-ado-work``)
        ``discord_general_channel`` — General-purpose triggers (default: ``trigger-agents-general``)
        ``discord_trigger_channel`` — Deprecated alias for ``discord_ado_channel``
    """

    # Resolve config from env if not supplied
    _token = discord_token or os.environ.get("DISCORD_BOT_TOKEN", "")
    _guild = discord_guild_name or os.environ.get("DISCORD_GUILD_NAME", "arigato-mr-roboto")

    # ADO channel: explicit param > DISCORD_ADO_CHANNEL > DISCORD_TRIGGER_CHANNEL (deprecated) > default
    _ado_channel = (
        discord_ado_channel
        or os.environ.get("DISCORD_ADO_CHANNEL")
        or discord_trigger_channel
        or os.environ.get("DISCORD_TRIGGER_CHANNEL")
        or "trigger-agents-ado-work"
    )
    _general_channel = (
        discord_general_channel
        or os.environ.get("DISCORD_GENERAL_CHANNEL")
        or "trigger-agents-general"
    )
    _poll = discord_poll_seconds or int(os.environ.get("DISCORD_POLL_SECONDS", "10"))
    _ado_secret = ado_webhook_secret if ado_webhook_secret is not None else os.environ.get("ADO_WEBHOOK_SECRET", "")
    _backend = backend or os.environ.get("BACKEND") or None
    _repo = _resolve_default_repo(
        default_repo or os.environ.get("DEFAULT_REPO") or None
    )

    if runner_script is None:
        _runner = _find_runner_script(_repo)
    else:
        _runner = runner_script

    if general_runner_script is None:
        _general_runner = _find_general_runner_script(_repo)
    else:
        _general_runner = general_runner_script

    # Shared state
    _store = run_store if run_store is not None else RunStore()

    # Action registry
    if action_registry is not None:
        _registry: dict[str, Any] = action_registry
    else:
        _run_handler = RunWorkflowHandler(
            run_store=_store,
            default_repo=_repo,
            runner_script=_runner,
            backend=_backend,
        )
        _general_handler = GeneralRunHandler(
            run_store=_store,
            default_repo=_repo,
            runner_script=_general_runner,
            backend=_backend,
        )
        _registry = {
            _run_handler.action_name: _run_handler,
            _general_handler.action_name: _general_handler,
        }

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tasks: list[asyncio.Task] = []

        if _token:
            # ADO channel poller
            if "run" in _registry:
                ado_poller = DiscordPollerAdapter(
                    token=_token,
                    guild_name=_guild,
                    trigger_channel_name=_ado_channel,
                    poll_seconds=_poll,
                    action_handler=_registry["run"],
                    run_store=_store,
                    channel_type="ado",
                )
                tasks.append(
                    asyncio.create_task(ado_poller.run(), name="discord-poller-ado")
                )

            # General channel poller
            if "general_run" in _registry:
                general_poller = DiscordPollerAdapter(
                    token=_token,
                    guild_name=_guild,
                    trigger_channel_name=_general_channel,
                    poll_seconds=_poll,
                    action_handler=_registry["general_run"],
                    run_store=_store,
                    channel_type="general",
                )
                tasks.append(
                    asyncio.create_task(general_poller.run(), name="discord-poller-general")
                )

        yield

        for t in tasks:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    app = FastAPI(
        title="Agent Trigger API",
        description=(
            "HTTP facade for triggering agent workflows from Discord, "
            "Azure DevOps, or any HTTP client."
        ),
        version="2.0.0",
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------ #
    # Routes                                                               #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/health", response_model=HealthResponse, tags=["meta"])
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            active_runs=_store.count_active(),
            known_actions=sorted(_registry.keys()),
        )

    # ---- Generic trigger -------------------------------------------------

    @app.post(
        "/api/v1/trigger",
        response_model=RunRecord,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["triggers"],
    )
    async def trigger(event: TriggerEvent) -> RunRecord:
        """Fire a trigger event from any HTTP client.

        The ``action`` field must match a registered handler
        (``run`` for ADO workflows, ``general_run`` for general-purpose runs).

        For ``general_run``, the ``prompt``, ``backend``, and ``repo_path``
        fields are required.
        """
        handler = _registry.get(event.action)
        if handler is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown action: {event.action!r}",
            )

        # Validate required fields for general_run
        if event.action == "general_run":
            missing = []
            if not event.prompt:
                missing.append("prompt")
            if not event.backend:
                missing.append("backend")
            if not event.repo_path:
                missing.append("repo_path")
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"general_run requires: {', '.join(missing)}",
                )

        if _store.has_active(event.change_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{event.change_id} is already running",
            )
        return await handler.execute(event)

    # ---- Run management --------------------------------------------------

    @app.get(
        "/api/v1/runs",
        response_model=list[RunRecord],
        tags=["runs"],
    )
    async def list_runs(
        status_filter: str | None = Query(default=None, alias="status"),
    ) -> list[RunRecord]:
        return _store.list(status_filter=status_filter)

    @app.get(
        "/api/v1/runs/{change_id}",
        response_model=RunRecord,
        tags=["runs"],
    )
    async def get_run(change_id: str) -> RunRecord:
        record = _store.get(change_id.upper() if not change_id.upper().startswith("WI-") else change_id.upper())
        # Also try prefixed form
        if record is None:
            prefixed = change_id.upper() if change_id.upper().startswith("WI-") else f"WI-{change_id.upper()}"
            record = _store.get(prefixed)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No run found for {change_id!r}",
            )
        return record

    @app.delete(
        "/api/v1/runs/{change_id}",
        response_model=CancelResponse,
        tags=["runs"],
    )
    async def cancel_run(change_id: str) -> CancelResponse:
        normalised = change_id.upper() if change_id.upper().startswith("WI-") else f"WI-{change_id.upper()}"
        cancelled = False
        for handler in _registry.values():
            if hasattr(handler, "cancel"):
                cancelled = await handler.cancel(normalised)
                if cancelled:
                    break
        return CancelResponse(change_id=normalised, cancelled=cancelled)

    # ---- Azure DevOps webhook --------------------------------------------

    @app.post(
        "/api/v1/webhooks/azure-devops",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["webhooks"],
    )
    async def azure_devops_webhook(
        payload: dict,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """Receive an Azure DevOps service hook.

        Configure ADO to POST ``Work item commented on`` events here.
        Set HTTP Basic auth with the value of ``ADO_WEBHOOK_SECRET`` as the
        password.
        """
        # Auth check — skip if no secret configured
        if _ado_secret:
            if not verify_basic_auth(authorization, _ado_secret):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or missing ADO webhook secret",
                )

        event = parse_ado_webhook(payload)
        if event is None:
            # Unsupported event type or non-RUN comment — acknowledge silently
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"accepted": False, "reason": "not a RUN command"},
            )

        handler = _registry.get(event.action)
        if handler is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown action: {event.action!r}",
            )

        if _store.has_active(event.change_id):
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"accepted": False, "reason": f"{event.change_id} already running"},
            )

        record = await handler.execute(event)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"accepted": True, "change_id": record.change_id},
        )

    return app


# ---------------------------------------------------------------------------
# Module-level app instance for uvicorn
# ---------------------------------------------------------------------------

app = create_app()
