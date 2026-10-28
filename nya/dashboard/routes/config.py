"""
Mutating config routes: adding upstream providers and client API keys.

Writes go through the live config manager (schema-validated ``set`` +
``save`` with rollback on save failure), so these routes are only
registered when the dashboard is started with ``enable_control=True``.
"""

import logging
import re
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from nacho import ValidationError
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nacho import Nacho

    from ..api import DashboardAPI


class AddApiRequest(BaseModel):
    """Payload for adding an upstream provider to ``apis``."""

    name: str = Field(..., min_length=1)
    endpoint: str = Field(..., pattern=r"^https?://.+")
    keys: List[str] = Field(..., min_length=1)
    key: Optional[str] = Field(None, pattern=r"^[a-zA-Z0-9_-]+$")
    headers: Optional[Dict[str, str]] = Field(None, max_length=20)
    """Custom request headers for the provider.

    Optional: when omitted (or empty) the route writes the default
    ``Authorization: Bearer ${{...}}`` header.  Admin-authored config,
    so values are not checked for ``${{`` templates.
    """

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: Optional[Dict[str, str]]):
        if value is None:
            return value
        for name, val in value.items():
            stripped = name.strip()
            if not stripped:
                raise ValueError("header name must not be blank")
            if len(stripped) > 100:
                raise ValueError("header name must be at most 100 characters")
            if not val:
                raise ValueError(f"header '{stripped}' value must not be empty")
            if len(val) > 300:
                raise ValueError(
                    f"header '{stripped}' value must be at most 300 characters"
                )
        return value


class AddClientRequest(BaseModel):
    """Payload for adding a client key to ``server.api_key``."""

    key: str = Field(..., min_length=1)


def _slugify(name: str) -> str:
    """Derive an ``apis`` section key from a display name."""
    slug = re.sub(r"[^a-z0-9]", "-", name.lower()).strip("-")
    return slug or f"api-{int(time.time() * 1000)}"


def _key_variable(config: "Nacho") -> str:
    """Name of the config variable that holds the upstream keys."""
    return config.get_str("default_settings.key_variable") or "keys"


def _set_and_save(
    config: "Nacho",
    path: str,
    value: Any,
    rollback: Callable[[], Any],
) -> Optional[JSONResponse]:
    """Write ``path`` in one ``set``, persist, roll back on save failure.

    Returns ``None`` on success or a JSONResponse describing the failure
    (422 for schema/write validation, 500 for a failed save).
    """
    try:
        config.set(path, value)
    except ValidationError as e:
        logger.error(f"Config validation failed for {path}: {e}")
        return JSONResponse(
            status_code=422,
            content={"error": f"Invalid config: {e}"},
        )
    except (ValueError, PermissionError) as e:
        logger.error(f"Cannot write config at {path}: {e}")
        return JSONResponse(status_code=422, content={"error": str(e)})
    try:
        config.save()
    except Exception as e:
        logger.error(f"Error saving config after writing {path}: {e}")
        try:
            rollback()
        except Exception as rollback_error:
            logger.error(f"Rollback of {path} failed: {rollback_error}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Error saving config: {e}"},
        )
    return None


def register_config_routes(app: FastAPI, dashboard: "DashboardAPI") -> None:
    """Attach the provider/client-key config routes to ``app``."""

    @app.get("/api/config/apis")
    async def list_apis():
        """List configured providers with their upstream key counts."""
        if not dashboard.config_manager:
            return JSONResponse(
                status_code=503,
                content={"error": "Config manager not available"},
            )
        try:
            config = dashboard.config_manager.config
            key_variable = _key_variable(config)
            apis = config.get_dict("apis", {})
            result = []
            for key, api in apis.items():
                if not isinstance(api, dict):
                    continue
                variables = api.get("variables") or {}
                keys = (
                    variables.get(key_variable) if isinstance(variables, dict) else None
                )
                result.append(
                    {
                        "key": key,
                        "name": api.get("name"),
                        "endpoint": api.get("endpoint"),
                        "key_count": len(keys) if isinstance(keys, list) else 0,
                    }
                )
            return result
        except Exception as e:
            logger.error(f"Error retrieving apis: {str(e)}")
            return JSONResponse(
                status_code=500,
                content={"error": f"Error retrieving apis: {str(e)}"},
            )

    @app.post("/api/config/apis")
    async def add_api(body: AddApiRequest):
        """Add a new upstream provider to the config."""
        if not dashboard.config_manager:
            return JSONResponse(
                status_code=503,
                content={"error": "Config manager not available"},
            )
        try:
            config = dashboard.config_manager.config
            key = body.key if body.key is not None else _slugify(body.name)
            apis = config.get_dict("apis", {})
            if key in apis:
                return JSONResponse(
                    status_code=409,
                    content={"error": f"API '{key}' already exists"},
                )
            key_variable = _key_variable(config)
            if body.headers:
                # Custom headers replace the default Authorization header.
                # Keys are stripped; blank-after-strip keys are dropped
                # (pydantic rejects them upfront, this is a safety net).
                headers = {
                    name.strip(): val
                    for name, val in body.headers.items()
                    if name.strip()
                }
                if not headers:
                    return JSONResponse(
                        status_code=422,
                        content={"error": "headers must not be empty"},
                    )
            else:
                # No headers (None or {}): keep the default Bearer header.
                headers = {"Authorization": "Bearer ${{" + key_variable + "}}"}
            value = {
                "name": body.name,
                "endpoint": body.endpoint,
                "headers": headers,
                "variables": {key_variable: list(body.keys)},
            }
            error = _set_and_save(
                config,
                f"apis.{key}",
                value,
                rollback=lambda: config.delete(f"apis.{key}"),
            )
            if error is not None:
                return error
            return {"status": "ok", "key": key}
        except Exception as e:
            logger.error(f"Error adding api: {str(e)}")
            return JSONResponse(
                status_code=500,
                content={"error": f"Error adding api: {str(e)}"},
            )

    @app.post("/api/config/clients")
    async def add_client_key(body: AddClientRequest):
        """Append a client key to ``server.api_key``."""
        if not dashboard.config_manager:
            return JSONResponse(
                status_code=503,
                content={"error": "Config manager not available"},
            )
        try:
            config = dashboard.config_manager.config
            key = body.key.strip()
            if not key:
                return JSONResponse(
                    status_code=422,
                    content={"error": "Client key must not be empty"},
                )
            raw_old = config.get("server.api_key")
            if raw_old is None:
                current: List[str] = []
            elif isinstance(raw_old, str):
                current = [raw_old]
            elif isinstance(raw_old, list):
                current = list(raw_old)
            else:
                logger.warning(
                    f"Unexpected server.api_key type: {type(raw_old).__name__}"
                )
                current = []
            if key in current:
                return JSONResponse(
                    status_code=409,
                    content={"error": "Client key already exists"},
                )

            def _rollback() -> None:
                if raw_old is None:
                    config.delete("server.api_key")
                else:
                    config.set("server.api_key", raw_old)

            error = _set_and_save(
                config,
                "server.api_key",
                current + [key],
                rollback=_rollback,
            )
            if error is not None:
                return error
            return {"status": "ok"}
        except Exception as e:
            logger.error(f"Error adding client key: {str(e)}")
            return JSONResponse(
                status_code=500,
                content={"error": f"Error adding client key: {str(e)}"},
            )
