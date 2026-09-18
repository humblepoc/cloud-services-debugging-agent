"""Agent setup helpers — shared between CLI and Lambda entry points.

This module intentionally avoids Rich/console imports so it can be used
in headless environments (Lambda, tests) without pulling in terminal dependencies.
"""

from __future__ import annotations

from agent.config import AgentConfig
from agent.tools.base import ToolRegistry
from agent.tools.bash import BashTool


def build_registry(cfg: AgentConfig) -> ToolRegistry:
    """Build tool registry based on flavor config."""
    registry = ToolRegistry()
    registry.register(BashTool())

    if cfg.flavor == "devops":
        try:
            from agent.flavors.devops import register_devops_tools
            register_devops_tools(registry, cfg)
        except ImportError:
            pass  # DevOps tools not yet available

    return registry


def get_system_prompt(cfg: AgentConfig) -> str:
    """Get system prompt based on flavor."""
    if cfg.flavor == "devops":
        try:
            from agent.flavors.devops import DevOpsFlavor
            return DevOpsFlavor().system_prompt(cfg)
        except ImportError:
            pass
    return "You are a helpful assistant with access to tools. Use them to help the user."
