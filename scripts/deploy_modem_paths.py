#!/usr/bin/env python3
"""Deploy the path daemon with verified, fsynced, atomic file replacements."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
FILES = ((ROOT/'scripts/modem_path_manager.py', Path('/usr/local/lib/laiting-uav/modem_path_manager.py'), 0o755),
         (ROOT/'scripts/systemd/uav-modem-paths.service', Path('/etc/systemd/system/uav-modem-paths.service'), 0o644))


def stage(source, target, mode):
    payload = source.read_bytes()
    if len(payload) < 150:
        raise ValueError('Refusing an empty or incomplete deployment source')
    if source.suffix == '.py':
        compile(payload, str(source), 'exec')
    elif b'ExecStart=/usr/bin/python3 -I /usr/local/lib/laiting-uav/modem_path_manager.py monitor' not in payload:
        raise ValueError('Unexpected path service command')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.'+target.name+'-verified-', dir=target.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if temporary.read_bytes() != payload:
            raise ValueError('Staged deployment failed verification')
        return temporary, hashlib.sha256(payload).hexdigest()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main():
    if os.geteuid() != 0:
        raise PermissionError('Local administrator authentication is required')
    os.umask(0o077)
    backup = Path(tempfile.mkdtemp(prefix='onboard-path-atomic-', dir='/var/backups/laiting-uav'))
    prepared = []
    try:
        for source, target, mode in FILES:
            if target.exists():
                shutil.copyfile(target, backup/target.name)
            temporary, digest = stage(source, target, mode)
            prepared.append((temporary, target, digest))
        for temporary, target, digest in prepared:
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError('Installed file verification failed')
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'enable', 'uav-modem-paths'], check=True)
        subprocess.run(['systemctl', 'restart', 'uav-modem-paths'], check=True)
        subprocess.run(['systemctl', 'is-active', '--quiet', 'uav-modem-paths'], check=True)
        print(json.dumps({'deployed': True, 'backup': str(backup),
                          'files': {str(target): digest for _, target, digest in prepared}}))
    finally:
        for temporary, _, _ in prepared:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
