"""Select newer stable Windows downloads from the public ArtLapse releases."""

import re
from dataclasses import dataclass

REPOSITORY = "dmitriyermolenko81/ArtLapse"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
LATEST_API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_RESPONSE_BYTES = 1024 * 1024
CHECK_TIMEOUT_MS = 8000
STARTUP_DELAY_MS = 1500


def version_number(value: str) -> tuple[int, int, int]:
    if len(value) > 64:
        raise ValueError("Version is too long")
    match = re.fullmatch(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)
    if match is None:
        raise ValueError("A stable major.minor.patch version is required")
    return int(match[1]), int(match[2]), int(match[3])


@dataclass(frozen=True)
class ReleaseUpdate:
    version: str
    download_url: str


def available_update(data: object, current: str) -> ReleaseUpdate | None:
    """Ignore source archives, preview releases, and older/equal versions."""
    if not isinstance(data, dict):
        raise ValueError("Invalid release response")
    if not isinstance(data.get("draft"), bool) or not isinstance(data.get("prerelease"), bool):
        raise ValueError("Release status is missing")
    if data["draft"] or data["prerelease"]:
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str):
        raise ValueError("Release tag is missing")
    try:
        latest = version_number(tag)
    except ValueError:
        return None
    if latest <= version_number(current):
        return None
    version = ".".join(str(part) for part in latest)
    filename = f"ArtLapse-{version}-Windows-x64.zip"
    download = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{filename}"
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise ValueError("Release assets are missing")
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != filename:
            continue
        if (asset.get("state") != "uploaded" or not isinstance(asset.get("size"), int)
                or asset["size"] <= 0):
            continue
        if asset.get("browser_download_url") != download:
            raise ValueError("Unexpected release download URL")
        return ReleaseUpdate(version, download)
    return None
