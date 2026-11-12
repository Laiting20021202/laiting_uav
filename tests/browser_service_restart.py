"""Explicit live restart test: retain login and recover the model worker in-page."""
import os
import subprocess
import time

from playwright.sync_api import sync_playwright, expect


def main():
    base=os.environ.get('UAV_TEST_ORIGIN','https://uav.laiting1202.com')
    errors=[]
    tunnel_pid=subprocess.check_output(['systemctl','--user','show','uav-cloudflared','-p','MainPID','--value'],text=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ.get('UAV_CHROMIUM','/snap/bin/chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        page=browser.new_page()
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(base+'/login')
        page.locator('#username').fill(os.environ['UAV_TEST_USERNAME'])
        page.locator('#password').fill(os.environ['UAV_TEST_PASSWORD'])
        page.locator('button[type=submit]').click()
        page.wait_for_url(base+'/')
        expect(page.locator('#restart-service')).to_be_enabled(timeout=20000)
        before=page.request.get(base+'/api/status').json()
        me=page.request.get(base+'/api/me').json()
        with page.expect_response(lambda r:r.url==base+'/api/system/restart') as submitted:
            page.locator('#restart-service').click()
        assert submitted.value.status==202
        assert submitted.value.json()['login_required_after_restart'] is False
        deadline=time.monotonic()+90
        after=None
        while time.monotonic()<deadline:
            assert page.url==base+'/', 'Restart unexpectedly redirected away from the dashboard'
            try:
                response=page.request.get(base+'/api/status',timeout=4000)
            except Exception:
                page.wait_for_timeout(500)
                continue
            assert response.status!=401,'Restart invalidated the session'
            if response.ok:
                after=response.json()
                if after['boot_id']!=before['boot_id'] and not after['model_loading']:
                    break
            page.wait_for_timeout(500)
        assert after and after['boot_id']!=before['boot_id']
        assert not after['model_loading'] and not after['model_error']
        assert page.request.get(base+'/api/me').json()==me
        expect(page.locator('#restart-service')).to_be_enabled(timeout=10000)
        expect(page.locator('#recovery-message')).to_contain_text('已重啟',timeout=10000)
        assert not errors,errors
        print('PASS: real HTTPS service restart, unchanged login/CSRF, model reloaded, button re-enabled, zero JS errors; camera_connected='+str(after['camera_connected']),flush=True)
        page.request.post(base+'/api/logout',headers={'Origin':base,'X-CSRF-Token':me['csrf']})
        browser.close()
    assert subprocess.check_output(['systemctl','--user','show','uav-cloudflared','-p','MainPID','--value'],text=True)==tunnel_pid


if __name__=='__main__':
    main()
