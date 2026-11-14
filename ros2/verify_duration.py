#!/usr/bin/env python3
"""Read-only live GUI regression: interrupted history must not keep timing.
A temporary login is removed in finally; no recording or hardware fault is started.
"""
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
    evidence=ROOT/'data/ros2/evidence/2026-11-14-duration-fix'
    evidence.mkdir(parents=True,exist_ok=True)
    users_path=ROOT/'config/users.json'
    username='ros-verify-'+secrets.token_hex(5)
    password=secrets.token_urlsafe(32)
    credential=password_hash(password)
    users=json.loads(users_path.read_text());users[username]=credential;atomic_json(users_path,users)
    name='20261112T044555Z_737c60c76c'
    report=dict(recording=name,checks={},errors=[],started_at=time.time())
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='/snap/bin/chromium',headless=True,
                args=['--no-sandbox','--disable-dev-shm-usage'])
            page=browser.new_page(viewport={'width':1500,'height':1100})
            page.on('pageerror',lambda e:report['errors'].append(str(e)))
            base='https://uav.laiting1202.com'
            page.goto(base+'/login',timeout=60000)
            page.locator('#username').fill(username);page.locator('#password').fill(password)
            page.locator('button[type=submit]').click();page.wait_for_url(base+'/',timeout=30000)
            page.goto(base+'/ros2/',timeout=60000)
            row=page.locator('#history tr').filter(has_text=name)
            row.wait_for(timeout=30000)
            cells=row.locator('td')
            before=cells.nth(1).inner_text()
            assert before=='未知（異常中斷）',before
            assert cells.nth(2).inner_text()=='未完成打包'
            assert cells.nth(3).inner_text()=='未驗證'
            assert '失敗' in cells.nth(4).inner_text()
            assert row.locator('a').count()==0
            page.wait_for_timeout(3200)
            after=cells.nth(1).inner_text()
            assert before==after
            row.screenshot(path=str(evidence/'history-row.png'))
            status=page.request.get(base+'/ros2/api/status').json()
            history={j['name']:j for j in status['recording']['history']}
            assert history[name]['state']=='failed'
            assert history[name]['elapsed_s'] is None
            assert history[name].get('stopped_at') is None
            for j in history.values():
                if j['state']=='ready':
                    assert j['duration_s'] is not None and j['elapsed_s'] is not None
            rendering=page.evaluate("""() => ({
                active: durationText({state:'recording',elapsed_s:5},true),
                zero: durationText({state:'ready',duration_s:0,elapsed_s:2},true),
                unknown: durationText({state:'failed',elapsed_s:null},true)
            })""")
            assert rendering==dict(active='5.00 s',zero='0.00 s',unknown='未知（異常中斷）')
            report['checks']=dict(duration_before=before,duration_after=after,
                failed_elapsed_null=True,failed_download_hidden=True,
                unpacked_size_not_shown_as_zero=True,unverified_topic_count_not_shown_as_zero=True,
                completed_recordings_keep_known_duration=True,formatting=rendering,
                current_state=status['recording']['current']['state'])
            assert not report['errors'],report['errors']
            browser.close()
    finally:
        users=json.loads(users_path.read_text())
        if users.get(username)==credential:
            del users[username];atomic_json(users_path,users)
        report.update(stopped_at=time.time(),temporary_test_user_removed=True)
        (evidence/'browser-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
    print(json.dumps(report,indent=2,ensure_ascii=False))


if __name__=='__main__':main()
