"""Real FFmpeg pipeline without connected hardware; isolated synthetic video source."""
import shutil
import subprocess
import time
import pytest
import numpy as np
from uav.camera import Camera
from uav.g3p import G3PSession, G3PCommandError
from uav.config import load_settings


def test_busy_encoder_uses_bounded_backoff_and_explicit_recovery_hint(monkeypatch):
    now=[100.0]
    monkeypatch.setattr('uav.camera.time.monotonic',lambda:now[0])
    camera=Camera(load_settings()['camera'],None)
    for index,delay in enumerate((30,60,120,240,300,300),start=1):
        camera._control_failed(G3PCommandError('viewfinder reset',-21))
        state=camera.snapshot()
        assert state['recovery_retry_in_s']==delay
        assert state['power_cycle_required']==(index>=3)
        now[0]+=delay
    camera.request_reconnect()
    assert camera.snapshot()['recovery_retry_in_s']==0
    camera._control_failed(TimeoutError('unreachable'))
    assert not camera.snapshot()['power_cycle_required']


def test_camera_restarts_on_demand_controller_after_rejected_recovery(monkeypatch):
    import json
    import socket
    import threading
    from uav.g3p import G3PControl
    stop=threading.Event();commands=[];errors=[]
    with socket.socket() as server:
        server.bind(('127.0.0.1',0));server.listen();server.settimeout(.1)
        def peer():
            try:
                while not stop.is_set():
                    try:conn,_=server.accept()
                    except socket.timeout:continue
                    with conn,conn.makefile('rb') as stream:
                        conn.settimeout(2)
                        while True:
                            line=stream.readline()
                            if not line:break
                            payload=json.loads(line);commands.append(payload)
                            if payload['msg_id']==0x10000001:continue
                            reply=dict(msg_id=payload['msg_id'],rval=-21 if payload['msg_id']==259 else 0,
                                       param=17 if payload['msg_id']==257 else 'vf')
                            conn.sendall(json.dumps(reply).encode())
            except Exception as exc:errors.append(exc)
        worker=threading.Thread(target=peer,daemon=True);worker.start()
        config=load_settings()['camera']
        camera=Camera(config,None)
        camera.control=G3PControl('127.0.0.1',port=server.getsockname()[1])
        def no_frames():raise RuntimeError('No frames while encoder busy')
        monkeypatch.setattr(camera,'_probe',no_frames)
        failed=camera._control_failed
        def fast_retry(exc):
            failed(exc)
            camera.next_control_at=0
        monkeypatch.setattr(camera,'_control_failed',fast_retry)
        camera.start()
        try:
            deadline=time.monotonic()+8
            while camera.busy_failures<2 and time.monotonic()<deadline:time.sleep(.05)
            assert camera.busy_failures>=2,camera.error
            assert sum(p['msg_id']==259 for p in commands)>=2
        finally:
            camera.stop();stop.set();worker.join(timeout=3)
        assert not errors and not worker.is_alive(),errors


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg required')
def test_latest_frame_capture_and_clean_shutdown(tmp_path):
    path=tmp_path/'camera.mkv'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc=size=320x180:rate=10','-t','2','-c:v','ffv1',str(path)],check=True)
    config=load_settings()['camera'];config.update(source=str(path),width=320,height=180,g3p_start=False,timeout_s=3)
    class Flight:
        def snapshot(self,at):return {'connected':False,'test_at':at}
    camera=Camera(config,Flight());camera.start()
    try:
        deadline=time.monotonic()+8
        while (camera.get() is None or camera.get()['sequence']<3) and time.monotonic()<deadline:
            time.sleep(.05)
        sample=camera.get()
        assert sample is not None, camera.error
        assert sample['frame'].shape==(180,320,3)
        assert sample['flight']['test_at']==sample['at']
        assert sample['sequence']>=3
        # A finite input ends; the camera clears stale samples before reconnecting.
        deadline=time.monotonic()+4
        while camera.get() is not None and time.monotonic()<deadline:
            time.sleep(.05)
        assert camera.get() is None
        deadline=time.monotonic()+7
        while camera.get() is None and time.monotonic()<deadline:
            time.sleep(.05)
        assert camera.get() is not None
        assert camera.get()['sequence']>sample['sequence']
    finally:
        camera.stop()
    assert not camera.thread.is_alive()
    assert camera.process is None


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg required')
def test_planned_session_refresh_keeps_last_frame_and_reconnects(tmp_path):
    path=tmp_path/'long-camera.mkv'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
                    'testsrc=size=320x180:rate=10','-t','3','-c:v','ffv1',str(path)],check=True)
    config=load_settings()['camera'];config.update(source=str(path),width=320,height=180,
                                                   g3p_start=False,timeout_s=3,refresh_s=.5)
    class Flight:
        def snapshot(self,at):return {'connected':False}
    camera=Camera(config,Flight());camera.start()
    try:
        deadline=time.monotonic()+8
        while camera.refreshes<1 and time.monotonic()<deadline:time.sleep(.05)
        assert camera.refreshes>=1 and camera.connections>=1
        # Planned refresh preserves the last image while replacing FFmpeg.
        assert camera.get() is not None
        deadline=time.monotonic()+5
        while camera.connections<2 and time.monotonic()<deadline:time.sleep(.05)
        assert camera.connections>=2 and camera.error is None
    finally:
        camera.stop()


def test_rtsp_matches_fielded_low_latency_reader_and_uses_outer_deadline():
    c=Camera(load_settings()['camera'],None)
    command=c._command()
    assert '-rw_timeout' not in command and '-stimeout' not in command
    assert command[command.index('-fflags')+1]=='nobuffer+discardcorrupt'
    assert command[command.index('-probesize')+1]=='32'
    assert command[command.index('-analyzeduration')+1]=='0'
    assert command[command.index('-vsync')+1]=='drop'
    assert command[command.index('-vf')+1]=='fps=4'
    assert command[command.index('-pix_fmt')+1]=='bgr24'


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg required')
def test_capture_preserves_native_fine_detail_pixels(tmp_path):
    # Lossless video makes pixel equality meaningful, including one-pixel color
    # detail that resizing or a JPEG roundtrip would destroy.
    frame=np.random.default_rng(7).integers(0,256,(180,320,3),dtype=np.uint8)
    path=tmp_path/'detail.mkv'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','rawvideo','-pix_fmt','bgr24',
                    '-video_size','320x180','-framerate','10','-i','pipe:0',
                    '-c:v','ffv1',str(path)],input=frame.tobytes()*30,check=True)
    config=load_settings()['camera'];config.update(source=str(path),width=320,height=180,g3p_start=False)
    class Flight:
        def snapshot(self,at):return {'connected':False}
    camera=Camera(config,Flight());camera.start()
    try:
        deadline=time.monotonic()+8
        while camera.get() is None and time.monotonic()<deadline:time.sleep(.02)
        assert camera.get() is not None,camera.error
        np.testing.assert_array_equal(camera.get()['frame'],frame)
    finally:
        camera.stop()


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg required')
def test_lower_resolution_source_is_rejected_instead_of_upscaled(tmp_path):
    path=tmp_path/'low-resolution.mkv'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
                    'testsrc=size=320x180:rate=10','-t','0.2','-c:v','ffv1',str(path)],check=True)
    config=load_settings()['camera'];config.update(source=str(path),width=640,height=360,g3p_start=False)
    camera=Camera(config,None)
    with pytest.raises(ValueError,match='CameraResolutionMismatch: source 320x180, required 640x360'):
        camera._probe()
    assert camera.get() is None and camera.process is None


def test_manual_reconnect_clears_frame_and_cancels_probe(tmp_path):
    # A TCP peer accepts RTSP but never responds: reconnect/stop must cancel ffprobe.
    import socket
    import threading
    with socket.socket() as server:
        server.bind(('127.0.0.1',0));server.listen()
        ready=threading.Event();release=threading.Event()
        def stall():
            conn,_=server.accept()
            with conn:
                ready.set();release.wait(8)
        peer=threading.Thread(target=stall,daemon=True);peer.start()
        config=load_settings()['camera'];config.update(source=f'rtsp://127.0.0.1:{server.getsockname()[1]}/live',g3p_start=False)
        camera=Camera(config,None);camera.start()
        try:
            assert ready.wait(5)
            epoch=camera.epoch
            camera.request_reconnect()
            assert camera.epoch>epoch and camera.get() is None
            start=time.monotonic();camera.stop()
            assert time.monotonic()-start<5
            assert not camera.thread.is_alive()
        finally:
            release.set();camera.stop();peer.join(timeout=3)


def test_zero_sized_probe_retries_and_recovers_real_frames(tmp_path, monkeypatch):
    # Reproduce an RTSP server that accepts connections but has no H.264 headers.
    # Once valid dimensions arrive, the same worker must recover without restart.
    import json
    import os
    import sys
    video = tmp_path/'camera.mkv'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
                    'testsrc=size=320x180:rate=10','-t','3','-c:v','ffv1',str(video)],check=True)
    dimensions = tmp_path/'dimensions.json'
    dimensions.write_text(json.dumps({'streams':[{'width':0,'height':0}]}))
    probe = tmp_path/'ffprobe'
    probe.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nprint(Path({str(dimensions)!r}).read_text())\n')
    probe.chmod(0o700)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    config=load_settings()['camera'];config.update(source=str(video),width=320,height=180,g3p_start=False,timeout_s=3)
    class Flight:
        def snapshot(self,at):return {'connected':False}
    camera=Camera(config,Flight());camera.start()
    try:
        deadline=time.monotonic()+5
        while not camera.error and time.monotonic()<deadline:time.sleep(.05)
        assert 'no decodable video dimensions' in camera.error
        assert camera.thread.is_alive() and not camera.snapshot()['connected']
        for _ in range(3):camera._control_failed(G3PCommandError('viewfinder reset',-21))
        assert camera.snapshot()['power_cycle_required']
        dimensions.write_text(json.dumps({'streams':[{'width':320,'height':180}]}))
        deadline=time.monotonic()+8
        while camera.get() is None and time.monotonic()<deadline:time.sleep(.05)
        assert camera.get()['frame'].shape==(180,320,3)
        assert camera.snapshot()['connected'] and camera.attempts>=2
        assert camera.error is None
        assert camera.snapshot()['recovery_failures']==0
        assert not camera.snapshot()['power_cycle_required']
    finally:
        camera.stop()


@pytest.mark.parametrize('start_code',[0,-21])
def test_g3p_recovers_stalled_encoder_with_resetvf_and_settle(start_code):
    import json
    import socket
    import threading
    commands=[];pauses=[];errors=[]
    with socket.socket() as server:
        server.bind(('127.0.0.1',0));server.listen();server.settimeout(5)
        def camera_peer():
            try:
                conn,_=server.accept()
                with conn,conn.makefile('rb') as stream:
                    for _ in range(3):
                        payload=json.loads(stream.readline());commands.append(payload)
                        reply={'msg_id':payload['msg_id'],'rval':0}
                        if payload['msg_id']==257:reply['param']=17
                        if payload.get('param')=='none_force':reply['rval']=start_code
                        # Async camera notifications must not count as command ACKs.
                        conn.sendall((json.dumps({'msg_id':payload['msg_id'],'notification':True})+json.dumps(reply)).encode())
            except Exception as exc:errors.append(exc)
        peer=threading.Thread(target=camera_peer,daemon=True);peer.start()
        session=G3PSession('127.0.0.1',pause=pauses.append,port=server.getsockname()[1])
        try:
            session.open()
            if start_code:
                with pytest.raises(RuntimeError,match=r'viewfinder reset \(code -21\)'):
                    session.recover()
                result=start_code
            else:result=session.recover()
        finally:
            session.close()
        peer.join(timeout=5)
    assert not errors and not peer.is_alive()
    assert result==start_code
    assert commands==[{'msg_id':257,'token':0},{'msg_id':259,'token':17,'param':'none_force'},
                      {'msg_id':258,'token':17}]
    assert [v for v in pauses if v]==([12] if start_code==0 else [])
