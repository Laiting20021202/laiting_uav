#!/usr/bin/env python3
"""Passive, bounded xHCI metadata capture for the exact laboratory D-Link.

No USB, network, radio, or driver control is written.  Only this recorder's
private tracefs instance is configured.  Native tracepoints cannot expose the
finish_td error-time branch/state, so these captures are context evidence, not
proof that the f7812977456c bug caused a fault.
"""
import argparse
from collections import deque
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import threading
import time

INSTANCE = 'laiting-uav-usb-context'
UNIT = 'uav-usb-context-recorder.service'
INSTALLED = Path('/usr/local/lib/laiting-uav/usb_context_recorder.py')
DIRECTORY = Path('/var/lib/laiting-uav-usb-context')
USB_ROOT = Path('/sys/bus/usb/devices')
DEBUG_ROOT = Path('/sys/kernel/debug/usb/xhci')
IDENTITY = ('1286', '4e31', '200806006809080000')
CONTEXT_EVENTS = ('xhci_handle_cmd_reset_ep', 'xhci_handle_cmd_set_deq_ep',
                  'xhci_handle_cmd_stop_ep')
FORMATS = {
    'xhci_handle_event': ('type', 'field0', 'field1', 'field2', 'field3'),
    'xhci_handle_command': ('type', 'field0', 'field1', 'field2', 'field3'),
    'xhci_urb_enqueue': ('slot_id', 'length', 'actual', 'epnum', 'dir_in'),
    **{name: ('info', 'info2', 'deq', 'tx_info') for name in CONTEXT_EVENTS},
    'xhci_handle_port_status': ('portnum', 'portsc'),
}
PRE_SECONDS = 10.0
POST_SECONDS = 4.0
COOLDOWN_SECONDS = 60.0
MAX_RING_BYTES = 1024 * 1024
MAX_RING_ROWS = 4096
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_FILES = 4
LIMITATION = ('Native endpoint-context events lack slot/endpoint identity and '
              'occur after command handling, not at finish_td error dispatch. '
              'CPU/time correlation and later debugfs snapshots are indirect; '
              'they cannot establish the f7812977456c failure mechanism.')


def read_small(path, limit=256):
    with Path(path).open('r', encoding='ascii', errors='strict') as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise ValueError('oversized metadata')
    return value.strip()


@dataclass(frozen=True)
class Device:
    device: str
    controller: str
    slot: int
    busnum: int
    devnum: int
    serial: str


def identify(usb_root=USB_ROOT, debug_root=DEBUG_ROOT):
    """Reject ambiguous identity/controller/slot and mid-read re-enumeration."""
    controllers = [p for p in debug_root.iterdir() if (p/'devices').is_dir()]
    # Native trace records have slot, but no controller identity.
    if len(controllers) != 1:
        raise ValueError('native trace controller identity is ambiguous')
    candidates = []
    for path in usb_root.iterdir():
        if not re.fullmatch(r'\d+-\d+(?:\.\d+)*', path.name):
            continue
        try:
            values = tuple(read_small(path/key) for key in
                           ('idVendor', 'idProduct', 'serial'))
            if values == IDENTITY and read_small(path/'bDeviceClass') != '09':
                candidates.append(path)
        except (OSError, ValueError):
            continue
    if len(candidates) != 1:
        raise ValueError('exact D-Link identity is missing or ambiguous')
    path = candidates[0]
    generation = (int(read_small(path/'busnum')), int(read_small(path/'devnum')))
    slots = []
    for name in (controllers[0]/'devices').glob('*/name'):
        if read_small(name) == path.name and re.fullmatch(r'\d{2,3}', name.parent.name):
            slots.append(int(name.parent.name))
    if len(slots) != 1 or not 1 <= slots[0] <= 255:
        raise ValueError('exact D-Link debugfs slot is missing or ambiguous')
    if (tuple(read_small(path/key) for key in ('idVendor', 'idProduct', 'serial'))
            != IDENTITY or generation != (int(read_small(path/'busnum')),
                                           int(read_small(path/'devnum')))):
        raise ValueError('D-Link generation changed during identity sampling')
    return Device(path.name, controllers[0].name, slots[0], *generation, IDENTITY[2])


def validate_formats(trace_root):
    for name, required in FORMATS.items():
        text = read_small(trace_root/'events/xhci-hcd'/name/'format', 16384)
        fields = set(re.findall(r'field:[^;\n]*?\b(\w+)(?:\[\])?;', text))
        if not set(required) <= fields:
            raise ValueError('required xHCI metadata fields unavailable: ' + name)


def normalized_filter(value):
    return re.sub(r'\s+', '', value).replace('(', '').replace(')', '')


def slot_filter(slot):
    # This 5.15 tracefs parser supports field/op/constant predicates, not
    # '(field & mask) == value'. Slot is the unsigned field3 high byte.
    low = slot << 24
    return (f'field3 >= {low} && field3 < {(slot + 1) << 24}'
            if slot < 255 else f'field3 >= {low}')


class TraceSession:
    """Changes only a separate instance, leaving existing recorder untouched."""
    def __init__(self, trace_root):
        validate_formats(trace_root)
        self.instance = trace_root/'instances'/INSTANCE
        self.instance.mkdir(exist_ok=True)
        self.targets = {name: self.instance/'events/xhci-hcd'/name for name in FORMATS}
        # Refuse a reused instance with unrelated enabled events.
        for enabled in (self.instance/'events').glob('*/*/enable'):
            if read_small(enabled) == '1' and enabled.parent.name not in FORMATS:
                raise ValueError('private instance has an unrelated enabled event')
        self.disable()
        (self.instance/'tracing_on').write_text('0')
        (self.instance/'current_tracer').write_text('nop')
        (self.instance/'buffer_size_kb').write_text('256')  # per CPU, bounded
        (self.instance/'trace_clock').write_text('mono')
        (self.instance/'trace').write_text('')

    def disable(self):
        for target in self.targets.values():
            (target/'enable').write_text('0')
        (self.instance/'tracing_on').write_text('0')

    def bind(self, device):
        self.disable()
        try:
            # All normal/short/error completions for the strictly verified slot.
            filters = {
                'xhci_handle_event': slot_filter(device.slot),
                'xhci_handle_command': slot_filter(device.slot),
                'xhci_urb_enqueue': f'slot_id == {device.slot}',
                **{name: '0' for name in CONTEXT_EVENTS},
                'xhci_handle_port_status': '0',
            }
            for name, expression in filters.items():
                target = self.targets[name]
                (target/'filter').write_text(expression)
                accepted = normalized_filter(read_small(target/'filter', 4096))
                if not (accepted == normalized_filter(expression) or
                        expression == '0' and accepted == 'none'):
                    raise ValueError('kernel rejected metadata filter: ' + name)
            (self.instance/'trace').write_text('')
            for target in self.targets.values():
                (target/'enable').write_text('1')
            (self.instance/'tracing_on').write_text('1')
        except BaseException:
            self.disable()
            raise


HEADER = re.compile(r'\[([0-9]{1,4})\].*?\s([0-9]+\.[0-9]+):\s+(xhci_\w+):\s*(.{1,700})$')
TRANSFER = re.compile(r"EVENT: TRB ([0-9a-f]{16}) status '([A-Za-z0-9 /_-]{1,60})' len (\d+) slot (\d+) ep (\d+) type '(Transfer Event|Command Completion Event)' flags ([eE]:[cC])$")
URB = re.compile(r'ep(\d+)(in|out)-(bulk|control|intr|isoc): urb ([0-9a-f]+) pipe (\d+) slot (\d+) length (\d+)/(\d+) sgs (\d+)/(\d+) stream (\d+) flags ([0-9a-f]{8})$')
COMMAND = re.compile(r'CMD: (Reset Endpoint Command|Stop Ring Command|Set TR Dequeue Pointer Command): (.{1,160})$')
EP_CONTEXT = re.compile(r'State (\w+) mult (\d+) max P\. Streams (\d+) (?:LSA )?interval (\d+) us max ESIT payload (\d+) CErr (\d+) Type ([A-Za-z0-9 /_-]{1,24}) (?:HID)?burst (\d+) maxp (\d+) deq ([0-9a-f]{16}) avg trb len (\d+)$')
STATUS_CODES = {'Success': 1, 'USB Transaction Error': 4, 'Short Packet': 13}


def parse_context(text):
    match = EP_CONTEXT.fullmatch(text)
    if not match:
        return None
    value = match.groups()
    return dict(zip(('state', 'mult', 'max_streams', 'interval_us', 'max_esit',
                     'cerr', 'endpoint_type', 'burst', 'max_packet', 'dequeue',
                     'average_trb_length'), value))


def parse_metadata(line):
    """Whitelist decoded numbers/states only; never save raw trace lines/TRBs."""
    if len(line) > 1024:
        return None
    match = HEADER.search(line)
    if not match:
        return None
    cpu, timestamp, name, text = match.groups()
    row = {'cpu': int(cpu), 'monotonic': float(timestamp), 'event': name}
    if name == 'xhci_handle_event':
        match = TRANSFER.fullmatch(text)
        if not match:
            return None
        pointer, status, length, slot, endpoint, kind, flags = match.groups()
        row.update(trb_pointer=pointer, status=status, completion_code=STATUS_CODES.get(status),
                   residual_length=int(length), slot=int(slot), endpoint=int(endpoint),
                   trb_type=kind, flags=flags)
    elif name == 'xhci_urb_enqueue':
        match = URB.fullmatch(text)
        if not match:
            return None
        ep, direction, kind, urb, pipe, slot, actual, length, mapped, sgs, stream, flags = match.groups()
        row.update(endpoint=int(ep), direction=direction, transfer_type=kind,
                   urb=urb[:16], pipe=int(pipe), slot=int(slot), actual=int(actual),
                   requested_length=int(length), mapped_sgs=int(mapped), sgs=int(sgs),
                   stream=int(stream), flags=flags)
    elif name == 'xhci_handle_command':
        match = COMMAND.fullmatch(text)
        if not match:
            return None
        command, remainder = match.groups()
        fields = dict(re.findall(r'\b(slot|ep|stream|sp) (\d+)\b', remainder))
        # No command setup fields or arbitrary decoded parameter text retained.
        if 'slot' not in fields or 'ep' not in fields:
            return None
        row.update(command=command, slot=int(fields['slot']), endpoint=int(fields['ep']))
        pointer = re.search(r'\b(?:ctx|deq) ([0-9a-f]{16})\b', remainder)
        if pointer:
            row['pointer'] = pointer[1]
    elif name in CONTEXT_EVENTS:
        context = parse_context(text)
        if context is None:
            return None
        row['context'] = context
    elif name == 'xhci_handle_port_status':
        # Root port status is controller-wide, not the D-Link's hub child port.
        match = re.fullmatch(r'port-(\d+): (.{1,240})', text)
        if not match:
            return None
        row.update(root_port=int(match[1]), attribution='controller-wide, not modem-specific')
        state = re.search(r'\b(?:link|Link):?\s+(\w+)', match[2])
        if state:
            row['link_state'] = state[1]
    else:
        return None
    return row


class Window:
    def __init__(self):
        self.rows = deque()
        self.size = 0
        self.evicted = 0
        self.commands = {}

    def clear(self):
        self.rows.clear()
        self.size = 0
        self.commands.clear()
        self.evicted = 0

    def add(self, row, device):
        name = row['event']
        if 'slot' in row and row['slot'] != device.slot:
            return False
        if name == 'xhci_handle_command':
            self.commands[row['cpu']] = row
        elif name in CONTEXT_EVENTS:
            command = self.commands.pop(row['cpu'], None)
            expected = {'xhci_handle_cmd_reset_ep': 'Reset Endpoint Command',
                        'xhci_handle_cmd_set_deq_ep': 'Set TR Dequeue Pointer Command',
                        'xhci_handle_cmd_stop_ep': 'Stop Ring Command'}[name]
            if not command or command['command'] != expected or not (
                    0 <= row['monotonic'] - command['monotonic'] <= 0.005):
                return False
            row.update(attribution='indirect same-CPU command time correlation',
                       correlated_endpoint=command['endpoint'])
        encoded = json.dumps(row, separators=(',', ':'))
        self.rows.append((row, len(encoded) + 1))
        self.size += len(encoded) + 1
        while self.rows and (self.size > MAX_RING_BYTES or len(self.rows) > MAX_RING_ROWS or
                            self.rows[0][0]['monotonic'] < row['monotonic'] - PRE_SECONDS):
            self.size -= self.rows.popleft()[1]
            self.evicted += 1
        return True


def cdc_endpoints(device, usb_root=USB_ROOT):
    endpoints = {1}  # default control endpoint
    for interface in usb_root.glob(device.device + ':*'):
        try:
            if read_small(interface/'bInterfaceClass') != '0a':
                continue
            for path in interface.glob('ep_*'):
                address = int(read_small(path/'bEndpointAddress'), 16)
                attributes = int(read_small(path/'bmAttributes'), 16)
                if attributes & 3 == 2 and address & 15:
                    endpoints.add((address & 15) * 2 + bool(address & 0x80))
        except (OSError, ValueError):
            continue
    return sorted(endpoints)


def snapshot(device, usb_root=USB_ROOT, debug_root=DEBUG_ROOT):
    """Device-specific bounded read-only metadata; never read ring 'trbs'."""
    if identify(usb_root, debug_root) != device:
        return {'identity_valid': False}
    result = {'identity_valid': True, 'sampled_monotonic': time.monotonic(),
              'endpoint_context_timing': 'later asynchronous snapshot, not error-time state'}
    path = usb_root/device.device
    for key in ('speed', 'bcdDevice', 'quirks', 'power/control', 'power/runtime_status'):
        try:
            result[key] = read_small(path/key)
        except (OSError, ValueError):
            pass
    slot = debug_root/device.controller/'devices'/f'{device.slot:02d}'
    endpoint_ids = cdc_endpoints(device, usb_root)
    result['cdc_endpoint_ids'] = endpoint_ids
    result['cdc_bulk_descriptor_available'] = len(endpoint_ids) > 1
    contexts = []
    try:
        for index, line in enumerate(read_small(slot/'ep-context', 16384).splitlines()[:31]):
            # Native debugfs emits contexts in endpoint index order.
            context = parse_context(line.split(': ', 1)[-1])
            if context and index + 1 in endpoint_ids:
                contexts.append({'xhci_endpoint_id': index + 1, **context})
    except (OSError, ValueError):
        pass
    result['endpoint_contexts'] = contexts
    rings = {}
    for endpoint_id in endpoint_ids:
        index = endpoint_id - 1
        metadata = {}
        for key in ('enqueue', 'dequeue', 'cycle'):
            try:
                value = read_small(slot/f'ep{index:02d}'/key)
                if re.fullmatch(r'[0-9a-f]{1,16}', value):
                    metadata[key] = value
            except (OSError, ValueError):
                pass
        rings[str(index + 1)] = metadata
    result['software_ring_pointers'] = rings
    stats = {}
    for interface in usb_root.glob(device.device + ':*'):
        for net in interface.glob('net/*'):
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}', net.name):
                continue
            stats[net.name] = {}
            for key in ('rx_bytes', 'tx_bytes', 'rx_errors', 'tx_errors', 'rx_packets', 'tx_packets'):
                try:
                    stats[net.name][key] = int(read_small(net/'statistics'/key))
                except (OSError, ValueError):
                    pass
    result['net_stats'] = stats
    if identify(usb_root, debug_root) != device:
        return {'identity_valid': False, 'discarded_due_to_generation_change': True}
    return result


def private_directory(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.geteuid():
        raise ValueError('evidence directory must be owned by recorder user')
    directory.chmod(0o700)


def save_capture(directory, header, rows, rotate=True):
    """Four fixed file names, each capped; no free-form device path used."""
    payload = json.dumps(header, separators=(',', ':')) + '\n'
    used = len(payload.encode())
    if used > MAX_CAPTURE_BYTES - 256:
        raise ValueError('capture header exceeds bounded metadata budget')
    kept = []
    truncated = False
    for row in rows:
        line = json.dumps(row, separators=(',', ':')) + '\n'
        if used + len(line.encode()) > MAX_CAPTURE_BYTES - 256:
            truncated = True
            break
        kept.append(line)
        used += len(line.encode())
    payload += ''.join(kept) + json.dumps({'capture_end': True, 'truncated': truncated}) + '\n'
    if rotate:
        for index in range(MAX_FILES - 1, 0, -1):
            previous = directory/f'capture-{index - 1}.jsonl'
            if previous.exists():
                os.replace(previous, directory/f'capture-{index}.jsonl')
    target = directory/'capture-0.jsonl'
    temporary = directory/'.capture-new.jsonl'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, target)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def trace_stats(instance):
    """Bounded tracefs counters make missing ring data visible in evidence."""
    stats = {}
    for path in sorted((instance/'per_cpu').glob('cpu*/stats'))[:64]:
        try:
            values = {}
            for line in read_small(path, 2048).splitlines():
                match = re.fullmatch(r'(entries|overrun|commit overrun|dropped events|read events):\s*(\d+)', line.strip())
                if match:
                    values[match[1]] = int(match[2])
            stats[path.parent.name] = values
        except (OSError, ValueError):
            continue
    return stats


def monitor(directory=DIRECTORY):
    os.umask(0o077)
    private_directory(directory)
    lock = (directory/'recorder.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    trace_root = Path('/sys/kernel/tracing')
    if not (trace_root/'events/xhci-hcd/xhci_handle_event/format').exists():
        trace_root = Path('/sys/kernel/debug/tracing')
    trace = TraceSession(trace_root)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    boot_id = read_small('/proc/sys/kernel/random/boot_id')
    fd = os.open(trace.instance/'trace_pipe', os.O_RDONLY | os.O_NONBLOCK)
    window = Window()
    current, active = None, None
    next_identity, last_trigger, bound_since = 0.0, float('-inf'), float('inf')
    pending = b''
    dropped = 0
    try:
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_identity:
                try:
                    found = identify()
                except (OSError, ValueError):
                    found = None
                if found != current:
                    if active:
                        active['header']['ended_due_to_generation_change'] = True
                        active['header']['trace_stats_end'] = trace_stats(trace.instance)
                        save_capture(directory, active['header'], active['rows'], rotate=False)
                        active = None
                    trace.disable()
                    window.clear()
                    pending = b''
                    current = found
                    if current:
                        bound_since = time.monotonic()
                        trace.bind(current)
                next_identity = now + 0.25
            if active and now >= active['until']:
                active['header']['post_window_complete'] = True
                active['header']['parser_dropped_lines'] = dropped
                active['header']['trace_stats_end'] = trace_stats(trace.instance)
                save_capture(directory, active['header'], active['rows'], rotate=False)
                active = None
            if not select.select([fd], [], [], 0.1)[0]:
                continue
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            lines = (pending + chunk).split(b'\n')
            pending = lines.pop()
            if len(pending) > 1024:
                pending = b''
                dropped += 1
            for line in lines:
                if current is None:
                    continue
                row = parse_metadata(line.decode('ascii', 'replace'))
                if row is None:
                    dropped += 1
                    continue
                if row['monotonic'] < bound_since:
                    dropped += 1
                    continue
                if not window.add(row, current):
                    continue
                if active:
                    if len(active['rows']) < MAX_RING_ROWS * 2:
                        active['rows'].append(row)
                    else:
                        active['header']['post_rows_dropped'] += 1
                if (row.get('completion_code') == 4 and row.get('trb_type') == 'Transfer Event'
                        and active is None and now - last_trigger >= COOLDOWN_SECONDS):
                    try:
                        valid = identify() == current
                    except (OSError, ValueError):
                        valid = False
                    if not valid:
                        continue
                    last_trigger = now
                    try:
                        device_snapshot = snapshot(current)
                    except (OSError, ValueError):
                        device_snapshot = {'identity_valid': False, 'snapshot_unavailable': True}
                    active = {'header': {'kind': 'first_observed_transaction_error_context',
                        'boot_id': boot_id, 'realtime': time.time(), 'monotonic': now,
                        'trace_clock': 'mono', 'identity': asdict(current),
                        'post_rows_dropped': 0,
                        'identity_sampling_interval_seconds': 0.25,
                        'pre_seconds_requested': PRE_SECONDS, 'ring_evictions': window.evicted,
                        'limitations': LIMITATION, 'snapshot': device_snapshot,
                        'trace_stats_start': trace_stats(trace.instance)},
                        'until': now + POST_SECONDS, 'rows': [item[0] for item in window.rows]}
                    # Preserve the first snapshot even if power/reboot interrupts
                    # the short post-window. Later completion updates this file.
                    active['header']['post_window_complete'] = False
                    save_capture(directory, active['header'], active['rows'])
    finally:
        trace.disable()
        os.close(fd)
        if active:
            active['header']['ended_on_service_stop'] = True
            active['header']['trace_stats_end'] = trace_stats(trace.instance)
            save_capture(directory, active['header'], active['rows'], rotate=False)
        lock.close()


def service_unit():
    return f'''[Unit]
Description=UAV passive bounded D-Link USB metadata context
After=systemd-udevd.service
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I {INSTALLED} monitor
Restart=on-failure
RestartSec=10
TimeoutStopSec=8
UMask=0077
NoNewPrivileges=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/sys/kernel/tracing/instances -/sys/kernel/debug/tracing/instances
StateDirectory=laiting-uav-usb-context
StateDirectoryMode=0700
PrivateTmp=true
MemoryMax=96M
CPUQuota=15%

[Install]
WantedBy=multi-user.target
'''


def install():
    """Explicit CLI action only; parent reviews and chooses deployment timing."""
    validate_formats(Path('/sys/kernel/tracing'))
    INSTALLED.parent.mkdir(parents=True, exist_ok=True)
    INSTALLED.write_bytes(Path(__file__).read_bytes())
    INSTALLED.chmod(0o644)
    unit = Path('/etc/systemd/system')/UNIT
    unit.write_text(service_unit())
    unit.chmod(0o644)
    subprocess.run(['systemd-analyze', 'verify', str(unit)], check=True)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', UNIT], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'monitor'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('root service authentication is required')
    (install if args.action == 'install' else monitor)()


if __name__ == '__main__':
    main()
