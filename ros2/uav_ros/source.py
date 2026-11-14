"""Read-only Unix HTTP client used by both sides of the bridge."""
import http.client
import socket


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=3):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def get(path, route):
    conn = UnixHTTP(path)
    try:
        conn.request('GET', route)
        r = conn.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        conn.close()
