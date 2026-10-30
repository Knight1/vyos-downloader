# VyOS Image Downloader

Bulk-download VyOS stable release images from the support portal and verify their minisign and GPG signatures.

## Prerequisites

- Python 3.10+
- `requests` library
- `minisign` (optional, for minisign verification)
- `gpg` / GnuPG (optional, for GPG verification)

```bash
pip install -r requirements.txt

# Install minisign
apt install minisign          # Debian/Ubuntu
brew install minisign         # macOS

# GPG is usually pre-installed on Linux
# macOS: brew install gnupg
```

## Getting `input.json`

1. Log in to the [VyOS Support Portal](https://support.vyos.io/)
2. Navigate to the **Downloads** section
3. Open browser DevTools (F12) -> **Network** tab
4. Filter for `graphql` or `invokeExtension` requests
5. Find the request that returns the download list
6. Copy the response JSON and save it as `input.json`

## Usage

```bash
# Download everything (all versions, all platforms)
python download_vyos.py

# Dry run - see what would be downloaded
python download_vyos.py --dry-run

# Download specific version(s)
python download_vyos.py -v 1.3.8
python download_vyos.py -v 1.3.8 -v 1.4.4

# Download specific platform(s)
python download_vyos.py -p Generic
python download_vyos.py -p Generic -p AWS

# Download only fresh installs (no updates)
python download_vyos.py -t "Fresh Installation"

# Combine filters
python download_vyos.py -v 1.4.4 -p Generic -p QEMU/KVM

# Custom output directory
python download_vyos.py -o /path/to/storage

# Custom input file
python download_vyos.py -i portal_export.json

# Skip all signature verification
python download_vyos.py --no-verify

# Skip only GPG or only minisign
python download_vyos.py --no-gpg
python download_vyos.py --no-minisig

# Single-threaded download
python download_vyos.py -j 1
```

## Output Structure

```
downloads/
├── 1.3.8/
│   ├── generic-iso/
│   │   ├── vyos-1.3.8-amd64.iso
│   │   ├── vyos-1.3.8-amd64.iso.minisig
│   │   └── vyos-1.3.8-amd64.iso.asc
│   ├── aws/
│   │   ├── vyos-1.3.8-aws-amd64.iso
│   │   └── vyos-1.3.8-aws-amd64.iso.minisig
│   ├── qemu/
│   │   ├── vyos-1.3.8-qemu-amd64.qcow2
│   │   └── vyos-1.3.8-qemu-amd64.qcow2.minisig
│   └── source/
│       └── equuleus-1.3.8.tar.gz
├── 1.4.4/
│   ├── generic-iso/
│   └── ...
└── 1.5.0/
    └── ...
```

## Signature Verification

Downloaded images are automatically verified using both **minisign** and **GPG** when the tools are available. The VyOS GPG release key is automatically imported on first run.

The script reports per signature type:

- `[OK]` - Signature is valid
- `[FAIL]` - **Signature is invalid - do not use the image!**
- `[SKIP]` - Verification tool not installed

### Manual verification

**Minisign:**

```bash
minisign -V \
  -P RWSGGDXlHRkdCWosMDrGMqHmY/fDJsexHCfjnXqyiyvqGJGpaqf1RHWQ \
  -m vyos-1.3.8-amd64.iso \
  -x vyos-1.3.8-amd64.iso.minisig
```

**GPG:**

```bash
gpg --verify vyos-1.3.8-amd64.iso.asc vyos-1.3.8-amd64.iso
```

## Features

- Resumes interrupted downloads (skips files with matching size)
- Parallel downloads (configurable, default: 3)
- Filters by version, platform, and download type
- Automatic minisign and GPG verification
- Dry-run mode to preview before downloading

## License

MIT
