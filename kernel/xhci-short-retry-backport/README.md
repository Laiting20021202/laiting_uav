# NVIDIA 5.15.185 xHCI short-transfer retry backport

This is a one-line backport of upstream Linux commit
[e765ab012f73717238c95ab9c34bfc3c767fa48c](https://github.com/torvalds/linux/commit/e765ab012f73717238c95ab9c34bfc3c767fa48c),
`usb: xhci: Improve Soft Retries after short transfers` (2026-06-03).
The original patch is retained as `e765ab012f73.patch`. The NVIDIA-context
patch is `nvidia-5.15.185.patch`, which applies specifically to
`process_bulk_intr_td()` and preserves NVIDIA's debug output.

Successful `COMP_SHORT_PACKET` completion must reset `ep->err_count`, just
like `COMP_SUCCESS`. Otherwise successful short receives can leave the
software retry counter accumulated across transfers. The observed modem
uses this path: all 2,529 successful bulk-IN completions in the passive
ground sample were short packets. This establishes relevance to retry
handling; it does not establish the source of the initial transaction
errors or guarantee that the modem's stalls are eliminated.

The modified Linux source remains under its upstream GPL-2.0 license.
No controller firmware, device tree, module ABI, or unrelated source is
changed. xHCI is built into this Jetson's kernel, so loading the fix needs
a new kernel Image and a reboot.

`build.py` checks the source archive, base/patched source hashes, running
configuration, exact kernel release, and every built-in symbol CRC. The
2026-10-06 build matched all 13,492 installed built-in exports; installed
modules were retained without CRC forcing. Build evidence and the Image
are in `data/maintenance/20261006-repeat-outage-132751/kernel-build/`.

`scripts/xhci_kernel_stage.py` installs a separate `/boot/Image.uav-xhci-retry`
and boot menu entry, retaining stock Image, initrd, entry, and private
backups. Staging does not change the default boot entry. Activation changes
the default but never initiates a reboot:

```sh
sudo python3 scripts/xhci_kernel_stage.py activate --validation data/maintenance/20261006-repeat-outage-132751/kernel-build/validation.json
```

To choose the original kernel for the next boot:

```sh
sudo python3 scripts/xhci_kernel_stage.py rollback
```

The separate `uav-xhci-postboot-check` observer records kernel identity and
ten minutes of USB identities, error counters, and probes bound to D-Link
under `/var/lib/laiting-uav-xhci-validation/`. It never resets devices or
changes routes. Its kernel identity result is not a repair verdict; verify
modem generations, recovery actions, USB error records, and flight data
before assessing reliability.
