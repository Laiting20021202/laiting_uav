"""Explicit live GUI recovery test; reconnects hardware and restarts only uav-v2."""
import os
import subprocess
import time
from playwright.sync_api import sync_playwright, expect


def main():
    base=os.environ.get('UAV_TEST_ORIGIN','https://uav.laiting1202.com')
    username=os.environ['UAV_TEST_USERNAME'];password=os.environ['UAV_TEST_PASSWORD']
    errors=[]
    tunnel_pid=subprocess.check_output(['systemctl','--user','show','uav-cloudflared','-p','MainPID','--value'],text=True).strip()
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ.get('UAV_CHROMIUM','/snap/bin/chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        page=browser.new_page(viewport={'width':1440,'height':1100})
        page.on('pageerror',lambda error:errors.append(str(error)))
        def login():
            page.goto(base+'/');page.wait_for_url('**/login')
            page.locator('#username').fill(username);page.locator('#password').fill(password)
            page.locator('button[type=submit]').click();page.wait_for_url(base+'/')
            expect(page.locator('#camera')).to_be_visible(timeout=45000)
        def state():
            response=page.request.get(base+'/api/status');assert response.status==200
            return response.json()
        login();initial=state()
        assert initial['flight']['connected'] and initial['camera_connected']
        assert initial['flight']['system_id']==1 and initial['flight']['ages_s']['ATTITUDE']<2
        for device,button in [('camera','#reconnect-camera'),('flight','#reconnect-flight')]:
            before=state()[device]['connections']
            expect(page.locator(button)).to_be_enabled(timeout=10000)
            page.locator(button).click()
            deadline=time.monotonic()+45
            while time.monotonic()<deadline:
                current=state()
                if current[device]['connected'] and current[device]['connections']>before:
                    break
                page.wait_for_timeout(500)
            assert current[device]['connected'] and current[device]['connections']>before
            print('PASS GUI reconnect',device,flush=True)
        old_boot=state()['boot_id']
        page.locator('#restart-service').click()
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            assert page.url==base+'/'
            try:
                final=state()
                if final['boot_id']!=old_boot and final['flight']['connected'] and final['camera_connected']:
                    break
            except Exception:
                pass
            page.wait_for_timeout(500)
        assert final['boot_id']!=old_boot and final['flight']['connected'] and final['camera_connected']
        assert final['flight']['system_id']==1 and final['flight']['ages_s']['ATTITUDE']<2
        expect(page.locator('#model-status')).to_contain_text('visdrone-engine',timeout=30000)
        # A missing model registry must not block independent video/telemetry polling.
        page.route('**/api/models',lambda route:route.fulfill(status=503,content_type='application/json',body='{"detail":"model registry unavailable"}'))
        page.reload()
        expect(page.locator('#camera')).to_be_visible(timeout=20000)
        expect(page.locator('#flight-status')).to_have_text('已連線')
        expect(page.locator('#action-error')).to_contain_text('模型清單載入失敗')
        page.unroute('**/api/models');page.reload()
        expect(page.locator('#camera')).to_be_visible(timeout=20000)
        page.screenshot(path='/tmp/uav-recovery-desktop.png',full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path='/tmp/uav-recovery-mobile.png',full_page=True)
        assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth')
        assert not errors,errors
        me=page.request.get(base+'/api/me').json()
        page.request.post(base+'/api/logout',headers={'Origin':base,'X-CSRF-Token':me['csrf']})
        browser.close()
    assert subprocess.check_output(['systemctl','--user','show','uav-cloudflared','-p','MainPID','--value'],text=True).strip()==tunnel_pid
    print('PASS GUI service restart, both hardware connections recovered, unchanged SSH tunnel, mobile layout, zero JS errors')


if __name__=='__main__':
    main()
