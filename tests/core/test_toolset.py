"""Tests for WORKSPACE_MCP_TOOLSET allowlists and the removed modules."""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastmcp import Client, FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from core import tool_registry  # noqa: E402
from core.tool_registry import (  # noqa: E402
    filter_server_tools,
    get_tool_components,
    set_enabled_tools,
)
from core.toolset_loader import (  # noqa: E402
    ALWAYS_ALLOWED_TOOLS,
    ENV_VAR,
    apply_toolset,
    get_toolset_name,
    resolve_toolset,
)

SUPPLEMENT_BY_SERVICE = {
    "gmail": {
        "get_gmail_attachment_content",
        "send_gmail_message",
        "get_gmail_messages_content_batch",
        "get_gmail_threads_content_batch",
        "batch_modify_gmail_message_labels",
        "list_gmail_filters",
        "manage_gmail_filter",
    },
    "drive": {
        "manage_drive_access",
        "set_drive_file_permissions",
        "get_drive_file_permissions",
        "check_drive_file_public_access",
        "update_drive_file",
        "list_drive_items",
        "create_drive_file",
        "get_drive_file_download_url",
    },
    "docs": {
        "export_doc_to_pdf",
        "insert_doc_image",
        "get_doc_as_markdown",
        "inspect_doc_structure",
        "debug_table_structure",
        "create_table_with_data",
    },
    "tasks": {
        "list_task_lists",
        "get_task_list",
        "manage_task_list",
        "list_tasks",
        "get_task",
        "manage_task",
    },
}
SUPPLEMENT = set().union(*SUPPLEMENT_BY_SERVICE.values())

ALL_TOOL_MODULES = [
    "gmail.gmail_tools",
    "gdrive.drive_tools",
    "gdocs.docs_tools",
    "gsheets.sheets_tools",
    "gslides.slides_tools",
    "gtasks.tasks_tools",
]


# ---------------------------------------------------------------------------
# Toolset definition and resolution
# ---------------------------------------------------------------------------


def test_supplement_toolset_is_exactly_the_27_tools():
    tools, services = resolve_toolset("supplement")
    assert len(tools) == 27
    assert set(tools) == SUPPLEMENT
    assert services == ["gmail", "drive", "docs", "tasks"]
    assert "insert_doc_elements" not in tools


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", None),
        ("   ", None),
        ("full", None),
        ("ALL", None),
        ("supplement", "supplement"),
        (" Supplement ", "supplement"),
    ],
)
def test_toolset_name_normalization(raw, expected):
    assert get_toolset_name(raw) == expected


def test_toolset_name_comes_from_env(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert get_toolset_name() is None
    monkeypatch.setenv(ENV_VAR, "supplement")
    assert get_toolset_name() == "supplement"


def test_unknown_toolset_is_rejected():
    with pytest.raises(ValueError, match="Unknown WORKSPACE_MCP_TOOLSET"):
        resolve_toolset("minimal")


def test_apply_toolset_narrows_services_and_keeps_auth_helper():
    services, allowed = apply_toolset(
        "supplement",
        ["gmail", "drive", "docs", "sheets", "slides", "tasks"],
        None,
    )
    assert services == ["gmail", "drive", "docs", "tasks"]
    assert allowed == SUPPLEMENT | ALWAYS_ALLOWED_TOOLS


def test_apply_toolset_intersects_with_existing_filters():
    services, allowed = apply_toolset(
        "supplement",
        ["gmail", "sheets"],
        {"send_gmail_message", "search_gmail_messages", "read_sheet_values"},
    )
    assert services == ["gmail"]
    assert allowed == {"send_gmail_message"}


def test_toolset_yaml_is_shipped_as_package_data():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert '"toolsets.yaml"' in pyproject


# ---------------------------------------------------------------------------
# Real registry: filtering hides and rejects non-allowlisted tools
# ---------------------------------------------------------------------------


def _full_registry_copy() -> FastMCP:
    from core.server import server

    for module in ALL_TOOL_MODULES:
        importlib.import_module(module)

    copy = FastMCP("toolset-test")
    for tool in get_tool_components(server).values():
        copy.add_tool(tool)
    return copy


@pytest.mark.asyncio
async def test_supplement_exposes_exactly_27_tools_and_rejects_hidden(monkeypatch):
    srv = _full_registry_copy()
    registered = {t.name for t in await srv.list_tools()}
    # Every allowlisted tool must exist in the code base (catches typos/renames).
    assert SUPPLEMENT <= registered
    # Duplicates of the official connectors are still registered (code kept).
    assert {"manage_gmail_label", "create_doc", "read_sheet_values"} <= registered

    _, allowed = apply_toolset("supplement", ["gmail", "drive", "docs", "tasks"], None)
    # Production runs with OAuth 2.1, which also removes start_google_auth.
    monkeypatch.setattr(tool_registry, "is_oauth21_enabled", lambda: True)
    set_enabled_tools(allowed)
    try:
        filter_server_tools(srv)
    finally:
        set_enabled_tools(None)

    listed = {t.name for t in await srv.list_tools()}
    assert listed == SUPPLEMENT
    assert len(listed) == 27

    async with Client(srv) as client:
        listed_via_protocol = {t.name for t in await client.list_tools()}
        assert listed_via_protocol == SUPPLEMENT
        with pytest.raises(Exception, match="manage_gmail_label"):
            await client.call_tool("manage_gmail_label", {"action": "create"})
        with pytest.raises(Exception, match="insert_doc_elements"):
            await client.call_tool("insert_doc_elements", {})


@pytest.mark.asyncio
async def test_without_toolset_nothing_is_filtered(monkeypatch):
    srv = _full_registry_copy()
    before = {t.name for t in await srv.list_tools()}
    monkeypatch.setattr(tool_registry, "is_oauth21_enabled", lambda: False)
    set_enabled_tools(None)
    filter_server_tools(srv)
    after = {t.name for t in await srv.list_tools()}
    assert after == before
    assert len(after) > 27


def _run_cli(tmp_path, *cli_args, toolset=None):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {ENV_VAR, "MCP_ENABLE_OAUTH21", "WORKSPACE_MCP_STATELESS_MODE"}
    }
    env.update(
        {
            "GOOGLE_OAUTH_CLIENT_ID": "test-client-id",
            "GOOGLE_OAUTH_CLIENT_SECRET": "test-client-secret",
            "WORKSPACE_MCP_CREDENTIALS_DIR": str(tmp_path / "creds"),
        }
    )
    if toolset is not None:
        env[ENV_VAR] = toolset
    return subprocess.run(
        [sys.executable, "main.py", "--cli", *cli_args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_main_cli_lists_supplement_tools(tmp_path):
    result = _run_cli(tmp_path, "list", "--json", toolset="supplement")
    assert result.returncode == 0, result.stderr
    names = {t["name"] for t in json.loads(result.stdout)["tools"]}
    # CLI mode forces legacy OAuth 2.0, where start_google_auth stays available.
    assert names == SUPPLEMENT | {"start_google_auth"}


def test_main_cli_rejects_hidden_tool(tmp_path):
    result = _run_cli(
        tmp_path, "manage_gmail_label", "--args", "{}", toolset="supplement"
    )
    assert result.returncode != 0
    assert "not found" in (result.stdout + result.stderr).lower()


def test_main_cli_without_toolset_keeps_full_set(tmp_path):
    result = _run_cli(tmp_path, "list", "--json")
    assert result.returncode == 0, result.stderr
    names = {t["name"] for t in json.loads(result.stdout)["tools"]}
    assert SUPPLEMENT < names
    assert {"manage_gmail_label", "create_doc", "read_sheet_values"} <= names


def test_main_rejects_unknown_toolset(tmp_path):
    result = _run_cli(tmp_path, "list", toolset="bogus")
    assert result.returncode == 1
    assert "Unknown WORKSPACE_MCP_TOOLSET" in result.stderr


# ---------------------------------------------------------------------------
# Removed modules (Apps Script, Custom Search, dead Calendar)
# ---------------------------------------------------------------------------

REMOVED_TOOLS = {
    "list_script_projects",
    "get_script_project",
    "get_script_content",
    "create_script_project",
    "update_script_content",
    "delete_script_project",
    "run_script_function",
    "create_version",
    "get_version",
    "list_versions",
    "list_deployments",
    "manage_deployment",
    "list_script_processes",
    "get_script_metrics",
    "generate_trigger_code",
    "search_custom",
    "get_search_engine_info",
    "list_calendars",
    "get_events",
    "manage_event",
    "query_freebusy",
}


@pytest.mark.parametrize(
    "module",
    [
        "gappsscript.apps_script_tools",
        "gsearch.search_tools",
        "gcalendar.calendar_tools",
    ],
)
def test_removed_modules_are_gone(module):
    assert not (REPO_ROOT / module.split(".")[0]).exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


@pytest.mark.asyncio
async def test_removed_tools_are_not_registered():
    srv = _full_registry_copy()
    registered = {t.name for t in await srv.list_tools()}
    assert not (registered & REMOVED_TOOLS)


def test_removed_scopes_are_not_requested():
    from auth.scopes import TOOL_SCOPES_MAP, get_all_read_only_scopes, get_scopes_for_tools

    all_scopes = set(get_scopes_for_tools(None)) | set(get_all_read_only_scopes())
    assert not [s for s in all_scopes if "/auth/script." in s]
    assert not [s for s in all_scopes if s.endswith("/auth/cse")]
    assert not [s for s in all_scopes if "/auth/calendar" in s]
    assert not {"appscript", "search", "calendar"} & set(TOOL_SCOPES_MAP)


def test_removed_services_not_configurable():
    from auth.permissions import SERVICE_PERMISSION_LEVELS
    from auth.service_decorator import SERVICE_CONFIGS

    tiers = yaml.safe_load((REPO_ROOT / "core" / "tool_tiers.yaml").read_text())
    for name in ("appscript", "search", "calendar"):
        assert name not in tiers
        assert name not in SERVICE_PERMISSION_LEVELS
    assert not {"script", "customsearch", "calendar"} & set(SERVICE_CONFIGS)
