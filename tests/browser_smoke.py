"""Real Chromium smoke test against the local demo; credentials only from environment."""
import os
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]


def main():
    username=os.environ['UAV_TEST_USERNAME']
    password=os.environ['UAV_TEST_PASSWORD']
    errors=[]
    with tempfile.TemporaryDirectory(prefix='browser-',dir=ROOT/'data') as downloads, sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ.get('UAV_CHROMIUM','/snap/bin/chromium'),headless=True,downloads_path=downloads,args=['--no-sandbox','--disable-dev-shm-usage'])
        page=browser.new_page(viewport={'width':1440,'height':1100},device_scale_factor=1)
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto('http://127.0.0.1:8081/')
        page.wait_for_url('**/login')
        page.locator('#username').fill(username)
        page.locator('#password').fill(password)
        page.locator('button[type=submit]').click()
        page.wait_for_url('http://127.0.0.1:8081/')
        page.locator('#camera').wait_for(state='visible')
        page.wait_for_function("document.getElementById('camera').naturalWidth > 0")
        page.wait_for_function("document.querySelectorAll('#events .event-card').length > 0")
        assert page.locator('#demo-tag').is_visible()
        assert page.locator('#mode').inner_text()=='SIMULATION'
        page.screenshot(path=str(ROOT/'docs/dashboard-demo.png'),full_page=True)
        with page.expect_download() as download:
            page.locator('a[href="/api/events.csv"]').click()
        csv=Path(download.value.path()).read_text(encoding='utf-8-sig')
        assert 'target_latitude' in csv and 'simulation' in csv
        page.set_viewport_size({'width':390,'height':844})
        page.wait_for_timeout(500)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path='/tmp/uav-v2-mobile.png',full_page=True)
        page.locator('#logout').click()
        page.wait_for_url('**/login')
        assert page.request.get('http://127.0.0.1:8081/api/status').status==401
        assert not errors, errors
        browser.close()
    print('PASS: browser login, preview, telemetry, events, CSV, mobile layout, logout; zero JS errors')


if __name__=='__main__':
    main()
