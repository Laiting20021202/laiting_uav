"""Short-lived G3P control sessions for stream configuration and recovery."""
import json
import logging
import select
import socket
import threading
import time
from concurrent.futures import Future

log = logging.getLogger(__name__)


class ControlStopped(Exception):
    pass


class G3PCommandError(RuntimeError):
    def __init__(self, operation, code):
        self.code = code
        super().__init__(f"G3P rejected {operation} (code {code})")


class G3PSession:
    def __init__(self, host, pause=time.sleep, port=7878, timeout=4, on_notification=None):
        self.host, self.port, self.pause, self.timeout = host, port, pause, timeout
        self.sock = None
        self.token = None
        self.buffer = ''
        self.on_notification = on_notification

    def _send(self, payload):
        self.sock.sendall((json.dumps(payload)+'\n').encode())

    def _receive(self, wait):
        messages=[]
        def parse():
            while self.buffer.strip():
                self.buffer=self.buffer.lstrip()
                try:
                    message,end=json.JSONDecoder().raw_decode(self.buffer)
                except ValueError:
                    break
                self.buffer=self.buffer[end:]
                if isinstance(message,dict):messages.append(message)
            if len(self.buffer)>65536:
                raise RuntimeError('G3P control response exceeded size limit')
        parse()
        if not messages and select.select([self.sock],[],[],wait)[0]:
            block=self.sock.recv(4096)
            if not block:raise ConnectionError('G3P closed control session')
            self.buffer+=block.decode(errors='replace')
            parse()
        for message in messages:
            # Ambarella SESSION_HOLD is a server challenge, not a command reply.
            if message.get('msg_id')==0x701 and 'rval' not in message and self.token is not None:
                self._send({'msg_id':0x701,'rval':0,'token':self.token})
            if 'rval' not in message and self.on_notification:
                self.on_notification(message)
        return messages

    def poll(self, wait=.25):
        self._receive(wait)

    def heartbeat(self, index):
        # This firmware uses the legacy, unframed JSON transport. Its own app
        # sends this dedicated heartbeat; app_status queries do not replace it.
        self._send({'token':self.token, 'msg_id':0x10000001, 'msg_index':index})

    def open(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=2)
        self.sock.settimeout(.25)
        response = self.command({'msg_id':257, 'token':0})
        token = response.get('param')
        if response.get('rval') != 0:
            raise G3PCommandError('control session', response.get('rval'))
        if type(token) is not int or token <= 0:
            raise RuntimeError('G3P returned invalid control session token')
        self.token = token

    def command(self, payload):
        self.pause(0)
        if 'token' not in payload:
            payload = dict(payload, token=self.token)
        self._send(payload)
        deadline = time.monotonic()+self.timeout
        while time.monotonic() < deadline:
            self.pause(0)
            for response in self._receive(min(.25,max(0,deadline-time.monotonic()))):
                if (response.get('msg_id') == payload['msg_id']
                        and 'rval' in response):
                    return response
        raise TimeoutError('G3P control response timeout')

    def recover(self):
        # RESETVF is the protocol's viewfinder-start command. Changing camera
        # mode or inventing stop/start values for msg 259 leaves this camera idle.
        response = self.command({'msg_id':259, 'param':'none_force'})
        if response.get('rval') != 0:
            raise G3PCommandError('viewfinder reset', response.get('rval'))
        self.pause(12)
        return 0

    def get_setting(self, name):
        response = self.command({'msg_id': 1, 'type': name})
        if response.get('rval') != 0:
            raise G3PCommandError(f'reading {name}', response.get('rval'))
        return response.get('param')

    def set_setting(self, name, value):
        response = self.command({'msg_id': 2, 'type': name, 'param': value})
        code = response.get('rval')
        if code != 0:
            raise G3PCommandError(f'{name}={value}', code)

    def stop_viewfinder(self):
        response = self.command({'msg_id': 260})
        code = response.get('rval')
        if code != 0:
            raise G3PCommandError('viewfinder stop', code)
        # This firmware invalidates the session as part of STOP_VF.
        self.token = None

    def close(self, end_session=True):
        if self.sock:
            try:
                if end_session and self.token is not None:
                    self.sock.sendall((json.dumps({'msg_id':258, 'token':self.token})+'\n').encode())
            except OSError:
                pass
            self.sock.close()
            self.sock = None


class G3PControl:
    def __init__(self, host, port=7878, interval=8, retry=3, timeout=4, settings=None):
        self.host, self.port = host, port
        self.interval, self.retry, self.timeout = interval, retry, timeout
        self.settings = dict(settings or {})
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.pending = None
        self._session = None
        self.progress_at = time.monotonic()
        self.state = dict(connected=False, phase='waiting', app_status=None,
                          sessions=0, heartbeats=0, status_queries=0, last_reply=None, error=None,
                          notifications=0, hold_requests=0, last_notification=None,
                          configured=False, configuration={})

    def _notification(self, message):
        tag={'msg_id':message.get('msg_id'),'type':message.get('type')}
        with self.lock:
            previous=self.state['last_notification']
            self.state['last_notification']=tag
            self.state['notifications']+=1
            if tag['msg_id']==0x701:self.state['hold_requests']+=1
        if tag!=previous:
            log.info('G3P notification: msg_id=%s type=%s',tag['msg_id'],tag['type'])

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name='g3p-control', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=6)

    def snapshot(self):
        with self.lock:
            result = dict(self.state)
        result['keepalive_interval_s'] = self.interval
        return result

    def request_recovery(self):
        with self.lock:
            if self.stop_event.is_set():
                raise ControlStopped()
            if self.pending is None:
                self.pending = Future()
            future = self.pending
        self.wake.set()
        return future

    def _pause(self, seconds):
        end = time.monotonic()+seconds
        while True:
            self.progress_at = time.monotonic()
            if self.stop_event.is_set():
                raise ControlStopped()
            remaining = end-time.monotonic()
            if remaining <= 0:
                return
            if self._session:
                self._session.poll(min(.25,remaining))
            else:
                self.stop_event.wait(min(.25, remaining))

    def _wait(self, seconds):
        end = time.monotonic()+seconds
        while time.monotonic()<end:
            self._pause(0)
            if self.wake.is_set():
                self.wake.clear()
                return
            if self._session:
                self._session.poll(min(.25,max(0,end-time.monotonic())))
            else:
                self.wake.wait(min(.25,max(0,end-time.monotonic())))

    def _fail_pending(self, exc):
        with self.lock:
            future, self.pending = self.pending, None
        if future and not future.done():
            future.set_exception(exc)

    def _open_session(self):
        session = G3PSession(self.host, self._pause, self.port, self.timeout, self._notification)
        try:
            session.open()
        except BaseException:
            # Failed handshakes previously leaked the just-opened TCP socket:
            # _run only held its separate, unopened placeholder session.
            session.close(end_session=False)
            raise
        self._session = session
        with self.lock:
            self.state['sessions'] += 1
        return session

    def _configure_and_recover(self, session):
        current = {name: session.get_setting(name) for name in self.settings}
        changes = {name: value for name, value in self.settings.items()
                   if current.get(name) != value}
        if changes:
            # Encoder settings are locked while the viewfinder is active.
            # STOP_VF invalidates this token, so settings need a new session.
            session.stop_viewfinder()
            session.close(end_session=False)
            self._session = None
            self._pause(1)
            session = self._open_session()
            try:
                failures = []
                for name, value in changes.items():
                    try:
                        session.set_setting(name, value)
                    except Exception as exc:
                        failures.append(str(exc))
                # Always restart the viewfinder so a rejected optional setting
                # cannot leave the camera stopped.
                session.recover()
                verified = {name: session.get_setting(name) for name in self.settings}
                configured = all(verified.get(k) == v for k, v in self.settings.items())
                with self.lock:
                    self.state.update(configuration=verified, configured=configured)
                if failures or not configured:
                    details = '; '.join(failures) or f'verification failed: {verified}'
                    raise RuntimeError(f'G3P stream configuration failed: {details}')
                log.info('G3P stream profile applied: %s', verified)
            except Exception:
                session.close(end_session=False)
                self._session = None
                raise
        else:
            with self.lock:
                self.state.update(configuration=current, configured=True)
            session.recover()
        return session

    def _run(self):
        while not self.stop_event.is_set():
            session = G3PSession(self.host, self._pause, self.port, self.timeout, self._notification)
            try:
                with self.lock:
                    self.state.update(connected=False, phase='connecting')
                session = self._open_session()
                log.info('G3P on-demand control session established')
                heartbeat_index = 0
                while not self.stop_event.is_set():
                    self._pause(0)
                    with self.lock:
                        future, self.pending = self.pending, None
                    if future and future.set_running_or_notify_cancel():
                        with self.lock:
                            self.state['phase'] = 'recovering_encoder'
                        try:
                            session = self._configure_and_recover(session)
                            code = 0
                            future.set_result(code)
                        except Exception as exc:
                            future.set_exception(exc)
                            raise
                    heartbeat_index += 1
                    session.heartbeat(heartbeat_index)
                    with self.lock:
                        self.state['heartbeats'] += 1
                    response = session.command({'msg_id':1, 'type':'app_status'})
                    if response.get('rval') != 0:
                        raise RuntimeError(f"G3P status query rejected (code {response.get('rval')})")
                    with self.lock:
                        self.state.update(connected=True, phase='keepalive', app_status=response.get('param'),
                                          last_reply=time.time(), error=None)
                        self.state['status_queries'] += 1
                    self._wait(self.interval)
            except ControlStopped as exc:
                self._fail_pending(exc)
                break
            except Exception as exc:
                self._fail_pending(exc)
                message = f'{type(exc).__name__}: {exc}'[:200]
                with self.lock:
                    changed = self.state['error'] != message
                    self.state.update(connected=False, phase='retry_wait', app_status=None, error=message)
                if changed:
                    log.warning('G3P keepalive unavailable: %s', message)
            finally:
                self._session = None
                # Closing the TCP socket without STOP_SESSION matches the prior
                # fielded application. This firmware couples STOP_SESSION to
                # the RTSP encoder, including during an otherwise clean stop.
                session.close(end_session=False)
            try:
                self._wait(self.retry)
            except ControlStopped:
                break
        self._fail_pending(ControlStopped())
        with self.lock:
            self.state.update(connected=False, phase='stopped')
