"""Fixtures for the Home Assistant integration tests."""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load this custom integration in tests."""
    return


@pytest.fixture(autouse=True)
def auto_mock_bluetooth(mock_bluetooth):
    """Stand in for a real adapter.

    The integration now depends on bluetooth_adapters, so every flow - the
    cloud one included - sets that dependency up. Without this the real
    component tries to open a socket and pytest-socket blocks it.
    """
    return
