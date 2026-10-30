#!/usr/bin/env python3
"""
VyOS Image Downloader

Downloads VyOS images from the support portal export (input.json),
organizes them into version folders, and verifies signatures.

Requirements:
    pip install requests
    minisign (https://github.com/jedisct1/minisign) for minisign verification.
    gpg (GnuPG) for GPG signature verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    import requests
except ImportError:
    print("Error: 'requests' is required. Install with: pip install requests")
    sys.exit(1)

# VyOS minisign public key (from https://vyos.net/get/nightly-builds/)
VYOS_MINISIGN_PUBKEY = "RWSGGDXlHRkdCWosMDrGMqHmY/fDJsexHCfjnXqyiyvqGJGpaqf1RHWQ"

# VyOS GPG public keys directory (all .key files are imported)
VYOS_GPG_KEY_DIR = Path(__file__).parent

PLATFORM_FOLDER_MAP = {
    "Generic": "generic-iso",
    "AWS": "aws",
    "Azure": "azure",
    "Google Cloud": "gcloud",
    "Hyper-V": "hyperv",
    "QEMU/KVM": "qemu",
    "VMware vSphere": "vmware",
    "Proxmox VE": "proxmox",
    "Nutanix": "nutanix",
    "OpenStack": "openstack",
    "Oracle Cloud": "oracle",
    "Oracle Linux Virtualization Manager": "oracle-lvm",
    "XCP-NG": "xcp-ng",
    "Equinix NE": "equinix",
    "Exoscale": "exoscale",
    "Dell EMC": "dell-emc",
    "Edgecore Networks": "edgecore",
    "IBM Cloud": "ibm-cloud",
    "PXE": "pxe",
    "Polywell": "polywell",
    "Protectli": "protectli",
    "Lanner": "lanner",
    "Red Hat OpenShift": "openshift",
}


def parse_input(input_file: str) -> list[dict]:
    with open(input_file) as f:
        data = json.load(f)
    return data["data"]["invokeExtension"]["response"]["body"]["results"]


def sanitize_platform(platform: str) -> str:
    return PLATFORM_FOLDER_MAP.get(platform, platform.lower().replace(" ", "-").replace("/", "-"))


def load_etags(directory: Path) -> dict[str, str]:
    etag_file = directory / ".etags.json"
    if etag_file.exists():
        with open(etag_file) as f:
            return json.load(f)
    return {}


def save_etags(directory: Path, etags: dict[str, str]) -> None:
    etag_file = directory / ".etags.json"
    with open(etag_file, "w") as f:
        json.dump(etags, f, indent=2)


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(url: str, dest: Path, session: requests.Session) -> bool:
    etags = load_etags(dest.parent)

    if dest.exists():
        try:
            head = session.head(url, allow_redirects=True, timeout=30)
            remote_etag = head.headers.get("etag", "").strip('"')
            remote_size = int(head.headers.get("content-length", 0))

            # ETag match = definitive skip
            stored_etag = etags.get(dest.name)
            if stored_etag and remote_etag and stored_etag == remote_etag:
                print(f"  [SKIP] {dest.name} (ETag match)")
                return True

            # Fallback to size match
            if remote_size and dest.stat().st_size == remote_size:
                # Verify via MD5 if ETag looks like an MD5 (32 hex chars, no dash)
                if remote_etag and len(remote_etag) == 32 and "-" not in remote_etag:
                    local_md5 = md5_file(dest)
                    if local_md5 == remote_etag:
                        etags[dest.name] = remote_etag
                        save_etags(dest.parent, etags)
                        print(f"  [SKIP] {dest.name} (MD5 verified)")
                        return True
                else:
                    print(f"  [SKIP] {dest.name} (size match)")
                    return True
        except Exception:
            pass

    print(f"  [DOWN] {dest.name}")
    try:
        with session.get(url, stream=True, timeout=600) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            resp_etag = r.headers.get("etag", "").strip('"')
            downloaded = 0
            md5 = hashlib.md5()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                    f.write(chunk)
                    md5.update(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        print(f"\r  [DOWN] {dest.name} ... {pct:.1f}%", end="", flush=True)
            if total:
                print()

            # Verify download integrity via ETag/MD5
            if resp_etag and len(resp_etag) == 32 and "-" not in resp_etag:
                local_md5 = md5.hexdigest()
                if local_md5 != resp_etag:
                    print(f"  [FAIL] {dest.name}: MD5 mismatch (expected {resp_etag}, got {local_md5})")
                    dest.unlink()
                    return False
                print(f"  [OK]   {dest.name} MD5 verified")

            # Store ETag for future runs
            if resp_etag:
                etags[dest.name] = resp_etag
                save_etags(dest.parent, etags)

        return True
    except Exception as e:
        print(f"\n  [FAIL] {dest.name}: {e}")
        if dest.exists():
            dest.unlink()
        return False


def verify_minisig(image_path: Path, minisig_path: Path, pubkey: str) -> bool | None:
    if not shutil.which("minisign"):
        return None
    try:
        result = subprocess.run(
            ["minisign", "-V", "-P", pubkey, "-m", str(image_path), "-x", str(minisig_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except Exception as e:
        print(f"  [WARN] minisign verification error for {image_path.name}: {e}")
        return None


def import_gpg_keys() -> bool:
    if not shutil.which("gpg"):
        return False
    key_files = list(VYOS_GPG_KEY_DIR.glob("*.key"))
    if not key_files:
        print("[WARN] No .key files found for GPG import")
        return False
    imported = 0
    for key_file in key_files:
        try:
            result = subprocess.run(
                ["gpg", "--batch", "--no-tty", "--import", str(key_file)],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                imported += 1
                print(f"[GPG]  Imported {key_file.name}")
            else:
                print(f"[WARN] Failed to import {key_file.name}: {result.stderr.strip()}")
        except Exception as e:
            print(f"[WARN] Failed to import {key_file.name}: {e}")
    return imported > 0


def verify_gpg(image_path: Path, sig_path: Path) -> bool | None:
    if not shutil.which("gpg"):
        return None
    try:
        result = subprocess.run(
            ["gpg", "--batch", "--no-tty", "--verify", str(sig_path), str(image_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"  [DBG]  gpg stderr: {result.stderr.strip()}")
        return result.returncode == 0
    except Exception as e:
        print(f"  [WARN] GPG verification error for {image_path.name}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Download VyOS images from support portal export")
    parser.add_argument("-i", "--input", default="input.json", help="Path to input.json (default: input.json)")
    parser.add_argument("-o", "--output", default="downloads", help="Output directory (default: downloads)")
    parser.add_argument("-v", "--version", action="append", help="Only download specific version(s), e.g. -v 1.3.8 -v 1.4.4")
    parser.add_argument("-p", "--platform", action="append", help="Only download specific platform(s), e.g. -p Generic -p AWS")
    parser.add_argument("-t", "--type", choices=["Fresh Installation", "Update", "Source Code"], action="append",
                        help="Only download specific type(s)")
    parser.add_argument("--skip-addons", action="store_true", default=True, help="Skip VyOS Addons (default: true)")
    parser.add_argument("--include-addons", action="store_true", help="Include VyOS Addons")
    parser.add_argument("--no-verify", action="store_true", help="Skip all signature verification")
    parser.add_argument("--no-minisig", action="store_true", help="Skip minisign verification")
    parser.add_argument("--no-gpg", action="store_true", help="Skip GPG verification")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be downloaded without downloading")
    parser.add_argument("-j", "--jobs", type=int, default=3, help="Parallel downloads (default: 3)")
    parser.add_argument("--pubkey", default=VYOS_MINISIGN_PUBKEY, help="Minisign public key for verification")
    args = parser.parse_args()

    if args.include_addons:
        args.skip_addons = False

    results = parse_input(args.input)
    print(f"Loaded {len(results)} entries from {args.input}")

    # Filter
    filtered = []
    for r in results:
        if args.skip_addons and r["product"] == "VyOS Addons":
            continue
        if args.version and r["version"] not in args.version:
            continue
        if args.platform and r["deploymentPlatform"] not in args.platform:
            continue
        if args.type and r["downloadType"] not in args.type:
            continue
        if r["version"] == "N/A":
            continue
        filtered.append(r)

    print(f"Selected {len(filtered)} images to download")
    if not filtered:
        print("Nothing to download. Check your filters.")
        return

    # Group by version for display
    by_version: dict[str, list[dict]] = {}
    for r in filtered:
        by_version.setdefault(r["version"], []).append(r)

    for ver in sorted(by_version):
        entries = by_version[ver]
        print(f"\n  {ver}: {len(entries)} images")
        for e in sorted(entries, key=lambda x: x["deploymentPlatform"]):
            fname = unquote(urlparse(e["fileLink"]).path.split("/")[-1])
            print(f"    - [{e['downloadType']}] {e['deploymentPlatform']}: {fname}")

    if args.dry_run:
        print("\n[DRY RUN] No files downloaded.")
        return

    print()
    output_dir = Path(args.output)
    session = requests.Session()
    session.headers["User-Agent"] = "vyos-downloader/1.0"

    # Import GPG key if needed
    gpg_available = False
    if not args.no_verify and not args.no_gpg:
        gpg_available = import_gpg_keys()
        if not gpg_available and shutil.which("gpg"):
            print("[WARN] Failed to import VyOS GPG key, GPG verification disabled")
        elif not shutil.which("gpg"):
            print("[INFO] gpg not installed, GPG verification skipped")

    stats = {
        "downloaded": 0, "skipped": 0, "failed": 0,
        "minisig_ok": 0, "minisig_fail": 0, "minisig_skip": 0,
        "gpg_ok": 0, "gpg_fail": 0, "gpg_skip": 0,
    }

    def process_entry(entry: dict) -> dict:
        version = entry["version"]
        platform = sanitize_platform(entry["deploymentPlatform"])
        dtype = entry["downloadType"].lower().replace(" ", "-")

        if entry["downloadType"] == "Source Code":
            version_dir = output_dir / version / "source"
        else:
            version_dir = output_dir / version / platform
        version_dir.mkdir(parents=True, exist_ok=True)

        file_url = entry["fileLink"]
        filename = unquote(urlparse(file_url).path.split("/")[-1])
        image_path = version_dir / filename

        result = {"entry": entry, "success": False, "minisig": None, "gpg": None}

        # Download image
        if not download_file(file_url, image_path, session):
            result["success"] = False
            return result
        result["success"] = True

        # Download and verify minisign signature
        minisig_url = entry.get("minisignSignature", "N/A")
        if minisig_url and minisig_url != "N/A":
            minisig_path = version_dir / (filename + ".minisig")
            download_file(minisig_url, minisig_path, session)
            if not args.no_verify and not args.no_minisig:
                v = verify_minisig(image_path, minisig_path, args.pubkey)
                result["minisig"] = v
                if v is True:
                    print(f"  [OK]   {filename} minisig valid")
                elif v is False:
                    print(f"  [FAIL] {filename} MINISIG INVALID!")
                else:
                    print(f"  [SKIP] {filename} minisign not installed")

        # Download and verify GPG signature
        gpg_url = entry.get("gpgSignature", "N/A")
        if gpg_url and gpg_url != "N/A":
            gpg_path = version_dir / (filename + ".asc")
            download_file(gpg_url, gpg_path, session)
            if not args.no_verify and not args.no_gpg and gpg_available:
                v = verify_gpg(image_path, gpg_path)
                result["gpg"] = v
                if v is True:
                    print(f"  [OK]   {filename} GPG valid")
                elif v is False:
                    print(f"  [FAIL] {filename} GPG INVALID!")
                else:
                    print(f"  [SKIP] {filename} gpg not installed")

        return result

    # Process downloads (sequential for cleaner output, parallel for speed)
    for ver in sorted(by_version):
        entries = by_version[ver]
        print(f"\n{'='*60}")
        print(f"Version {ver} ({len(entries)} images)")
        print(f"{'='*60}")

        def tally(r):
            if r["success"]:
                stats["downloaded"] += 1
                for key in ("minisig", "gpg"):
                    if r[key] is True:
                        stats[f"{key}_ok"] += 1
                    elif r[key] is False:
                        stats[f"{key}_fail"] += 1
                    else:
                        stats[f"{key}_skip"] += 1
            else:
                stats["failed"] += 1

        if args.jobs > 1:
            with ThreadPoolExecutor(max_workers=args.jobs) as executor:
                futures = {executor.submit(process_entry, e): e for e in entries}
                for future in as_completed(futures):
                    tally(future.result())
        else:
            for e in entries:
                tally(process_entry(e))

    print(f"\n{'='*60}")
    print("Summary")
    print(f"{'='*60}")
    print(f"  Downloaded:    {stats['downloaded']}")
    print(f"  Failed:        {stats['failed']}")
    print(f"  Minisig OK:    {stats['minisig_ok']}")
    print(f"  GPG OK:        {stats['gpg_ok']}")
    if stats["minisig_fail"]:
        print(f"  Minisig FAIL:  {stats['minisig_fail']}  *** CHECK THESE FILES ***")
    if stats["gpg_fail"]:
        print(f"  GPG FAIL:      {stats['gpg_fail']}  *** CHECK THESE FILES ***")
    if stats["minisig_skip"]:
        print(f"  Minisig skip:  {stats['minisig_skip']}")
    if stats["gpg_skip"]:
        print(f"  GPG skip:      {stats['gpg_skip']}")


if __name__ == "__main__":
    main()
