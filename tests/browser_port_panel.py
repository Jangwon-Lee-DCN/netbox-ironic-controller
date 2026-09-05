"""Authenticated browser acceptance for an already-seeded development NetBox."""

import os

from playwright.sync_api import sync_playwright


base = os.environ["PORT_PANEL_BASE_URL"].rstrip("/")
username = os.environ["PORT_PANEL_USERNAME"]
password = os.environ["PORT_PANEL_PASSWORD"]
expected_device = os.environ.get("PORT_PANEL_DEVICE", "tor-dev")
expected_ports = int(os.environ.get("PORT_PANEL_EXPECTED_PORTS", "2"))
expected_heading = os.environ.get("PORT_PANEL_EXPECTED_HEADING", "Physical port panel")
expected_peer = os.environ.get("PORT_PANEL_EXPECTED_PEER")
errors = []
http_errors = []

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.on("console", lambda message: errors.append(f"console:{message.type}:{message.text}") if message.type in {"error", "warning"} else None)
    page.on("pageerror", lambda error: errors.append(f"pageerror:{error}"))
    page.on("requestfailed", lambda request: errors.append(f"request:{request.url}:{request.failure}"))
    page.on("response", lambda response: http_errors.append((response.status, response.url)) if response.status >= 400 else None)
    page.goto(f"{base}/login/", wait_until="networkidle")
    page.get_by_label("Username").fill(username)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign In").click()
    page.wait_for_load_state("networkidle")
    page.goto(f"{base}/plugins/dcn-port-panel/", wait_until="networkidle")
    assert page.get_by_text(expected_device).is_visible()
    page.get_by_role("link", name="Open port panel").click()
    page.wait_for_load_state("networkidle")
    assert page.get_by_text(expected_heading).is_visible()
    assert page.locator("#port-panel .port").count() == expected_ports
    if expected_peer:
        assert page.get_by_text(expected_peer, exact=False).is_visible()
    page.wait_for_timeout(1000)
    assert "Loading" not in page.locator("#panel-summary").inner_text()
    assert not errors, errors
    assert not http_errors, http_errors
    browser.close()
