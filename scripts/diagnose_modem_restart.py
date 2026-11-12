#!/usr/bin/env python3
"""Private, bounded observation of one explicitly requested onboard modem reboot.

The optional action is the vendor dashboard's authenticated modem reboot CGI.
This program never writes USB sysfs, resets a hub/controller, or touches flight
controls. A reset request is never retried, including after an uncertain reply.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

INSTALLED = Path('/usr/local/lib/laiting-uav')
AUTH = Path('/home/hrc/.config/laiting-uav/modem-radio-auth.json')
STATUS = Path('/run/laiting-uav-network/status.json')
BOOT = Path('/proc/sys/kernel/random/boot_id')
USB_ROOT = Path('/sys/bus/usb/devices')
NET_ROOT = Path('/sys/class/net')
USB_INTERFACE = 'enxac0033aa9633'
USB_MAC = 'ac:00:33:aa:96:33'
WIFI_INTERFACE = 'wlP1p1s0'
WIFI_PROFILE = '7b8a7e5f-0e63-46ed-9c9c-9666959890b2'
USB_IDS = {'idVendor': '1286', 'idProduct': '4e31', 'serial': '200806006809080000'}
FC_PORT = '1-2.4'
FC_IDS = {'idVendor': '2a58', 'idProduct': 'a002', 'serial': '330032000D51333331333534'}
RESET_PATH = '/xml_action.cgi?method=get&module=duster&file=reset'
STATUS_PATH = '/xml_action.cgi?method=get&module=duster&file=status1'
PUBLIC_URL = 'https://uav.laiting1202.com/healthz'
COUNTERS = ('rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets', 'rx_errors', 'tx_errors')
MODEM_DEVICE = None


def private_json(path, maximum=32768, root=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or
                (root and info.st_uid != 0)):
            raise ValueError('Private regular file required')
        raw = stream.read(maximum+1)
    if len(raw) > maximum:
        raise ValueError('Private file exceeds limit')
    return json.loads(raw)


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('Report directory must be root owned and private')


def write_json(path, value, *, exclusive=False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        json.dump(value, output, separators=(',', ':'), allow_nan=False)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())


def installed_dependencies():
    global MODEM_DEVICE
    info = INSTALLED.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Trusted installed helper directory required')
    for name in ('modem_radio', 'modem_path_manager', 'modem_recovery'):
        info = (INSTALLED/(name+'.py')).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('Trusted installed helper file required')
    sys.path.insert(0, str(INSTALLED))
    recovery = importlib.import_module('modem_recovery')
    radio = importlib.import_module('modem_radio')
    paths = importlib.import_module('modem_path_manager')
    if any(Path(module.__file__).resolve() != INSTALLED/(name+'.py')
           for module, name in ((radio, 'modem_radio'), (paths, 'modem_path_manager'), (recovery, 'modem_recovery'))):
        raise ValueError('Unexpected loaded helper identity')
    MODEM_DEVICE = recovery.modem_device
    return radio, paths.bound_connect


def device_generation(port, identities):
    device = USB_ROOT/port
    try:
        if not all((device/key).read_text().strip() == expected for key, expected in identities.items()):
            return None
        generation = tuple(int((device/key).read_text()) for key in ('busnum', 'devnum'))
        return generation if all(number > 0 for number in generation) else None
    except (OSError, ValueError):
        return None


def observed_modem():
    """Require a unique exact device, then the trusted helper's net-owner check."""
    matches = []
    try:
        for candidate in USB_ROOT.iterdir():
            try:
                if (re.fullmatch(r'\d+-\d+(?:\.\d+)*', candidate.name) and
                        all((candidate/key).read_text().strip() == expected for key, expected in USB_IDS.items()) and
                        (candidate/'bDeviceClass').read_text().strip() != '09'):
                    matches.append(candidate)
            except OSError:
                pass
    except OSError:
        pass
    if len(matches) != 1 or MODEM_DEVICE is None:
        return None, None, len(matches)
    try:
        device = MODEM_DEVICE(net_root=NET_ROOT, usb_root=USB_ROOT)
        if device.resolve(strict=True) != matches[0].resolve(strict=True):
            return None, None, len(matches)
        generation = device_generation(device.name, USB_IDS)
        if generation is not None and (device/'bDeviceClass').read_text().strip() != '09':
            return device, generation, len(matches)
    except (OSError, ValueError):
        pass
    return None, None, len(matches)


def usb_sample():
    interface = NET_ROOT/USB_INTERFACE
    device, generation, matches = observed_modem()
    result = {'device': device.name if device else None, 'matching_devices': matches,
              'port_present': matches > 0, 'identity_matched': generation is not None,
              'generation': generation, 'interface_present': interface.exists(),
              'interface_matched': False, 'stats': None, 'runtime_status': None, 'cdc_driver': None}
    if device is None:
        return result
    try:
        runtime = (device/'power/runtime_status').read_text().strip()
        result['runtime_status'] = runtime if runtime in ('active', 'suspended', 'suspending', 'resuming') else 'unknown'
    except OSError:
        pass
    try:
        driver = (device/(device.name+':1.0')/'driver').resolve(strict=True).name
        result['cdc_driver'] = driver if driver == 'cdc_ether' else 'other'
    except OSError:
        pass
    try:
        matched = (generation is not None and (interface/'address').read_text().strip().lower() == USB_MAC and
                   (interface/'device').resolve(strict=True).parent == device.resolve(strict=True))
        if matched:
            counters = {name: int((interface/'statistics'/name).read_text()) for name in COUNTERS}
            # Reject a device replaced during the read, including stale counters.
            current, current_generation, _ = observed_modem()
            if (any(value < 0 for value in counters.values()) or current != device or
                    current_generation != generation):
                return result
            result.update(interface_matched=True, stats=counters)
    except (OSError, ValueError):
        pass
    return result


def network_sample(boot_id, now):
    result = {'fresh': False, 'active_path': 'unknown', 'phase': 'unknown',
              'wifi_connected': None, 'wifi_internet': None, 'usb_connected': None, 'usb_internet': None}
    try:
        parent = STATUS.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
            return result
        descriptor = os.open(STATUS, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                return result
            raw = stream.read(32769)
        if len(raw) > 32768:
            return result
        status = json.loads(raw)
        updated = status.get('monotonic')
        if (status.get('boot_id') != boot_id or not isinstance(updated, (int, float)) or
                isinstance(updated, bool) or not 0 <= now-updated <= 8):
            return result
        result.update(fresh=True,
                      active_path=status.get('active_path') if status.get('active_path') in ('usb', 'wifi', 'none') else 'unknown',
                      phase=status.get('phase') if status.get('phase') in ('stable', 'preparing', 'switching', 'recovering_tunnel', 'offline') else 'unknown')
        wifi, usb = status.get('wifi', {}), status.get('usb', {})
        if isinstance(wifi, dict) and wifi.get('interface') == WIFI_INTERFACE and wifi.get('profile_uuid') == WIFI_PROFILE:
            result.update(wifi_connected=wifi.get('connected') is True, wifi_internet=wifi.get('internet') is True)
        if isinstance(usb, dict) and usb.get('interface') == USB_INTERFACE:
            result.update(usb_connected=usb.get('connected') is True, usb_internet=usb.get('internet') is True)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return result


def public_health():
    try:
        result = subprocess.run(['/usr/bin/curl', '--noproxy', '*', '-fsS', '--connect-timeout', '1',
                                 '--max-time', '2', '--max-filesize', '1024', PUBLIC_URL],
                                capture_output=True, text=True, timeout=2.5)
        return result.returncode == 0 and json.loads(result.stdout) == {'status': 'ok'}
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def authenticated_device(client):
    client.login()
    _, body = client.request(STATUS_PATH, client.header())
    root = ET.fromstring(body)
    if root.findtext('.//login_status') in ('UNAUTHORIZED', 'KICKOFF'):
        raise PermissionError('Authenticated modem status required')
    version = {key: root.findtext('.//sysinfo/'+field) for key, field in
               (('device_name', 'device_name'), ('hardware_version', 'hardware_version'), ('firmware_version', 'version_num'))}
    if version != {'device_name': 'DWM-222W', 'hardware_version': 'B1', 'firmware_version': '1.01.00'}:
        raise ValueError('Unexpected authenticated modem version; no reboot sent')
    return version


def request_one_reboot(client):
    """An EOF/timeout can mean a delivered reset; it must never cause a retry."""
    try:
        _, body = client.request(RESET_PATH, client.header(), method='GET')
        root = ET.fromstring(body)
        code = root.findtext('.//return_code')
        unauthorized = root.findtext('.//login_status') in ('UNAUTHORIZED', 'KICKOFF')
        return {'attempted': True, 'result': 'accepted' if code == '0' and not unauthorized else 'reply_unconfirmed'}
    except Exception as error:
        # Exception class only: no URLs, XML, digest headers, or credential text.
        return {'attempted': True, 'result': 'exception_unconfirmed', 'exception': type(error).__name__}


def kernel_observation(after_monotonic, confirmed_ports=()):
    """Read a bounded journal window; preserve USB messages without payloads."""
    events = []
    newest = after_monotonic
    try:
        result = subprocess.run(['/usr/bin/journalctl', '-k', '-b', '-n', '200', '-o', 'json', '--no-pager'],
                                capture_output=True, text=True, timeout=2)
        if result.returncode:
            return {'events': [], 'after_monotonic': newest, 'available': False, 'truncated': False}
        if len(result.stdout) > 512*1024:
            return {'events': [], 'after_monotonic': newest, 'available': True, 'truncated': True}
        for line in result.stdout.splitlines()[-200:]:
            value = json.loads(line)
            observed = int(value.get('__MONOTONIC_TIMESTAMP', 0))/1000000
            if observed <= after_monotonic:
                continue
            newest = max(newest, observed)
            message = value.get('MESSAGE')
            ports = {port for port in confirmed_ports if re.fullmatch(r'\d+-\d+(?:\.\d+)*', port)}
            usb_port = re.search(r'\b(?:usb|cdc_ether) (\d+-\d+(?:\.\d+)*)(?=[:\s])', message) if isinstance(message, str) else None
            if isinstance(message, str) and (
                    (usb_port and usb_port[1] in ports | {FC_PORT}) or
                    any(token in message for token in ('xhci-hcd', 'xhci_hcd', USB_INTERFACE))):
                scope = 'modem' if usb_port and usb_port[1] in ports else 'flight' if usb_port and usb_port[1] == FC_PORT else 'controller'
                events.append({'monotonic': observed, 'scope': scope, 'message': message[:500]})
        return {'events': events[-50:], 'after_monotonic': newest, 'available': True,
                'truncated': len(events) > 50}
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return {'events': [], 'after_monotonic': newest, 'available': False, 'truncated': False}


class StableUSB:
    def __init__(self):
        self.started = None
        self.generation = None
        self.device = None
        self.initial = None
        self.stable_seconds = 0

    def update(self, usb, probe, now):
        stats = usb.get('stats')
        healthy = (usb.get('identity_matched') is True and usb.get('interface_matched') is True and
                   isinstance(stats, dict) and isinstance(probe, dict) and
                   isinstance(usb.get('device'), str) and probe.get('device') == usb['device'] and
                   probe.get('generation') == usb.get('generation') and
                   0 <= now-probe.get('monotonic', float('-inf')) <= 4 and
                   probe.get('internet') is True)
        continued = (healthy and self.started is not None and self.device == usb['device'] and self.generation == usb['generation'] and
                     stats['rx_errors'] == self.initial['rx_errors'] and stats['tx_errors'] == self.initial['tx_errors'] and
                     all(stats[name] >= self.initial[name] for name in COUNTERS))
        if not continued:
            self.started = now if healthy else None
            self.generation = usb.get('generation') if healthy else None
            self.device = usb.get('device') if healthy else None
            self.initial = dict(stats) if healthy else None
        self.stable_seconds = now-self.started if self.started is not None else 0
        return (self.stable_seconds >= 30 and stats['rx_bytes'] > self.initial['rx_bytes'] and
                stats['tx_bytes'] > self.initial['tx_bytes']) if self.initial is not None else False


def observe(directory, seconds, boot_id, before, flight_before, bound_connect, *, clock=time.monotonic,
            sleep=time.sleep, stop_when_verified=False):
    started = clock()
    stable = StableUSB()
    last_public = None
    last_probe = None
    public_future = usb_future = None
    kernel_future = None
    kernel_after = 0
    next_kernel = started
    flight_unchanged = flight_before is not None
    first_usb, first_public, stable_verified_at = None, None, None
    count = 0
    final = before
    descriptor = os.open(directory/'samples.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    pool = ThreadPoolExecutor(max_workers=3)

    def probe_usb(device, generation):
        # Both destinations are literal addresses; a certificate-verified TLS
        # exchange proves returned traffic on USB rather than mere enumeration.
        def same():
            sample = usb_sample()
            return (sample['interface_matched'] and sample['device'] == device and
                    sample['generation'] == generation)
        before_probe = same()
        first = before_probe and bound_connect(USB_INTERFACE, '1.1.1.1', 443, True, timeout=.8)
        after_first = before_probe and same()
        second = after_first and bound_connect(USB_INTERFACE, '8.8.8.8', 443, True, timeout=.8)
        after_probe = after_first and same()
        return {'device': device, 'generation': generation, 'internet': after_probe and (first or second),
                'identity_unchanged': after_probe, 'tls_first': first, 'tls_second': second, 'monotonic': clock()}

    confirmed_ports = {before['device']} if before.get('identity_matched') and before.get('device') else set()
    try:
        with os.fdopen(descriptor, 'w') as output:
            while clock()-started < seconds:
                now = clock()
                final = usb_sample()
                if final.get('identity_matched') and final.get('device'):
                    confirmed_ports.add(final['device'])
                network = network_sample(boot_id, now)
                flight_current = device_generation(FC_PORT, FC_IDS)
                same_flight = flight_before is not None and flight_current == flight_before
                flight_unchanged = flight_unchanged and same_flight
                if public_future is not None and public_future.done():
                    last_public = {'ok': public_future.result(), 'monotonic': now}
                    public_future = None
                if usb_future is not None and usb_future.done():
                    last_probe = usb_future.result()
                    usb_future = None
                kernel = None
                if kernel_future is not None and kernel_future.done():
                    kernel = kernel_future.result()
                    kernel_after = kernel['after_monotonic']
                    kernel_future = None
                if kernel_future is None and now >= next_kernel:
                    kernel_future = pool.submit(kernel_observation, kernel_after, tuple(sorted(confirmed_ports)))
                    next_kernel = now+5
                if public_future is None:
                    public_future = pool.submit(public_health)
                if usb_future is None and final['interface_matched']:
                    usb_future = pool.submit(probe_usb, final['device'], final['generation'])
                public_ok = bool(last_public and now-last_public['monotonic'] <= 4 and last_public['ok'])
                usb_verified = stable.update(final, last_probe, now)
                if first_usb is None and final['identity_matched']:
                    first_usb = round(now-started, 4)
                if first_public is None and public_ok:
                    first_public = round(now-started, 4)
                if usb_verified and stable_verified_at is None:
                    stable_verified_at = round(now-started, 4)
                row = {'elapsed': round(now-started, 4), 'monotonic': now, 'usb': final,
                       'network': network, 'usb_probe': last_probe, 'public_ok': public_ok,
                       'flight_generation': flight_current, 'flight_unchanged': same_flight,
                       'usb_stable_seconds': round(stable.stable_seconds, 4), 'usb_verified_30s': usb_verified}
                if kernel is not None:
                    row['kernel'] = kernel
                output.write(json.dumps(row, separators=(',', ':'), allow_nan=False)+'\n')
                output.flush()
                count += 1
                if count == 1 or count % 5 == 0:
                    os.fsync(output.fileno())
                if stop_when_verified and usb_verified and public_ok and flight_unchanged:
                    break
                sleep(min(1, max(0, started+seconds-clock())))
            os.fsync(output.fileno())
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return {'observation_seconds': round(clock()-started, 4), 'samples': count,
            'usb_before': before, 'usb_final': final, 'first_usb_enumeration_seconds': first_usb,
            'first_public_healthy_seconds': first_public, 'usb_verified_30s_at_seconds': stable_verified_at,
            'usb_verified_30s': stable_verified_at is not None and stable.stable_seconds >= 30,
            'confirmed_usb_ports': sorted(confirmed_ports),
            'flight_generation_unchanged': flight_unchanged, 'permanent_fix_proven': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--reboot', action='store_true', help='Send exactly one authenticated modem reboot')
    actions.add_argument('--observe-only', action='store_true', help='Default: read-only, no modem authentication or mutation')
    reports = parser.add_mutually_exclusive_group(required=True)
    reports.add_argument('--directory', '--output-directory', type=Path,
                         help='A new root-private directory for this single run')
    reports.add_argument('--boot-observe-base', type=Path,
                         help='Read-only boot witness: write to this root-private base / boot UUID')
    parser.add_argument('--auth-file', type=Path, default=AUTH)
    parser.add_argument('--seconds', type=int, default=180)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run the root-owned installed copy as administrator')
    if not 1 <= args.seconds <= 180:
        parser.error('--seconds must be between 1 and 180')
    os.umask(0o077)
    boot_id = BOOT.read_text().strip()
    if str(uuid.UUID(boot_id)) != boot_id:
        raise ValueError('Canonical boot UUID required')
    if args.boot_observe_base:
        if args.reboot:
            parser.error('--boot-observe-base is read-only; use --directory for an explicit reboot')
        private_directory(args.boot_observe_base)
        args.directory = args.boot_observe_base/boot_id
    private_directory(args.directory)
    metadata = {'boot_id': boot_id, 'started_at': time.time(), 'started_monotonic': time.monotonic(),
                'reboot_requested': args.reboot, 'maximum_observation_seconds': args.seconds,
                'usb_identity_tracking': 'unique_vid_pid_serial_and_net_owner_each_sample'}
    # Never reuse a report directory: an interrupted run cannot trigger a
    # repeated reset when restarted with the same command.
    write_json(args.directory/'run.json', metadata, exclusive=True)
    radio, bound_connect = installed_dependencies()
    before = usb_sample()
    flight_before = device_generation(FC_PORT, FC_IDS)
    outcome = {'attempted': False, 'result': 'observe_only'}
    if args.reboot:
        if flight_before is None:
            raise ValueError('Exact flight-controller generation must be present before reboot test')
        client = radio.RadioClient(private_json(args.auth_file), transport='onboard-wifi')
        version = authenticated_device(client)
        if device_generation(FC_PORT, FC_IDS) != flight_before:
            raise ValueError('Flight-controller generation changed before modem request')
        write_json(args.directory/'action.json', {'version': version, 'monotonic': time.monotonic(),
                                                'request': 'one_vendor_modem_reboot'}, exclusive=True)
        outcome = request_one_reboot(client)
        write_json(args.directory/'action-result.json', outcome, exclusive=True)
    summary = observe(args.directory, args.seconds, boot_id, before, flight_before, bound_connect,
                      stop_when_verified=args.reboot)
    summary.update(boot_id=boot_id, reboot=outcome)
    write_json(args.directory/'summary.json', summary, exclusive=True)
    print(json.dumps({'report_written': True, 'samples': summary['samples'],
                      'usb_verified_30s': summary['usb_verified_30s'],
                      'flight_generation_unchanged': summary['flight_generation_unchanged']}, separators=(',', ':')))


if __name__ == '__main__':
    main()
