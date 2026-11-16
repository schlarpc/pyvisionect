"""Visionect's firmware repository: which builds exist, and the files.

``http://firmware.visionect.com:8089`` is what Visionect's own Software Suite
asks. ``/api/v2/list`` names every build (JSON, ETag-aware, about 6 MB: the v1
list leaves out the 32" Place & Play) and ``/api/v1/get/<checksum>`` returns
one file. Only ``UniFw`` / ``firmware`` entries are firmware for a frame; the
others (``UniFw_AC``) are skipped.

The list is cached on disk under a caller-supplied directory and re-checked with
``If-None-Match``; the files are cached once they walk cleanly (see
``pyvisionect.bootloader.walk_firmware``). No firmware files are included in the package.
A repository that can't be reached is not an error: the cached list, if any,
keeps being used, and callers that only read the cache never touch the network.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import http.client
import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..bootloader import BootloaderError, walk_firmware

logger = logging.getLogger(__name__)

BASE_URL = "http://firmware.visionect.com:8089"
LIST_PATH = "/api/v2/list"
GET_PATH = "/api/v1/get/"
TIMEOUT_S = 15.0
# The v2 list is ~6 MB and a 32" firmware file ~230 KB; anything far past
# that is not what we asked for.
MAX_LIST_BYTES = 40 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
# Re-ask the repository for the list at most this often; builds appear rarely.
LIST_MAX_AGE_S = 6 * 3600.0
FAILED_RETRY_S = 15 * 60.0
APP_NAME = "UniFw"
KIND = "firmware"
CACHE_VERSION = 1
# Digests recorded for the two firmware files used on the reference frame.
KNOWN_SHA256: dict[int, str] = {
    # 32" Place & Play, hardware 1.1.0
    0x2CB91ED6: "491cdd69ee0561a15d164f3882c76e690e26e426a068f3fa177529356f199a68",  # 7.4.4407
    0xA529A106: "30c23c9a0d8f8dda4db4ec6d349022dfb59c909cd46f07bf6ca4306da7e64628",  # 7.3.4358
}


class RepositoryError(RuntimeError):
    """The repository could not give what was asked, in plain words."""


@dataclass(frozen=True)
class Build:
    """One firmware build for one hardware revision and display."""

    checksum: int
    checksum_text: str
    """The checksum as the repository writes it (hex, leading zeros dropped)."""
    version: tuple[int, int, int]
    hardware: tuple[int, int, int]
    """Hardware major, minor, BOM."""
    hardware_name_id: int
    display_id: int

    @property
    def version_text(self) -> str:
        return ".".join(map(str, self.version))

    @property
    def checksum_hex(self) -> str:
        return f"{self.checksum:08x}"

    def validate_firmware(self, data: bytes) -> None:
        """Check record structure and the trusted digest, when one is known.

        Raises ``BootloaderError`` for malformed records or ``RepositoryError``
        for a digest mismatch. An unknown digest leaves integrity unverified.
        """
        walk_firmware(data)
        expected = KNOWN_SHA256.get(self.checksum)
        if expected is not None and hashlib.sha256(data).hexdigest() != expected:
            raise RepositoryError(f"SHA-256 mismatch for firmware {self.checksum_hex}")


def _int(value: Any) -> int | None:
    try:
        return int(str(value), 10)
    except (TypeError, ValueError):
        return None


def parse_list(payload: Any) -> list[Build]:
    """The firmware entries of a ``/api/v2/list`` (or v1) document.

    Entries that aren't ``UniFw`` firmware, or are missing a field, are
    skipped. A document of the wrong shape raises ``RepositoryError``.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("Firmwares"), dict):
        raise RepositoryError("the repository's list has an unexpected shape")
    builds: list[Build] = []
    for entries in payload["Firmwares"].values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            meta = entry.get("Meta") if isinstance(entry, dict) else None
            if not isinstance(meta, dict):
                continue
            if meta.get("AppName") != APP_NAME or meta.get("type") != KIND:
                continue
            text = str(meta.get("Checksum") or "").strip().lower()
            if len(text) > 8 or any(c not in "0123456789abcdef" for c in text):
                continue
            try:
                checksum = int(text, 16)
            except ValueError:
                continue
            nums = [
                _int(meta.get(k))
                for k in (
                    "FwMajor",
                    "FwMinor",
                    "FwRevision",
                    "HwMajor",
                    "HwMinor",
                    "HwBOM",
                    "HwNameID",
                    "DisplayID",
                )
            ]
            vals = [n for n in nums if n is not None]
            if len(vals) != len(nums) or not 0 <= checksum <= 0xFFFFFFFF:
                continue
            builds.append(
                Build(
                    checksum=checksum,
                    checksum_text=text,
                    version=(vals[0], vals[1], vals[2]),
                    hardware=(vals[3], vals[4], vals[5]),
                    hardware_name_id=vals[6],
                    display_id=vals[7],
                )
            )
    return builds


def compatible(
    builds: list[Build],
    *,
    hardware: tuple[int, int, int],
    hardware_name_id: int,
    display_id: int,
) -> list[Build]:
    """Builds for exactly this hardware revision, hardware name id and display,
    newest first. Duplicate checksums collapse to one."""
    seen: set[int] = set()
    out: list[Build] = []
    for b in sorted(builds, key=lambda b: b.version, reverse=True):
        if (
            b.hardware == hardware
            and b.hardware_name_id == hardware_name_id
            and b.display_id == display_id
            and b.checksum not in seen
        ):
            seen.add(b.checksum)
            out.append(b)
    return out


def _read_capped(resp: Any, limit: int) -> bytes:
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise RepositoryError("the repository's answer is larger than expected")
    return bytes(data)


class FirmwareRepository:
    """List and file access with an on-disk cache.

    ``builds()`` and ``cached_file()`` only read the cache. ``refresh()`` and
    ``fetch()`` use the network and are meant for a worker or request thread.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        base_url: str = BASE_URL,
        timeout: float = TIMEOUT_S,
        clock: Any = time.time,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._clock = clock
        self._lock = threading.RLock()
        self._builds: list[Build] | None = None
        self._etag = ""
        self._checked_at = 0.0
        self._failed_at = 0.0
        self._loaded = False
        self._refreshing = False
        self.last_error = ""
        self.generation = 0
        """Bumped whenever a refresh finishes."""

    # -- the list ------------------------------------------------------------

    @property
    def _list_path(self) -> Path:
        return self.cache_dir / "list.json"

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                doc = json.loads(self._list_path.read_text(encoding="utf-8"))
                if not isinstance(doc, dict) or doc.get("version") != CACHE_VERSION:
                    return
                self._builds = [
                    Build(
                        checksum=int(r[0], 16),
                        checksum_text=r[0],
                        version=(r[1], r[2], r[3]),
                        hardware=(r[4], r[5], r[6]),
                        hardware_name_id=r[7],
                        display_id=r[8],
                    )
                    for r in doc["builds"]
                ]
                self._etag = str(doc.get("etag") or "")
                self._checked_at = float(doc.get("checked_at") or 0.0)
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                self._builds = None

    def builds(self) -> list[Build] | None:
        """The cached list, or None when there isn't one. No network."""
        self._load()
        return self._builds

    def stale(self) -> bool:
        """Whether the list should be asked for again now."""
        self._load()
        now = self._clock()
        if self._refreshing:
            return False
        if self._failed_at and now - self._failed_at < FAILED_RETRY_S:
            return False
        return self._builds is None or now - self._checked_at >= LIST_MAX_AGE_S

    def refresh(self) -> bool:
        """Ask the repository for the list (conditionally). True when the
        cache is now current; False when it could not be reached, in which
        case ``last_error`` says why and any cached list stays in use."""
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True
        try:
            return self._refresh()
        finally:
            with self._lock:
                self._refreshing = False
                self.generation += 1

    def refresh_in_background(self) -> None:
        """Start ``refresh`` on a daemon thread when the list is stale."""
        if not self.stale():
            return
        threading.Thread(target=self.refresh, name="visionect-firmware-list", daemon=True).start()

    def _refresh(self) -> bool:
        self._load()
        headers = {"User-Agent": "pyvisionect", "Accept": "application/json"}
        headers["Accept-Encoding"] = "gzip"
        if self._etag and self._builds is not None:
            headers["If-None-Match"] = self._etag
        req = urllib.request.Request(self.base_url + LIST_PATH, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = _read_capped(resp, MAX_LIST_BYTES)
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = self._gunzip(raw, MAX_LIST_BYTES)
                etag = resp.headers.get("ETag", "")
            builds = parse_list(json.loads(raw))
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 304 and self._builds is not None:
                with self._lock:
                    self._checked_at = self._clock()
                    self._failed_at = 0.0
                    self.last_error = ""
                self._save()
                return True
            return self._failed(f"the repository answered {exc.code}")
        except (
            OSError,
            ValueError,
            EOFError,
            RecursionError,
            zlib.error,
            http.client.HTTPException,
            RepositoryError,
        ) as exc:
            return self._failed(f"{type(exc).__name__}: {exc}")
        with self._lock:
            self._builds = builds
            self._etag = etag
            self._checked_at = self._clock()
            self._failed_at = 0.0
            self.last_error = ""
        self._save()
        logger.info("visionect: firmware list refreshed, %d builds", len(builds))
        return True

    @staticmethod
    def _gunzip(raw: bytes, limit: int) -> bytes:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as fh:
            data = fh.read(limit + 1)
        if len(data) > limit:
            raise RepositoryError("the repository's answer is larger than expected")
        return data

    def _failed(self, why: str) -> bool:
        with self._lock:
            self._failed_at = self._clock()
            self.last_error = why
        logger.info("visionect: firmware repository not reachable (%s)", why)
        return False

    def _save(self) -> None:
        with self._lock:
            builds = self._builds or []
            doc = {
                "version": CACHE_VERSION,
                "etag": self._etag,
                "checked_at": self._checked_at,
                "builds": [
                    [
                        b.checksum_text,
                        *b.version,
                        *b.hardware,
                        b.hardware_name_id,
                        b.display_id,
                    ]
                    for b in builds
                ],
            }
        try:
            self._write_atomic(self._list_path, json.dumps(doc, separators=(",", ":")).encode())
        except OSError as exc:
            logger.warning("visionect: couldn't cache the firmware list: %s", exc)

    def _write_atomic(self, path: Path, data: bytes) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()

    # -- the files -----------------------------------------------------------

    def _file_path(self, build: Build) -> Path:
        return self.cache_dir / f"{build.checksum_hex}.bin"

    def cached_file(self, build: Build) -> bytes | None:
        """The cached file for ``build`` if it is there and still walks
        cleanly; no network."""
        try:
            data = self._file_path(build).read_bytes()
            build.validate_firmware(data)
        except (OSError, BootloaderError, RepositoryError):
            return None
        return data

    def fetch(self, build: Build) -> bytes:
        """The firmware file for ``build``: from the cache, else downloaded.
        It is only returned (and cached) when it walks cleanly to the end.

        Raises ``RepositoryError`` with a plain reason when it can't."""
        cached = self.cached_file(build)
        if cached is not None:
            return cached
        names = [build.checksum_text]
        if build.checksum_hex != build.checksum_text:
            names.append(build.checksum_hex)  # leading zeros restored
        data = b""
        last: Exception | None = None
        for name in names:
            req = urllib.request.Request(
                self.base_url + GET_PATH + name, headers={"User-Agent": "pyvisionect"}
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = _read_capped(resp, MAX_FILE_BYTES)
                last = None
                break
            except urllib.error.HTTPError as exc:
                exc.close()
                last = exc
                if exc.code != 404:
                    break
            except (OSError, ValueError, http.client.HTTPException, RepositoryError) as exc:
                last = exc
                break
        if last is not None:
            raise RepositoryError(f"couldn't download the firmware: {last}") from last
        try:
            build.validate_firmware(data)
        except BootloaderError as exc:
            raise RepositoryError(
                f"the downloaded firmware is not a file the frame accepts ({exc}); not used"
            ) from exc
        if build.checksum not in KNOWN_SHA256:
            logger.warning(
                "firmware %s has no trusted digest; downloaded SHA-256: %s",
                build.checksum_hex,
                hashlib.sha256(data).hexdigest(),
            )
        try:
            self._write_atomic(self._file_path(build), data)
        except OSError as exc:
            logger.warning("visionect: couldn't cache firmware %s: %s", build.checksum_hex, exc)
        return data
