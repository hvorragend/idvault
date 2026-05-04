"""
IDVScope – SSL/TLS-Hilfsmodul
============================
Aktiviert HTTPS-Verbindungen über Zertifikate.

Bootstrap-Keys in ``config.json`` (Top-Level):
  IDV_HTTPS          1 = HTTPS aktivieren                    (Standard: 0)
  IDV_SSL_CERT       Pfad zum Zertifikat (PEM)
                     (Standard: instance/certs/cert.pem)
  IDV_SSL_KEY        Pfad zum privaten Schlüssel (PEM)
                     (Standard: instance/certs/key.pem)
  IDV_SSL_AUTOGEN    1 = Selbstsigniertes Zertifikat erzeugen,
                     falls die Dateien fehlen                (Standard: 1)

Das selbstsignierte Zertifikat ist 10 Jahre gültig und enthält
Subject-Alternative-Names für den Hostnamen, ``localhost``, ``127.0.0.1``
und ``::1``. Für den produktiven Einsatz sollte ein von einer
(internen) CA signiertes Zertifikat verwendet werden – einfach die
gewünschten PEM-Dateien nach ``instance/certs/`` legen oder die
Bootstrap-Keys ``IDV_SSL_CERT`` / ``IDV_SSL_KEY`` in ``config.json`` setzen.
"""

import datetime
import ipaddress
import os
import socket
import ssl
from typing import Optional

from webapp import config_store


def default_cert_dir(instance_path: str) -> str:
    return os.path.join(instance_path, "certs")


def default_cert_path(instance_path: str) -> str:
    return os.path.join(default_cert_dir(instance_path), "cert.pem")


def default_key_path(instance_path: str) -> str:
    return os.path.join(default_cert_dir(instance_path), "key.pem")


def https_enabled() -> bool:
    """True, wenn die Anwendung im HTTPS-Modus starten soll."""
    return config_store.get_bool("IDV_HTTPS", False)


def generate_self_signed(cert_path: str, key_path: str,
                         hostname: Optional[str] = None,
                         days_valid: int = 3650) -> None:
    """Erzeugt ein selbstsigniertes RSA-2048-Zertifikat.

    Die SAN-Liste enthält den lokalen Hostnamen sowie ``localhost``,
    ``127.0.0.1`` und ``::1``, damit der Zugriff über verschiedene
    Adressen ohne Zertifikatsfehler im Browser funktioniert.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    hostname = hostname or socket.gethostname() or "localhost"

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "IDVScope"),
    ])

    san_entries = [
        x509.DNSName(hostname),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]

    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days_valid))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .sign(key, hashes.SHA256())
    )

    os.makedirs(os.path.dirname(cert_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))

    # Unter Unix: Private-Key nur für den Eigentümer lesbar machen.
    # Unter Windows ist chmod ohne Wirkung — das schadet aber nicht.
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass


def resolve_ssl_paths(instance_path: str) -> Optional[tuple]:
    """Ermittelt (cert_path, key_path) und legt sie bei Bedarf an.

    Gibt ``None`` zurück, wenn HTTPS nicht aktiviert ist. Bei fehlenden
    Dateien + aktivem ``IDV_SSL_AUTOGEN`` wird ein selbstsigniertes
    Zertifikat erzeugt.
    """
    if not https_enabled():
        return None

    cert_path = config_store.get_str("IDV_SSL_CERT", "") or default_cert_path(instance_path)
    key_path  = config_store.get_str("IDV_SSL_KEY",  "") or default_key_path(instance_path)

    if not (os.path.isfile(cert_path) and os.path.isfile(key_path)):
        if not config_store.get_bool("IDV_SSL_AUTOGEN", True):
            raise FileNotFoundError(
                f"HTTPS aktiviert, aber Zertifikatsdateien fehlen: "
                f"{cert_path} / {key_path}. Entweder die Dateien bereitstellen "
                f"oder IDV_SSL_AUTOGEN=1 in config.json setzen, um ein "
                f"selbstsigniertes Zertifikat zu erzeugen."
            )
        print(f"  [SSL] Erzeuge selbstsigniertes Zertifikat:")
        print(f"        Zertifikat: {cert_path}")
        print(f"        Schlüssel:  {key_path}")
        generate_self_signed(cert_path, key_path)

    return cert_path, key_path


def build_ssl_context(instance_path: str) -> Optional[ssl.SSLContext]:
    """Liefert einen ``ssl.SSLContext`` für ``app.run(ssl_context=…)``.

    Gibt ``None`` zurück, wenn HTTPS nicht aktiviert ist (``IDV_HTTPS``
    nicht gesetzt). Fehlen die Zertifikatsdateien und ``IDV_SSL_AUTOGEN``
    ist aktiv (Standard), wird ein selbstsigniertes Zertifikat erzeugt.
    """
    paths = resolve_ssl_paths(instance_path)
    if paths is None:
        return None
    cert_path, key_path = paths

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # #406-2: Cipher-/Protokoll-Hardening. Auf aelteren Windows-Builds kann
    # PROTOCOL_TLS_SERVER ohne expliziten Cipher-Filter schwache Suiten
    # einbinden (RC4, 3DES, CBC-only). Wir erzwingen TLS >= 1.2 und einen
    # konservativen ECDHE/AES-GCM/CHACHA20-Filter; das deckt alle modernen
    # Browser und reduziert die Angriffsoberflaeche.
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except (AttributeError, ValueError):
        # Aeltere Python-Versionen (< 3.7) – kein Showstopper, der
        # PROTOCOL_TLS_SERVER selbst schliesst SSLv2/3 bereits aus.
        pass
    try:
        ctx.set_ciphers(
            "ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:!aNULL:!MD5:!DSS"
        )
    except ssl.SSLError:
        # Wenn die Plattform die Liste nicht parsen kann (uralte OpenSSL),
        # bleiben wir bei den Defaults statt mit RuntimeError abzubrechen.
        pass
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return ctx
