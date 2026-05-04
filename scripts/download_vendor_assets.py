"""
download_vendor_assets.py
=========================

Lädt die Frontend-Assets (Bootstrap, Bootstrap Icons, QuillJS) nach
``webapp/static/vendor/`` in der Ordnerstruktur, die die Templates erwarten.

Dieses Skript ist **kein** Bestandteil des normalen Build-Prozesses: die
Assets sind im Repository eingecheckt, damit die Anwendung auch ohne
Internet-Verbindung gebaut und betrieben werden kann. Das Skript ist
nur für den erstmaligen Bezug oder ein späteres Version-Upgrade gedacht.

Quellen: offizielle GitHub-Release-Archive bzw. das npm-Registry-Tarball
(für die pre-built ``dist/`` von QuillJS). Kein CDN.

Aufruf:
    python scripts/download_vendor_assets.py          # neu holen & überschreiben
    python scripts/download_vendor_assets.py --check  # nur prüfen, nichts ändern
"""
from __future__ import annotations

import argparse
import io
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "webapp" / "static" / "vendor"

BOOTSTRAP_VERSION = "5.3.3"
BOOTSTRAP_ICONS_VERSION = "1.11.3"
QUILL_VERSION = "1.3.7"

BOOTSTRAP_ZIP = (
    f"https://github.com/twbs/bootstrap/releases/download/"
    f"v{BOOTSTRAP_VERSION}/bootstrap-{BOOTSTRAP_VERSION}-dist.zip"
)
BOOTSTRAP_ICONS_ZIP = (
    f"https://github.com/twbs/icons/releases/download/"
    f"v{BOOTSTRAP_ICONS_VERSION}/bootstrap-icons-{BOOTSTRAP_ICONS_VERSION}.zip"
)
QUILL_TGZ = (
    f"https://registry.npmjs.org/quill/-/quill-{QUILL_VERSION}.tgz"
)

# (Quell-Pfad innerhalb des Archivs, Ziel unterhalb VENDOR)
ASSETS = [
    (BOOTSTRAP_ZIP, "zip", [
        (f"bootstrap-{BOOTSTRAP_VERSION}-dist/css/bootstrap.min.css",
         f"bootstrap-{BOOTSTRAP_VERSION}/bootstrap.min.css"),
        (f"bootstrap-{BOOTSTRAP_VERSION}-dist/js/bootstrap.bundle.min.js",
         f"bootstrap-{BOOTSTRAP_VERSION}/bootstrap.bundle.min.js"),
    ]),
    (BOOTSTRAP_ICONS_ZIP, "zip", [
        (f"bootstrap-icons-{BOOTSTRAP_ICONS_VERSION}/font/bootstrap-icons.min.css",
         f"bootstrap-icons-{BOOTSTRAP_ICONS_VERSION}/bootstrap-icons.min.css"),
        (f"bootstrap-icons-{BOOTSTRAP_ICONS_VERSION}/font/fonts/bootstrap-icons.woff",
         f"bootstrap-icons-{BOOTSTRAP_ICONS_VERSION}/fonts/bootstrap-icons.woff"),
        (f"bootstrap-icons-{BOOTSTRAP_ICONS_VERSION}/font/fonts/bootstrap-icons.woff2",
         f"bootstrap-icons-{BOOTSTRAP_ICONS_VERSION}/fonts/bootstrap-icons.woff2"),
    ]),
    (QUILL_TGZ, "tgz", [
        ("package/dist/quill.snow.css",
         f"quill-{QUILL_VERSION}/quill.snow.css"),
        ("package/dist/quill.min.js",
         f"quill-{QUILL_VERSION}/quill.min.js"),
    ]),
]


def fetch(url: str) -> bytes:
    print(f"  · {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "idvscope-asset-fetch"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def extract_from_zip(blob: bytes, members: list[tuple[str, str]]) -> None:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for src, dst in members:
            target = VENDOR / dst
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(src) as fp, open(target, "wb") as out:
                shutil.copyfileobj(fp, out)
            print(f"    → {target.relative_to(ROOT)}")


def extract_from_tgz(blob: bytes, members: list[tuple[str, str]]) -> None:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for src, dst in members:
            target = VENDOR / dst
            target.parent.mkdir(parents=True, exist_ok=True)
            member = tf.getmember(src)
            with tf.extractfile(member) as fp, open(target, "wb") as out:
                shutil.copyfileobj(fp, out)
            print(f"    → {target.relative_to(ROOT)}")


def run(check_only: bool) -> int:
    if check_only:
        missing = []
        for _, _, members in ASSETS:
            for _, dst in members:
                if not (VENDOR / dst).exists():
                    missing.append(dst)
        if missing:
            print("FEHLENDE ASSETS:")
            for m in missing:
                print(f"  - webapp/static/vendor/{m}")
            print("\nBitte 'python scripts/download_vendor_assets.py' ausführen.")
            return 1
        print("Alle Vendor-Assets sind vorhanden.")
        return 0

    VENDOR.mkdir(parents=True, exist_ok=True)
    for url, kind, members in ASSETS:
        print(f"\n{url}")
        blob = fetch(url)
        if kind == "zip":
            extract_from_zip(blob, members)
        elif kind == "tgz":
            extract_from_tgz(blob, members)
        else:
            raise RuntimeError(f"Unbekanntes Archivformat: {kind}")

    print("\nFertig. Die Dateien liegen unter webapp/static/vendor/.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Nur prüfen ob alle Assets vorhanden sind (exit 1 wenn nicht).",
    )
    args = parser.parse_args()
    try:
        return run(check_only=args.check)
    except Exception as exc:  # noqa: BLE001 – Skript, Top-Level-Fehler anzeigen
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
