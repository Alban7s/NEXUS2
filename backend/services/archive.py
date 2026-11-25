"""
Safe handling of uploaded ZIP archives.

Why this module exists
----------------------
An uploaded ZIP is *untrusted input*. Before a single byte is written to disk
we validate the archive's table of contents, and while extracting we count the
bytes that are really written (a malicious ZIP can lie about sizes in its
headers).

Threats covered
---------------
* Path traversal ("zip-slip")        ``../../etc/passwd``, absolute paths, ``C:\\x``
* Symlink / special-file attacks     symlink, device, FIFO entries
* ZIP bombs                          compression ratio, total size, per-file size
* Resource exhaustion                file-count and directory-depth limits
* Dangerous file names               NUL/control characters, Windows reserved
                                     names, over-long names, duplicate names
* Secrets in uploads                 .env, private keys, .npmrc, ...
* Source-control / nested archives   .git, nested .zip
* Encrypted entries                  cannot be inspected -> rejected

Nothing in here executes uploaded code.
"""
from __future__ import annotations

import json
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import settings
from ..errors import ArchiveError

# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

SOURCE_CONTROL_DIRECTORIES = {".git", ".svn", ".hg"}
# Skipped (not extracted, not an error): junk that is common in student ZIPs.
SKIP_DIRECTORIES = {"__macosx", "node_modules"}

SENSITIVE_FILE_NAMES = {
    ".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", "credential", ".npmrc", ".pypirc", ".netrc", ".htpasswd",
}
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")

WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"|?*]')
LIFECYCLE_SCRIPTS = ("preinstall", "install", "postinstall", "prepare")
MAX_PACKAGE_JSON_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 25
MAX_NAME_LENGTH = 255
MAX_PATH_LENGTH = 1024
# Ratio check only applies to files bigger than this (tiny files compress oddly).
RATIO_MIN_BYTES = 1 * 1024 * 1024


@dataclass(frozen=True)
class ArchiveLimits:
    max_upload_bytes: int = settings.max_upload_bytes
    max_uncompressed_bytes: int = settings.max_uncompressed_mb * 1024 * 1024
    max_single_file_bytes: int = settings.max_single_file_mb * 1024 * 1024
    max_files: int = settings.max_files
    max_ratio: int = settings.max_compression_ratio


@dataclass
class ArchiveReport:
    root_folder: str | None
    file_count: int
    total_uncompressed: int
    entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = field(repr=False)
    skipped: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Naming helpers
# --------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Turn arbitrary input into a DNS-1123 / Docker-tag safe slug."""
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return (s or "site")[:50]


def _member_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    """Return normalised path parts for an entry or raise ArchiveError."""
    raw = info.filename
    if "\x00" in raw:
        raise ArchiveError("Archive contains a file name with a NUL byte.", code="bad_name")
    raw = raw.replace("\\", "/")

    if len(raw) > MAX_PATH_LENGTH:
        raise ArchiveError("Archive contains an over-long file path.", code="bad_name")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or raw.startswith("//"):
        raise ArchiveError(
            f"Unsafe archive path '{raw[:80]}': absolute paths are not allowed.",
            code="path_traversal",
        )

    parts = tuple(p for p in PurePosixPath(raw).parts if p not in ("", "."))
    if not parts:
        raise ArchiveError("Archive contains an empty file name.", code="bad_name")
    if ".." in parts:
        raise ArchiveError(
            f"Unsafe archive path '{raw[:80]}': '..' segments are not allowed (path traversal).",
            code="path_traversal",
        )
    if len(parts) > MAX_DEPTH:
        raise ArchiveError("Archive directory nesting is too deep.", code="too_deep")

    for part in parts:
        if len(part) > MAX_NAME_LENGTH:
            raise ArchiveError("Archive contains an over-long file name.", code="bad_name")
        if _BAD_CHARS.search(part):
            raise ArchiveError(
                f"Archive contains a dangerous file name: '{part[:60]!r}'.", code="bad_name"
            )
        stem = part.split(".", 1)[0].lower()
        if stem in WINDOWS_RESERVED:
            raise ArchiveError(
                f"Archive contains a reserved system file name: '{part}'.", code="bad_name"
            )
        if part.endswith((" ", ".")) and part not in ("..",):
            raise ArchiveError(
                f"Archive contains a file name ending in space or dot: '{part}'.", code="bad_name"
            )
    return parts


def _policy_violation(parts: tuple[str, ...], info: zipfile.ZipInfo) -> str | None:
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        return "symbolic links are not allowed"
    kind = stat.S_IFMT(mode)
    if kind and kind not in (stat.S_IFREG, stat.S_IFDIR):
        return "special files (devices, pipes, sockets) are not allowed"
    if info.flag_bits & 0x1:
        return "encrypted entries cannot be inspected"

    lower = tuple(p.lower() for p in parts)
    name = lower[-1]
    if any(p in SOURCE_CONTROL_DIRECTORIES for p in lower):
        return "source-control metadata is not allowed"
    if info.is_dir():
        return None
    if name in SENSITIVE_FILE_NAMES or name.startswith(".env."):
        return "environment or credential files are not allowed"
    if name.endswith(SENSITIVE_SUFFIXES) or "service-account" in name:
        return "private key or service-account files are not allowed"
    if name.endswith((".zip", ".jar", ".war", ".7z", ".rar", ".tar", ".gz", ".tgz")):
        return "nested archives are not allowed"
    return None


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_archive(zip_path: Path, limits: ArchiveLimits | None = None) -> ArchiveReport:
    """Validate an archive's table of contents. Writes nothing to disk."""
    limits = limits or ArchiveLimits()
    zip_path = Path(zip_path)

    try:
        size = zip_path.stat().st_size
    except OSError as exc:
        raise ArchiveError("Uploaded file could not be read.", code="unreadable") from exc
    if size == 0:
        raise ArchiveError("The uploaded file is empty (0 bytes).", code="empty")
    if size > limits.max_upload_bytes:
        raise ArchiveError(
            f"Upload is {size // (1024 * 1024)} MB; the limit is "
            f"{limits.max_upload_bytes // (1024 * 1024)} MB.",
            code="too_large",
        )

    entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    skipped: list[str] = []
    seen: set[str] = set()
    total = 0
    files = 0

    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > limits.max_files:
                raise ArchiveError(
                    f"Archive has too many entries ({len(infos)} > {limits.max_files}).",
                    code="too_many_files",
                )
            for info in infos:
                parts = _member_parts(info)
                reason = _policy_violation(parts, info)
                if reason:
                    raise ArchiveError(
                        f"Upload rejected by the input policy. Blocked: "
                        f"'{'/'.join(parts)[:120]}'. Reason: {reason}.",
                        code="policy",
                    )

                key = "/".join(parts).lower()
                if key in seen:
                    raise ArchiveError(
                        f"Archive contains duplicate entries for '{'/'.join(parts)[:120]}'.",
                        code="duplicate",
                    )
                seen.add(key)

                if info.is_dir():
                    entries.append((info, parts))
                    continue

                files += 1
                if info.file_size > limits.max_single_file_bytes:
                    raise ArchiveError(
                        f"File '{'/'.join(parts)[:80]}' is larger than the "
                        f"{limits.max_single_file_bytes // (1024 * 1024)} MB per-file limit.",
                        code="file_too_large",
                    )
                if (
                    info.file_size > RATIO_MIN_BYTES
                    and info.compress_size > 0
                    and info.file_size / info.compress_size > limits.max_ratio
                ):
                    raise ArchiveError(
                        f"File '{'/'.join(parts)[:80]}' has a suspicious compression ratio "
                        f"(possible ZIP bomb).",
                        code="zip_bomb",
                    )

                total += info.file_size
                if total > limits.max_uncompressed_bytes:
                    raise ArchiveError(
                        f"Archive expands to more than "
                        f"{limits.max_uncompressed_bytes // (1024 * 1024)} MB (possible ZIP bomb).",
                        code="zip_bomb",
                    )
                entries.append((info, parts))
    except ArchiveError:
        raise
    except zipfile.BadZipFile as exc:
        raise ArchiveError(
            "Upload is not a valid ZIP archive (it may be corrupt or truncated).",
            code="corrupt",
        ) from exc
    except (zipfile.LargeZipFile, NotImplementedError, RuntimeError) as exc:
        raise ArchiveError(f"Archive cannot be processed: {exc}", code="unsupported") from exc

    if files == 0:
        raise ArchiveError("The ZIP archive contains no files.", code="empty")

    # Skipped directories (node_modules, __MACOSX) are not extracted.
    kept: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    for info, parts in entries:
        if any(p.lower() in SKIP_DIRECTORIES for p in parts):
            if parts[0].lower() not in skipped and len(skipped) < 5:
                skipped.append(parts[0] if parts[0].lower() in SKIP_DIRECTORIES else "/".join(parts[:2]))
            continue
        kept.append((info, parts))

    kept_files = sum(1 for info, _ in kept if not info.is_dir())
    if kept_files == 0:
        raise ArchiveError(
            "The ZIP contains no usable files (only node_modules / system folders).",
            code="empty",
        )

    return ArchiveReport(
        root_folder=_archive_root(kept),
        file_count=kept_files,
        total_uncompressed=total,
        entries=kept,
        skipped=skipped,
    )


def _archive_root(entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]]) -> str | None:
    files = [parts for info, parts in entries if not info.is_dir()]
    if not files:
        return None
    first = files[0][0]
    if all(len(p) > 1 and p[0] == first for p in files):
        return first
    return None


# --------------------------------------------------------------------------
# Inspection (reads only package.json; nothing is extracted)
# --------------------------------------------------------------------------

FRAMEWORK_HINTS = [
    ("next", "Next.js"),
    ("vite", "Vite"),
    ("react-scripts", "Create React App"),
    ("react", "React"),
    ("vue", "Vue"),
    ("@angular/core", "Angular"),
    ("svelte", "Svelte"),
    ("astro", "Astro"),
]


def summarize_package(pkg: dict[str, Any]) -> dict[str, Any]:
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    scripts = pkg.get("scripts") or {}
    framework = next((label for key, label in FRAMEWORK_HINTS if key in deps), "Node.js")
    if framework == "Vite" and "react" in deps:
        framework = "React (Vite)"
    test_script = str(scripts.get("test", ""))
    has_real_test = bool(test_script) and "no test specified" not in test_script
    lifecycle = [s for s in LIFECYCLE_SCRIPTS if s in scripts]
    return {
        "framework": framework,
        "type": "node",
        "kind": "Node frontend",
        "hasBuildScript": "build" in scripts,
        "hasTestScript": has_real_test,
        "lifecycleScripts": lifecycle,
    }


def inspect_archive(zip_path: Path, original_filename: str,
                    limits: ArchiveLimits | None = None) -> dict[str, Any]:
    """Validate and return a safe, content-free summary of the project."""
    report = validate_archive(zip_path, limits)
    root = report.root_folder

    package_entry: zipfile.ZipInfo | None = None
    has_index = False
    for info, parts in report.entries:
        if info.is_dir():
            continue
        rel = parts[1:] if root and parts[0] == root else parts
        if rel == ("package.json",):
            package_entry = info
        elif rel == ("index.html",):
            has_index = True

    package: dict[str, Any] | None = None
    if package_entry is not None:
        if package_entry.file_size > MAX_PACKAGE_JSON_BYTES:
            raise ArchiveError("package.json is too large to inspect safely.", code="bad_package")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                package = json.loads(zf.read(package_entry).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArchiveError("package.json is not valid UTF-8 JSON.", code="bad_package") from exc
        if not isinstance(package, dict):
            raise ArchiveError("package.json must contain a JSON object.", code="bad_package")
        summary = summarize_package(package)
    elif has_index:
        summary = {
            "framework": "Static HTML/CSS/JS", "type": "static", "kind": "Static website",
            "hasBuildScript": False, "hasTestScript": False, "lifecycleScripts": [],
        }
    else:
        raise ArchiveError(
            "Missing expected application files: NEXUS needs a root index.html (static "
            "site) or a package.json (Node/React/Vite project).",
            code="no_project",
        )

    suggested = slugify(str((package or {}).get("name") or root or Path(original_filename).stem))
    return {
        "suggestedName": suggested,
        "fileCount": report.file_count,
        "uncompressedBytes": report.total_uncompressed,
        "skipped": report.skipped,
        **summary,
    }


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def safe_extract(zip_path: Path, dest: Path, limits: ArchiveLimits | None = None) -> Path:
    """Extract a validated archive into `dest` and return the project root.

    Bytes are counted as they are written, so a lying header cannot exceed the
    limits. Files are created with default permissions (no executable bits, no
    ownership information from the archive).
    """
    limits = limits or ArchiveLimits()
    report = validate_archive(zip_path, limits)

    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    written_total = 0

    with zipfile.ZipFile(zip_path) as zf:
        for info, parts in report.entries:
            target = (dest_resolved.joinpath(*parts)).resolve()
            # Defence in depth: the resolved target must stay inside dest.
            if dest_resolved != target and dest_resolved not in target.parents:
                raise ArchiveError("Unsafe archive path detected during extraction.",
                                   code="path_traversal")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with zf.open(info) as src, open(target, "xb") as out:
                while chunk := src.read(64 * 1024):
                    written += len(chunk)
                    written_total += len(chunk)
                    if written > limits.max_single_file_bytes or written_total > limits.max_uncompressed_bytes:
                        out.close()
                        raise ArchiveError(
                            "Archive expanded beyond the allowed size while extracting "
                            "(possible ZIP bomb).", code="zip_bomb",
                        )
                    out.write(chunk)

    return find_project_root(dest)


def find_project_root(root: Path) -> Path:
    """Find the project root, handling a single wrapping folder."""
    current = root
    for _ in range(4):
        entries = [p for p in current.iterdir() if not p.name.lower().startswith("__macosx")]
        has_marker = (current / "index.html").exists() or (current / "package.json").exists()
        if has_marker or len(entries) != 1 or not entries[0].is_dir():
            return current
        current = entries[0]
    return current


def scan_extracted_tree(root: Path) -> None:
    """Defence in depth after extraction: no symlinks / unsafe names remain."""
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ArchiveError("Extracted tree contains a symbolic link.", code="policy")
        rel = path.relative_to(root).parts
        synthetic = zipfile.ZipInfo("/".join(rel) + ("/" if path.is_dir() else ""))
        reason = _policy_violation(tuple(rel), synthetic)
        if reason:
            raise ArchiveError(f"Extracted tree rejected: {reason}.", code="policy")
