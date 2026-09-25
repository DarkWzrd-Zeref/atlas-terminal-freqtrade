"""Inert, content-addressed storage for owner-reviewed native strategy bundles.

No HTTP server, Python import, compilation, interface validation, or execution lives
here. A broker must authenticate the owner, authorize the lab role, bound its request
body before decoding, and isolate native validation/backtests from paper trading.
Neither a manifest, a digest, nor successful native validation proves code is safe.

Manifest v1: ``version``, ``strategy_class``, ``timeframe``, ``entrypoint``,
``provenance`` (string map including kind/source), and ``files`` (list of
``{path, size, sha256}``). File paths are relative POSIX paths. Contents are passed
separately as an iterable of (path, bytes), retaining duplicate detection. All
support files are preserved byte-for-byte; archives must be unpacked before review.
Dependencies are installed separately by a reviewed image change, never by this module.

Candidates are immutable through this API and published by same-filesystem rename.
This is not an OS security boundary: trusted broker-owned storage must not be writable
by running strategy code. Reverify the reviewed digest immediately before native use.
Use the returned strategy_path with the native resolver. Freqtrade adds the entry
file's parent to sys.path, so authors retain control of their helper/package layout.
Do not enable recursive discovery over unrelated candidates (discovery executes code).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import keyword
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Iterable, Mapping
import unicodedata


MAX_FILES = 128
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
MANIFEST_NAME = ".atlas-manifest.json"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TIMEFRAME = re.compile(r"[1-9][0-9]{0,5}[smhdwMy]\Z")
_ARCHIVE_SUFFIXES = {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar",
                     ".zst", ".lz4", ".cab", ".whl", ".egg", ".npz"}
_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                   *(f"lpt{i}" for i in range(1, 10))}


class BundleError(ValueError):
    """The package or its review identity cannot be accepted without changes."""


@dataclass(frozen=True)
class ValidatedBundle:
    canonical_manifest: bytes
    files: tuple[tuple[str, bytes], ...]
    sha256: str

    @property
    def manifest(self) -> dict:
        """Return a fresh copy, never mutable internal state."""
        return json.loads(self.canonical_manifest)


@dataclass(frozen=True)
class InstalledCandidate:
    directory: Path
    strategy_path: Path
    strategy_class: str
    timeframe: str
    sha256: str


def _path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise BundleError("File paths must be nonempty relative UTF-8 names up to 512 bytes.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise BundleError("File paths must be valid UTF-8.") from exc
    if len(encoded) > 512:
        raise BundleError("File paths must be nonempty relative UTF-8 names up to 512 bytes.")
    if unicodedata.normalize("NFC", value) != value:
        raise BundleError("File paths must use NFC Unicode normalization.")
    if "\\" in value or ":" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise BundleError("File paths contain a forbidden separator or control character.")
    parts = value.split("/")
    if len(parts) > 16 or any(p in ("", ".", "..") for p in parts):
        raise BundleError("Absolute paths, traversal, empty segments, or excessive nesting are forbidden.")
    for part in parts:
        if part.endswith((".", " ")) or part.split(".")[0].casefold() in _RESERVED_NAMES:
            raise BundleError("File paths contain a non-portable device name or trailing character.")
        if any(c in part for c in '<>"|?*'):
            raise BundleError("File paths contain a non-portable character.")
    if value.casefold() == MANIFEST_NAME.casefold():
        raise BundleError("The internal manifest filename is reserved.")
    return value


def _archive(path: str, data: bytes) -> bool:
    signatures = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\x1f\x8b", b"BZh",
                  b"\xfd7zXZ\x00", b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07", b"MSCF",
                  b"\x28\xb5\x2f\xfd", b"\x04\x22\x4d\x18")
    return (PurePosixPath(path).suffix.lower() in _ARCHIVE_SUFFIXES
            or data.startswith(signatures) or data[257:263] in (b"ustar\x00", b"ustar "))


def _manifest(raw: Mapping) -> dict:
    required = {"version", "strategy_class", "timeframe", "entrypoint", "provenance", "files"}
    if not isinstance(raw, Mapping) or set(raw) != required:
        raise BundleError("Manifest must contain exactly the version 1 fields.")
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise BundleError("Unsupported manifest version.")
    name = raw["strategy_class"]
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name) or len(name) > 128:
        raise BundleError("strategy_class must be a Python class identifier.")
    timeframe = raw["timeframe"]
    if not isinstance(timeframe, str) or not _TIMEFRAME.fullmatch(timeframe):
        raise BundleError("timeframe must name a positive candle interval; native support is checked later.")
    entrypoint = _path(raw["entrypoint"])
    if PurePosixPath(entrypoint).suffix != ".py":
        raise BundleError("The strategy entrypoint must be a Python source file.")
    provenance = raw["provenance"]
    if not isinstance(provenance, Mapping) or not {"kind", "source"} <= set(provenance) or len(provenance) > 12:
        raise BundleError("Provenance must include kind and source.")
    if any(not isinstance(k, str) or not 1 <= len(k) <= 64
           or not isinstance(v, str) or not 1 <= len(v) <= 2048 for k, v in provenance.items()):
        raise BundleError("Provenance fields must be bounded, nonempty strings.")
    raw_files = raw["files"]
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= MAX_FILES:
        raise BundleError("The bundle has an invalid number of files.")
    files = []
    seen = set()
    total = 0
    for record in raw_files:
        if not isinstance(record, Mapping) or set(record) != {"path", "size", "sha256"}:
            raise BundleError("Every file needs path, size, and sha256 fields.")
        path = _path(record["path"])
        folded = path.casefold()
        if folded in seen:
            raise BundleError("Duplicate or case-colliding file paths are forbidden.")
        seen.add(folded)
        size, digest = record["size"], record["sha256"]
        if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise BundleError("A file exceeds the permitted size.")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise BundleError("Every file hash must be lowercase SHA256.")
        total += size
        if total > MAX_BUNDLE_BYTES:
            raise BundleError("The bundle exceeds the total size limit.")
        files.append({"path": path, "size": size, "sha256": digest})
    for path in seen:
        if any(str(parent) in seen for parent in PurePosixPath(path).parents if str(parent) != "."):
            raise BundleError("A file path is also used as a directory.")
    if entrypoint not in {f["path"] for f in files}:
        raise BundleError("The entrypoint is not present in the manifest.")
    return {"version": 1, "strategy_class": name, "timeframe": timeframe, "entrypoint": entrypoint,
            "provenance": dict(provenance), "files": sorted(files, key=lambda x: x["path"])}


def parse_manifest_json(payload: bytes) -> dict:
    """Decode a bounded JSON manifest, including rejection of duplicate object keys."""
    if not isinstance(payload, bytes) or len(payload) > MAX_MANIFEST_BYTES:
        raise BundleError("Manifest JSON must be bounded bytes.")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BundleError("Duplicate manifest JSON keys are forbidden.")
            result[key] = value
        return result

    try:
        return _manifest(json.loads(payload.decode("utf-8"), object_pairs_hook=unique_pairs))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BundleError("Manifest is not valid bounded UTF-8 JSON.") from exc


def validate_bundle(manifest: Mapping, files: Iterable[tuple[str, bytes]]) -> ValidatedBundle:
    """Check transport/schema/integrity only, without parsing or executing source.

    Provenance is a recorded assertion, not independently verified attribution.
    The digest binds every byte plus class, timeframe, entrypoint and provenance.
    """
    normalized = _manifest(manifest)
    try:
        canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except UnicodeError as exc:
        raise BundleError("Manifest strings must be valid UTF-8.") from exc
    if len(canonical) > MAX_MANIFEST_BYTES:
        raise BundleError("The manifest exceeds its size limit.")
    expected = {record["path"]: record for record in normalized["files"]}
    actual = {}
    for index, item in enumerate(files):
        if index >= MAX_FILES:
            raise BundleError("Too many file payloads.")
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise BundleError("File payloads must be path/bytes pairs.")
        path, content = item
        path = _path(path)
        if path in actual:
            raise BundleError("Duplicate file payload.")
        if path not in expected:
            raise BundleError("File payload is not declared in the manifest.")
        if not isinstance(content, bytes) or len(content) != expected[path]["size"]:
            raise BundleError("File payload type or length does not match its declaration.")
        if not hmac.compare_digest(hashlib.sha256(content).hexdigest(), expected[path]["sha256"]):
            raise BundleError("File content differs from its declared hash.")
        if _archive(path, content):
            raise BundleError("Archive/compressed payloads are forbidden; review unpacked files instead.")
        actual[path] = content
    if set(actual) != set(expected):
        raise BundleError("Declared file payloads are missing.")
    return ValidatedBundle(canonical, tuple(sorted(actual.items())), hashlib.sha256(canonical).hexdigest())


def _regular(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and not _link(info)


def _link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _plain_directory(directory: Path) -> Path:
    absolute = Path(os.path.abspath(directory))
    for part in (*reversed(absolute.parents), absolute):
        info = part.lstat()
        if _link(info) or not stat.S_ISDIR(info.st_mode):
            raise BundleError("Bundle directories and their parents must not be links or special files.")
    return absolute


def _read_regular(path: Path, limit: int) -> bytes:
    before = path.lstat()
    if not _regular(before) or before.st_size > limit:
        raise BundleError("Only bounded regular files can enter a bundle.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not _regular(opened) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise BundleError("A bundle source changed while opening it.")
        content = handle.read(limit + 1)
        if len(content) > limit:
            raise BundleError("File exceeds its size limit.")
        return content


def load_bundle(manifest: Mapping, source_directory: Path, *, _stored: bool = False) -> ValidatedBundle:
    """Read a broker-owned directory, rejecting links, special files and undeclared files.

    Concurrent hostile modification of directory ancestors is out of scope: keep
    staging/storage owned by the broker, inaccessible to candidate execution.
    """
    normalized = _manifest(manifest)
    root = _plain_directory(Path(source_directory))
    expected = {record["path"]: record for record in normalized["files"]}
    pending = [root]
    files = []
    directories = 0
    while pending:
        directory = pending.pop()
        directories += 1
        if directories > MAX_FILES * 16:
            raise BundleError("Too many directories in the source bundle.")
        for path in directory.iterdir():
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if _link(info):
                raise BundleError("Symlinks and filesystem reparse points are forbidden.")
            if _stored and relative == MANIFEST_NAME:
                if not _regular(info):
                    raise BundleError("Stored manifest must be a regular file.")
                continue
            _path(relative)
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif _regular(info):
                if len(files) >= MAX_FILES:
                    raise BundleError("Too many files in the source bundle.")
                if relative not in expected or info.st_size != expected[relative]["size"]:
                    raise BundleError("Source file is undeclared or differs from its declared size.")
                files.append((relative, _read_regular(path, expected[relative]["size"])))
            else:
                raise BundleError("Special files are forbidden.")
    return validate_bundle(normalized, files)


def verify_candidate(directory: Path, *, expected_sha256: str) -> ValidatedBundle:
    """Recheck all installed bytes and manifest against a previously reviewed digest."""
    root = _plain_directory(Path(directory))
    manifest = parse_manifest_json(_read_regular(root / MANIFEST_NAME, MAX_MANIFEST_BYTES))
    bundle = load_bundle(manifest, root, _stored=True)
    if (not isinstance(expected_sha256, str) or not _DIGEST.fullmatch(expected_sha256)
            or not hmac.compare_digest(bundle.sha256, expected_sha256)):
        raise BundleError("Candidate does not match the reviewed bundle digest.")
    return bundle


def install_candidate(lab_strategies: Path, bundle: ValidatedBundle, *, role: str,
                      reviewed_sha256: str) -> InstalledCandidate:
    """Atomically install an immutable candidate; never mutate a paper strategy.

    ``role`` must come from trusted deployment configuration, not request data.
    Storage must already exist so this primitive never creates arbitrary roots.
    Existing matching candidates are verified and reused, never overwritten.
    """
    if role != "lab":
        raise BundleError("Candidate installation is allowed only in the isolated lab role.")
    return _install_reviewed_bundle(lab_strategies, bundle, reviewed_sha256=reviewed_sha256)


def _install_reviewed_bundle(root: Path, bundle: ValidatedBundle, *,
                             reviewed_sha256: str) -> InstalledCandidate:
    """Internal inert storage primitive; callers must enforce their deployment role."""
    checked = validate_bundle(bundle.manifest, bundle.files)
    if (not isinstance(reviewed_sha256, str) or not _DIGEST.fullmatch(reviewed_sha256)
            or not hmac.compare_digest(checked.sha256, reviewed_sha256)):
        raise BundleError("Installation requires the exact reviewed bundle digest.")
    root = _plain_directory(Path(root))
    destination = root / ("atlas_" + checked.sha256)
    if destination.exists() or destination.is_symlink():
        verify_candidate(destination, expected_sha256=checked.sha256)
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".atlas-staging-", dir=root))
        try:
            for path, content in checked.files:
                target = temporary.joinpath(*PurePosixPath(path).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            with (temporary / MANIFEST_NAME).open("xb") as handle:
                handle.write(checked.canonical_manifest)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temporary.rename(destination)
            except OSError:
                if not destination.exists():
                    raise
                verify_candidate(destination, expected_sha256=checked.sha256)
        finally:
            # Only this invocation's fresh, bounded mkdtemp directory is removed.
            if temporary.exists():
                shutil.rmtree(temporary)
    data = checked.manifest
    strategy_path = destination.joinpath(*PurePosixPath(data["entrypoint"]).parts).parent
    return InstalledCandidate(destination, strategy_path, data["strategy_class"],
                              data["timeframe"], checked.sha256)
