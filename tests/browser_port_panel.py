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
expected_state = os.environ.get("PORT_PANEL_EXPECTED_STATE")
device_id = os.environ.get("PORT_PANEL_DEVICE_ID")
screenshot_path = os.environ.get("PORT_PANEL_SCREENSHOT")
viewport_width = int(os.environ.get("PORT_PANEL_VIEWPORT_WIDTH", "1440"))
viewport_height = int(os.environ.get("PORT_PANEL_VIEWPORT_HEIGHT", "1000"))
ignore_https_errors = os.environ.get("PORT_PANEL_IGNORE_HTTPS_ERRORS", "false").lower() == "true"
errors = []
http_errors = []

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(
        viewport={"width": viewport_width, "height": viewport_height},
        ignore_https_errors=ignore_https_errors,
    )
    page.on("console", lambda message: errors.append(f"console:{message.type}:{message.text}") if message.type in {"error", "warning"} else None)
    page.on("pageerror", lambda error: errors.append(f"pageerror:{error}"))
    page.on("requestfailed", lambda request: errors.append(f"request:{request.url}:{request.failure}"))
    page.on("response", lambda response: http_errors.append((response.status, response.url)) if response.status >= 400 else None)
    page.goto(f"{base}/login/", wait_until="networkidle")
    page.locator('input[name="username"]').fill(username)
    page.locator('input[name="password"]').fill(password)
    page.locator('button[type="submit"]').click()
    page.wait_for_load_state("networkidle")
    page.goto(f"{base}/plugins/dcn-port-panel/", wait_until="networkidle")
    if device_id:
        page.goto(f"{base}/dcim/devices/{device_id}/port-panel/", wait_until="networkidle")
    else:
        assert page.get_by_text(expected_device).is_visible()
        page.get_by_role("link", name="Open port panel").click()
    page.wait_for_load_state("networkidle")
    assert page.get_by_text(expected_heading).is_visible()
    assert page.locator("#port-panel .port").count() == expected_ports
    if expected_peer:
        assert page.get_by_text(expected_peer, exact=False).is_visible()
    if expected_state:
        assert page.locator("#port-panel .port-state", has_text=expected_state).first.is_visible()
    page.wait_for_timeout(1000)
    assert "Loading" not in page.locator("#panel-summary").inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    for port in page.locator("#port-panel .port").all():
        box = port.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= viewport_width
    if screenshot_path:
        page.screenshot(path=screenshot_path, full_page=True)
    assert not errors, errors
    assert not http_errors, http_errors
    browser.close()
