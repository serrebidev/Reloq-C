"""Published package updates, independent of speech and dictionary workers.

Releases are read from Forgejo, which publishes no per-asset checksum in its
API. Each package therefore ships a sibling ``.sha256`` asset in the same
release; the updater downloads that small file first and then verifies the
package against the published size and SHA-256 checksum while streaming.
"""
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

RELEASES = "https://git.serrebiradio.com/api/v1/repos/serrebi/Reloq-C/releases?limit=30"
LIMIT = 128 * 1024 * 1024
CHECKSUM_SUFFIX = ".sha256"
CHECKSUM_LIMIT = 4096
PACKAGE = re.compile(r"reloq-c-synth-x64-(\d+\.\d+\.\d+)\.nvda-addon\Z")


@dataclass(frozen=True)
class Update:
    version: str
    name: str
    url: str
    size: int
    sha256: str
    checksum_url: str


def version(value):
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Invalid package version")
    return tuple(map(int, value.split('.')))


def _download_url(name, url, kind):
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.netloc != 'git.serrebiradio.com' or parts.query or parts.fragment
            or not parts.path.startswith('/serrebi/Reloq-C/releases/download/')
            or unquote(parts.path.rsplit('/', 1)[-1]) != name):
        raise ValueError(f"Invalid {kind} download URL")


def select(releases, installed):
    development = re.fullmatch(r"(\d+\.\d+\.\d+)-(?:dev|rc)\d+", installed)
    current = version(development[1] if development else installed)
    if not isinstance(releases, list):
        raise ValueError("Invalid release list")
    available = []
    for release in releases:
        if not isinstance(release, dict) or release.get('draft'):
            continue
        by_name = {}
        for asset in release.get('assets', []):
            if isinstance(asset, dict) and isinstance(asset.get('name'), str):
                by_name.setdefault(asset['name'], asset)
        for name, asset in by_name.items():
            match = PACKAGE.fullmatch(name)
            if not match:
                continue
            candidate = version(match[1])
            if candidate < current or (candidate == current and not development):
                continue
            url, size = asset.get('browser_download_url'), asset.get('size')
            if not isinstance(url, str):
                raise ValueError("Update metadata is missing its download or SHA-256 checksum")
            _download_url(name, url, 'package')
            if type(size) is not int or not 0 < size <= LIMIT:
                raise ValueError("Invalid package size")
            sidecar = by_name.get(name + CHECKSUM_SUFFIX)
            if not isinstance(sidecar, dict):
                raise ValueError("Update metadata is missing its SHA-256 checksum")
            checksum_url, checksum_size = (sidecar.get(k) for k in ('browser_download_url', 'size'))
            if not isinstance(checksum_url, str):
                raise ValueError("Update metadata is missing its SHA-256 checksum")
            _download_url(name + CHECKSUM_SUFFIX, checksum_url, 'checksum')
            if type(checksum_size) is not int or not 0 < checksum_size <= CHECKSUM_LIMIT:
                raise ValueError("Invalid update checksum size")
            # The checksum itself is downloaded by resolve(); selection only
            # records where the release publishes it.
            available.append(Update(match[1], name, url, size, '', checksum_url))
    return max(available, key=lambda update: version(update.version), default=None)


def _request(url):
    return Request(url, headers={'User-Agent': 'ReLoq-C-updater'})


def resolve(update, *, fetch=urlopen):
    """Download the published checksum and attach it to the candidate."""
    if not update.checksum_url:
        raise ValueError("Update metadata is missing its SHA-256 checksum")
    with fetch(_request(update.checksum_url), timeout=20) as response:
        data = response.read(CHECKSUM_LIMIT + 1)
    if len(data) > CHECKSUM_LIMIT:
        raise ValueError("Update checksum exceeds its size limit")
    try:
        fields = data.decode('ascii').split()
    except UnicodeDecodeError:
        raise ValueError("Invalid package SHA-256 checksum")
    digest = fields[0] if fields else ''
    if not re.fullmatch(r'[0-9a-fA-F]{64}', digest) or len(fields) > 2:
        raise ValueError("Invalid package SHA-256 checksum")
    if len(fields) == 2 and fields[1].lstrip('*') != update.name:
        raise ValueError("Update checksum names a different package")
    return replace(update, sha256=digest.lower())


def check(installed, *, fetch=urlopen):
    with fetch(_request(RELEASES), timeout=20) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Release list is too large")
    candidate = select(json.loads(data), installed)
    if candidate is None:
        return None
    return resolve(candidate, fetch=fetch)


def download(update, directory, *, fetch=urlopen):
    # A candidate can also be supplied by a caller; validate it before I/O.
    validated = select([{'assets': [dict(name=update.name, browser_download_url=update.url,
        size=update.size), dict(name=update.name + CHECKSUM_SUFFIX,
        browser_download_url=update.checksum_url, size=CHECKSUM_LIMIT)]}], '0.0.0')
    if (validated is None or validated.version != update.version or validated.name != update.name
            or validated.url != update.url or validated.size != update.size
            or validated.checksum_url != update.checksum_url
            or not re.fullmatch(r'[0-9a-f]{64}', update.sha256)):
        raise ValueError("Invalid update candidate")
    target = Path(directory) / update.name
    partial = target.with_suffix(target.suffix + '.part')
    digest, size = hashlib.sha256(), 0
    try:
        with fetch(_request(update.url), timeout=20) as response, partial.open('wb') as output:
            while block := response.read(65536):
                size += len(block)
                if size > update.size:
                    raise ValueError("Downloaded package exceeds its published size")
                digest.update(block)
                output.write(block)
        if size != update.size or digest.hexdigest() != update.sha256:
            raise ValueError("Downloaded package does not match its published size or SHA-256 checksum")
        partial.replace(target)
        return target
    finally:
        partial.unlink(missing_ok=True)
