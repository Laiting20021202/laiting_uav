"""Isolated browser acceptance test; use --real-tiles to check the public basemap."""
import copy
import io
import json
import secrets
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from uav.auth import password_hash
from uav.config import load_settings
from uav.runtime import Runtime
from uav.web import create_app


def main():
    real_tiles = "--real-tiles" in sys.argv
    with tempfile.TemporaryDirectory(prefix="uav-flight-map-", dir=ROOT / "data") as directory:
        directory = Path(directory)
        users = directory / "users.json"
        password = secrets.token_urlsafe(24)
        users.write_text(json.dumps({"verify":password_hash(password)}))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        config = load_settings(demo=True)
        config["flight_tracks"]["enabled"] = True
        config["web"].update(origin=base, port=port)
        app = create_app(config, data_dir=directory / "data", users_path=users, start_workers=False)
        flight = Runtime._demo_flight()
        app.state.runtime.flight_snapshot = lambda: copy.deepcopy(flight)
        tracks = app.state.tracks
        tracks.flight = app.state.runtime.flight_snapshot
        tracks.start_worker()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        tile = Image.new("RGB", (256,256), "#e3e9df")
        draw = ImageDraw.Draw(tile)
        draw.line((0,128,256,128), fill="#fff", width=16)
        draw.line((128,0,128,256), fill="#fff", width=16)
        buf = io.BytesIO()
        tile.save(buf, "PNG")
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if time.monotonic() > deadline:
                    raise RuntimeError("Test server not ready")
                time.sleep(.05)
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path="/snap/bin/chromium", headless=True,
                    downloads_path=str(directory / "downloads"),
                    args=["--no-sandbox", "--disable-dev-shm-usage"])
                context = browser.new_context(viewport={"width":1440,"height":1100}, accept_downloads=True)
                if not real_tiles:
                    context.route("https://tile.openstreetmap.org/**", lambda route: route.fulfill(
                        body=buf.getvalue(), content_type="image/png", headers={"Access-Control-Allow-Origin":"*"}))
                page = context.new_page()
                errors, csp_errors = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda msg: csp_errors.append(msg.text) if "Content Security Policy" in msg.text else None)
                page.goto(base + "/login")
                page.locator("#username").fill("verify")
                page.locator("#password").fill(password)
                page.locator("button[type=submit]").click()
                page.wait_for_url(base + "/")
                expect(page.locator("#track-start")).to_be_enabled()
                expect(page.locator("#track-download")).to_be_disabled()
                expect(page.locator("#map-gps-status")).to_have_text("模擬 GPS")
                assert page.evaluate("flightMap.points.length") == 0
                flight["longitude"] += .0005
                page.wait_for_function("() => Math.abs(flightMap.marker.getLatLng().lng - 121.564968) < .0000001")
                assert tracks.snapshot()["track"] is None
                page.locator("#track-start").click()
                expect(page.locator("#track-status")).to_have_text("● 紀錄中")
                expect(page.locator("#track-start")).to_be_disabled()
                first_id = tracks.snapshot()["active"]["id"]
                for dlat, dlon in ((.0004,0), (.0002,.0005), (-.0002,.0004)):
                    flight["latitude"] += dlat
                    flight["longitude"] += dlon
                    tracks.sample()
                page.wait_for_function("() => flightMap.points.length >= 4")
                # The server keeps collecting when no dashboard page exists.
                page.goto("about:blank")
                before = tracks.snapshot()["active"]["point_count"]
                flight["latitude"] += .0002
                page.wait_for_timeout(1300)
                assert tracks.snapshot()["active"]["point_count"] > before
                page.goto(base + "/")
                expect(page.locator("#track-status")).to_have_text("● 紀錄中")
                page.wait_for_function("() => flightMap.points.length >= 5")
                flight["connected"] = False
                tracks.sample()
                expect(page.locator("#map-gps-status")).to_have_text("飛控離線")
                expect(page.locator(".aircraft-icon")).to_have_class("leaflet-marker-icon aircraft-icon leaflet-zoom-animated leaflet-interactive stale")
                count = tracks.snapshot()["active"]["point_count"]
                page.wait_for_timeout(1100)
                assert tracks.snapshot()["active"]["point_count"] == count
                flight.update(connected=True, longitude=flight["longitude"]+.0008)
                tracks.sample()
                page.wait_for_function("() => flightMap.points.some(p => p.segment === 1)")
                flight["latitude"] -= .0005
                tracks.sample()
                page.locator("#track-stop").click()
                expect(page.locator("#track-status")).to_have_text("已停止")
                expect(page.locator("#track-download")).to_be_enabled()
                completed_count = tracks.snapshot()["latest"]["point_count"]
                page.wait_for_timeout(1200)
                assert tracks.snapshot()["latest"]["point_count"] == completed_count
                assert page.evaluate("flightMap.route.getLatLngs().length") == 2
                page.locator(".map-workspace").scroll_into_view_if_needed()
                page.wait_for_function("() => Array.from(document.querySelectorAll('.leaflet-tile')).some(img => img.complete && img.naturalWidth > 0)")
                page.screenshot(path="/tmp/uav-flight-map-desktop.png")
                with page.expect_download(timeout=30000) as downloaded:
                    page.locator("#track-download").click()
                path = downloaded.value.path()
                image = Image.open(path)
                assert image.format == "JPEG" and image.size == (1400,1000)
                assert downloaded.value.suggested_filename == f"uav-flight-track-{first_id}-demo.jpg"
                pixels = image.crop((0,112,1400,900))
                assert any(b > r+60 and b > g+30 for r,g,b in pixels.get_flattened_data()), "Missing blue route in JPG"
                image.save("/tmp/uav-flight-track-export.jpg")
                page.reload()
                expect(page.locator("#track-download")).to_be_enabled()
                expect(page.locator("#track-status")).to_have_text("已停止")
                page.set_viewport_size({"width":390,"height":844})
                page.locator(".map-workspace").scroll_into_view_if_needed()
                page.wait_for_timeout(300)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path="/tmp/uav-flight-map-mobile.png")
                # A new session keeps the previous completed export available.
                page.locator("#track-start").click()
                expect(page.locator("#track-status")).to_have_text("● 紀錄中")
                expect(page.locator("#track-download")).to_be_enabled()
                assert tracks.snapshot()["active"]["id"] != first_id
                assert tracks.snapshot()["latest"]["id"] == first_id
                page.locator("#track-stop").click()
                expect(page.locator("#track-status")).to_have_text("已停止")
                # Missing tiles must report a failed export instead of a blank JPG.
                context.unroute("https://tile.openstreetmap.org/**")
                context.route("https://tile.openstreetmap.org/**", lambda route: route.abort())
                page.locator("#track-download").click()
                expect(page.locator("#track-export-message")).to_contain_text("下載失敗", timeout=20000)
                expect(page.locator("#track-download")).to_be_enabled()
                # A GPS-free session cannot produce a meaningless image.
                flight["connected"] = False
                page.locator("#track-start").click()
                expect(page.locator("#track-status")).to_have_text("● 紀錄中")
                page.locator("#track-stop").click()
                expect(page.locator("#track-status")).to_have_text("已停止")
                expect(page.locator("#track-download")).to_be_disabled()
                assert not errors, errors
                assert not csp_errors, csp_errors
                browser.close()
                print(f"PASS: {'real OSM' if real_tiles else 'fixture'} tiles, live position, opt-in recording, browser-away sampling, GPS gap, stop, JPG pixels, reload, mobile, repeated sessions, tile failure, empty track; zero JS/CSP errors")
        finally:
            server.should_exit = True
            worker.join(timeout=10)


if __name__ == "__main__":
    main()
