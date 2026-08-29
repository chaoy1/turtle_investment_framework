"""Shared test fixtures for Turtle Investment Framework."""

import os
import sys

import pytest

# Add scripts/ to path so we can import from there
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def fixtures_dir():
    """Path to the test fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture
def sample_stock_code():
    """Standard test stock code (Yili 伊利股份)."""
    return "600887.SH"


@pytest.fixture
def tmp_output_dir(tmp_path):
    """Temporary output directory for test file generation."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    return output_dir


# Live public-data tests are opt-in to keep the suite deterministic.
integration = pytest.mark.skipif(
    not os.environ.get("RUN_LIVE_DATA_TESTS"),
    reason="RUN_LIVE_DATA_TESTS not set, skipping live public-data test",
)


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "integration: mark test as requiring live public-data access")


@pytest.fixture(autouse=True)
def _isolate_env_file(monkeypatch, tmp_path):
    """Prevent _load_env_file from finding the real .env during tests."""
    import config as config_mod

    monkeypatch.setattr(config_mod, "__file__", str(tmp_path / "scripts" / "config.py"))
