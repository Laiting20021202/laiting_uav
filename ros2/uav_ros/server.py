"""Authenticated LAN dashboard and private Unix upstream for the existing website."""
import base64
import hashlib
import json
import re
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uav.auth import Auth
from .contract import clean
from .source import get


def serve(config, monitor, bags):
    class SourceUsers:
        def read_text(self):
            status, _, data = get(config['source_socket'], '/credentials')
            if status != 200:
                raise OSError('credential source unavailable')
            return data.decode()
    auth = Auth(SourceUsers())
    auth_tokens = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Never log Authorization or cookies.
            if args and str(args[1] if len(args)>1 else '') not in ('200','204'):
                print('HTTP', fmt % args, flush=True)

        def authorized(self):
            if getattr(self.server, 'private', False):
                return True
            header = self.headers.get('Authorization','')
            key = hashlib.sha256(header.encode()).hexdigest()
            token = auth_tokens.get(key)
            try:
                if token and auth.get(token, refresh=True):
                    return True
                if len(header) > 2048 or not header.startswith('Basic '):
                    raise ValueError()
                user, password = base64.b64decode(header[6:], validate=True).decode().split(':',1)
                token, error = auth.login(user, password, self.client_address[0])
                if error:
                    raise ValueError()
                if len(auth_tokens) > 256:
                    auth_tokens.clear()
                auth_tokens[key] = token
                return True
            except (ValueError, UnicodeError):
                self.send_response(401)
                self.send_header('WWW-Authenticate','Basic realm="UAV ROS 2"')
                self.send_header('Content-Length','0')
                self.end_headers()
                return False
            except OSError:
                self.reply(503, {'error':'credential source unavailable'})
                return False

        def reply(self, status, body, typ='application/json', extra=None):
            if not isinstance(body, bytes):
                body = json.dumps(clean(body), ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', typ)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; frame-ancestors 'none'")
            for k,v in (extra or {}).items():
                self.send_header(k,v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.connection.settimeout(30)
            route = urlsplit(self.path).path
            if route == '/healthz':
                self.reply(200 if time.monotonic()-monitor.heartbeat < 10 else 503, {'status':'alive'})
                return
            if not self.authorized():
                return
            try:
                if route in ('/','/app.js','/style.css'):
                    name = 'index.html' if route == '/' else route[1:]
                    typ = {'index.html':'text/html; charset=utf-8','app.js':'text/javascript','style.css':'text/css'}[name]
                    self.reply(200, (Path('/app/web')/name).read_bytes(), typ)
                elif route == '/api/status':
                    self.reply(200, dict(**monitor.snapshot(), recording=bags.status(),
                        environment=dict(ros_distro='humble', domain_id=__import__('os').getenv('ROS_DOMAIN_ID','0'), qos='image reliable; sensors best_effort / volatile / depth 5')))
                elif route == '/api/preview':
                    image_topic = monitor.snapshot()['topics'][0]
                    with monitor.lock:
                        jpeg = monitor.preview
                    if jpeg is None or image_topic['state'] != 'normal':
                        self.reply(503, {'error':'no fresh camera image'})
                    else:
                        self.reply(200, jpeg, 'image/jpeg')
                elif re.fullmatch(r'/api/download/[0-9TZ_a-f]+', route):
                    p = bags.download(route.rsplit('/',1)[1])
                    self.send_response(200)
                    self.send_header('Content-Type','application/zip')
                    self.send_header('Content-Length',str(p.stat().st_size))
                    self.send_header('Content-Disposition','attachment; filename="'+p.name+'"')
                    self.send_header('Cache-Control','no-store')
                    self.end_headers()
                    with p.open('rb') as f:
                        while b := f.read(256*1024):
                            self.wfile.write(b)
                else:
                    self.reply(404, {'error':'not found'})
            except (ValueError, FileNotFoundError) as exc:
                self.reply(409, {'error':str(exc)})

        def do_POST(self):
            self.connection.settimeout(10)
            if not self.authorized():
                return
            if not getattr(self.server,'private',False):
                origin = self.headers.get('Origin')
                if origin and origin != 'http://'+self.headers.get('Host'):
                    self.reply(403, {'error':'Origin rejected'})
                    return
                if self.headers.get('X-UAV-Request') != '1':
                    self.reply(403, {'error':'X-UAV-Request required'})
                    return
            if int(self.headers.get('Content-Length','0')) > 4096 or self.headers.get('Transfer-Encoding'):
                self.reply(413, {'error':'body too large'})
                return
            try:
                route = urlsplit(self.path).path
                if route == '/api/start':
                    result = bags.start()
                elif route == '/api/stop':
                    result = bags.stop()
                else:
                    self.reply(404, {'error':'not found'})
                    return
                self.reply(200,result)
            except (ValueError, OSError) as exc:
                self.reply(409, {'error':str(exc)})

    class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        private = True
    socket_path = Path(config['control_socket'])
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    private = UnixServer(str(socket_path), Handler)
    socket_path.chmod(0o600)
    tcp = ThreadingHTTPServer((config['web_host'], config['web_port']), Handler)
    for s in (private,tcp):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    return (private,tcp)
