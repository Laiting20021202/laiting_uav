"""HTTP boundary tests: images/events/model changes never bypass authentication."""
import json
import time
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from uav.auth import password_hash
from uav.config import load_settings
from uav.web import create_app


@pytest.fixture
def client(tmp_path):
    users = tmp_path/"users.json"
    users.write_text(json.dumps({"test": password_hash("test-password")}))
    config = load_settings()
    app = create_app(config, data_dir=tmp_path/"data", users_path=users, start_workers=False)
    with TestClient(app, base_url=config["web"]["origin"]) as client:
        yield client


def login(client):
    result = client.post('/api/login', json={"username": "test", "password": "test-password"}, headers={"Origin": str(client.base_url).rstrip('/')})
    assert result.status_code == 200
    return client.get('/api/me').json()['csrf']


@pytest.mark.parametrize("path", ['/api/status','/api/events','/api/events.csv','/api/models','/api/frame.jpg','/api/video.mjpg','/api/me'])
def test_private_routes(client, path):
    assert client.get(path).status_code == 401


def test_missing_model_registry_is_reported_without_breaking_status(client,monkeypatch):
    login(client)
    def missing():raise FileNotFoundError('registry moved')
    monkeypatch.setattr('uav.web.read_registry',missing)
    response=client.get('/api/models')
    assert response.status_code==503 and '模型清單' in response.json()['detail']
    assert client.get('/api/status').status_code==200


def test_login_cookie_and_logout_revoke(client):
    csrf = login(client)
    cookie = client.cookies.get('__Host-uav_session')
    assert cookie
    response = client.get('/api/status')
    assert response.status_code == 200
    assert response.headers['Cache-Control'].startswith('no-store')
    assert response.headers['Content-Security-Policy'].find("frame-ancestors 'none'") >= 0
    headers = {'Origin': str(client.base_url).rstrip('/'), 'X-CSRF-Token': csrf}
    assert client.post('/api/logout', headers=headers).status_code == 200
    client.cookies.set('__Host-uav_session', cookie)
    assert client.get('/api/status').status_code == 401


def test_cross_origin_and_csrf(client):
    assert client.post('/api/login', json={'username':'test','password':'test-password'}, headers={'Origin':'https://evil.test'}).status_code == 403
    login(client)
    assert client.post('/api/models/select', json={'name':'visdrone-pt'}, headers={'Origin':str(client.base_url).rstrip('/')}).status_code == 403
    assert client.post('/api/logout').status_code == 403


def test_cookie_flags_and_expiry(client):
    result=client.post('/api/login',json={'username':'test','password':'test-password'},headers={'Origin':str(client.base_url).rstrip('/')})
    for text in ('HttpOnly','Secure','SameSite=strict','Path=/'):
        assert text in result.headers['set-cookie']
    for session in client.app.state.auth.sessions.values():
        session['expires'] = time.monotonic()-1
    assert client.get('/api/status').status_code == 401


def test_password_change_revokes_existing_session(client):
    login(client)
    client.app.state.auth.users_path.write_text(json.dumps({'test':password_hash('changed-password')}))
    assert client.get('/api/me').status_code == 401


@pytest.mark.parametrize('path,status', [('/api/status',200),('/api/me',200),
                                      ('/api/frame.jpg',503),('/',200)])
def test_viewing_renews_server_session_and_browser_cookie(client, monkeypatch, path, status):
    now=[100.0]
    monkeypatch.setattr('uav.auth.time',SimpleNamespace(monotonic=lambda:now[0],time=lambda:1700000000+now[0]))
    auth=client.app.state.auth
    auth.lifetime=10
    login(client)
    cookie=client.cookies.get('__Host-uav_session')
    csrf=auth.get(cookie)['csrf']
    for at in (109,118,127):
        now[0]=at
        response=client.get(path)
        assert response.status_code==status
        for flag in ('Max-Age=10','HttpOnly','Secure','SameSite=strict','Path=/'):
            assert flag in response.headers['set-cookie']
        assert auth.get(cookie)['expires']==at+10
        assert auth.get(cookie)['csrf']==csrf
    # A viewer who stops making requests still expires; refresh cannot revive it.
    now[0]=137
    response=client.get('/api/me')
    assert response.status_code==401 and 'set-cookie' not in response.headers
    assert auth.get(cookie,refresh=True) is None


def test_public_routes_and_rejected_actions_do_not_renew_session(client, monkeypatch):
    now=[100.0]
    monkeypatch.setattr('uav.auth.time',SimpleNamespace(monotonic=lambda:now[0],time=lambda:1700000000+now[0]))
    client.app.state.auth.lifetime=10
    login(client)
    now[0]=109
    for path in ('/healthz','/login','/assets/app.js'):
        assert 'set-cookie' not in client.get(path).headers
    rejected=client.post('/api/system/restart',headers={'Origin':str(client.base_url).rstrip('/')})
    assert rejected.status_code==403 and 'set-cookie' not in rejected.headers
    now[0]=110
    assert client.get('/api/me').status_code==401


def test_logout_only_sends_cookie_deletion(client):
    csrf=login(client)
    response=client.post('/api/logout',headers={'Origin':str(client.base_url).rstrip('/'),'X-CSRF-Token':csrf})
    assert len(response.headers.get_list('set-cookie'))==1
    assert 'Max-Age=0' in response.headers['set-cookie']


def test_rate_limit_and_oversized_body(client):
    headers={'Origin':str(client.base_url).rstrip('/')}
    for _ in range(10):
        assert client.post('/api/login',json={'username':'test','password':'wrong'},headers=headers).status_code == 401
    assert client.post('/api/login',json={'username':'test','password':'wrong'},headers=headers).status_code == 429
    assert client.post('/api/login',content='x'*4097,headers=headers).status_code == 413


def test_no_stale_camera_snapshot_or_arbitrary_files(client):
    login(client)
    runtime=client.app.state.runtime
    runtime.jpeg=b'old-image';runtime.preview_at=time.monotonic()-4
    assert client.get('/api/frame.jpg').status_code == 503
    assert client.get('/assets/settings.json').status_code == 404
    assert client.get('/config/users.json').status_code == 404


def test_reject_unknown_model_and_conflicting_switch(client):
    csrf=login(client)
    headers={'Origin':str(client.base_url).rstrip('/'),'X-CSRF-Token':csrf}
    assert client.post('/api/models/select',json={'name':'../../etc/passwd'},headers=headers).status_code == 400
    assert client.post('/api/models/select',json={'name':'visdrone-pt'},headers=headers).status_code == 409


def test_host_validation(client):
    assert client.get('/healthz',headers={'Host':'evil.test'}).status_code == 400


@pytest.mark.parametrize('path',['/api/system/reconnect','/api/system/restart'])
def test_recovery_needs_login_and_csrf(client,path):
    headers={'Origin':str(client.base_url).rstrip('/')}
    assert client.post(path,json={'device':'all'},headers=headers).status_code==401
    login(client)
    assert client.post(path,json={'device':'all'},headers=headers).status_code==403


def test_reconnect_only_known_devices_and_no_duplicate_work(client):
    csrf=login(client);headers={'Origin':str(client.base_url).rstrip('/'),'X-CSRF-Token':csrf}
    assert client.post('/api/system/reconnect',json={'device':'camera'},headers=headers).status_code==202
    assert client.app.state.runtime.camera.reconnect_event.is_set()
    assert not client.app.state.runtime.telemetry.reconnect_event.is_set()
    assert client.post('/api/system/reconnect',json={'device':'all'},headers=headers).status_code==409
    assert client.post('/api/system/reconnect',json={'device':'../../bin/sh'},headers=headers).status_code==422


def test_restart_submits_fixed_service_only(client,monkeypatch):
    csrf=login(client);headers={'Origin':str(client.base_url).rstrip('/'),'X-CSRF-Token':csrf}
    assert client.post('/api/system/restart',headers=headers).status_code==503
    runtime=client.app.state.runtime;runtime.control.enabled=True
    calls=[]
    monkeypatch.setattr('uav.control.subprocess.run',lambda cmd,**kwargs:calls.append(cmd))
    assert client.post('/api/system/restart',headers=headers).status_code==202
    assert calls==[['systemd-run','--user','--collect','--unit=uav-v2-restart-request',
                    '--on-active=2s','--timer-property=RemainAfterElapse=no',
                    '/usr/bin/systemctl','--user','--no-block','restart','uav-v2.service']]
    assert client.post('/api/system/restart',headers=headers).status_code==409
