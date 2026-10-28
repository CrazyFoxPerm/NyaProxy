"""
Tests for the dashboard config write-API (``nya.dashboard.routes.config``).

The routes mutate the live config through the wired-in config manager
(schema-validated ``set`` + ``save`` with rollback on save failure), so
they are registered only with ``enable_control=True`` and answer 503
when no config manager is attached.  The config client is faked
in-memory: no file-backed Nacho, no writes to ``nya/config.yaml``.
"""

from types import SimpleNamespace

from nacho import ValidationError
from starlette.testclient import TestClient

from nya.dashboard.api import DashboardAPI

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeConfig:
    """In-memory stand-in for the Nacho client the config routes use."""

    def __init__(self, data=None):
        self.data = data if data is not None else {}
        self.set_error = None  # raised by set() when set
        self.save_error = None  # raised by save() when set
        self.save_calls = 0

    def get(self, path, default=None):
        current = self.data
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def get_str(self, path, default=""):
        value = self.get(path, default)
        return default if value is None else str(value)

    def get_dict(self, path, default=None):
        value = self.get(path)
        if isinstance(value, dict):
            return value
        return {} if default is None else default

    def set(self, path, value):
        if self.set_error is not None:
            raise self.set_error
        parts = path.split(".")
        current = self.data
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = value

    def delete(self, path):
        parts = path.split(".")
        current = self.data
        for part in parts[:-1]:
            if not isinstance(current, dict) or part not in current:
                return
            current = current[part]
        if isinstance(current, dict):
            current.pop(parts[-1], None)

    def save(self):
        self.save_calls += 1
        if self.save_error is not None:
            raise self.save_error


class FakeConfigManager:
    """Minimal ConfigManager stand-in exposing only what routes read."""

    def __init__(self, config):
        self.config = config


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def make_client(config=None, enable_control=True):
    """TestClient over DashboardAPI, optionally wired to a fake config."""
    dashboard = DashboardAPI(enable_control=enable_control)
    if config is not None:
        dashboard.set_config_manager(FakeConfigManager(config))
    return TestClient(dashboard.app)


def config_paths(enable_control):
    """/api/config/* paths present in the app's OpenAPI schema."""
    paths = DashboardAPI(enable_control=enable_control).app.openapi()["paths"]
    return {path for path in paths if path.startswith("/api/config/")}


def api_payload(**overrides):
    """A valid AddApiRequest body; override fields per test."""
    payload = {
        "name": "My API",
        "endpoint": "https://api.test/v1",
        "keys": ["k1", "k2"],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# Registration under enable_control
# --------------------------------------------------------------------------


def test_config_routes_absent_when_control_disabled():
    assert config_paths(enable_control=False) == set()


def test_config_routes_present_when_control_enabled():
    assert config_paths(enable_control=True) == {
        "/api/config/apis",
        "/api/config/clients",
    }


# --------------------------------------------------------------------------
# GET /api/config/apis
# --------------------------------------------------------------------------


def test_list_apis_returns_empty_list_when_none_configured():
    resp = make_client(FakeConfig()).get("/api/config/apis")

    assert resp.status_code == 200
    assert resp.json() == []


def test_list_apis_returns_503_without_config_manager():
    assert make_client().get("/api/config/apis").status_code == 503


def test_list_apis_reports_key_count_for_default_key_variable():
    config = FakeConfig(
        {
            "apis": {
                "openai": {
                    "name": "OpenAI",
                    "endpoint": "https://api.openai.test",
                    "variables": {"keys": ["k1", "k2", "k3"]},
                }
            }
        }
    )

    resp = make_client(config).get("/api/config/apis")

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "key": "openai",
            "name": "OpenAI",
            "endpoint": "https://api.openai.test",
            "key_count": 3,
        }
    ]


def test_list_apis_counts_keys_via_custom_key_variable():
    config = FakeConfig(
        {
            "default_settings": {"key_variable": "tokens"},
            "apis": {
                "custom": {
                    "name": "Custom",
                    "endpoint": "https://custom.test",
                    "variables": {"tokens": ["t1", "t2"], "keys": ["only-one"]},
                },
                "legacy": {
                    "name": "Legacy",
                    "endpoint": "https://legacy.test",
                    "variables": {"keys": ["x", "y"]},
                },
            },
        }
    )

    resp = make_client(config).get("/api/config/apis")

    counts = {item["key"]: item["key_count"] for item in resp.json()}
    # key_count must follow default_settings.key_variable ("tokens"):
    # "custom" counts its tokens (2), "legacy" sees no tokens (0).
    assert counts == {"custom": 2, "legacy": 0}


# --------------------------------------------------------------------------
# POST /api/config/apis
# --------------------------------------------------------------------------


def test_add_api_returns_503_without_config_manager():
    resp = make_client().post("/api/config/apis", json=api_payload())

    assert resp.status_code == 503


def test_add_api_success_stores_slugified_entry():
    config = FakeConfig()

    resp = make_client(config).post("/api/config/apis", json=api_payload())

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "key": "my-api"}
    assert config.get("apis.my-api") == {
        "name": "My API",
        "endpoint": "https://api.test/v1",
        "headers": {"Authorization": "Bearer ${{keys}}"},
        "variables": {"keys": ["k1", "k2"]},
    }
    assert config.save_calls == 1


def test_add_api_uses_custom_key_variable_in_header_and_variables():
    config = FakeConfig({"default_settings": {"key_variable": "tokens"}})

    resp = make_client(config).post("/api/config/apis", json=api_payload())

    assert resp.status_code == 200
    entry = config.get("apis.my-api")
    assert entry["headers"] == {"Authorization": "Bearer ${{tokens}}"}
    assert entry["variables"] == {"tokens": ["k1", "k2"]}
    assert "keys" not in entry["variables"]


def test_add_api_slug_falls_back_to_timestamp_for_non_latin_name(monkeypatch):
    monkeypatch.setattr(
        "nya.dashboard.routes.config.time",
        SimpleNamespace(time=lambda: 1700000000.0),
    )
    config = FakeConfig()

    resp = make_client(config).post(
        "/api/config/apis",
        json=api_payload(name="Кофе"),
    )

    assert resp.status_code == 200
    # No latin slug can be derived, so the key is api-<ms timestamp>.
    assert resp.json() == {"status": "ok", "key": "api-1700000000000"}
    assert "api-1700000000000" in config.get("apis", {})


def test_add_api_duplicate_key_returns_409():
    config = FakeConfig(
        {"apis": {"my-api": {"name": "My API", "endpoint": "https://old.test"}}}
    )

    resp = make_client(config).post("/api/config/apis", json=api_payload())

    assert resp.status_code == 409
    assert config.get("apis.my-api.endpoint") == "https://old.test"
    assert config.save_calls == 0


def test_add_api_rejects_invalid_endpoint_before_touching_config():
    config = FakeConfig()

    resp = make_client(config).post(
        "/api/config/apis",
        json=api_payload(endpoint="ftp://bad.test"),
    )

    # Pydantic rejects the body before the route runs: config untouched.
    assert resp.status_code == 422
    assert "apis" not in config.data
    assert config.save_calls == 0


def test_add_api_config_set_validation_error_returns_422():
    config = FakeConfig()
    config.set_error = ValidationError(["apis.my-api: rejected by schema"])

    resp = make_client(config).post("/api/config/apis", json=api_payload())

    assert resp.status_code == 422
    assert "my-api" not in config.data.get("apis", {})
    assert config.save_calls == 0


def test_add_api_save_failure_returns_500_and_rolls_back():
    config = FakeConfig()
    config.save_error = RuntimeError("disk full")

    resp = make_client(config).post("/api/config/apis", json=api_payload())

    assert resp.status_code == 500
    # The failed save must roll the write back: no partial entry remains.
    assert "my-api" not in config.data.get("apis", {})
    assert config.save_calls == 1


# --------------------------------------------------------------------------
# POST /api/config/apis — custom headers
# --------------------------------------------------------------------------


def test_add_api_without_headers_keeps_default_bearer():
    config = FakeConfig()

    resp = make_client(config).post("/api/config/apis", json=api_payload())

    assert resp.status_code == 200
    assert config.get("apis.my-api.headers") == {"Authorization": "Bearer ${{keys}}"}


def test_add_api_with_empty_headers_dict_keeps_default_bearer():
    config = FakeConfig()

    resp = make_client(config).post("/api/config/apis", json=api_payload(headers={}))

    assert resp.status_code == 200
    assert config.get("apis.my-api.headers") == {"Authorization": "Bearer ${{keys}}"}


def test_add_api_custom_headers_replace_default_authorization():
    config = FakeConfig()
    headers = {"x-api-key": "${{keys}}", "anthropic-version": "2023-06-01"}

    resp = make_client(config).post(
        "/api/config/apis",
        json=api_payload(headers=headers),
    )

    assert resp.status_code == 200
    entry = config.get("apis.my-api")
    # Exactly the submitted headers, no default Authorization header.
    assert entry["headers"] == headers
    assert "Authorization" not in entry["headers"]
    # Structure of the rest of the entry is unchanged.
    assert entry["name"] == "My API"
    assert entry["endpoint"] == "https://api.test/v1"
    assert entry["variables"] == {"keys": ["k1", "k2"]}
    assert config.save_calls == 1


def test_add_api_header_keys_are_stripped_before_storing():
    config = FakeConfig()

    resp = make_client(config).post(
        "/api/config/apis",
        json=api_payload(headers={"  x-api-key  ": "  v  "}),
    )

    assert resp.status_code == 200
    # Key is stripped; value is stored as submitted.
    assert config.get("apis.my-api.headers") == {"x-api-key": "  v  "}


def test_add_api_headers_blank_key_after_strip_returns_422():
    config = FakeConfig()

    resp = make_client(config).post(
        "/api/config/apis",
        json=api_payload(headers={"  ": "v"}),
    )

    assert resp.status_code == 422
    # Rejected before anything is written.
    assert "apis" not in config.data
    assert config.save_calls == 0


def test_add_api_more_than_20_headers_returns_422():
    config = FakeConfig()
    headers = {f"h{i}": "v" for i in range(21)}

    resp = make_client(config).post(
        "/api/config/apis",
        json=api_payload(headers=headers),
    )

    assert resp.status_code == 422
    assert "apis" not in config.data
    assert config.save_calls == 0


# --------------------------------------------------------------------------
# POST /api/config/clients
# --------------------------------------------------------------------------


def test_add_client_key_returns_503_without_config_manager():
    resp = make_client().post("/api/config/clients", json={"key": "k"})

    assert resp.status_code == 503


def test_add_client_key_none_becomes_list():
    config = FakeConfig()

    resp = make_client(config).post("/api/config/clients", json={"key": "new-key"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert config.get("server.api_key") == ["new-key"]
    assert config.save_calls == 1


def test_add_client_key_appends_to_existing_string():
    config = FakeConfig({"server": {"api_key": "old"}})

    resp = make_client(config).post("/api/config/clients", json={"key": "new-key"})

    assert resp.status_code == 200
    assert config.get("server.api_key") == ["old", "new-key"]


def test_add_client_key_appends_to_existing_list():
    config = FakeConfig({"server": {"api_key": ["a", "b"]}})

    resp = make_client(config).post("/api/config/clients", json={"key": "c"})

    assert resp.status_code == 200
    assert config.get("server.api_key") == ["a", "b", "c"]


def test_add_client_key_duplicate_returns_409():
    config = FakeConfig({"server": {"api_key": ["dup"]}})

    resp = make_client(config).post("/api/config/clients", json={"key": "dup"})

    assert resp.status_code == 409
    assert config.get("server.api_key") == ["dup"]
    assert config.save_calls == 0


def test_add_client_key_empty_and_whitespace_only_return_422():
    config = FakeConfig()
    client = make_client(config)

    empty = client.post("/api/config/clients", json={"key": ""})
    blank = client.post("/api/config/clients", json={"key": "   "})

    # "" is rejected by pydantic min_length, "   " by the route after strip.
    assert empty.status_code == 422
    assert blank.status_code == 422
    assert config.get("server.api_key") is None
    assert config.save_calls == 0


def test_add_client_key_is_stripped_before_storing():
    config = FakeConfig()

    resp = make_client(config).post("/api/config/clients", json={"key": "  pad  "})

    assert resp.status_code == 200
    assert config.get("server.api_key") == ["pad"]


def test_add_client_key_save_failure_rolls_back_absent_value():
    config = FakeConfig()
    config.save_error = RuntimeError("disk full")

    resp = make_client(config).post("/api/config/clients", json={"key": "new-key"})

    assert resp.status_code == 500
    # raw_old was None, so rollback deletes the freshly written value.
    assert config.get("server.api_key") is None
    assert config.save_calls == 1


def test_add_client_key_save_failure_restores_previous_value():
    config = FakeConfig({"server": {"api_key": "old"}})
    config.save_error = RuntimeError("disk full")

    resp = make_client(config).post("/api/config/clients", json={"key": "new-key"})

    assert resp.status_code == 500
    assert config.get("server.api_key") == "old"
