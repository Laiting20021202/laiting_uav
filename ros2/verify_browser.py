#!/usr/bin/env python3
"""Live acceptance through the existing HTTPS site. No synthetic publishers.
Creates a random temporary test login, removes only that credential in finally.
"""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import sys
import time
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from uav.auth import password_hash
from uav.config import atomic_json


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--base',default='https://uav.laiting1202.com')
    p.add_argument('--seconds',type=int,default=12)
    p.add_argument('--allow-camera-unavailable',action='store_true',help='Explicit partial-data acceptance; never fabricates an image')
    p.add_argument('--view-only',action='store_true',help='Check the dashboard without recording or fault injection')
    a=p.parse_args()
    evidence=ROOT/'data/ros2/evidence';evidence.mkdir(parents=True,exist_ok=True)
    users_path=ROOT/'config/users.json'
    username='ros-verify-'+secrets.token_hex(5);password=secrets.token_urlsafe(32)
    credential=password_hash(password)
    users=json.loads(users_path.read_text());users[username]=credential;atomic_json(users_path,users)
    report={'base':a.base,'started_at':time.time(),'checks':{},'errors':[]}
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='/snap/bin/chromium',headless=True,
                downloads_path=str(evidence/'browser-downloads'),
                args=['--no-sandbox','--disable-dev-shm-usage'])
            ctx=browser.new_context(viewport={'width':1500,'height':1100},accept_downloads=True)
            page=ctx.new_page();page.on('pageerror',lambda e:report['errors'].append(str(e)))
            page.goto(a.base+'/login',timeout=60000)
            page.locator('#username').fill(username);page.locator('#password').fill(password)
            page.locator('button[type=submit]').click();page.wait_for_url(a.base+'/',timeout=30000)
            report['checks']['original_login']=True
            # Original API confirms the original camera, inference and flight loop.
            original=ctx.request.get(a.base+'/api/status').json()
            (evidence/'original-status.json').write_text(json.dumps(original,indent=2))
            report['checks']['original_model']=original.get('model')
            report['checks']['original_inference_ms']=original.get('inference_ms')
            page.goto(a.base+'/ros2/',timeout=60000)
            page.wait_for_function("() => document.querySelectorAll('#topics tr').length===10",timeout=30000)
            page.wait_for_function("() => document.querySelectorAll('#topics .badge')[1].textContent==='正常'",timeout=45000)
            if not a.allow_camera_unavailable:
                page.wait_for_function("() => document.querySelector('#topics .badge').textContent==='正常'",timeout=45000)
            before=ctx.request.get(a.base+'/ros2/api/status').json()
            page.wait_for_timeout(6000)
            after=ctx.request.get(a.base+'/ros2/api/status').json()
            report['checks']['topic_counters_advance']={r['name']:r['count']>before['topics'][i]['count'] for i,r in enumerate(after['topics'])}
            report['checks']['states']={r['name']:r['state'] for r in after['topics']}
            (evidence/'gui-status.json').write_text(json.dumps(after,indent=2))
            page.screenshot(path=str(evidence/'gui-live.png'),full_page=True)
            if a.view_only:
                report['checks']['preflight_visible']=page.locator('#preflight').is_visible()
                report['checks']['preview_visible']=page.locator('#preview').is_visible()
                assert not report['errors'],report['errors']
                browser.close()
                return
            # Real bridge transport outage, without touching the camera or serial
            # owner. Always restore the socket, even when an assertion fails.
            source=ROOT/'data/ros2/source/source.sock'
            held=source.with_name('source.sock.acceptance-held')
            source.rename(held)
            try:
                page.wait_for_function("() => [...document.querySelectorAll('#topics .badge')].every(x=>x.textContent==='來源斷線')",timeout=15000)
                assert page.locator('#preview').is_hidden()
                page.screenshot(path=str(evidence/'gui-bridge-disconnected.png'),full_page=True)
                report['checks']['bridge_outage_detected']=True
            finally:
                held.rename(source)
            page.wait_for_function("() => document.querySelectorAll('#topics .badge')[1].textContent==='正常'",timeout=20000)
            report['checks']['bridge_reconnect']=True
            ctx.set_offline(True)
            try:
                page.wait_for_timeout(6000)
                assert page.locator('#preview').is_hidden()
                assert page.locator('#topics .normal').count()==0
                report['checks']['browser_stale_values_marked']=True
            finally:
                ctx.set_offline(False)
            page.wait_for_function("() => document.querySelectorAll('#topics .badge')[1].textContent==='正常'",timeout=20000)
            page.locator('#start').click()
            page.wait_for_function("() => document.querySelector('#record-state').textContent==='錄製中'",timeout=30000)
            me=ctx.request.get(a.base+'/api/me').json()
            headers={'Origin':a.base,'X-CSRF-Token':me['csrf']}
            dup=ctx.request.post(a.base+'/ros2/api/start',headers=headers)
            assert dup.status==409,dup.text()
            report['checks']['duplicate_start_rejected']=True
            page.wait_for_timeout(a.seconds*1000)
            page.locator('#stop').click()
            dup_stop=ctx.request.post(a.base+'/ros2/api/stop',headers=headers)
            assert dup_stop.status==409,dup_stop.text()
            report['checks']['duplicate_stop_rejected']=True
            page.wait_for_function("() => document.querySelector('#record-state').textContent==='ZIP 已完成' || document.querySelector('#record-state').textContent.startsWith('失敗')",timeout=150000)
            status=ctx.request.get(a.base+'/ros2/api/status').json()
            (evidence/'recording-status.json').write_text(json.dumps(status,indent=2))
            job=status['recording']['current']
            assert job['state']=='ready',job.get('error')
            report['checks']['recording_finalized']=True
            report['checks']['recording_integrity']=job['integrity']
            report['recording_name']=job['name']
            with page.expect_download(timeout=60000) as event:
                page.locator('#history a.download').first.click()
            download=event.value;target=evidence/'download.zip';download.save_as(str(target))
            assert download.failure() is None,download.failure()
            report['checks']['selected_zip_download']=target.stat().st_size
            digest=hashlib.sha256(target.read_bytes()).hexdigest()
            assert digest==job['zip_sha256']
            report['checks']['download_sha256_matches']=True
            older=next((h for h in status['recording']['history'] if h['name']!=job['name'] and h['state']=='ready'),None)
            if older:
                with page.expect_download(timeout=60000) as event:
                    page.locator('#history a[href$="'+older['name']+'"]').click()
                event.value.save_as(str(evidence/'download-history.zip'))
                assert event.value.failure() is None,event.value.failure()
                assert hashlib.sha256((evidence/'download-history.zip').read_bytes()).hexdigest()==older['zip_sha256']
                report['checks']['select_older_archive']=older['name']
            page.screenshot(path=str(evidence/'gui-zip-ready.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(1000)
            assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth')
            report['checks']['mobile_layout']=True
            page.screenshot(path=str(evidence/'gui-mobile.png'),full_page=True)
            # LAN browser path uses the same stored credentials via HTTP Basic.
            lan=browser.new_context(http_credentials={'username':username,'password':password})
            local=lan.new_page();local.goto('http://127.0.0.1:8090/',timeout=30000)
            local.wait_for_function("() => document.querySelectorAll('#topics tr').length===10")
            report['checks']['lan_gui_authenticated']=True
            browser.close()
        assert not report['errors'],report['errors']
    finally:
        users=json.loads(users_path.read_text())
        if users.get(username)==credential:
            del users[username];atomic_json(users_path,users)
        report['temporary_test_user_removed']=True
        report['stopped_at']=time.time()
        (evidence/('browser-smoke-report.json' if a.view_only else 'browser-report.json')).write_text(json.dumps(report,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
