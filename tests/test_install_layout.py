"""Tests for install layout — verifies exe in bin/ subfolder,
cred files in ~/.devops-agent/, and correct search path resolution."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "src")

from agent.config import _exe_dir, _cred_search_paths, _config_search_paths


# ── _exe_dir() ────────────────────────────────────────────────────────

def test_exe_dir_frozen_returns_parent_parent():
    """When frozen, exe is in bin/ so _exe_dir() should return parent.parent."""
    fake_exe = Path("C:/Users/someone/.devops-agent/bin/devops-agent.exe")
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "executable", str(fake_exe)):
        result = _exe_dir()
    assert result == fake_exe.parent.parent
    assert result.name == ".devops-agent"


def test_exe_dir_not_frozen_returns_cwd():
    """When not frozen (dev mode), _exe_dir() returns cwd."""
    # Ensure frozen is not set
    frozen = getattr(sys, "frozen", None)
    try:
        if hasattr(sys, "frozen"):
            delattr(sys, "frozen")
        result = _exe_dir()
        assert result == Path.cwd()
    finally:
        if frozen is not None:
            sys.frozen = frozen


# ── _cred_search_paths() ─────────────────────────────────────────────

def test_cred_search_starts_with_install_dir():
    """First search path for creds should be ~/.devops-agent/."""
    paths = _cred_search_paths("creds_logging.txt")
    expected_first = Path.home() / ".devops-agent" / "creds_logging.txt"
    assert paths[0] == expected_first


def test_cred_search_does_not_include_home_root():
    """Cred search should NOT look in ~/ directly (no fallback)."""
    paths = _cred_search_paths("creds_logging.txt")
    home_root = Path.home() / "creds_logging.txt"
    assert home_root not in paths


def test_cred_search_service_file():
    """Same rules apply for creds_service.txt."""
    paths = _cred_search_paths("creds_service.txt")
    expected_first = Path.home() / ".devops-agent" / "creds_service.txt"
    assert paths[0] == expected_first
    home_root = Path.home() / "creds_service.txt"
    assert home_root not in paths


def test_cred_search_frozen_includes_install_dir():
    """When frozen, exe dir resolves to install dir (same as ~/.devops-agent/)
    so no duplicate path should appear."""
    fake_exe = Path.home() / ".devops-agent" / "bin" / "devops-agent.exe"
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "executable", str(fake_exe)):
        paths = _cred_search_paths("creds_logging.txt")
    # Install dir should be first
    assert paths[0] == Path.home() / ".devops-agent" / "creds_logging.txt"
    # exe dir == install dir, so no duplicate
    install_path = Path.home() / ".devops-agent" / "creds_logging.txt"
    assert paths.count(install_path) == 1


# ── _config_search_paths() ───────────────────────────────────────────

def test_config_search_includes_install_dir():
    """Config search should include ~/.devops-agent/config.yaml."""
    paths = _config_search_paths()
    install_cfg = Path.home() / ".devops-agent" / "config.yaml"
    assert install_cfg in paths


def test_config_search_starts_with_cwd():
    """First config search path should be cwd/config.yaml."""
    paths = _config_search_paths()
    assert paths[0] == Path("config.yaml")


# ── Install layout structure ──────────────────────────────────────────

def test_install_layout_exe_in_bin():
    """Verify the expected install layout: exe in bin/ subfolder."""
    # Simulate the installed path structure
    install_dir = Path.home() / ".devops-agent"
    bin_dir = install_dir / "bin"
    exe_path = bin_dir / "devops-agent.exe"

    # When frozen with this exe path, _exe_dir should return install_dir
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "executable", str(exe_path)):
        result = _exe_dir()
    assert result == install_dir

    # And cred search should find files in install_dir
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "executable", str(exe_path)):
        paths = _cred_search_paths("creds_logging.txt")
    assert paths[0] == install_dir / "creds_logging.txt"
