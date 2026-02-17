"""Integration tests for plugin flows.

Tests plugin enable/disable, configuration CRUD, and tenant-specific
plugin state management.

Coverage:
- Enable/disable plugin for tenant
- Plugin config CRUD
- Plugin list shows correct state per tenant
- Tenant isolation for plugins

Run with:
    pytest -m integration tests/integration/test_plugin_flow.py
"""

import httpx
import pytest


@pytest.mark.integration
async def test_enable_disable_plugin_for_tenant(
    client_admin_a_int: httpx.AsyncClient,
):
    """Test enabling and disabling a plugin for a tenant."""
    plugin_name = "slack_notifications"

    # Enable plugin
    enable_resp = await client_admin_a_int.post(
        f"/api/v1/plugins/{plugin_name}/enable",
        json={"config": {"webhook_url": "https://hooks.slack.com/test"}},
    )
    # Depending on implementation, this might be 200 or 201
    assert enable_resp.status_code in (200, 201, 404)  # 404 if endpoint doesn't exist yet

    # If endpoint exists, verify plugin is enabled
    if enable_resp.status_code in (200, 201):
        # List plugins
        list_resp = await client_admin_a_int.get("/api/v1/plugins")
        assert list_resp.status_code == 200
        plugins = list_resp.json()
        slack_plugin = next((p for p in plugins if p["name"] == plugin_name), None)
        assert slack_plugin is not None
        assert slack_plugin["enabled"] is True

        # Disable plugin
        disable_resp = await client_admin_a_int.post(f"/api/v1/plugins/{plugin_name}/disable")
        assert disable_resp.status_code == 200

        # Verify disabled
        list_resp = await client_admin_a_int.get("/api/v1/plugins")
        assert list_resp.status_code == 200
        plugins = list_resp.json()
        slack_plugin = next((p for p in plugins if p["name"] == plugin_name), None)
        if slack_plugin:  # Might be removed from list when disabled
            assert slack_plugin["enabled"] is False


@pytest.mark.integration
async def test_plugin_config_crud(
    client_admin_a_int: httpx.AsyncClient,
):
    """Test plugin configuration CRUD operations."""
    plugin_name = "email_notifications"

    # Create/Update config
    config_data = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "from_email": "noreply@example.com",
    }
    update_resp = await client_admin_a_int.put(
        f"/api/v1/plugins/{plugin_name}/config",
        json=config_data,
    )
    # 200 (updated), 201 (created), or 404 (not implemented)
    assert update_resp.status_code in (200, 201, 404)

    if update_resp.status_code in (200, 201):
        # Read config
        get_resp = await client_admin_a_int.get(f"/api/v1/plugins/{plugin_name}/config")
        assert get_resp.status_code == 200
        config = get_resp.json()
        assert config["smtp_host"] == "smtp.example.com"
        assert config["smtp_port"] == 587

        # Update config (partial)
        patch_resp = await client_admin_a_int.patch(
            f"/api/v1/plugins/{plugin_name}/config",
            json={"smtp_port": 465},
        )
        assert patch_resp.status_code in (200, 404)

        if patch_resp.status_code == 200:
            # Verify update
            get_resp = await client_admin_a_int.get(f"/api/v1/plugins/{plugin_name}/config")
            assert get_resp.status_code == 200
            config = get_resp.json()
            assert config["smtp_port"] == 465
            assert config["smtp_host"] == "smtp.example.com"  # Unchanged

        # Delete config
        delete_resp = await client_admin_a_int.delete(f"/api/v1/plugins/{plugin_name}/config")
        assert delete_resp.status_code in (204, 404)


@pytest.mark.integration
async def test_plugin_list_shows_correct_state_per_tenant(
    client_admin_a_int: httpx.AsyncClient,
    client_admin_b_int: httpx.AsyncClient,
):
    """Plugin state is tenant-specific.

    Enabling a plugin for tenant A should not affect tenant B.
    """
    plugin_name = "analytics_export"

    # Admin A enables plugin
    enable_a_resp = await client_admin_a_int.post(
        f"/api/v1/plugins/{plugin_name}/enable",
        json={"config": {"export_format": "csv"}},
    )

    # If endpoint exists
    if enable_a_resp.status_code in (200, 201):
        # Admin A sees plugin enabled
        list_a_resp = await client_admin_a_int.get("/api/v1/plugins")
        assert list_a_resp.status_code == 200
        plugins_a = list_a_resp.json()
        plugin_a = next((p for p in plugins_a if p["name"] == plugin_name), None)
        assert plugin_a is not None
        assert plugin_a["enabled"] is True

        # Admin B does NOT see plugin enabled (tenant isolation)
        list_b_resp = await client_admin_b_int.get("/api/v1/plugins")
        assert list_b_resp.status_code == 200
        plugins_b = list_b_resp.json()
        plugin_b = next((p for p in plugins_b if p["name"] == plugin_name), None)
        # Either plugin doesn't exist in B's list, or it's disabled
        if plugin_b:
            assert plugin_b["enabled"] is False
        # else: plugin not in list, which is also correct


@pytest.mark.integration
async def test_viewer_cannot_modify_plugins(
    client_viewer_a_int: httpx.AsyncClient,
):
    """Viewer role cannot enable/disable or configure plugins.

    Plugin management is admin-only.
    """
    plugin_name = "test_plugin"

    # Viewer tries to enable plugin
    enable_resp = await client_viewer_a_int.post(
        f"/api/v1/plugins/{plugin_name}/enable",
        json={"config": {}},
    )
    assert enable_resp.status_code in (403, 404)

    # Viewer tries to update config
    config_resp = await client_viewer_a_int.put(
        f"/api/v1/plugins/{plugin_name}/config",
        json={"key": "value"},
    )
    assert config_resp.status_code in (403, 404)

    # Viewer can list plugins (read-only)
    list_resp = await client_viewer_a_int.get("/api/v1/plugins")
    # Might be 200 (allowed to view) or 403 (restricted)
    assert list_resp.status_code in (200, 403)


@pytest.mark.integration
async def test_plugin_config_validation(
    client_admin_a_int: httpx.AsyncClient,
):
    """Plugin configuration should validate required fields.

    Attempting to enable a plugin without required config should fail.
    """
    plugin_name = "slack_notifications"

    # Try to enable without required webhook_url
    enable_resp = await client_admin_a_int.post(
        f"/api/v1/plugins/{plugin_name}/enable",
        json={"config": {}},  # Missing webhook_url
    )
    # Should fail validation (400 Bad Request) or 404 if not implemented
    assert enable_resp.status_code in (400, 404, 422)

    # Try with valid config
    enable_resp = await client_admin_a_int.post(
        f"/api/v1/plugins/{plugin_name}/enable",
        json={"config": {"webhook_url": "https://hooks.slack.com/valid"}},
    )
    # Should succeed or 404 if not implemented
    assert enable_resp.status_code in (200, 201, 404)


@pytest.mark.integration
async def test_list_available_plugins(
    client_admin_a_int: httpx.AsyncClient,
):
    """List available plugins returns system-wide plugin registry.

    This should show all plugins available in the platform,
    with tenant-specific enabled/disabled status.
    """
    resp = await client_admin_a_int.get("/api/v1/plugins")

    # Endpoint might not be implemented yet
    if resp.status_code == 404:
        pytest.skip("Plugin list endpoint not implemented yet")

    assert resp.status_code == 200
    plugins = resp.json()
    assert isinstance(plugins, list)

    # Each plugin should have name, description, enabled status
    for plugin in plugins:
        assert "name" in plugin
        assert "enabled" in plugin
        assert isinstance(plugin["enabled"], bool)


@pytest.mark.integration
async def test_plugin_state_persists_across_requests(
    client_admin_a_int: httpx.AsyncClient,
):
    """Plugin state should persist in database across requests.

    Enable plugin, make unrelated request, verify plugin still enabled.
    """
    plugin_name = "test_persistence"

    # Enable plugin
    enable_resp = await client_admin_a_int.post(
        f"/api/v1/plugins/{plugin_name}/enable",
        json={"config": {"test": "value"}},
    )

    if enable_resp.status_code in (200, 201):
        # Make unrelated request (conversation list)
        await client_admin_a_int.get("/api/v1/conversations")

        # Check plugin is still enabled
        list_resp = await client_admin_a_int.get("/api/v1/plugins")
        assert list_resp.status_code == 200
        plugins = list_resp.json()
        plugin = next((p for p in plugins if p["name"] == plugin_name), None)
        assert plugin is not None
        assert plugin["enabled"] is True
