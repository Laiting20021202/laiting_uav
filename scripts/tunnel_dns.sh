#!/bin/sh
# A rootless mount namespace gives cloudflared AND its probes reliable DNS.
# This does not require host root or change /etc/resolv.conf on the host.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec /usr/bin/unshare --user --map-root-user --mount --propagation private \
    /bin/sh -eu -c '
        /usr/bin/mount --bind "$1" /etc/resolv.conf
        shift
        exec "$@"
    ' uav-tunnel-dns "$root/config/tunnel-resolv.conf" "$@"
