#!/usr/bin/env python3
"""Atomically deploy bounded onboard network fixes, with file/service rollback.

Does not reboot, reset USB, reactivate profiles, or restart the UAV or tunnel.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LIB = Path('/usr/local/lib/laiting-uav')
UNITS = Path('/etc/systemd/system')
FILES = tuple((ROOT/'scripts'/name, LIB/name) for name in
              ('modem_path_manager.py', 'modem_recovery.py', 'modem_rf_guard.py', 'network_boot_check.py'))
FILES += tuple((ROOT/'scripts/systemd'/name, UNITS/name) for name in
               ('uav-modem-paths.service', 'uav-4g-boot-validation.service'))
SERVICES = ('uav-modem-paths.service', 'uav-modem-recovery.service', 'uav-modem-rf-guard.service')
BOOT_UNIT = 'uav-4g-boot-validation.service'
ENABLE = (*SERVICES, BOOT_UNIT, 'uav-tunnel-resolver.service')


def run(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, timeout=30, check=check)


def source_payloads():
    payloads = []
    for source, target in FILES:
        payload = source.read_bytes()
        if len(payload) < 150:
            raise ValueError('Deployment source is incomplete: '+source.name)
        if source.suffix == '.py':
            compile(payload, str(source), 'exec')
        payloads.append((target, payload))
    return payloads


def replace(target, payload):
    descriptor, name = tempfile.mkstemp(prefix='.'+target.name+'-', dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if target.read_bytes() != payload:
            raise ValueError('Installed payload differs: '+target.name)
    finally:
        temporary.unlink(missing_ok=True)


def deploy(payloads):
    # No live USB identity is required: the USB function can be absent while
    # its independently verified onboard Wi-Fi rescue is keeping us online.
    if __package__:
        from .modem_recovery import modem_profile_check
        from .modem_path_manager import load_config
    else:
        import sys
        sys.path.insert(0, str(ROOT/'scripts'))
        from modem_recovery import modem_profile_check
        from modem_path_manager import load_config
    modem_profile_check()
    load_config()
    run('systemd-analyze', 'verify', *(str(source) for source, _ in FILES if source.suffix == '.service'))
    parent = Path('/var/backups/laiting-uav')
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(prefix='network-stability-', dir=parent))
    original = {target: target.read_bytes() if target.exists() else None for target, _ in payloads}
    for target, payload in original.items():
        if payload is not None:
            (backup/target.name).write_bytes(payload)
    enabled = {unit: run('systemctl', 'is-enabled', unit, check=False).stdout.strip() for unit in ENABLE}
    active = {unit: run('systemctl', 'is-active', '--quiet', unit, check=False).returncode == 0
              for unit in (*SERVICES, BOOT_UNIT)}
    (backup/'services.json').write_text(json.dumps({'enabled': enabled, 'active': active}, indent=2)+'\n')
    try:
        for target, payload in payloads:
            replace(target, payload)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', *ENABLE)
        for unit in SERVICES:
            run('systemctl', 'restart', unit)
            run('systemctl', 'is-active', '--quiet', unit)
        run('systemctl', 'restart', '--no-block', BOOT_UNIT)
    except BaseException:
        failures = []
        for target, payload in original.items():
            try:
                if payload is None:
                    target.unlink(missing_ok=True)
                else:
                    replace(target, payload)
            except OSError:
                failures.append(str(target))
        run('systemctl', 'daemon-reload', check=False)
        for unit, before in enabled.items():
            if before in ('disabled', 'not-found', ''):
                run('systemctl', 'disable', unit, check=False)
        for unit, before in active.items():
            run('systemctl', 'restart' if before else 'stop', '--no-block', unit, check=False)
        if failures:
            print('Rollback needs attention for: '+', '.join(failures), flush=True)
        print('Deployment failed; previous files/service settings restored. Backup: '+str(backup), flush=True)
        raise
    return {'deployed': True, 'backup': str(backup),
            'boot_acceptance': 'running; inspect /var/lib/laiting-uav-boot-validation/network-stability/latest.json',
            'sha256': {str(target): hashlib.sha256(payload).hexdigest() for target, payload in payloads}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Validate sources without system changes')
    args = parser.parse_args()
    payloads = source_payloads()
    if args.check:
        print(json.dumps({'source_valid': True, 'targets': [str(target) for target, _ in payloads]}, indent=2))
        return
    if os.geteuid() != 0:
        parser.error('Run sudo /usr/bin/python3 scripts/deploy_network_stability.py in your local terminal.')
    os.umask(0o077)
    print(json.dumps(deploy(payloads), indent=2), flush=True)


if __name__ == '__main__':
    main()
