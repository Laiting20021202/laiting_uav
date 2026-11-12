"""Chromium checks against isolated responses; never switches the live network."""
import os
from pathlib import Path
import re
from uuid import uuid4

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / 'uav/web/index.html').read_text()
PANEL = re.search(r'<section class="panel network-panel".*?</section>', INDEX, re.S).group(0)
SCRIPT = (ROOT / 'uav/web/network.js').read_text()
STYLE = (ROOT / 'uav/web/style.css').read_text()


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get('UAV_CHROMIUM', '/snap/bin/chromium'),
                                              headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    page = browser.new_page(viewport={'width': 1100, 'height': 900})
    page.route('http://127.0.0.1:9876/', lambda route: route.fulfill(
        body='<html><head></head><body>' + PANEL + '</body></html>', content_type='text/html'))
    page.goto('http://127.0.0.1:9876/')
    page.add_style_tag(content=STYLE)
    page.add_script_tag(content=SCRIPT)
    page.evaluate("""() => {
      window.state = {active_path:'usb', preferred_path:'auto', phase:'stable', switching:false,
        controller_available:true,tunnel_ready:true,error:'',last_switch:null,
        usb:{available:true,connected:true,internet:true},wifi:{available:true,connected:true,internet:true}};
      window.posts = [];
      window.panel = new NetworkPanel({getCsrf:()=> 'csrf-for-test', api:async(path, options) => {
        if(options?.method==='POST') {window.posts.push(JSON.parse(options.body)); return {accepted:true};}
        return window.state;
      }});
      window.panel.render(window.state);
    }""")
    yield page
    page.close()


def test_current_path_comes_from_actual_route_even_when_usb_is_preferred(page):
    page.evaluate("() => panel.render({...state, active_path:'wifi', preferred_path:'usb'})")
    assert page.locator('#network-active-path').inner_text() == '網卡 Wi-Fi 4G'
    assert page.locator('#network-use-usb').get_attribute('aria-pressed') == 'true'
    assert page.locator('#network-use-usb').is_enabled()
    assert '備援就緒' in page.locator('#network-usb-detail').inner_text()


def test_timed_out_switch_is_not_replayed_and_finished_request_survives_reload(page):
    page.evaluate("""() => { panel.api = async(path,options) => {
      if(options?.method==='POST') {posts.push(JSON.parse(options.body));throw new Error('timeout');}
      return state;
    }; }""")
    page.locator('#network-use-wifi').click()
    page.wait_for_function('posts.length === 1 && panel.pending !== null')
    pending = page.evaluate('panel.pending')
    assert pending['target'] == 'wifi'
    assert len(pending['request_id']) == 36
    page.evaluate("""async() => {
      panel.offline(); await panel.refresh(); await panel.refresh();
      window.reloaded = new NetworkPanel({getCsrf:()=> 'csrf-for-test',api:panel.api});
    }""")
    assert page.evaluate('posts.length') == 1
    assert page.evaluate('reloaded.pending.request_id') == pending['request_id']
    page.evaluate("""() => reloaded.render({...state,active_path:'wifi',preferred_path:'wifi',
      last_switch:{request_id:reloaded.pending.request_id,target:'wifi',finished_at:100,route_seconds:1.2,website_seconds:4.5}})""")
    assert page.evaluate('reloaded.pending') is None
    assert '已確認目前使用 網卡 Wi-Fi 4G' in page.locator('#network-message').inner_text()
    assert '網頁 4.5 秒' in page.locator('#network-timing').inner_text()


def test_other_request_cannot_claim_switch_completion_and_root_error_releases_pending(page):
    page.locator('#network-use-wifi').click()
    page.wait_for_function('posts.length === 1')
    page.evaluate("""() => panel.render({...state,last_switch:{request_id:'other',finished_at:100}})""")
    assert page.evaluate('panel.pending') is not None
    page.evaluate("""() => panel.render({...state,active_path:'wifi',error:'出口已切換，網頁尚未恢復',
      last_switch:{request_id:panel.pending.request_id,target:'wifi',finished_at:null}})""")
    assert page.evaluate('panel.pending') is None
    assert '網頁尚未恢復' in page.locator('#network-message').inner_text()


def test_own_failed_result_unlocks_even_when_failure_precedes_last_switch(page):
    page.locator('#network-use-wifi').click()
    page.wait_for_function('posts.length === 1')
    page.evaluate("""() => panel.render({...state, error:'指定路徑已失效，保留 USB',
      recent_requests:[{request_id:panel.pending.request_id,target:'wifi',state:'failed'}]})""")
    assert page.evaluate('panel.pending') is None
    assert all(page.locator('#network-use-' + target).is_enabled() for target in ['auto', 'usb', 'wifi'])
    assert '此次切換未完成' in page.locator('#network-message').inner_text()
    assert 'USB 4G' in page.locator('#network-message').inner_text()
    assert page.evaluate('posts.length') == 1


def test_own_route_result_unlocks_after_other_client_replaces_last_switch(page):
    page.locator('#network-use-wifi').click()
    page.wait_for_function('posts.length === 1')
    page.evaluate("""() => panel.render({...state,
      last_switch:{request_id:crypto.randomUUID(),target:'usb',finished_at:100,route_seconds:0.4},
      recent_requests:[{request_id:panel.pending.request_id,target:'wifi',state:'route_ready'}]})""")
    assert page.evaluate('panel.pending') is None
    assert page.locator('#network-use-usb').is_enabled()
    assert '此次出口切換已完成；目前出口為 USB 4G' in page.locator('#network-message').inner_text()
    assert '目前使用 網卡 Wi-Fi' not in page.locator('#network-message').inner_text()
    page.locator('#network-use-usb').click()
    page.wait_for_function('posts.length === 2')


def test_own_route_result_unlocks_while_same_request_waits_for_tunnel(page):
    page.locator('#network-use-wifi').click()
    page.wait_for_function('posts.length === 1')
    page.evaluate("""() => panel.render({...state, active_path:'wifi',phase:'recovering_tunnel',tunnel_ready:false,
      last_switch:{request_id:panel.pending.request_id,target:'wifi',finished_at:null},
      recent_requests:[{request_id:panel.pending.request_id,target:'wifi',state:'route_ready'}]})""")
    assert page.evaluate('panel.pending.route_complete') is True
    assert page.locator('#network-use-usb').is_enabled()
    assert '網頁重連中' in page.locator('#network-message').inner_text()


@pytest.mark.parametrize('record', ['different_id', 'different_target', 'noncanonical_id', 'unknown_state', 'queued'])
def test_unrelated_or_incomplete_result_cannot_unlock_request(page, record):
    page.locator('#network-use-wifi').click()
    page.wait_for_function('posts.length === 1')
    page.evaluate("""(kind) => {
      const record = {request_id:panel.pending.request_id,target:'wifi',state:'route_ready'};
      if(kind==='different_id') record.request_id=crypto.randomUUID();
      if(kind==='different_target') record.target='usb';
      if(kind==='noncanonical_id') record.request_id=record.request_id.toUpperCase();
      if(kind==='unknown_state') record.state='complete';
      if(kind==='queued') record.state='queued';
      panel.render({...state,recent_requests:[record]});
    }""", record)
    assert page.evaluate('panel.pending.route_complete') is False
    assert page.locator('#network-use-usb').is_disabled()
    assert page.evaluate('posts.length') == 1


@pytest.mark.parametrize('result', ['failed', 'route_ready'])
def test_controller_fallback_cannot_acknowledge_even_matching_request(page, result):
    page.locator('#network-use-wifi').click()
    page.wait_for_function('posts.length === 1')
    page.evaluate("""(result) => panel.render({...state,controller_available:false,active_path:'wifi',
      last_switch:{request_id:panel.pending.request_id,target:'wifi',finished_at:100,route_seconds:0.4},
      recent_requests:[{request_id:panel.pending.request_id,target:'wifi',state:result}]})""", result)
    assert page.evaluate('panel.pending.route_complete') is False
    assert page.locator('#network-use-usb').is_disabled()
    assert '已完成' not in page.locator('#network-message').inner_text()
    assert '已確認目前使用' not in page.locator('#network-message').inner_text()


def test_queued_result_outweighs_conflicting_last_switch_and_unrelated_error(page):
    page.locator('#network-use-wifi').click()
    page.wait_for_function('posts.length === 1')
    page.evaluate("""() => panel.render({...state,active_path:'wifi',error:'其他檢查正在重試',
      last_switch:{request_id:panel.pending.request_id,target:'wifi',finished_at:100,route_seconds:0.4},
      recent_requests:[{request_id:panel.pending.request_id,target:'wifi',state:'queued'}]})""")
    assert page.evaluate('panel.pending.route_complete') is False
    assert page.locator('#network-use-usb').is_disabled()
    assert '已確認目前使用' not in page.locator('#network-message').inner_text()


def test_auto_result_tracks_request_mode_while_last_switch_records_actual_path(page):
    page.locator('#network-use-auto').click()
    page.wait_for_function('posts.length === 1')
    page.evaluate("""() => panel.render({...state,phase:'recovering_tunnel',tunnel_ready:false,
      last_switch:{request_id:panel.pending.request_id,target:'usb',finished_at:null},
      recent_requests:[{request_id:panel.pending.request_id,target:'auto',state:'route_ready'}]})""")
    assert page.evaluate('panel.pending.route_complete') is True
    assert page.locator('#network-use-wifi').is_enabled()


def test_same_identity_for_wrong_last_switch_target_cannot_claim_completion(page):
    page.locator('#network-use-wifi').click()
    page.wait_for_function('posts.length === 1')
    page.evaluate("""() => panel.render({...state,active_path:'wifi',
      last_switch:{request_id:panel.pending.request_id,target:'usb',finished_at:100,route_seconds:0.4}})""")
    assert page.evaluate('panel.pending.route_complete') is False
    assert page.locator('#network-use-usb').is_disabled()


@pytest.mark.parametrize('identity', ['bad-id', 'UPPER-CASE', 'a' * 100])
def test_reload_rejects_noncanonical_pending_identity(page, identity):
    page.evaluate("""(identity) => {
      sessionStorage.setItem('uav-network-switch', JSON.stringify({target:'wifi',request_id:identity,
        started_at:Date.now(),route_complete:true}));
      window.reloaded = new NetworkPanel({getCsrf:()=> 'csrf-for-test',api:panel.api});
      reloaded.render(state);
    }""", identity)
    assert page.evaluate('reloaded.pending') is None
    assert page.locator('#network-use-usb').is_enabled()


@pytest.mark.parametrize('phase', ['preparing', 'switching'])
def test_inflight_operation_disables_controls_for_other_clients(page, phase):
    page.evaluate('(phase) => panel.render({...state,phase})', phase)
    assert all(page.locator('#network-use-' + target).is_disabled() for target in ['auto', 'usb', 'wifi'])


@pytest.mark.parametrize('path', ['usb', 'wifi'])
@pytest.mark.parametrize('field, value, badge', [
    ('available', False, '故障'), ('connected', False, '修復中'), ('internet', False, '修復中'),
    ('available', None, '確認中'), ('connected', None, '確認中'), ('internet', None, '確認中'),
])
def test_unhealthy_or_unknown_path_is_disabled_then_recovery_enables_it(page, path, field, value, badge):
    page.evaluate("""({path,field,value}) => panel.render({...state,
      [path]:{available:true,connected:true,internet:true,[field]:value}})""", dict(path=path, field=field, value=value))
    button = page.locator('#network-use-' + path)
    assert button.is_disabled()
    assert '暫不可用' in button.inner_text()
    assert page.locator('#network-' + path + '-health').inner_text() == badge
    assert 'network-' + path + '-action-hint' in button.get_attribute('aria-describedby')
    assert page.locator('#network-' + path + '-action-hint').inner_text()
    page.evaluate('(path) => panel.switch(path)', path)
    assert page.evaluate('posts.length') == 0
    page.evaluate("""(path) => panel.render({...state,
      [path]:{available:true,connected:true,internet:true}})""", path)
    assert button.is_enabled()
    assert page.locator('#network-' + path + '-health').inner_text() == '可使用'
    button.click()
    page.wait_for_function('posts.length === 1')
    assert page.evaluate('posts[0].target') == path


def test_already_selected_healthy_path_can_be_chosen_again(page):
    page.evaluate("() => panel.render({...state,active_path:'usb',preferred_path:'usb'})")
    assert page.locator('#network-use-usb').is_enabled()
    assert page.locator('#network-use-usb').get_attribute('aria-pressed') == 'true'
    page.locator('#network-use-usb').click()
    page.wait_for_function('posts.length === 1')
    assert page.evaluate('posts[0].target') == 'usb'


@pytest.mark.parametrize('switching', [False, True])
def test_tunnel_recovery_does_not_lock_healthy_paths_for_other_clients(page, switching):
    page.evaluate("""(switching) => panel.render({...state,phase:'recovering_tunnel',
      tunnel_ready:false,switching})""", switching)
    assert all(page.locator('#network-use-' + target).is_enabled() for target in ['auto', 'usb', 'wifi'])
    page.locator('#network-use-wifi').click()
    page.wait_for_function('posts.length === 1')


def test_route_complete_unlocks_override_but_keeps_same_request_until_website_confirmation(page):
    page.locator('#network-use-wifi').click()
    page.wait_for_function('posts.length === 1')
    first_id = page.evaluate('panel.pending.request_id')
    page.evaluate("""() => {
      state = {...state,active_path:'wifi',phase:'recovering_tunnel',tunnel_ready:false,
        last_switch:{request_id:panel.pending.request_id,target:'wifi',finished_at:null,
          route_seconds:0.4,website_seconds:null}};
      panel.render(state);
    }""")
    assert page.evaluate('panel.pending.request_id') == first_id
    assert page.evaluate('panel.pending.route_complete') is True
    assert '出口已切換，網頁重連中' in page.locator('#network-message').inner_text()
    assert all(page.locator('#network-use-' + target).is_enabled() for target in ['auto', 'usb', 'wifi'])
    page.evaluate('async() => {await panel.refresh();await panel.refresh();}')
    assert page.evaluate('posts.length') == 1
    page.evaluate("""() => {
      window.reloaded = new NetworkPanel({getCsrf:()=> 'csrf-for-test',api:panel.api});
      reloaded.render(state);
    }""")
    assert page.evaluate('reloaded.pending.request_id') == first_id
    assert page.evaluate('reloaded.pending.route_complete') is True
    page.evaluate("""() => reloaded.render({...state,phase:'stable',tunnel_ready:true,
      last_switch:{...state.last_switch,finished_at:100,website_seconds:4.5}})""")
    assert page.evaluate('reloaded.pending') is None
    assert '網頁 4.5 秒' in page.locator('#network-timing').inner_text()


def test_healthy_override_uses_new_id_and_late_old_response_cannot_clear_it(page):
    page.evaluate("""() => {
      panel.api = async(path, options) => {
        if(options?.method === 'POST') {
          posts.push(JSON.parse(options.body));
          if(posts.length === 1) return new Promise((resolve,reject) => {window.rejectOld=reject;});
          return {accepted:true};
        }
        return state;
      };
    }""")
    page.locator('#network-use-wifi').click()
    page.wait_for_function('posts.length === 1')
    first_id = page.evaluate('panel.pending.request_id')
    page.evaluate("""() => panel.render({...state,active_path:'wifi',phase:'recovering_tunnel',tunnel_ready:false,
      last_switch:{request_id:panel.pending.request_id,target:'wifi',finished_at:null,
        route_seconds:0.4,website_seconds:null}})""")
    page.locator('#network-use-usb').click()
    page.wait_for_function('posts.length === 2')
    second_id = page.evaluate('panel.pending.request_id')
    assert first_id != second_id
    page.evaluate("""() => {let error=new Error('late rejection');error.status=409;rejectOld(error);} """)
    page.wait_for_timeout(50)
    assert page.evaluate('panel.pending.request_id') == second_id
    assert page.evaluate('posts.length') == 2


def test_wrong_request_or_unknown_route_cannot_unlock_pending_operation(page):
    page.locator('#network-use-wifi').click()
    page.wait_for_function('posts.length === 1')
    page.evaluate("""() => panel.render({...state,active_path:'wifi',phase:'recovering_tunnel',tunnel_ready:false,
      last_switch:{request_id:'other',target:'wifi',finished_at:null,route_seconds:0.4}})""")
    assert page.evaluate('panel.pending.route_complete') is False
    assert page.locator('#network-use-usb').is_disabled()
    page.evaluate("""() => panel.render({...state,active_path:'unknown',phase:'recovering_tunnel',tunnel_ready:false,
      last_switch:{request_id:panel.pending.request_id,target:'wifi',finished_at:null,route_seconds:0.4}})""")
    assert page.evaluate('panel.pending.route_complete') is False
    assert page.locator('#network-use-usb').is_disabled()


def test_controller_outage_does_not_discard_request_and_fresh_route_restores_controls(page):
    page.locator('#network-use-wifi').click()
    page.wait_for_function('posts.length === 1')
    request_id = page.evaluate('panel.pending.request_id')
    page.evaluate("""() => panel.render({...state,controller_available:false,phase:'offline',
      error:'切換服務暫時無法使用',last_switch:{request_id:panel.pending.request_id,target:'wifi',
        finished_at:null,route_seconds:null}})""")
    assert page.evaluate('panel.pending.request_id') == request_id
    assert page.locator('#network-use-usb').is_disabled()
    page.evaluate("""() => panel.render({...state,active_path:'wifi',phase:'recovering_tunnel',tunnel_ready:false,
      last_switch:{request_id:panel.pending.request_id,target:'wifi',finished_at:null,route_seconds:0.4}})""")
    assert page.evaluate('panel.pending.request_id') == request_id
    assert page.locator('#network-use-usb').is_enabled()
    assert page.evaluate('posts.length') == 1


def test_unavailable_broker_and_offline_page_do_not_show_stale_healthy_route(page):
    page.evaluate("() => panel.render({...state,active_path:'unknown',controller_available:false,phase:'offline',tunnel_ready:null})")
    assert '等待確認' in page.locator('#network-active-path').inner_text()
    assert page.locator('#network-use-wifi').is_disabled()
    page.evaluate('() => panel.offline()')
    assert '等待確認出口' in page.locator('#network-active-path').inner_text()
    assert page.locator('#network-usb-health').inner_text() == '確認中'
    assert page.locator('#network-wifi-health').inner_text() == '確認中'


def test_remote_error_is_literal_text_and_mobile_layout_has_no_overflow(page):
    page.evaluate("() => panel.render({...state,error:'<img src=x onerror=alert(1)>'})")
    assert page.locator('#network-message img').count() == 0
    assert '<img' in page.locator('#network-message').inner_text()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
