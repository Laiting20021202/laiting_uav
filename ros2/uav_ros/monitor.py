import copy
import threading
import time
from collections import deque
from .contract import CONTRACT, TYPES, inspect, clean


class Monitor:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.rows = {t: dict(count=0, valid_count=0, invalid_count=0, last=None,
                             content_valid=False, reason='no messages received', values={},
                             arrivals=deque(maxlen=1000), header_stamp_ns=None) for t in CONTRACT}
        self.sources = dict(bridge=False, flight={}, camera={}, error='waiting for source bridge',
                            dropped_events=0, time_basis='host_receive_utc', source_clock_synced=False)
        self.preview = None

    def receive(self, topic, m):
        valid, reason, values = inspect(topic, m)
        stamp = m.header.stamp.sec*10**9+m.header.stamp.nanosec if hasattr(m, 'header') else None
        if stamp is not None:
            age = (time.time_ns()-stamp)/1e9
            if not -.5 <= age <= self.config['topics'][topic]['timeout_s']:
                valid, reason = False, 'header timestamp stale or in future'
        with self.lock:
            r = self.rows[topic]
            r['count'] += 1
            r['valid_count' if valid else 'invalid_count'] += 1
            r.update(last=time.monotonic(), content_valid=valid, reason=reason,
                     values=values, header_stamp_ns=stamp)
            r['arrivals'].append(r['last'])

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            result = []
            source = copy.deepcopy(self.sources)
            for t, r in self.rows.items():
                age = now-r['last'] if r['last'] is not None else None
                cfg = self.config['topics'][t]
                online = source['bridge'] and source['camera' if t.endswith('image_raw') else 'flight'].get('connected', False)
                times = [x for x in r['arrivals'] if now-x <= 10]
                hz = (len(times)-1)/(times[-1]-times[0]) if len(times) > 1 else 0.
                if not online:
                    state = 'disconnected'
                elif age is None:
                    state = 'waiting'
                elif age > cfg['timeout_s']:
                    state = 'timeout'
                elif not r['content_valid']:
                    state = 'invalid'
                else:
                    state = 'normal'
                result.append(dict(name=t, type=TYPES[t], state=state, age_s=age,
                    hz=hz if age is not None and age <= cfg['timeout_s'] else 0,
                    expected_hz=cfg['expected_hz'], timeout_s=cfg['timeout_s'],
                    below_expected=hz < cfg['expected_hz']*.7,
                    count=r['count'], valid_count=r['valid_count'], invalid_count=r['invalid_count'],
                    content_valid=r['content_valid'], live=state == 'normal',
                    reason=r['reason'] if state != 'waiting' else 'requested MAVLink stream not received; check firmware support',
                    values=copy.deepcopy(r['values']), header_stamp_ns=r['header_stamp_ns']))
        return clean(dict(topics=result, sources=source, updated_at_ns=time.time_ns()))
