"""
Self-signed HTTPS certificate for the call server.

Phone browsers (and desktop ones too) refuse microphone access on a plain
http:// page unless the origin is exactly "localhost" -- this is why
call mode worked visually but never captured any audio: getUserMedia was
silently failing on the LAN-IP http:// page. A self-signed cert is enough
to fix this: what matters for the browser's "secure context" check is the
https:// scheme, not whether the certificate is trusted. You'll see a
"connection not private" warning the first time -- tapping through it
("Advanced" -> "Proceed") gets a fully working secure context afterwards.

Regenerated automatically whenever the LAN IP doesn't match what's already
cached (e.g. after a different WiFi network / DHCP reassignment).
"""
import datetime
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from shared import ROOT

CERT_DIR = ROOT / ".certs"
KEY_PATH = CERT_DIR / "key.pem"
CERT_PATH = CERT_DIR / "cert.pem"


def local_ipv4_interfaces() -> list[tuple[str, str]]:
    """[(ip, adapter_name)] for every usable IPv4 address on this machine.

    Uses psutil rather than socket.getaddrinfo(gethostname()) because that
    proved unreliable on Windows -- it silently omitted the Wi-Fi adapter
    (the one the phone was actually on) while listing three useless ones.
    Adapter names are kept so the startup banner can say WHICH network
    each address belongs to, instead of making you guess.
    """
    found: list[tuple[str, str]] = []
    try:
        import psutil

        for name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET and addr.address:
                    found.append((addr.address, name))
    except Exception:
        pass

    # Fallbacks, in case psutil is unavailable for some reason.
    if not found:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                found.append((info[4][0], "unknown"))
        except Exception:
            pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        route_ip = s.getsockname()[0]
        if not any(ip == route_ip for ip, _ in found):
            found.append((route_ip, "default route"))
    except OSError:
        pass
    finally:
        s.close()

    usable = [
        (ip, name)
        for ip, name in found
        # 169.254.x is APIPA/link-local (adapter failed to get a real
        # address); loopback is obviously not reachable from the phone.
        if ip != "127.0.0.1" and not ip.startswith("169.254.")
    ]
    # Most-likely-useful first: real Wi-Fi, then the Windows Mobile Hotspot
    # range, then everything else (VirtualBox's 192.168.56.x sinks to the
    # bottom since it can never reach a phone).
    def rank(entry: tuple[str, str]) -> tuple[int, str]:
        ip, name = entry
        lowered = name.lower()
        if ip.startswith("192.168.56."):
            return (3, ip)
        if "wi-fi" in lowered or "wifi" in lowered or "wlan" in lowered:
            return (0, ip)
        if ip.startswith("192.168.137."):
            return (1, ip)
        return (2, ip)

    return sorted(set(usable), key=rank)


def all_local_ipv4() -> list[str]:
    return [ip for ip, _ in local_ipv4_interfaces()]


def describe_interface(ip: str, name: str) -> str:
    """Human hint about what a given address actually is."""
    if ip.startswith("192.168.137."):
        return f"{name} - Windows Mobile Hotspot (use if your phone is on the PC's hotspot)"
    if ip.startswith("192.168.56."):
        return f"{name} - VirtualBox virtual adapter (a phone can never reach this)"
    return name


def _cert_covers(ips: list[str]) -> bool:
    if not CERT_PATH.exists():
        return False
    try:
        cert = x509.load_pem_x509_certificate(CERT_PATH.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        covered = {str(v) for v in san.value.get_values_for_type(x509.IPAddress)}
        return all(ip in covered for ip in ips)
    except Exception:
        return False


def ensure_certificate(lan_ip: str) -> tuple[str, str]:
    """Returns (keyfile_path, certfile_path), generating a fresh
    self-signed cert covering localhost plus every local IPv4 address --
    regenerated automatically whenever the machine picks up an address the
    cached cert doesn't already cover (new WiFi network, hotspot, etc.)."""
    wanted = sorted(set(all_local_ipv4()) | {lan_ip})
    if _cert_covers(wanted):
        return str(KEY_PATH), str(CERT_PATH)

    CERT_DIR.mkdir(exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "aria-call-server")])
    san_entries = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    for ip in wanted:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            continue
    san = x509.SubjectAlternativeName(san_entries)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650)
        )
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )

    KEY_PATH.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(KEY_PATH), str(CERT_PATH)
