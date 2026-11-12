# D-Link USB networking backport for the current Jetson

Base: NVIDIA Jetson Linux 36.5.0 `usbnet.c`, built only for `5.15.185-tegra`.
The source retains its GPL license. `provenance.json` records source hashes and
four official Linux stable commits also present in NVIDIA 36.5.2:

- Defer carrier-on handling out of the interrupt path to prevent an RCU stall;
  include the follow-up correction that enables carrier before testing it.
- Cancel active `kevent` work after netdev unregister during disconnect.
- Disable bottom halves while returning packets from the paused RX queue.

Only the new event bit 14 is added; `struct usbnet` and all exported interfaces
remain unchanged. The installer checks every exported and imported symbol CRC
against the installed kernel and preserves a stock module backup. DKMS selects
the backport on this kernel; the kernel restriction avoids overriding later
official kernels that already contain these fixes.

```bash
sudo python3 scripts/usbnet_backport.py install
sudo python3 scripts/usbnet_backport.py load
cat /sys/module/usbnet/version
```

Loading briefly disconnects the verified D-Link and reactivates its saved 4G
profile. It refuses to reload if any other CDC Ethernet device or usbnet driver
is active. Application, flight-controller serial, Ethernet camera and the USB
host controller are retained.

```bash
sudo python3 scripts/usbnet_backport.py rollback
```

These are real driver fixes, but they do not prove the recorded flight outage
was caused by any one of these bugs. The XUSB firmware and `xhci-tegra.c` are
identical between the inspected 36.5.0 and 36.5.2 releases; they are not replaced
by this backport. Flight reliability still requires validation under the actual
conditions that triggered the outage.
