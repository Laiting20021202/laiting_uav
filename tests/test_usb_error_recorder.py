from pathlib import Path

import pytest

from scripts.usb_error_recorder import ERROR_FILTER, INSTANCE, configure_trace, identity_snapshot


def test_error_trace_is_isolated_and_does_not_enable_all_urb_events(tmp_path):
    # An isolated tracefs layout verifies no shared tracing or USB reset control is used.
    event = tmp_path/'events/xhci-hcd/xhci_handle_event'
    event.mkdir(parents=True)
    (event/'format').write_text('format')
    (event/'enable').write_text('0')
    instance = tmp_path/'instances'/INSTANCE
    target = instance/'events/xhci-hcd/xhci_handle_event'
    target.mkdir(parents=True)
    for name in ('enable', 'filter'):
        (target/name).write_text('')
    for name in ('buffer_size_kb', 'trace_clock', 'trace'):
        (instance/name).write_text('')
    assert configure_trace(tmp_path) == instance
    assert (event/'enable').read_text() == '0'
    assert (target/'filter').read_text() == ERROR_FILTER
    assert (target/'enable').read_text() == '1'


def test_missing_tracepoint_is_rejected_before_enabling_anything(tmp_path):
    with pytest.raises(RuntimeError, match='tracepoint'):
        configure_trace(tmp_path)
    assert not (tmp_path/'instances'/INSTANCE).exists()


def test_slot_identity_distinguishes_modem_and_flight_controller(tmp_path):
    debug = tmp_path/'debug'
    usb = tmp_path/'usb'
    for slot, name, vendor, product in (
            ('04', '1-2.1', '1286', '4e31'), ('05', '1-2.4', '2a58', 'a002')):
        parent = debug/'3610000.usb/devices'/slot
        parent.mkdir(parents=True)
        (parent/'name').write_text(name)
        device = usb/name
        device.mkdir(parents=True)
        (device/'idVendor').write_text(vendor)
        (device/'idProduct').write_text(product)
    state = identity_snapshot(debug, usb)
    assert state['04']['device'] == '1-2.1'
    assert state['05']['idVendor'] == '2a58'
    assert state['05']['controller'] == '3610000.usb'
