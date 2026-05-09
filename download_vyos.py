#!/usr/bin/env python3
"""
VyOS Image Downloader

Downloads VyOS images from the support portal export (input.json),
organizes them into version folders, and verifies minisign signatures.

Requirements:
    pip install requests
    minisign (https://github.com/jedisct1/minisign) must be installed for verification.
"""

from __future__ import annotations

import argparse
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


def download_file(url: str, dest: Path, session: requests.Session) -> bool:
    if dest.exists():
        # Check if remote size matches local size
        try:
            head = session.head(url, allow_redirects=True, timeout=30)
            remote_size = int(head.headers.get("content-length", 0))
            if remote_size and dest.stat().st_size == remote_size:
                print(f"  [SKIP] {dest.name} (already exists, size matches)")
                return True
        except Exception:
            pass

    print(f"  [DOWN] {dest.name}")
    try:
        with session.get(url, stream=True, timeout=600) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        print(f"\r  [DOWN] {dest.name} ... {pct:.1f}%", end="", flush=True)
            if total:
                print()
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
        print(f"  [WARN] Verification error for {image_path.name}: {e}")
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
    parser.add_argument("--no-verify", action="store_true", help="Skip minisign verification")
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

    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "verified": 0, "verify_fail": 0, "verify_skip": 0}

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

        result = {"entry": entry, "success": False, "verified": None}

        # Download image
        if not download_file(file_url, image_path, session):
            result["success"] = False
            return result
        result["success"] = True

        # Download minisig
        minisig_url = entry.get("minisignSignature", "N/A")
        if minisig_url and minisig_url != "N/A" and not args.no_verify:
            minisig_path = version_dir / (filename + ".minisig")
            if download_file(minisig_url, minisig_path, session):
                v = verify_minisig(image_path, minisig_path, args.pubkey)
                result["verified"] = v
                if v is True:
                    print(f"  [OK]   {filename} signature valid")
                elif v is False:
                    print(f"  [FAIL] {filename} SIGNATURE INVALID!")
                else:
                    print(f"  [SKIP] {filename} minisign not installed, skipping verification")
        elif not args.no_verify:
            # Also download GPG signature if available
            gpg_url = entry.get("gpgSignature", "N/A")
            if gpg_url and gpg_url != "N/A":
                gpg_path = version_dir / (filename + ".asc")
                download_file(gpg_url, gpg_path, session)

        return result

    # Process downloads (sequential for cleaner output, parallel for speed)
    for ver in sorted(by_version):
        entries = by_version[ver]
        print(f"\n{'='*60}")
        print(f"Version {ver} ({len(entries)} images)")
        print(f"{'='*60}")

        if args.jobs > 1:
            with ThreadPoolExecutor(max_workers=args.jobs) as executor:
                futures = {executor.submit(process_entry, e): e for e in entries}
                for future in as_completed(futures):
                    r = future.result()
                    if r["success"]:
                        stats["downloaded"] += 1
                        if r["verified"] is True:
                            stats["verified"] += 1
                        elif r["verified"] is False:
                            stats["verify_fail"] += 1
                        else:
                            stats["verify_skip"] += 1
                    else:
                        stats["failed"] += 1
        else:
            for e in entries:
                r = process_entry(e)
                if r["success"]:
                    stats["downloaded"] += 1
                    if r["verified"] is True:
                        stats["verified"] += 1
                    elif r["verified"] is False:
                        stats["verify_fail"] += 1
                    else:
                        stats["verify_skip"] += 1
                else:
                    stats["failed"] += 1

    print(f"\n{'='*60}")
    print("Summary")
    print(f"{'='*60}")
    print(f"  Downloaded: {stats['downloaded']}")
    print(f"  Failed:     {stats['failed']}")
    print(f"  Verified:   {stats['verified']}")
    if stats["verify_fail"]:
        print(f"  INVALID:    {stats['verify_fail']}  *** CHECK THESE FILES ***")
    if stats["verify_skip"]:
        print(f"  Not verified: {stats['verify_skip']} (install minisign to verify)")


if __name__ == "__main__":
    main()
