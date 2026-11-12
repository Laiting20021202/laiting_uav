"""Protect unrelated VPN configuration and resolver targets during DNS repair."""
from scripts.fix_host_dns import atomic_write, without_wireguard_dns


def test_removing_interface_dns_preserves_peer_keys_routes_and_comments():
    source = (
        "# DNS = comment\n[Interface]\nPrivateKey = synthetic-private-key\n"
        "Address = 10.0.0.8/24\nDNS = 8.8.8.8 # old DNS\n"
        "PostUp = echo DNS=unchanged\n\n[Peer]\nPublicKey = synthetic-public-key\n"
        "AllowedIPs = 10.0.0.0/24\nEndpoint = vpn.example:51820\n"
    )
    expected = source.replace(
        "DNS = 8.8.8.8 # old DNS\n",
        "# Host DNS is managed by NetworkManager and systemd-resolved.\n",
    )
    assert without_wireguard_dns(source) == expected
    assert without_wireguard_dns(expected) == expected


def test_atomic_write_replaces_symlink_without_overwriting_its_target(tmp_path):
    original = tmp_path / "original"
    original.write_text("existing resolver\n")
    link = tmp_path / "replacement"
    link.symlink_to(original)
    atomic_write(link, "new configuration\n", 0o600)
    assert original.read_text() == "existing resolver\n"
    assert not link.is_symlink()
    assert link.read_text() == "new configuration\n"
    assert link.stat().st_mode & 0o777 == 0o600
