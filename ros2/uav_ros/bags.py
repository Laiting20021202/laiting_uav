"""Serialized rosbag2 lifecycle. Only validated, closed, atomic ZIPs are downloadable."""
import copy
import fcntl
import hashlib
import json
import os
import signal
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
import yaml
from rclpy.serialization import deserialize_message
from .contract import CONTRACT, TYPES, inspect, clean


def save(path, obj):
    temp = path.with_suffix(path.suffix+'.tmp')
    with temp.open('w') as f:
        json.dump(clean(obj), f, indent=2, ensure_ascii=False, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def size(path):
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file())


def elapsed_seconds(job, now=None):
    """Only an active recorder may use the current time as its end time.

    After an abrupt interruption the actual stop time may be unknown. Keep it
    unknown rather than counting recovery/downtime as recorded data.
    """
    started = job.get('started_at')
    if started is None:
        return None
    stopped = job.get('stopped_at')
    if stopped is None:
        if job.get('state') not in {'starting', 'recording', 'stopping'}:
            return None
        stopped = time.time() if now is None else now
    return max(0, stopped-started)


def validate(bag):
    meta = yaml.safe_load((bag/'metadata.yaml').read_text())['rosbag2_bagfile_information']
    expected = {r['topic_metadata']['name']: r for r in meta['topics_with_message_count']}
    rows = {t: dict(type=TYPES[t], count=0, valid_count=0, invalid_count=0,
                   first_ns=None, last_ns=None, sample=None, reasons={}) for t in CONTRACT}
    files = meta['relative_file_paths']
    if not files:
        raise ValueError('bag has no database files')
    for name in files:
        db = (bag/name).resolve()
        if db.parent != bag.resolve() or not db.is_file():
            raise ValueError('invalid database path')
        with sqlite3.connect('file:'+str(db)+'?mode=ro', uri=True) as conn:
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('SQLite integrity check failed')
            types = dict(conn.execute('SELECT name,type FROM topics'))
            for t, typ in types.items():
                if t in TYPES and typ != TYPES[t]:
                    raise ValueError('topic type mismatch: '+t)
            for t, ts, data in conn.execute('SELECT topics.name,messages.timestamp,messages.data FROM messages JOIN topics ON messages.topic_id=topics.id ORDER BY messages.timestamp'):
                if t not in rows:
                    raise ValueError('unexpected recorded topic: '+t)
                r = rows[t]
                msg = deserialize_message(data, CONTRACT[t])
                valid, reason, values = inspect(t, msg)
                if hasattr(msg, 'header'):
                    stamp = msg.header.stamp.sec*10**9+msg.header.stamp.nanosec
                    if not -.5 <= (ts-stamp)/1e9 <= 5:
                        valid, reason = False, 'header stamp differs from recorder receive time by more than allowed latency'
                r['count'] += 1
                r['valid_count' if valid else 'invalid_count'] += 1
                r['first_ns'] = min(ts, r['first_ns']) if r['first_ns'] is not None else ts
                r['last_ns'] = max(ts, r['last_ns']) if r['last_ns'] is not None else ts
                r['sample'] = values
                if reason:
                    r['reasons'][reason] = r['reasons'].get(reason, 0)+1
    for t, r in rows.items():
        if r['count'] != expected.get(t, {}).get('message_count', 0):
            raise ValueError('metadata count mismatch: '+t)
        if t in expected and expected[t]['topic_metadata']['type'] != TYPES[t]:
            raise ValueError('metadata type mismatch: '+t)
    total = sum(r['count'] for r in rows.values())
    if total == 0 or total != meta['message_count']:
        raise ValueError('empty bag or total count mismatch')
    start = min(r['first_ns'] for r in rows.values() if r['count'])
    end = max(r['last_ns'] for r in rows.values() if r['count'])
    if start != meta['starting_time']['nanoseconds_since_epoch'] or end-start != meta['duration']['nanoseconds']:
        raise ValueError('metadata time range mismatch')
    missing = [t for t, r in rows.items() if not r['count']]
    invalid = [t for t, r in rows.items() if r['invalid_count']]
    return clean(dict(topics=rows, message_count=total, topic_count=sum(r['count'] > 0 for r in rows.values()),
                first_ns=start, last_ns=end, duration_s=(end-start)/1e9,
                integrity='partial' if missing or invalid else 'complete', missing=missing, invalid=invalid,
                checks=['sqlite_integrity', 'deserialize_every_message', 'content_validity', 'metadata_types_counts_time']))


class Bags:
    busy = {'starting','recording','stopping','validating','packaging'}

    def __init__(self, config, monitor):
        self.config, self.monitor = config, monitor
        self.root = Path(config['data_dir'])/'recordings'
        self.root.mkdir(parents=True, exist_ok=True)
        self.guard = (self.root/'.lock').open('w')
        fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.lock = threading.RLock()
        self.process = self.worker = None
        self.job = None
        self.logfile = None
        for p in self.root.glob('*/job.json'):
            j = json.loads(p.read_text())
            if j['state'] in self.busy:
                j.update(state='failed', interrupted_detected_at=time.time(),
                         error='service interrupted; retained files require inspection/reindex; ZIP not released')
                save(p, j)

    def disk(self):
        d = shutil.disk_usage(self.root)
        return dict(free_mb=d.free/1048576, total_mb=d.total/1048576,
                    reserve_mb=self.config['min_free_mb'])

    def _persist(self):
        save(self.root/self.job['name']/'job.json', self.job)

    def _state(self, state, **kw):
        with self.lock:
            self.job.update(state=state, **kw)
            self._persist()

    def status(self):
        with self.lock:
            j = copy.deepcopy(self.job) if self.job else dict(state='idle')
            if self.job:
                j['bytes'] = size(self.root/j['name']/'bag')
                j['elapsed_s'] = elapsed_seconds(j)
            return dict(current=j, disk=self.disk(), history=self.history())

    def history(self):
        rows = []
        now = time.time()
        for p in sorted(self.root.glob('*/job.json'), reverse=True):
            j = json.loads(p.read_text())
            j['elapsed_s'] = elapsed_seconds(j, now)
            rows.append(j)
        return rows

    def start(self):
        with self.lock:
            if self.job and self.job['state'] in self.busy:
                raise ValueError('recording or finalization already in progress')
            if self.disk()['free_mb'] < self.config['min_free_mb']+256:
                raise ValueError('insufficient disk space before recording')
            name = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())+'_'+uuid.uuid4().hex[:10]
            folder = self.root/name
            folder.mkdir()
            self.job = dict(name=name, state='starting', started_at=time.time(), error=None,
                            progress=0, zip_bytes=0, topic_count=0,
                            preflight=self.monitor.snapshot(), environment={
                                'ros_distro':'humble', 'ROS_DOMAIN_ID':os.getenv('ROS_DOMAIN_ID','0'),
                                'RMW_IMPLEMENTATION':os.getenv('RMW_IMPLEMENTATION'),
                                'image_id':os.getenv('UAV_IMAGE_ID','see deployment image.lock'),
                                'timestamp_basis':'host receive UTC (not synchronized sensor acquisition time)',
                                'settings':self.config})
            self._persist()
            try:
                self.logfile = (folder/'recorder.log').open('wb')
                self.process = subprocess.Popen(['ros2','bag','record','-s','sqlite3',
                    '--qos-profile-overrides-path','/app/config/qos.yaml',
                    '--max-cache-size','16000000', '-o',str(folder/'bag'),*CONTRACT],
                    stdout=self.logfile, stderr=subprocess.STDOUT, start_new_session=True)
                self._state('recording', pid=self.process.pid)
            except Exception as exc:
                self._state('failed', error=str(exc))
                if self.logfile:
                    self.logfile.close()
                raise
            return self.status()

    def stop(self, reason=None):
        with self.lock:
            if not self.job or self.job['state'] != 'recording':
                raise ValueError('no recording is running, or stop is already in progress')
            self._state('stopping', stop_reason=reason)
            self.worker = threading.Thread(target=self._finish, name='bag-finalize')
            self.worker.start()
            return self.status()

    def tick(self):
        with self.lock:
            if not self.job or self.job['state'] != 'recording':
                return
            reason = None
            if self.process.poll() is not None:
                reason = 'recorder exited unexpectedly: '+str(self.process.returncode)
            elif self.disk()['free_mb'] < self.config['min_free_mb']:
                reason = 'disk reserve reached'
            elif size(self.root/self.job['name']/'bag') > self.config['max_recording_gb']*1024**3:
                reason = 'configured recording size limit reached'
            elif time.time()-self.job['started_at'] > self.config['max_recording_seconds']:
                reason = 'configured recording duration limit reached'
            if reason:
                self.stop(reason)

    def _finish(self):
        folder = self.root/self.job['name']
        try:
            if self.process.poll() is not None:
                raise RuntimeError('recorder exited before clean stop; inspect recorder.log')
            os.killpg(self.process.pid, signal.SIGINT)
            try:
                code = self.process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=5)
                raise RuntimeError('rosbag2 did not close within 45 seconds; files retained')
            self.logfile.close()
            if code not in (0, -signal.SIGINT):
                raise RuntimeError('recorder failed on stop: '+str(code))
            self._state('validating', stopped_at=time.time(), postflight=self.monitor.snapshot())
            summary = validate(folder/'bag')
            info = subprocess.run(['ros2','bag','info',str(folder/'bag')], capture_output=True, text=True, timeout=30, check=True)
            (folder/'bag-info.txt').write_text(info.stdout)
            summary.update(started_at=self.job['started_at'], stopped_at=self.job['stopped_at'],
                           recording_duration_s=self.job['stopped_at']-self.job['started_at'],
                           preflight=self.job['preflight'], postflight=self.job['postflight'],
                           environment=self.job['environment'], stop_reason=self.job.get('stop_reason'))
            save(folder/'summary.json', summary)
            missing = '\n'.join('- '+t for t in summary['missing']) or 'None'
            invalid = '\n'.join('- '+t for t in summary['invalid']) or 'None'
            (folder/'README.md').write_text('# UAV ROS 2 recording\n\n'
                'Environment: ROS 2 Humble, sqlite3/CDR. Original live G3P/AP6 data.\n'
                'Header times: host receive UTC; camera decode receive time, not sensor exposure time. '
                'Float64 altitudes have no header; bag timestamps are recorder receive UTC.\n'
                'Frames: base_link FLU; attitude base_link->map_enu; velocity ENU; camera_optical_frame optical axes. '
                'Relative home is NOT measured ground clearance. MSL is NOT ellipsoid height. '
                'Battery current positive=charging, negative=discharging.\n\n'
                '## Topics and types\n\n'+ '\n'.join('- '+t+' `'+typ+'`' for t,typ in TYPES.items())+
                '\n\n## Missing topics\n\n'+missing+'\n\n## Topics with invalid/partial content\n\n'+invalid+
                '\n\nSee summary.json for every topic count, samples, validity and UTC nanosecond time range. '
                'Partial recordings are preserved and explicitly labeled.\n\n'
                '## Verify and replay\n\n```bash\nsource /opt/ros/humble/setup.bash\n'
                'sha256sum -c SHA256SUMS\nros2 bag info bag\n'
                'ROS_DOMAIN_ID=91 ros2 bag play bag --clock --rate 0.5\n```\n'
                '\nUse a separate domain from live UAV. Image subscribers use reliable/volatile; other sensors use best_effort/volatile. '
                'Missing data cannot be recreated by playback. No static camera mounting transform is assumed.\n')
            (folder/'environment.json').write_text(json.dumps(clean(self.job['environment']), indent=2))
            for source, target in (('/opt/uav-packages.lock','packages.lock'),('/app/source.sha256','source.sha256')):
                if Path(source).is_file():
                    shutil.copyfile(source, folder/target)
            members = [p for p in folder.rglob('*') if p.is_file() and p.name not in ('job.json',)
                       and p.suffix not in ('.zip','.partial','.tmp')]
            hashes = []
            for p in members:
                h = hashlib.file_digest(p.open('rb'), 'sha256').hexdigest() if hasattr(hashlib,'file_digest') else self._hash(p)
                hashes.append(h+'  '+str(p.relative_to(folder)))
            (folder/'SHA256SUMS').write_text('\n'.join(hashes)+'\n')
            members.append(folder/'SHA256SUMS')
            total = sum(p.stat().st_size for p in members)
            if shutil.disk_usage(folder).free < total+self.config['min_free_mb']*1048576:
                raise RuntimeError('insufficient free space to safely package; bag retained')
            self._state('packaging', progress=0, integrity=summary['integrity'], topic_count=summary['topic_count'],
                        duration_s=summary['duration_s'], missing=summary['missing'], invalid=summary['invalid'])
            partial = folder/(self.job['name']+'.zip.partial')
            completed = 0
            with zipfile.ZipFile(partial,'w',zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as z:
                for p in members:
                    # Chunk writes expose useful progress even for a large database.
                    with p.open('rb') as src, z.open(str(p.relative_to(folder)), 'w', force_zip64=True) as dst:
                        while chunk := src.read(1024*1024):
                            if self.disk()['free_mb'] < self.config['min_free_mb']:
                                raise RuntimeError('disk reserve reached during packaging; partial ZIP retained')
                            dst.write(chunk)
                            completed += len(chunk)
                            with self.lock:
                                self.job['progress'] = round(100*completed/max(total,1),1)
            with zipfile.ZipFile(partial) as z:
                if z.testzip() is not None:
                    raise RuntimeError('ZIP CRC validation failed')
            with partial.open('rb') as f:
                os.fsync(f.fileno())
            final = folder/(self.job['name']+'.zip')
            partial.replace(final)
            digest = self._hash(final)
            (folder/(final.name+'.sha256')).write_text(digest+'  '+final.name+'\n')
            self._state('ready', progress=100, zip_bytes=final.stat().st_size, zip_sha256=digest)
        except Exception as exc:
            failure = dict(error=type(exc).__name__+': '+str(exc))
            if self.job.get('stopped_at') is None:
                failure['stopped_at'] = time.time()
            self._state('failed', **failure)
        finally:
            if self.logfile and not self.logfile.closed:
                self.logfile.close()

    @staticmethod
    def _hash(p):
        h = hashlib.sha256()
        with p.open('rb') as f:
            for b in iter(lambda:f.read(1024*1024), b''):
                h.update(b)
        return h.hexdigest()

    def download(self, name):
        if '/' in name or '..' in name:
            raise ValueError('invalid recording name')
        folder = self.root/name
        j = json.loads((folder/'job.json').read_text())
        if j['state'] != 'ready':
            raise ValueError('recording is not a validated completed archive')
        return folder/(name+'.zip')

    def close(self):
        if self.job and self.job['state'] == 'recording':
            self.stop('service shutdown')
        if self.worker:
            self.worker.join()
        fcntl.flock(self.guard, fcntl.LOCK_UN)
        self.guard.close()
