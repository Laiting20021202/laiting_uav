from scripts.network_recorder import select_record


def test_usb_evidence_keeps_boot_and_monotonic_time_after_clock_jump():
    common = {"_TRANSPORT": "kernel", "_BOOT_ID": "boot-a", "_COMM": "kernel"}
    records = [
        {**common, "MESSAGE": "usb 1-2.1: USB disconnect, device number 4",
         "__REALTIME_TIMESTAMP": "9000", "__MONOTONIC_TIMESTAMP": "100"},
        {**common, "MESSAGE": "usb 1-2.1: can't read configurations, error -71",
         "__REALTIME_TIMESTAMP": "5000", "__MONOTONIC_TIMESTAMP": "200"},
    ]
    saved = [select_record(record) for record in records]
    assert [r["__MONOTONIC_TIMESTAMP"] for r in saved] == ["100", "200"]
    assert all(r["_BOOT_ID"] == "boot-a" for r in saved)
    assert "error -71" in saved[1]["MESSAGE"]


def test_recorder_limits_saved_sources_and_fields():
    assert select_record({"_TRANSPORT": "kernel", "MESSAGE": "unrelated event"}) is None
    assert select_record({"_SYSTEMD_UNIT": "other.service", "MESSAGE": "secret"}) is None
    assert select_record({"_TRANSPORT": "kernel", "MESSAGE": [0, 1]}) is None
    assert select_record({"_SYSTEMD_USER_UNIT": "uav-cloudflared.service",
                          "MESSAGE": "unrelated request"}) is None
    for field, unit, message in [
        ("_SYSTEMD_UNIT", "NetworkManager.service", "device removed"),
        ("_SYSTEMD_UNIT", "systemd-timesyncd.service", "Initial synchronization"),
        ("_SYSTEMD_UNIT", "systemd-logind.service", "Power key pressed"),
        ("_SYSTEMD_UNIT", "systemd-resolved.service", "Using degraded feature set TCP"),
        ("_SYSTEMD_UNIT", "uav-modem-recovery.service", "Resetting stalled modem only: 1-2.1"),
        ("_SYSTEMD_USER_UNIT", "uav-network-health.service", "Network upstream evidence: {}"),
        ("_SYSTEMD_USER_UNIT", "uav-cloudflared.service", "Public SSH transport: ready"),
        ("_SYSTEMD_USER_UNIT", "uav-v2.service", "WARNING uav.telemetry: Flight-controller heartbeat expired"),
        ("_SYSTEMD_USER_UNIT", "uav-v2.service", "INFO uav.telemetry: Flight controller connected: session 6"),
    ]:
        assert select_record({field: unit, "MESSAGE": message, "UNNEEDED": "value"}) == {
            field: unit, "MESSAGE": message,
        }


def test_application_transport_events_do_not_capture_login_or_requests():
    assert select_record({"_SYSTEMD_USER_UNIT": "uav-v2.service",
                          "MESSAGE": "Login successful for operator"}) is None
    assert select_record({"_SYSTEMD_USER_UNIT": "uav-v2.service",
                          "MESSAGE": "Application watchdog stalled"}) is not None
