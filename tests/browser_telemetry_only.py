"""Isolated Chromium acceptance for live position with route recording disabled."""
import copy
import json
import secrets
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from uav.auth import password_hash
from uav.config import load_settings
from uav.runtime import Runtime
from uav.web import create_app


def main():
    with tempfile.TemporaryDirectory(prefix='uav-telemetry-only-') as temporary:
        directory = Path(temporary)
        password = secrets.token_urlsafe(24)
        users = directory / 'users.json'
        users.write_text(json.dumps({'verify': password_hash(password)}))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        config = load_settings(demo=True)
        config['web'].update(origin=origin, port=port)
        app = create_app(config, data_dir=directory / 'data', users_path=users, start_workers=False)
        flight = Runtime._demo_flight()
        app.state.runtime.flight_snapshot = lambda: copy.deepcopy(flight)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                              log_level='warning', access_log=False))
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if time.monotonic() > deadline:
                    raise TimeoutError('Test server did not start')
                time.sleep(.05)
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path='/snap/bin/chromium', headless=True,
                                           args=['--no-sandbox', '--disable-dev-shm-usage'])
                context = browser.new_context(viewport={'width':1440, 'height':1000})
                context.route('https://tile.openstreetmap.org/**', lambda route: route.abort())
                page = context.new_page()
                errors, route_requests = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: route_requests.append(request.url)
                        if '/api/flight-tracks' in request.url else None)
                page.goto(origin + '/login')
                page.locator('#username').fill('verify')
                page.locator('#password').fill(password)
                page.locator('button[type=submit]').click()
                page.wait_for_url(origin + '/')
                expect(page.locator('#map-gps-status')).to_have_text('模擬 GPS')
                expect(page.locator('#flight-track-panel')).to_be_hidden()
                expect(page.locator('#map-fit-track')).to_be_hidden()
                expect(page.locator('#track-disabled-note')).to_be_visible()
                assert not page.evaluate('flightMap.recordingEnabled')
                assert page.evaluate('flightMap.route === null && flightMap.ends === null')
                flight['longitude'] += .001
                expect(page.locator('#map-coordinate')).to_contain_text(f"{flight['longitude']:.7f}")
                flight['connected'] = False
                expect(page.locator('#map-gps-status')).to_have_text('飛控離線')
                expect(page.locator('#latitude')).to_have_text('—')
                flight['connected'] = True
                expect(page.locator('#map-gps-status')).to_have_text('模擬 GPS')
                # Wait past the previous one-second route polling cadence.
                page.wait_for_timeout(5500)
                assert not route_requests, route_requests
                assert not errors, errors
                assert not (directory / 'data/flight-tracks.sqlite3').exists()
                page.set_viewport_size({'width':390, 'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                browser.close()
        finally:
            server.should_exit = True
            worker.join(timeout=10)
    print('PASS: live GPS updates, stale position, reconnect, no route database or polling, mobile layout, zero JS errors')


if __name__ == '__main__':
    main()
