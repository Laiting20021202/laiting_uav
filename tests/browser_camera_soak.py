"""Observe the live dashboard for sustained video; never send hardware commands."""
import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--minutes', type=float, default=15)
    parser.add_argument('--report',type=Path,default=Path('/tmp/uav-camera-soak.json'))
    parser.add_argument('--screenshot',type=Path,default=Path('/tmp/uav-camera-soak.png'))
    args = parser.parse_args()
    if args.minutes <= 0:
        parser.error('--minutes must be positive')
    base = os.environ.get('UAV_TEST_ORIGIN', 'https://uav.laiting1202.com')
    rows, errors = [], []
    report = args.report
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get('UAV_CHROMIUM', '/snap/bin/chromium'),
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width':1440, 'height':1100})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base+'/'); page.wait_for_url('**/login')
        page.locator('#username').fill(os.environ['UAV_TEST_USERNAME'])
        page.locator('#password').fill(os.environ['UAV_TEST_PASSWORD'])
        page.locator('button[type=submit]').click(); page.wait_for_url(base+'/')
        expect(page.locator('#camera')).to_be_visible(timeout=30000)
        started = time.monotonic()
        next_report = 0
        while time.monotonic()-started < args.minutes*60:
            row = {'elapsed_s':round(time.monotonic()-started, 1), 'timestamp':time.time(), 'ok':False,
                   'dashboard_url':page.url}
            try:
                response = page.request.get(base+'/api/status', timeout=8000)
                row['status_http'] = response.status
                if response.ok:
                    state = response.json()
                    camera, flight = state['camera'], state['flight']
                    row.update(camera_connected=camera['connected'], camera_fps=camera['fps'],
                               camera_sessions=camera['connections'], boot_id=state['boot_id'],
                               camera_refreshes=camera.get('refreshes', 0),
                               camera_error=camera['error'], camera_phase=camera['phase'],
                               preview_age_s=state['preview_age_s'],
                               flight_connected=flight['connected'],
                               attitude_age_s=flight['ages_s'].get('ATTITUDE'))
                    if camera.get('control'):
                        row['control_connected'] = camera['control']['connected']
                        row['control_heartbeats'] = camera['control']['heartbeats']
                        row['control_sessions'] = camera['control']['sessions']
                        row['control_status'] = camera['control'].get('app_status')
                        row['control_error'] = camera['control'].get('error')
                        row['control_notifications'] = camera['control'].get('notifications')
                        row['control_hold_requests'] = camera['control'].get('hold_requests')
                        row['control_last_notification'] = camera['control'].get('last_notification')
                        row['control_configured'] = camera['control'].get('configured')
                        row['control_configuration'] = camera['control'].get('configuration')
                    frame = page.request.get(base+'/api/frame.jpg', timeout=8000)
                    row['frame_http'] = frame.status
                    if frame.ok:
                        row['frame_sha256'] = hashlib.sha256(frame.body()).hexdigest()
                    row['image_visible'] = page.locator('#camera').is_visible()
                    row['ok'] = bool(camera['connected'] and frame.ok and row['image_visible']
                                     and row['preview_age_s'] is not None and row['preview_age_s'] < 3)
            except Exception as error:
                row['error'] = type(error).__name__
            rows.append(row)
            report.write_text(json.dumps({'samples':rows, 'javascript_errors':errors}, indent=2))
            if row['elapsed_s'] >= next_report or not row['ok']:
                print(json.dumps(row), flush=True)
                next_report = row['elapsed_s']+60
            page.wait_for_timeout(5000)
        page.screenshot(path=str(args.screenshot), full_page=True)
        me = page.request.get(base+'/api/me')
        if me.ok:
            page.request.post(base+'/api/logout', headers={
                'Origin':base, 'X-CSRF-Token':me.json()['csrf']})
        browser.close()
    failures = sum(not row['ok'] for row in rows)
    hashes = {row['frame_sha256'] for row in rows if 'frame_sha256' in row}
    summary = {'duration_s':round(time.monotonic()-started, 1), 'samples':len(rows),
               'failed_samples':failures, 'distinct_frames':len(hashes),
               'javascript_errors':len(errors), 'report':str(report)}
    print(json.dumps(summary), flush=True)
    return 0 if rows and not failures and not errors and len(hashes)>1 else 1


if __name__ == '__main__':
    raise SystemExit(main())
