"""Safety tests use fake files only; no kernel/device/network operations."""
import json
from pathlib import Path

import pytest

from scripts import usb_context_recorder as recorder


def fake_device(tmp_path, name='1-2.2', slot=4, serial=recorder.IDENTITY[2], devnum=9):
    usb, debug = tmp_path/'usb', tmp_path/'debug'
    device = usb/name
    device.mkdir(parents=True, exist_ok=True)
    values = dict(zip(('idVendor', 'idProduct', 'serial'), (*recorder.IDENTITY[:2], serial)))
    values.update(bDeviceClass='00', busnum='1', devnum=str(devnum))
    for key, value in values.items():
        (device/key).write_text(value)
    slot_dir = debug/'3610000.usb/devices'/f'{slot:02d}'
    slot_dir.mkdir(parents=True, exist_ok=True)
    (slot_dir/'name').write_text(name)
    return usb, debug, device, slot_dir


def fake_trace(tmp_path):
    root = tmp_path/'trace'
    instance = root/'instances'/recorder.INSTANCE
    for name in recorder.FORMATS:
        event = root/'events/xhci-hcd'/name
        event.mkdir(parents=True)
        # Representative native format syntax, independent of private evidence.
        native = {'xhci_handle_event': 'u32 type; u32 field0; u32 field1; u32 field2; u32 field3;',
                  'xhci_handle_command': 'u32 type; u32 field0; u32 field1; u32 field2; u32 field3;',
                  'xhci_urb_enqueue': 'int slot_id; int length; int actual; int epnum; int dir_in;',
                  'xhci_handle_port_status': 'u32 portnum; u32 portsc;'}
        fields = native.get(name, 'u32 info; u32 info2; u64 deq; u32 tx_info;')
        (event/'format').write_text('\n'.join('field:' + field.strip() + ';' for field in fields.split(';') if field.strip()))
        (event/'enable').write_text('0')
        target = instance/'events/xhci-hcd'/name
        target.mkdir(parents=True)
        (target/'enable').write_text('0')
        (target/'filter').write_text('0')
    for name in ('tracing_on', 'current_tracer', 'buffer_size_kb', 'trace_clock', 'trace'):
        (instance/name).write_text('')
    return root, instance


def line(name, body, timestamp='100.000000', cpu='000'):
    return f'worker-21 [{cpu}] d..h1.. {timestamp}: {name}: {body}'


def transfer(status='USB Transaction Error', slot=4, ep=7):
    return line('xhci_handle_event', f"EVENT: TRB 0000007ffff68da0 status '{status}' len 1544 slot {slot} ep {ep} type 'Transfer Event' flags e:C")


CONTEXT = ('State Running mult 1 max P. Streams 0 interval 125 us max ESIT payload 0 '
           'CErr 3 Type Bulk IN burst 0 maxp 512 deq 0000007ffff68da1 avg trb len 1544')


def test_identity_is_strict_and_tracks_changed_slot_and_generation(tmp_path):
    usb, debug, path, slot_dir = fake_device(tmp_path)
    first = recorder.identify(usb, debug)
    assert (first.slot, first.devnum) == (4, 9)
    slot_dir.rename(slot_dir.parent/'06')
    (path/'devnum').write_text('13')
    assert recorder.identify(usb, debug) != first
    assert recorder.identify(usb, debug).slot == 6


@pytest.mark.parametrize('key,value', [('serial', 'other'), ('idVendor', '2a58'),
                                      ('idProduct', 'a002'), ('bDeviceClass', '09')])
def test_identity_refuses_flight_controller_other_card_or_hub(tmp_path, key, value):
    usb, debug, path, _ = fake_device(tmp_path)
    (path/key).write_text(value)
    with pytest.raises(ValueError, match='identity'):
        recorder.identify(usb, debug)


def test_duplicate_modem_identity_and_multiple_controllers_fail_closed(tmp_path):
    usb, debug, _, _ = fake_device(tmp_path)
    fake_device(tmp_path, name='1-2.3', slot=6)
    with pytest.raises(ValueError, match='ambiguous'):
        recorder.identify(usb, debug)
    (debug/'other-controller/devices').mkdir(parents=True)
    with pytest.raises(ValueError, match='controller'):
        recorder.identify(usb, debug)


def test_generation_change_during_sampling_is_rejected(tmp_path, monkeypatch):
    usb, debug, _, _ = fake_device(tmp_path)
    original = recorder.read_small
    reads = 0

    def changing(path, limit=256):
        nonlocal reads
        value = original(path, limit)
        if Path(path).name == 'devnum':
            reads += 1
            return '9' if reads == 1 else '10'
        return value

    monkeypatch.setattr(recorder, 'read_small', changing)
    with pytest.raises(ValueError, match='generation'):
        recorder.identify(usb, debug)


def test_native_formats_and_trace_controls_are_isolated(tmp_path):
    root, instance = fake_trace(tmp_path)
    session = recorder.TraceSession(root)
    session.bind(recorder.Device('1-2.2', '3610000.usb', 6, 1, 9, recorder.IDENTITY[2]))
    assert (root/'events/xhci-hcd/xhci_handle_event/enable').read_text() == '0'
    assert (instance/'events/xhci-hcd/xhci_handle_event/filter').read_text() == 'field3 >= 100663296 && field3 < 117440512'
    assert (instance/'events/xhci-hcd/xhci_urb_enqueue/filter').read_text() == 'slot_id == 6'
    assert (instance/'current_tracer').read_text() == 'nop'
    assert (instance/'buffer_size_kb').read_text() == '256'
    session.disable()
    assert all((p/'enable').read_text() == '0' for p in session.targets.values())


def test_unsupported_trace_format_refused_before_creating_instance(tmp_path):
    root = tmp_path/'trace'
    path = root/'events/xhci-hcd/xhci_handle_event'
    path.mkdir(parents=True)
    (path/'format').write_text('name: xhci_handle_event\nfield:u32 wrong;')
    with pytest.raises(ValueError, match='fields'):
        recorder.TraceSession(root)
    assert not (root/'instances').exists()


def test_rejected_filter_disables_all_own_events(tmp_path, monkeypatch):
    root, instance = fake_trace(tmp_path)
    session = recorder.TraceSession(root)
    original = recorder.read_small

    def rejected(path, limit=256):
        if Path(path).name == 'filter':
            return 'parse_error: unsupported expression'
        return original(path, limit)

    monkeypatch.setattr(recorder, 'read_small', rejected)
    with pytest.raises(ValueError, match='rejected'):
        session.bind(recorder.Device('1-2.2', '3610000.usb', 4, 1, 9, recorder.IDENTITY[2]))
    assert all((p/'enable').read_text() == '0' for p in session.targets.values())
    assert (instance/'tracing_on').read_text() == '0'


def test_unrelated_enabled_private_event_is_not_overwritten(tmp_path):
    root, instance = fake_trace(tmp_path)
    unrelated = instance/'events/sched/sched_switch'
    unrelated.mkdir(parents=True)
    (unrelated/'enable').write_text('1')
    with pytest.raises(ValueError, match='unrelated'):
        recorder.TraceSession(root)
    assert (unrelated/'enable').read_text() == '1'


@pytest.mark.parametrize('status,code', [('Success', 1), ('Short Packet', 13), ('USB Transaction Error', 4)])
def test_success_short_and_transaction_error_metadata(status, code):
    row = recorder.parse_metadata(transfer(status))
    assert row['completion_code'] == code
    assert row['residual_length'] == 1544
    assert 'raw' not in row


def test_enqueue_preserves_lengths_but_never_claims_final_status():
    row = recorder.parse_metadata(line('xhci_urb_enqueue', 'ep3in-bulk: urb 000000001234abcd pipe 3221291392 slot 4 length 0/1544 sgs 0/0 stream 0 flags 00000200'))
    assert (row['requested_length'], row['actual'], row['endpoint']) == (1544, 0, 3)
    assert 'status' not in row
    assert recorder.parse_metadata(line('xhci_urb_giveback', 'ep3in-bulk: urb 1234')) is None


@pytest.mark.parametrize('value', [transfer() + ' secret-payload', 'x' * 2048,
                                   line('xhci_queue_trb', 'bRequest 42 secret-payload')])
def test_raw_data_unknown_events_and_oversized_lines_are_discarded(value):
    assert recorder.parse_metadata(value) is None


def test_context_is_only_indirect_command_time_correlation_and_expires():
    device = recorder.Device('1-2.2', '3610000.usb', 4, 1, 9, recorder.IDENTITY[2])
    window = recorder.Window()
    context = recorder.parse_metadata(line('xhci_handle_cmd_reset_ep', CONTEXT, '100.001000'))
    assert not window.add(context.copy(), device)
    command = recorder.parse_metadata(line('xhci_handle_command', 'CMD: Reset Endpoint Command: ctx 0000000000000000 slot 4 ep 7 flags t:C'))
    assert window.add(command, device)
    assert window.add(context.copy(), device)
    assert window.rows[-1][0]['attribution'].startswith('indirect')
    assert window.rows[-1][0]['correlated_endpoint'] == 7
    window.add(command, device)
    context['monotonic'] = 100.006
    assert not window.add(context, device)
    window.clear()
    assert not window.commands


def test_other_slot_is_discarded_and_memory_ring_is_bounded(monkeypatch):
    device = recorder.Device('1-2.2', '3610000.usb', 4, 1, 9, recorder.IDENTITY[2])
    window = recorder.Window()
    assert not window.add(recorder.parse_metadata(transfer(slot=5)), device)
    monkeypatch.setattr(recorder, 'MAX_RING_ROWS', 4)
    monkeypatch.setattr(recorder, 'MAX_RING_BYTES', 1500)
    for index in range(100):
        row = recorder.parse_metadata(transfer())
        row['monotonic'] = float(index)
        window.add(row, device)
    assert len(window.rows) <= 4
    assert window.size <= 1500
    assert window.evicted > 0


def test_snapshot_never_reads_trbs_and_discards_reenumeration(tmp_path, monkeypatch):
    usb, debug, path, slot_dir = fake_device(tmp_path)
    device = recorder.identify(usb, debug)
    interface = usb/'1-2.2:1.1'
    interface.mkdir()
    (interface/'bInterfaceClass').write_text('0a')
    for address in ('83', '0c'):
        endpoint = interface/('ep_' + address)
        endpoint.mkdir()
        (endpoint/'bEndpointAddress').write_text(address)
        (endpoint/'bmAttributes').write_text('02')
    (slot_dir/'ep-context').write_text('\n'.join('0000007ffff60000: ' + CONTEXT for _ in range(31)))
    for index in (0, 6, 23):
        ring = slot_dir/f'ep{index:02d}'
        ring.mkdir()
        (ring/'trbs').write_text('secret inline control-transfer payload')
        (ring/'enqueue').write_text('0000007ffff68da0')
        (ring/'dequeue').write_text('0000007ffff68dc0')
        (ring/'cycle').write_text('1')
    result = recorder.snapshot(device, usb, debug)
    assert result['identity_valid']
    assert len(result['endpoint_contexts']) == 3
    assert 'secret' not in json.dumps(result)
    original = recorder.identify
    calls = 0

    def changing(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            (path/'devnum').write_text('15')
        return original(*args)

    monkeypatch.setattr(recorder, 'identify', changing)
    assert recorder.snapshot(device, usb, debug) == {
        'identity_valid': False, 'discarded_due_to_generation_change': True}


def test_evidence_is_private_rate_independent_fixed_disk_bound(tmp_path, monkeypatch):
    directory = tmp_path/'evidence'
    recorder.private_directory(directory)
    monkeypatch.setattr(recorder, 'MAX_CAPTURE_BYTES', 4096)
    for number in range(10):
        recorder.save_capture(directory, {'capture': number, 'limitations': recorder.LIMITATION},
                              [{'number': index, 'state': 'Running'} for index in range(1000)])
    files = sorted(directory.glob('capture-*.jsonl'))
    assert len(files) == recorder.MAX_FILES
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_size <= 4096 and p.stat().st_mode & 0o777 == 0o600 for p in files)
    assert json.loads(files[0].read_text().splitlines()[-1])['truncated']
    assert json.loads(files[0].read_text().splitlines()[0])['capture'] == 9


def test_post_window_rewrite_preserves_initial_capture_if_atomic_replace_fails(tmp_path, monkeypatch):
    directory = tmp_path/'evidence'
    recorder.private_directory(directory)
    recorder.save_capture(directory, {'post_window_complete': False}, [{'status': 'first'}])
    original = (directory/'capture-0.jsonl').read_bytes()
    replace = recorder.os.replace

    def fail(source, target):
        if Path(source).name == '.capture-new.jsonl':
            raise OSError('simulated interruption')
        return replace(source, target)

    monkeypatch.setattr(recorder.os, 'replace', fail)
    with pytest.raises(OSError):
        recorder.save_capture(directory, {'post_window_complete': True}, [{'status': 'post'}], rotate=False)
    assert (directory/'capture-0.jsonl').read_bytes() == original


def test_slot_filter_works_at_unsigned_last_slot_without_wrapped_upper_bound():
    assert recorder.slot_filter(4) == 'field3 >= 67108864 && field3 < 83886080'
    assert recorder.slot_filter(255) == 'field3 >= 4278190080'


def test_native_none_filter_readback_is_accepted(tmp_path, monkeypatch):
    root, _ = fake_trace(tmp_path)
    session = recorder.TraceSession(root)
    read = recorder.read_small

    def native(path, limit=256):
        value = read(path, limit)
        return 'none' if Path(path).name == 'filter' and value == '0' else value

    monkeypatch.setattr(recorder, 'read_small', native)
    session.bind(recorder.Device('1-2.2', '3610000.usb', 4, 1, 9, recorder.IDENTITY[2]))


def test_oversized_header_refused_without_unbounded_evidence_file(tmp_path, monkeypatch):
    directory = tmp_path/'evidence'
    recorder.private_directory(directory)
    monkeypatch.setattr(recorder, 'MAX_CAPTURE_BYTES', 4096)
    with pytest.raises(ValueError, match='budget'):
        recorder.save_capture(directory, {'oversized': 'x' * 8192}, [])
    assert not list(directory.iterdir())


def test_symlink_directory_refused(tmp_path):
    actual = tmp_path/'actual'
    actual.mkdir()
    link = tmp_path/'link'
    link.symlink_to(actual)
    with pytest.raises(ValueError, match='directory'):
        recorder.private_directory(link)


def test_unit_bounds_resources_and_only_permits_own_trace_instance_writes():
    text = recorder.service_unit()
    assert 'StateDirectoryMode=0700' in text
    assert 'MemoryMax=96M' in text
    assert 'CPUQuota=15%' in text
    assert 'ExecStart=/usr/bin/python3 -I' in text
    assert 'ReadWritePaths=/sys/kernel/tracing/instances' in text
    assert '/sys/bus/usb' not in text
    assert 'usb_error_recorder.py' not in text
