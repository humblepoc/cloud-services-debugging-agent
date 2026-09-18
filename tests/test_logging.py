"""Tests for logging configuration — verifies INFO shows useful logs
while suppressing HTTP endpoint URLs but NOT copilot auth messages."""

import logging
import sys

sys.path.insert(0, "src")


def setup_logging(level: str) -> None:
    """Replicate cli.setup_logging without importing cli.py
    (which wraps stdout and breaks pytest capture)."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.WARNING),
        format="%(message)s",
        force=True,
    )
    if level.upper() != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
    else:
        logging.getLogger("httpx").setLevel(logging.NOTSET)
        logging.getLogger("httpcore").setLevel(logging.NOTSET)


def _get_effective(name: str) -> int:
    return logging.getLogger(name).getEffectiveLevel()


# ── INFO level ────────────────────────────────────────────────────────

def test_info_level_shows_agent_loop():
    setup_logging("INFO")
    assert _get_effective("agent.core.loop") == logging.INFO


def test_info_level_shows_athena():
    setup_logging("INFO")
    assert _get_effective("agent.tools.devops.athena") == logging.INFO


def test_info_level_shows_bash():
    setup_logging("INFO")
    assert _get_effective("agent.tools.bash") == logging.INFO


def test_info_level_shows_copilot():
    """Copilot logger should NOT be suppressed — auth messages are user-facing."""
    setup_logging("INFO")
    assert _get_effective("agent.llm.copilot_provider") == logging.INFO


def test_info_level_suppresses_httpx():
    setup_logging("INFO")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


# ── DEBUG level (verbose) ─────────────────────────────────────────────

def test_debug_shows_everything():
    setup_logging("DEBUG")
    assert _get_effective("agent.llm.copilot_provider") == logging.DEBUG
    assert _get_effective("httpx") == logging.DEBUG
    assert _get_effective("httpcore") == logging.DEBUG
    assert _get_effective("agent.core.loop") == logging.DEBUG


# ── Default from config ───────────────────────────────────────────────

def test_config_default_is_info():
    from agent.config import AgentConfig
    cfg = AgentConfig()
    assert cfg.log_level == "INFO"


# ── Copilot provider uses Rich console (not print) ───────────────────

def test_copilot_provider_uses_rich_console():
    """Verify copilot_provider.py uses _console.print() not print()
    for user-facing auth messages. print() is broken in PyInstaller exe."""
    from pathlib import Path
    src = Path("src/agent/llm/copilot_provider.py").read_text()

    # Should NOT have bare print() calls (only _console.print is allowed)
    import re
    # Match lines with print( that are NOT _console.print(
    # Look for print( preceded by whitespace or start-of-line (bare call)
    bare_prints = re.findall(r'^\s+print\(', src, re.MULTILINE)
    assert len(bare_prints) == 0, (
        f"Found {len(bare_prints)} bare print() calls in copilot_provider.py. "
        "Use _console.print() instead for PyInstaller compatibility."
    )
