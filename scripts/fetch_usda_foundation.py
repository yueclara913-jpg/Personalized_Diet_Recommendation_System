"""Bounded official snapshot download to a new file outside the repository."""
import argparse
from datetime import datetime, timezone
import hashlib
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request

REPOSITORY = Path(__file__).resolve().parents[1]
MAX_BYTES = 10 * 1024 * 1024


def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'fdc.nal.usda.gov'
            or parsed.username or parsed.password or parsed.port not in (None,443)
            or parsed.query or parsed.fragment or not parsed.path.endswith('.zip')):
        raise ValueError('Requires an official HTTPS snapshot ZIP URL without credentials')


class OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_snapshot(url, destination, *, opener=None):
    """No retries, overwrite, extraction, database writes or API credentials."""
    validate_url(url)
    path = Path(destination).resolve()
    if path == REPOSITORY or REPOSITORY in path.parents:
        raise ValueError('Raw download must be outside the Git repository')
    if path.exists():
        raise FileExistsError('Destination already exists')
    client = opener or build_opener(OfficialRedirect())
    digest = hashlib.sha256()
    size = 0
    created = False
    try:
        with client.open(Request(url), timeout=30) as response:
            validate_url(response.geturl())
            length = response.headers.get('Content-Length')
            if length is not None and int(length) > MAX_BYTES:
                raise ValueError('Snapshot exceeds 10 MiB limit')
            with path.open('xb') as output:
                created = True
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError('Snapshot exceeds 10 MiB limit')
                    digest.update(chunk)
                    output.write(chunk)
        if size == 0 or not zipfile.is_zipfile(path):
            raise ValueError('Response is not a valid ZIP snapshot')
        with zipfile.ZipFile(path) as archive:
            if sum(info.file_size for info in archive.infolist()) > 64 * 1024 * 1024:
                raise ValueError('ZIP uncompressed content exceeds 64 MiB limit')
            if archive.testzip() is not None:
                raise ValueError('ZIP member CRC validation failed')
        return dict(source_url=url, downloaded_at=datetime.now(timezone.utc).isoformat(),
                    source_file_sha256=digest.hexdigest(), size_bytes=size)
    except BaseException:
        if created:
            path.unlink()
        raise


def main():
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Copy the exact verified official download link')
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    print(json.dumps(fetch_snapshot(args.url, args.destination), sort_keys=True))


if __name__ == '__main__':
    main()
