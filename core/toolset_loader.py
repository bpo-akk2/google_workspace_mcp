"""
Toolset Loader Module

Named tool allowlists selected with the WORKSPACE_MCP_TOOLSET environment
variable (definitions live in core/toolsets.yaml).

Tool tiers are cumulative and ``--tools`` works per service, so neither can
express an arbitrary cross-service selection. A toolset can: it lists the exact
tools to expose, grouped by service. When a toolset is active:

- only services that contribute at least one tool are imported (so OAuth
  scopes are requested only for those services),
- every registered tool outside the allowlist is removed from the server,
  which hides it from tools/list and makes calls to it fail as unknown.

WORKSPACE_MCP_TOOLSET unset, empty, "full" or "all" keeps the default
behaviour (no toolset filtering).
"""

import logging
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import yaml

logger = logging.getLogger(__name__)

ENV_VAR = "WORKSPACE_MCP_TOOLSET"
DEFAULT_CONFIG_PATH = Path(__file__).parent / "toolsets.yaml"

# Values that mean "no toolset filtering".
_NO_TOOLSET_VALUES = {"", "full", "all"}

# Infrastructure tools that are never part of a toolset definition but must
# stay available when they are registered. start_google_auth is the legacy
# OAuth 2.0 entry point; under OAuth 2.1 it is removed anyway by
# filter_server_tools().
ALWAYS_ALLOWED_TOOLS: Set[str] = {"start_google_auth"}


def get_toolset_name(value: Optional[str] = None) -> Optional[str]:
    """Return the normalized toolset name from ``value`` or the environment.

    Returns None when no toolset filtering should be applied.
    """
    raw = os.getenv(ENV_VAR, "") if value is None else value
    name = raw.strip().lower()
    if name in _NO_TOOLSET_VALUES:
        return None
    return name


def load_toolsets(config_path: Optional[Path] = None) -> Dict[str, Dict[str, List[str]]]:
    """Load and validate all toolset definitions."""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"Toolsets configuration not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in toolsets configuration: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("Toolsets configuration must be a mapping of toolset names")
    for toolset, services in data.items():
        if not isinstance(services, dict):
            raise ValueError(
                f"Toolset '{toolset}' must map service names to lists of tools"
            )
        for service, tools in services.items():
            if not isinstance(tools, list) or not all(
                isinstance(t, str) for t in tools
            ):
                raise ValueError(
                    f"Toolset '{toolset}', service '{service}': expected a list of tool names"
                )
    return data


def resolve_toolset(
    name: str, config_path: Optional[Path] = None
) -> Tuple[List[str], List[str]]:
    """Resolve a toolset into (tool_names, service_names).

    Raises:
        ValueError: if the toolset is not defined.
    """
    toolsets = load_toolsets(config_path)
    if name not in toolsets:
        available = ", ".join(sorted(toolsets)) or "none"
        raise ValueError(
            f"Unknown {ENV_VAR} value '{name}'. Available toolsets: {available} "
            f"(or leave it unset for the full tool set)."
        )

    tools: List[str] = []
    services: List[str] = []
    for service, service_tools in toolsets[name].items():
        if not service_tools:
            continue
        services.append(service)
        for tool in service_tools:
            if tool not in tools:
                tools.append(tool)

    logger.info(
        "Toolset '%s' resolved to %d tools across services: %s",
        name,
        len(tools),
        services,
    )
    return tools, services


def apply_toolset(
    name: str,
    services_to_import: Iterable[str],
    enabled_tools: Optional[Set[str]],
    config_path: Optional[Path] = None,
) -> Tuple[List[str], Set[str]]:
    """Narrow an existing service/tool selection to a toolset.

    Args:
        name: Toolset name (already normalized).
        services_to_import: Services selected so far (from --tools, tiers,
            --permissions or the default "all services").
        enabled_tools: Tool-name filter selected so far, or None for "all".
        config_path: Optional override of the toolsets YAML path.

    Returns:
        (services_to_import, enabled_tools) intersected with the toolset.
        ALWAYS_ALLOWED_TOOLS are kept if they were allowed before.
    """
    toolset_tools, toolset_services = resolve_toolset(name, config_path)

    services = [s for s in services_to_import if s in toolset_services]

    allowed = set(toolset_tools) | ALWAYS_ALLOWED_TOOLS
    if enabled_tools is not None:
        allowed &= set(enabled_tools)

    return services, allowed
