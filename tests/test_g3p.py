"""Real TCP control peer: inactivity expiry, server disconnect and recovery ordering."""
import json
import socket
import threading
import time
import pytest

from uav.g3p import G3PControl, G3PSession, G3PCommandError


def test_failed_control_handshake_closes_tcp_socket():
    errors=[];complete=threading.Event()
    with socket.socket() as server:
        server.bind(('127.0.0.1',0));server.listen();server.settimeout(3)
        def peer():
            try:
                # Repeated rejected handshakes must not accumulate live sockets.
                for _ in range(3):
                    conn,_=server.accept()
                    with conn,conn.makefile('rb') as stream:
                        conn.settimeout(2)
                        assert json.loads(stream.readline())['msg_id']==257
                        conn.sendall(b'{"msg_id":257,"rval":-21}')
                        assert stream.readline()==b''
            except Exception as exc:errors.append(exc)
            finally:complete.set()
        worker=threading.Thread(target=peer,daemon=True);worker.start()
        control=G3PControl('127.0.0.1',port=server.getsockname()[1])
        for _ in range(3):
            with pytest.raises(RuntimeError,match='rejected control session'):
                control._open_session()
        assert complete.wait(3) and not errors,errors
        worker.join(timeout=2)


def test_rejected_setting_read_does_not_become_missing_setting(monkeypatch):
    session=G3PSession('localhost')
    monkeypatch.setattr(session,'command',lambda payload:dict(rval=-21,param='off'))
    with pytest.raises(G3PCommandError) as error:session.get_setting('EIS')
    assert error.value.code==-21


@pytest.mark.parametrize('recover',[False,True])
def test_server_hold_is_answered_between_queries_and_during_recovery(recover):
    errors=[];complete=threading.Event();release=threading.Event();commands=[]
    with socket.socket() as server:
        server.bind(('127.0.0.1',0));server.listen();server.settimeout(3)
        def peer():
            try:
                conn,_=server.accept()
                with conn,conn.makefile('rb') as stream:
                    conn.settimeout(1)
                    assert json.loads(stream.readline())['msg_id']==257
                    conn.sendall(b'{"msg_id":257,"rval":0,"param":17}')
                    # A command ACK and hold challenge share a packet. The
                    # challenge after the ACK must not be left unread.
                    payload=json.loads(stream.readline());commands.append(payload)
                    assert payload['msg_id']==(259 if recover else 0x10000001)
                    if not recover:
                        payload=json.loads(stream.readline());commands.append(payload)
                        assert payload['msg_id']==1
                    conn.sendall(json.dumps({'msg_id':payload['msg_id'],'rval':0,'param':'vf'}).encode()+b'{"msg_id":1793}')
                    assert json.loads(stream.readline())=={'msg_id':1793,'rval':0,'token':17}
                    for _ in range(3):
                        # More challenges arrive while the next poll is 15s away.
                        time.sleep(.06)
                        conn.sendall(b'{"msg_id":17')
                        time.sleep(.02)
                        conn.sendall(b'93}')
                        assert json.loads(stream.readline())=={'msg_id':1793,'rval':0,'token':17}
                    complete.set();release.wait(3)
            except Exception as exc:errors.append(exc);complete.set()
        worker=threading.Thread(target=peer,daemon=True);worker.start()
        control=G3PControl('127.0.0.1',port=server.getsockname()[1],interval=15)
        if recover:control.request_recovery()
        control.start()
        try:
            assert complete.wait(4) and not errors,errors
            assert control.snapshot()['hold_requests']==4
            assert control.snapshot()['sessions']==1
            assert len(commands)==(1 if recover else 2)
        finally:
            release.set();control.stop();worker.join(timeout=2)


def test_failed_keepalive_does_not_send_stop_session():
    errors=[];complete=threading.Event();received=[]
    with socket.socket() as server:
        server.bind(('127.0.0.1',0));server.listen();server.settimeout(3)
        def peer():
            try:
                conn,_=server.accept()
                with conn,conn.makefile('rb') as stream:
                    conn.settimeout(2)
                    assert json.loads(stream.readline())['msg_id']==257
                    conn.sendall(b'{"msg_id":257,"rval":0,"param":17}')
                    assert json.loads(stream.readline())['msg_id']==0x10000001
                    assert json.loads(stream.readline())['msg_id']==1
                    conn.sendall(b'{"msg_id":1,"rval":-14}')
                    received.append(stream.readline())
            except Exception as exc:errors.append(exc)
            finally:complete.set()
        worker=threading.Thread(target=peer,daemon=True);worker.start()
        control=G3PControl('127.0.0.1',port=server.getsockname()[1],retry=5)
        control.start()
        try:
            assert complete.wait(3) and not errors,errors
            assert received==[b'']
        finally:
            control.stop();worker.join(timeout=2)


def test_clean_on_demand_stop_closes_socket_without_stopping_camera_session():
    connected=threading.Event();complete=threading.Event();received=[];errors=[]
    with socket.socket() as server:
        server.bind(('127.0.0.1',0));server.listen();server.settimeout(3)
        def peer():
            try:
                conn,_=server.accept()
                with conn,conn.makefile('rb') as stream:
                    while True:
                        line=stream.readline()
                        received.append(line)
                        if not line:break
                        payload=json.loads(line)
                        if payload['msg_id']==0x10000001:continue
                        reply={'msg_id':payload['msg_id'],'rval':0}
                        if payload['msg_id']==257:reply['param']=17
                        elif payload['msg_id']==1:reply['param']='vf';connected.set()
                        conn.sendall(json.dumps(reply).encode())
            except Exception as exc:errors.append(exc)
            finally:complete.set()
        worker=threading.Thread(target=peer,daemon=True);worker.start()
        control=G3PControl('127.0.0.1',port=server.getsockname()[1]);control.start()
        try:
            assert connected.wait(3)
            control.stop()
            assert complete.wait(2) and not errors,errors
            assert received[-1]==b''
            assert all(not line or json.loads(line)['msg_id']!=258 for line in received)
        finally:
            control.stop();worker.join(timeout=2)


def test_recovery_stops_viewfinder_reopens_session_and_applies_profile(monkeypatch):
    commands=[];errors=[];configured=threading.Event();release=threading.Event()
    def fast_recover(session):
        response=session.command({'msg_id':259,'param':'none_force'})
        if response.get('rval') != 0:raise RuntimeError('reset failed')
        return 0
    monkeypatch.setattr('uav.g3p.G3PSession.recover',fast_recover)
    with socket.socket() as server:
        server.bind(('127.0.0.1',0));server.listen();server.settimeout(3)
        def peer():
            try:
                value='on'
                for connection,token in enumerate((17,18),start=1):
                    conn,_=server.accept()
                    with conn,conn.makefile('rb') as stream:
                        while True:
                            line=stream.readline()
                            if not line:break
                            payload=json.loads(line);commands.append(payload)
                            if payload['msg_id']==0x10000001:continue
                            response={'msg_id':payload['msg_id'],'rval':0}
                            if payload['msg_id']==257:response['param']=token
                            elif payload['msg_id']==1:response['param']='vf' if payload.get('type')=='app_status' else value
                            elif payload['msg_id']==2:value=payload['param']
                            conn.sendall(json.dumps(response).encode())
                            if payload['msg_id']==260:break
                            if connection==2 and payload['msg_id']==1 and payload.get('type')=='EIS':
                                configured.set()
                            if connection==2 and payload['msg_id']==1 and payload.get('type')=='app_status':
                                release.wait(3)
            except Exception as exc:errors.append(exc);configured.set()
        worker=threading.Thread(target=peer,daemon=True);worker.start()
        control=G3PControl('127.0.0.1',port=server.getsockname()[1],settings={'EIS':'off'},interval=10)
        future=control.request_recovery();control.start()
        try:
            assert future.result(timeout=4)==0
            assert configured.wait(2) and not errors,errors
            state=control.snapshot()
            assert state['sessions']==2 and state['configured']
            assert state['configuration']=={'EIS':'off'}
        finally:
            release.set();control.stop();worker.join(timeout=2)
    assert [p['msg_id'] for p in commands[:7]]==[257,1,260,257,2,259,1]
    assert all(p['msg_id']!=258 for p in commands)


def test_keepalive_preserves_session_and_reconnects_after_server_disconnect():
    stop = threading.Event()
    disconnected = threading.Event()
    commands = []
    connections = []
    errors = []
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0)); server.listen(); server.settimeout(.1)
        def peer():
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                connections.append(time.monotonic())
                try:
                    with conn, conn.makefile('rb') as stream:
                        # A silent client loses the session; periodic queries keep it alive.
                        conn.settimeout(.4)
                        count = 0
                        while not stop.is_set():
                            line = stream.readline()
                            if not line:
                                break
                            payload = json.loads(line); commands.append(payload)
                            if payload['msg_id'] == 258:
                                break
                            reply = {'msg_id':payload['msg_id'], 'rval':0}
                            if payload['msg_id'] == 257:
                                reply['param'] = len(connections)
                            elif payload['msg_id']==1:
                                reply['param'] = 'vf'; count += 1
                            if len(connections) == 1 and count == 8:
                                disconnected.set()
                                break
                            # An asynchronous event shares the same receive packet.
                            conn.sendall((json.dumps({'msg_id':7, 'type':'keepalive'})+json.dumps(reply)).encode())
                except Exception as exc:
                    if not stop.is_set(): errors.append(exc)
        worker = threading.Thread(target=peer, daemon=True); worker.start()
        control = G3PControl('127.0.0.1', port=server.getsockname()[1], interval=.08, retry=.05, timeout=.25)
        control.start()
        try:
            assert disconnected.wait(4)
            deadline = time.monotonic()+4
            while time.monotonic()<deadline:
                state = control.snapshot()
                if state['sessions']>=2 and state['connected'] and state['heartbeats']>=12:
                    break
                time.sleep(.02)
            assert state['sessions']==2 and state['connected'] and state['heartbeats']>=12
            assert state['app_status']=='vf'
            assert all(p['msg_id'] in (257,258,1,0x10000001) for p in commands)
            assert all(p.get('type')=='app_status' for p in commands if p['msg_id']==1)
        finally:
            control.stop(); stop.set(); worker.join(timeout=2)
        assert not control.thread.is_alive() and not worker.is_alive()
        assert not errors


def test_stop_cancels_unresponsive_control_socket():
    release = threading.Event(); accepted = threading.Event()
    with socket.socket() as server:
        server.bind(('127.0.0.1',0));server.listen();server.settimeout(3)
        def stall():
            conn,_=server.accept()
            with conn:
                accepted.set();release.wait(5)
        peer=threading.Thread(target=stall,daemon=True);peer.start()
        control=G3PControl('127.0.0.1',port=server.getsockname()[1]);control.start()
        try:
            assert accepted.wait(3)
            future=control.request_recovery()
            start=time.monotonic();control.stop()
            assert time.monotonic()-start<1
            assert future.done() and future.exception() is not None
        finally:
            release.set();control.stop();peer.join(timeout=2)


def test_stop_cancels_queued_recovery_during_encoder_settle():
    reset_requested=threading.Event()
    with socket.socket() as server:
        server.bind(('127.0.0.1',0));server.listen();server.settimeout(3)
        def peer():
            conn,_=server.accept()
            with conn,conn.makefile('rb') as stream:
                while True:
                    try:line=stream.readline()
                    except ConnectionResetError:break
                    if not line:break
                    payload=json.loads(line)
                    if payload['msg_id']==258:break
                    reply={'msg_id':payload['msg_id'],'rval':0,'param':17 if payload['msg_id']==257 else 'vf'}
                    conn.sendall(json.dumps(reply).encode())
                    if payload['msg_id']==259:reset_requested.set()
        worker=threading.Thread(target=peer,daemon=True);worker.start()
        control=G3PControl('127.0.0.1',port=server.getsockname()[1]);control.start()
        try:
            future=control.request_recovery()
            assert reset_requested.wait(3)
            before=time.monotonic();control.stop()
            assert time.monotonic()-before<1
            assert future.done() and future.exception() is not None
        finally:
            control.stop();worker.join(timeout=2)
        assert not worker.is_alive()
