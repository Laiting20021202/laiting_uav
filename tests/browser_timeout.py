"""Isolated Chromium regression: real idle cookies, slow startup and network recovery.

Run with `.venv/bin/python tests/browser_timeout.py`. Uses temporary credentials,
an ephemeral loopback demo server, and no live hardware or production accounts.
"""
import asyncio
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
from playwright.async_api import async_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from uav.auth import password_hash
from uav.config import load_settings
from uav.web import create_app


async def exercise(app, base, password):
    errors=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(
            executable_path=os.environ.get('UAV_CHROMIUM','/snap/bin/chromium'),
            headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        context=await browser.new_context()
        page=await context.new_page()
        page.on('pageerror',lambda e:errors.append(str(e)))
        counts={'me':0,'frame':0,'status':0}
        failures={'events':True,'models':True,'frame401':False,'status401':False,'proxy':False}

        async def me_route(route):
            counts['me']+=1
            if counts['me']==1:
                # Exceeds the JS request deadline and several real cookie lifetimes.
                await asyncio.sleep(13)
                try:
                    await route.abort('timedout')
                except Exception:
                    pass  # The browser may already have cancelled the request.
            elif failures['proxy']:
                await route.fulfill(status=401,content_type='text/html',body='Proxy temporarily unavailable')
            else:
                await route.continue_()

        async def api_route(route):
            path=route.request.url.rsplit('/',1)[-1]
            if path in ('events','models') and failures[path]:
                await route.fulfill(status=503,content_type='text/html',body='Upstream timeout')
            elif path in ('frame.jpg','status'):
                key='frame' if path=='frame.jpg' else 'status'
                counts[key]+=1
                if failures[key+'401']:
                    failures[key+'401']=False
                    await route.fulfill(status=401,json={'detail':'Login required'})
                else:
                    await route.continue_()
            else:
                await route.continue_()

        await context.route('**/api/**',api_route)
        await context.route('**/api/me',me_route)

        async def login():
            await page.goto(base+'/login')
            await page.locator('#username').fill('timeout-test')
            await page.locator('#password').fill(password)
            await page.locator('button[type=submit]').click()
            await page.wait_for_url(base+'/')
            await expect(page.locator('#camera')).to_be_visible(timeout=20000)
            await page.wait_for_function("document.getElementById('camera').naturalWidth>0")

        try:
            await login()
            await expect(page.locator('#events-error')).to_be_visible()
            await expect(page.locator('#action-error')).to_contain_text('模型清單載入失敗')
            await expect(page.locator('#mode')).to_have_text('SIMULATION')
            await expect(page.locator('#user')).to_have_text('timeout-test',timeout=20000)
            assert counts['me']>=2 and counts['frame']>10 and counts['status']>5
            assert page.url==base+'/'
            cookies=await context.cookies()
            cookie=next(c for c in cookies if c['name']=='uav_dev_session')
            assert cookie['expires']>time.time()
            print('PASS: 12s startup timeout retries; 4s cookie/session stays valid while viewing; optional APIs do not hide preview',flush=True)

            failures.update(events=False,models=False)
            await expect(page.locator('#events-error')).to_be_hidden(timeout=10000)
            await expect(page.locator('#action-error')).to_be_hidden(timeout=10000)
            await expect(page.locator('#model option').first).to_be_attached()

            failures.update(frame401=True,status401=True)
            await asyncio.sleep(3)
            assert not failures['frame401'] and not failures['status401']
            assert page.url==base+'/'
            await expect(page.locator('#camera')).to_be_visible()
            print('PASS: isolated/concurrent 401 responses confirmed without false logout',flush=True)

            # An HTML 401 from an intermediary must not count as session expiry.
            failures.update(proxy=True,frame401=True)
            await asyncio.sleep(2)
            assert page.url==base+'/'
            failures['proxy']=False
            await expect(page.locator('#camera')).to_be_visible(timeout=10000)

            await context.set_offline(True)
            await expect(page.locator('#camera')).to_be_visible(timeout=5000)
            await expect(page.locator('#preview-info')).to_contain_text('最後畫面',timeout=10000)
            await context.set_offline(False)
            await expect(page.locator('#camera')).to_be_visible(timeout=10000)
            assert page.url==base+'/'
            print('PASS: brief offline period recovers preview automatically',flush=True)

            # Genuine idle expiry still logs out, explains why, and can log in again.
            await context.set_offline(True)
            await asyncio.sleep(6)
            await context.set_offline(False)
            await page.wait_for_url(base+'/login?reason=expired',timeout=15000)
            await expect(page.locator('#error')).to_contain_text('登入已到期')
            await login()
            await expect(page.locator('#user')).to_have_text('timeout-test')
            await page.locator('#logout').click()
            await page.wait_for_url(base+'/login')
            assert (await page.request.get(base+'/api/status')).status==401
            assert not errors,errors
            print('PASS: idle expiry, re-login restores video, explicit logout revokes access; zero JS errors',flush=True)
        finally:
            await context.unroute_all(behavior='ignoreErrors')
            await browser.close()


def main():
    with tempfile.TemporaryDirectory(prefix='uav-timeout-test-') as directory:
        directory=Path(directory)
        password=secrets.token_urlsafe(24)
        users=directory/'users.json'
        users.write_text(json.dumps({'timeout-test':password_hash(password)}))
        config=load_settings(demo=True)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0))
            base=f'http://127.0.0.1:{listener.getsockname()[1]}'
            config['web']['origin']=base
            app=create_app(config,data_dir=directory/'data',users_path=users)
            app.state.auth.lifetime=4
            server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
            worker=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True)
            worker.start()
            try:
                deadline=time.monotonic()+15
                while not server.started:
                    if not worker.is_alive() or time.monotonic()>deadline:
                        raise RuntimeError('Demo server failed to start')
                    time.sleep(.05)
                asyncio.run(exercise(app,base,password))
            finally:
                server.should_exit=True
                worker.join(timeout=15)
                assert not worker.is_alive(),'Demo server failed to stop'


if __name__=='__main__':
    main()
