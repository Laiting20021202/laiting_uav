"""Isolated Chromium check for confidence controls and both new model choices."""
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from uav.auth import password_hash
from uav.config import load_settings
from uav.web import create_app


def main():
    with tempfile.TemporaryDirectory(prefix='uav-detection-browser-',dir=ROOT/'data') as directory:
        directory=Path(directory)
        settings=directory/'settings.json'
        settings.write_text((ROOT/'config/settings.json').read_text())
        users=directory/'users.json';password=secrets.token_urlsafe(24)
        users.write_text(json.dumps({'verify':password_hash(password)}))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        config=load_settings(settings);config['web'].update(origin=base,port=port)
        # Keep real hardware readers stopped and persistence isolated in this fixture.
        app=create_app(config,data_dir=directory/'data',users_path=users,start_workers=False)
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,
                                           log_level='warning',access_log=False))
        worker=threading.Thread(target=server.run,daemon=True);worker.start()
        try:
            deadline=time.monotonic()+10
            while not server.started:
                if time.monotonic()>deadline:raise RuntimeError('Test server not ready')
                time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=os.environ.get('UAV_CHROMIUM','/snap/bin/chromium'),
                                          headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
                page=browser.new_page(viewport={'width':1440,'height':1100});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(base+'/login');page.locator('#username').fill('verify')
                page.locator('#password').fill(password);page.locator('button[type=submit]').click()
                page.wait_for_url(base+'/')
                expect(page.locator('#model')).to_have_value('president-cup-v5')
                assert page.locator('#model option[value="competition-5class-v1-3"]').count()==1
                field=page.locator('#confidence');apply=page.locator('#apply-confidence')
                expect(field).to_be_enabled();expect(field).to_have_value('45')
                continuation=page.locator('#track-confidence');hold=page.locator('#track-ttl')
                expect(continuation).to_have_value('10');expect(hold).to_have_value('0.35')
                continuation.fill('12');hold.fill('0.25')
                field.fill('15')
                page.wait_for_timeout(2100)
                expect(field).to_have_value('15')  # Polling must preserve unsaved edits.
                expect(page.locator('#confidence-range')).to_have_value('15')
                apply.click()
                expect(page.locator('#confidence-message')).to_contain_text('已套用 15%')
                assert load_settings(settings)['detection']['vehicle_conf']==0.15
                assert load_settings(settings)['detection']['track_conf']==0.12
                assert load_settings(settings)['detection']['track_ttl_s']==0.25
                assert page.request.get(base+'/api/status').json()['detection_confidence']==0.15
                slider=page.locator('#confidence-range');slider.fill('45')
                expect(field).to_have_value('45');apply.click()
                expect(page.locator('#confidence-message')).to_contain_text('已套用 45%')
                page.reload();expect(field).to_have_value('45')
                expect(continuation).to_have_value('12');expect(hold).to_have_value('0.25')
                # Invalid form input cannot submit a threshold outside the supported range.
                field.fill('0');apply.click()
                assert load_settings(settings)['detection']['vehicle_conf']==0.45
                field.fill('20')
                page.route('**/api/detection/confidence',lambda route:route.fulfill(
                    status=503,content_type='application/json',body=json.dumps({'detail':'test save failure'})))
                apply.click()
                expect(page.locator('#confidence-message')).to_contain_text('套用未確認完成')
                expect(field).to_have_value('20');expect(apply).to_be_enabled()
                assert load_settings(settings)['detection']['vehicle_conf']==0.45
                page.unroute('**/api/detection/confidence');apply.click()
                expect(page.locator('#confidence-message')).to_contain_text('已套用 20%')
                assert load_settings(settings)['detection']['vehicle_conf']==0.2
                page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(300)
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                assert '2 秒' not in page.locator('.events-panel .panel-heading p').inner_text()
                assert not errors,errors
                browser.close()
        finally:
            server.should_exit=True;worker.join(timeout=10)
    print('PASS: both model choices; numeric/slider confidence; live API and isolated persistence; reload; unsaved edits; invalid input; failed save and retry; mobile layout; zero JS errors')


if __name__=='__main__':
    main()
