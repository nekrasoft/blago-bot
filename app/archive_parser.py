from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .docx_parser import SUPPORTED_EXTENSIONS, extract_document_text

SUPPORTED_ARCHIVE_EXTENSIONS = {".rar"}
MAX_ARCHIVE_DOCUMENTS = 30


class ArchiveExtractionError(RuntimeError):
    """Raised when archive extraction fails."""


def extract_archive_document_texts(archive_path: Path) -> list[tuple[str, str]]:
    suffix = archive_path.suffix.lower()
    if suffix not in SUPPORTED_ARCHIVE_EXTENSIONS:
        raise ArchiveExtractionError(
            f"Unsupported archive extension: {suffix or '<none>'}. "
            f"Supported: {', '.join(sorted(SUPPORTED_ARCHIVE_EXTENSIONS))}"
        )

    with tempfile.TemporaryDirectory(prefix="archive_extract_") as tmp_dir_name:
        extract_dir = Path(tmp_dir_name)
        _extract_archive(archive_path=archive_path, output_dir=extract_dir)

        docs = sorted(
            path
            for path in extract_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        )

        if not docs:
            raise ArchiveExtractionError("Archive does not contain .doc/.docx files")

        extracted: list[tuple[str, str]] = []
        errors: list[str] = []

        for path in docs[:MAX_ARCHIVE_DOCUMENTS]:
            relative_name = path.relative_to(extract_dir).as_posix()
            try:
                text = extract_document_text(path)
                if text.strip():
                    extracted.append((relative_name, text))
                else:
                    errors.append(f"{relative_name}: empty text")
            except Exception as exc:
                errors.append(f"{relative_name}: {exc}")

        if not extracted:
            error_details = "; ".join(errors) if errors else "no readable documents"
            raise ArchiveExtractionError(
                "Archive documents could not be parsed. "
                f"Details: {error_details}"
            )

        return extracted


def _extract_archive(archive_path: Path, output_dir: Path) -> None:
    commands = _build_extraction_commands(archive_path=archive_path, output_dir=output_dir)
    if not commands:
        raise ArchiveExtractionError(
            "No archive extractor found. Install one of: unrar, 7z, bsdtar, unar"
        )

    errors: list[str] = []
    for command in commands:
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=120,
            )
            return
        except Exception as exc:
            errors.append(f"{' '.join(command[:2])}: {exc}")

    raise ArchiveExtractionError(
        "Failed to unpack archive. "
        "Make sure the .rar is not encrypted or corrupted. "
        f"Details: {'; '.join(errors)}"
    )


def _build_extraction_commands(archive_path: Path, output_dir: Path) -> list[list[str]]:
    commands: list[list[str]] = []

    unrar = shutil.which("unrar")
    if unrar:
        commands.append([unrar, "x", "-idq", "-o+", str(archive_path), str(output_dir)])

    seven_zip = shutil.which("7z") or shutil.which("7za")
    if seven_zip:
        commands.append([seven_zip, "x", "-y", "-bd", f"-o{output_dir}", str(archive_path)])

    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        commands.append([bsdtar, "-xf", str(archive_path), "-C", str(output_dir)])

    unar = shutil.which("unar")
    if unar:
        commands.append(
            [
                unar,
                "-quiet",
                "-force-overwrite",
                "-output-directory",
                str(output_dir),
                str(archive_path),
            ]
        )

    return commands
