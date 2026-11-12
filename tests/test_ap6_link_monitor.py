"""No lsusb-only acceptance, stale boot evidence, or hidden mid-window failures."""
import pytest
from scripts.ap6_link_monitor import BY_ID, LinkEvidence, REQUIRED

BOOT = "a68f7990046d413f81939c4c99a0d4d3"
PID = 1731


def event(at, message, cursor=None, pid=PID, boot=BOOT):
    return {"_PID": str(pid), "_BOOT_ID": boot,
            "__MONOTONIC_TIMESTAMP": str(int(at * 1e6)),
            "__CURSOR": cursor or str(at), "MESSAGE": "INFO uav.telemetry: " + message}


def connect(state, at=0, component=1, session=4):
    state.consume(event(at, f"Flight controller connected: {BY_ID} system=1 component={component}, session {session}"), {BY_ID})


def integrity(state, at, complete=True, session=4, ages=None, **kwargs):
    ages = ages if ages is not None else {name: .1 for name in ("HEARTBEAT", *REQUIRED)}
    text = f"Flight telemetry integrity: session={session} complete={complete} missing={'none' if complete else 'GPS_RAW_INT'} ages_s={ages}"
    state.consume(event(at, text, **kwargs), {BY_ID})


def evaluate(state, at, device=True, reader=True):
    return state.evaluate(at, device, reader)


def ready_state():
    state = LinkEvidence(BOOT, 0)
    state.set_context(PID, "1:18", 0)
    connect(state)
    for at in (1, 31, 61):
        integrity(state, at)
        report = evaluate(state, at)
    assert report["accepted"]
    return state


def test_enumerated_device_and_reader_are_insufficient_without_telemetry():
    state = LinkEvidence(BOOT, 0)
    state.set_context(PID, "1:18", 0)
    assert evaluate(state, 100)["phase"] == "waiting_verified_source"
    connect(state)
    assert not evaluate(state, 100)["accepted"]


def test_accepted_window_needs_multiple_fresh_records():
    state = ready_state()
    assert evaluate(state, 62)["fresh_integrity_records"] == 3
    assert evaluate(state, 102)["phase"] == "integrity_record_expired"
    assert not evaluate(state, 102)["accepted"]


def test_same_record_cannot_prove_a_continuous_window():
    state = LinkEvidence(BOOT, 0, healthy_seconds=10)
    state.set_context(PID, "1:18", 0)
    connect(state)
    integrity(state, 1)
    for at in (1, 6, 11, 16):
        report = evaluate(state, at)
        assert report["fresh_integrity_records"] == 1 and not report["accepted"]


def test_transient_incomplete_event_in_one_batch_resets_window():
    state = ready_state()
    integrity(state, 62, complete=False)
    integrity(state, 63)
    report = evaluate(state, 63)
    assert not report["accepted"] and report["observed_healthy_s"] == 0
    assert report["fault_events_since_start"] == 1


def test_unavailable_then_new_session_cannot_reuse_acceptance():
    state = ready_state()
    state.consume(event(62, "Flight controller unavailable: TimeoutError: heartbeat expired"), {BY_ID})
    connect(state, 63, session=5)
    integrity(state, 64, session=5)
    report = evaluate(state, 64)
    assert not report["accepted"] and report["fresh_integrity_records"] == 1


@pytest.mark.parametrize("pid,generation", [(PID, "1:19"), (2000, "1:18")])
def test_generation_or_reader_change_discards_old_journal_health(pid, generation):
    state = ready_state()
    state.set_context(pid, generation, 70)
    connect(state, 0)  # Older connection metadata can identify only the source.
    integrity(state, 61, pid=pid)
    assert not evaluate(state, 71)["accepted"] and state.integrity is None


@pytest.mark.parametrize("name,age", [("HEARTBEAT", 8), ("GPS_RAW_INT", 3),
                                      ("ATTITUDE", -1), ("SYS_STATUS", None),
                                      ("VFR_HUD", True)])
def test_complete_label_cannot_override_bad_freshness(name, age):
    state = ready_state()
    ages = {key: .1 for key in ("HEARTBEAT", *REQUIRED)}
    ages[name] = age
    integrity(state, 62, ages=ages)
    report = evaluate(state, 62)
    assert not report["accepted"] and report["phase"] == "invalid_or_stale_message_ages"


def test_other_component_boot_pid_or_session_is_not_accepted():
    state = LinkEvidence(BOOT, 0)
    state.set_context(PID, "1:18", 0)
    connect(state, component=235)
    integrity(state, 1)
    assert evaluate(state, 1)["phase"] == "waiting_verified_source"
    connect(state, 2)
    integrity(state, 3, session=5)
    assert evaluate(state, 3)["phase"] == "integrity_session_mismatch"
    state.integrity = None
    integrity(state, 4, boot="0" * 32)
    integrity(state, 5, pid=9999)
    assert state.integrity is None


@pytest.mark.parametrize("device,reader", [(False, True), (True, False)])
def test_loss_of_device_or_reader_immediately_revokes_readiness(device, reader):
    state = ready_state()
    assert not evaluate(state, 62, device, reader)["accepted"]
