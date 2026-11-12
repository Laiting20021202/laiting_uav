"""Isolated Chromium check: pinned example, JPG download, paging and one-click clear."""
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
import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from uav.auth import password_hash
from uav.config import load_settings, model_spec
from uav.geo import locate
from uav.web import create_app
from scripts.create_record_example import make_example


def main():
    with tempfile.TemporaryDirectory(prefix='uav-records-browser-',dir=ROOT/'data') as directory:
        directory=Path(directory);users=directory/'users.json';password=secrets.token_urlsafe(24)
        users.write_text(json.dumps({'verify':password_hash(password)}))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        config=load_settings(demo=True);config['web'].update(origin=base,port=port)
        app=create_app(config,data_dir=directory/'data',users_path=users,start_workers=False)
        event,jpeg,thumb=make_example();store=app.state.store
        sample=store.add(event,jpeg,thumb)
        flight={**event['aircraft'],'fix_type':3}
        estimated={**event,'example':False,'target_gps':locate(event['detection']['pixel'],(1920,1080),flight,load_settings()['geolocation'])}
        for _ in range(23):store.add(estimated,jpeg,thumb)
        names=model_spec('president-cup-v5')['classes']
        candidates=[dict(class_name=name,confidence=score) for name,score in zip(names,[.9,.3,.05,.001,.00001])]
        detection={**event['detection'],'class_name':names[0],'confidence':.9,'class_candidates':candidates}
        runtime=app.state.runtime;runtime.demo=False
        runtime._process(np.zeros((1080,1920,3),np.uint8),flight,0,time.time(),[detection])
        runtime.demo=True
        candidate_ids={e['id'] for e in store.list(5)}
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning',access_log=False))
        worker=threading.Thread(target=server.run,daemon=True);worker.start()
        try:
            deadline=time.monotonic()+10
            while not server.started:
                if time.monotonic()>deadline:raise RuntimeError('Test server not ready')
                time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path='/snap/bin/chromium',headless=True,downloads_path=str(directory/'downloads'),args=['--no-sandbox','--disable-dev-shm-usage'])
                page=browser.new_page(viewport={'width':1440,'height':1100});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(base+'/login');page.locator('#username').fill('verify');page.locator('#password').fill(password)
                page.locator('button[type=submit]').click();page.wait_for_url(base+'/')
                cards=page.locator('.event-card');expect(cards).to_have_count(21)
                expect(cards.first).to_have_class('event-card example-record')
                expect(cards.first).to_contain_text('目標緯度')
                expect(cards.first).to_contain_text(f"{event['target_gps']['latitude']:.7f}")
                expect(cards.first).to_contain_text(f"{event['target_gps']['longitude']:.7f}")
                expect(cards.nth(1)).to_contain_text('估算座標（鏡頭未校正）')
                expect(cards.nth(1)).to_contain_text('<0.1%')
                expect(cards.nth(1)).to_contain_text('可能類別 5/5')
                expect(cards.nth(1)).to_contain_text('最高機率：'+names[0])
                for rank in range(1,6):
                    expect(cards.nth(rank)).to_contain_text(names[5-rank])
                    assert cards.nth(rank).evaluate("el=>el.querySelector('.badge').getBoundingClientRect().right<=el.getBoundingClientRect().right")
                expect(cards.nth(1)).to_contain_text('GPS 3D')
                expect(cards.nth(1)).to_contain_text(f"{estimated['target_gps']['latitude']:.7f}")
                expect(cards.nth(1)).to_contain_text('假設地面與起飛點同高')
                page.locator('.events-panel').evaluate("el=>el.scrollIntoView({block:'start'})")
                page.wait_for_function("()=>document.querySelector('.event-card img').naturalWidth>0")
                page.screenshot(path='/tmp/uav-records-desktop.png')
                with page.expect_download() as downloaded:
                    cards.first.get_by_role('link',name='下載 JPG ↓').click()
                image=Image.open(downloaded.value.path())
                assert downloaded.value.suggested_filename.endswith('.jpg')
                assert image.width==1920 and image.height>1080
                assert 'EPSG:3826' in image.getexif()[270] and 'EXAMPLE' in image.getexif()[270]
                assert 'EPSG:4326' in image.getexif()[270]
                for candidate_id in candidate_ids:
                    card=page.locator(f'.event-card[data-event-id="{candidate_id}"]')
                    with page.expect_download() as downloaded:
                        card.get_by_role('link',name='下載 JPG ↓').click()
                    description=Image.open(downloaded.value.path()).getexif()[270]
                    assert 'Candidate:' in description and 'Most likely: '+names[0] in description
                photo_url=f"{base}/api/events/{sample['id']}/image.jpg"
                assert page.request.get(photo_url).body()==jpeg
                page.locator('#more-events').click();expect(cards).to_have_count(29)
                page.locator('#more-events').click();expect(page.locator('#more-events')).to_be_hidden()
                page.set_viewport_size({'width':390,'height':844})
                page.locator('.events-panel').evaluate("el=>el.scrollIntoView({block:'start'})");page.wait_for_timeout(300)
                assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                for candidate_id in candidate_ids:
                    assert page.locator(f'.event-card[data-event-id="{candidate_id}"]').evaluate("el=>el.querySelector('.badge').getBoundingClientRect().right<=el.getBoundingClientRect().right")
                page.screenshot(path='/tmp/uav-records-mobile.png')
                with page.expect_response(lambda r:r.url==base+'/api/events' and r.request.method=='DELETE') as deleted:
                    page.locator('#clear-events').click()
                assert deleted.value.json()['deleted']==29
                expect(cards).to_have_count(0);expect(page.locator('#no-events')).to_be_visible()
                assert page.request.get(photo_url).status==404
                page.wait_for_timeout(3500)
                expect(cards).to_have_count(0)
                page.reload();expect(page.locator('#no-events')).to_be_visible()
                expect(cards).to_have_count(0)
                assert not errors,errors
                browser.close()
                print('PASS: five candidate cards and JPG downloads, low positive confidence, pinned example, older records, mobile layout, one-click clear, deleted JPG 404, empty after refresh; zero JS errors')
        finally:
            server.should_exit=True;worker.join(timeout=10)


if __name__=='__main__':main()
