"""Private session checkpoints survive restart without reviving revoked logins."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from uav.auth import Auth, password_hash
from uav.config import load_settings
from uav.web import create_app


@pytest.fixture
def setup_auth(tmp_path,monkeypatch):
    clock={'mono':100.0,'wall':1700000000.0}
    monkeypatch.setattr('uav.auth.time',SimpleNamespace(monotonic=lambda:clock['mono'],time=lambda:clock['wall']))
    users=tmp_path/'users.json'
    users.write_text(json.dumps({'test':password_hash('password')}))
    path=tmp_path/'sessions.json'
    def make():return Auth(users,hours=10/3600,sessions_path=path)
    return make,clock,users,path


def test_restart_restores_login_csrf_and_remaining_idle_time(setup_auth):
    make,clock,_,path=setup_auth
    auth=make();token,error=auth.login('test','password','127.0.0.1')
    assert error is None
    original=auth.get(token)
    auth.close()
    assert token not in path.read_text() and path.stat().st_mode & 0o777 == 0o600
    clock.update(mono=2,wall=1700000003)
    restarted=make()
    assert restarted.get(token)['csrf']==original['csrf']
    assert restarted.get(token)['expires']==9
    assert restarted.get('forged-token') is None
    clock.update(mono=9,wall=1700000010)
    assert restarted.get(token) is None


@pytest.mark.parametrize('reason',['logout','password','idle'])
def test_restart_does_not_revive_invalid_sessions(setup_auth,reason):
    make,clock,users,_=setup_auth
    auth=make();token,_=auth.login('test','password','127.0.0.1')
    if reason=='logout':auth.logout(token)
    elif reason=='password':users.write_text(json.dumps({'test':password_hash('changed')}))
    else:clock.update(mono=111,wall=1700000011)
    assert make().get(token) is None


def test_periodic_renewal_checkpoint_survives_abrupt_exit(setup_auth):
    make,clock,_,_=setup_auth
    auth=make();token,_=auth.login('test','password','127.0.0.1')
    clock.update(mono=108,wall=1700000008)
    auth.get(token,refresh=True)
    clock.update(mono=112,wall=1700000012)
    assert make().get(token) is not None  # No graceful close before replacement.


def test_corrupt_checkpoint_requires_login(setup_auth):
    make,_,_,path=setup_auth
    path.write_text('invalid checkpoint')
    auth=make()
    assert auth.get('arbitrary') is None
    token,error=auth.login('test','password','127.0.0.1')
    assert error is None and make().get(token)


def test_http_cookie_survives_application_recreation(tmp_path):
    users=tmp_path/'users.json';users.write_text(json.dumps({'test':password_hash('password')}))
    config=load_settings();base=config['web']['origin']
    def app():return create_app(config,data_dir=tmp_path/'data',users_path=users,start_workers=False)
    with TestClient(app(),base_url=base) as first:
        assert first.post('/api/login',json={'username':'test','password':'password'},headers={'Origin':base}).status_code==200
        cookie=first.cookies.get('__Host-uav_session')
        csrf=first.get('/api/me').json()['csrf']
    with TestClient(app(),base_url=base) as second:
        second.cookies.set('__Host-uav_session',cookie)
        assert second.get('/api/me').json()['csrf']==csrf
        assert second.get('/api/status').status_code==200
        assert second.post('/api/logout',headers={'Origin':base,'X-CSRF-Token':csrf}).status_code==200
    with TestClient(app(),base_url=base) as third:
        third.cookies.set('__Host-uav_session',cookie)
        assert third.get('/api/status').status_code==401
