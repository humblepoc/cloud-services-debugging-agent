"""Flavor base class — bundles a system prompt + tool set."""
from __future__ import annotations

from abc import ABC, abstractmethod

from agent.config import AgentConfig
from agent.tools.base import Tool


class Flavor(ABC):
    """A flavor specializes the agent for a particular domain."""

    @abstractmethod
    def system_prompt(self, config: AgentConfig) -> str:
        """Return the system prompt for this flavor."""
        ...

    @abstractmethod
    def tools(self, config: AgentConfig) -> list[Tool]:
        """Return the tools available in this flavor."""
        ...
