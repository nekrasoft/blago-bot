from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .docx_parser import SUPPORTED_EXTENSIONS, extract_document_text

SUPPORTED_ARCHIVE_EXTENSIONS = {".rar", ".zip"}
SUPPORTED_ARCHIVE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_ARCHIVE_DOCUMENTS = 30
MAX_ARCHIVE_IMAGES = 30
MAX_ARCHIVE_IMAGE_BYTES = 20 * 1024 * 1024


class ArchiveExtractionError(RuntimeError):
    """Raised when archive extraction fails."""


@dataclass(frozen=True)
class ArchiveImage:
    relative_name: str
    file_name: str
    data: bytes


@dataclass(frozen=True)
class ArchiveContents:
    documents: list[tuple[str, str]]
    images: list[ArchiveImage]
    skipped_images_count: int = 0


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
            raise ArchiveExtractionError(
                "Archive does not contain supported files "
                f"({', '.join(sorted(SUPPORTED_EXTENSIONS))})"
            )

        extracted, errors = _extract_document_texts_from_paths(
            docs[:MAX_ARCHIVE_DOCUMENTS],
            extract_dir=extract_dir,
        )

        if not extracted:
            error_details = "; ".join(errors) if errors else "no readable documents"
            raise ArchiveExtractionError(
                "Archive documents could not be parsed. "
                f"Details: {error_details}"
            )

        return extracted


def extract_archive_contents(archive_path: Path) -> ArchiveContents:
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
        image_paths = sorted(
            path
            for path in extract_dir.rglob("*")
            if (
                path.is_file()
                and path.suffix.lower() in SUPPORTED_ARCHIVE_IMAGE_EXTENSIONS
            )
        )

        if not docs and not image_paths:
            supported_files = sorted(
                SUPPORTED_EXTENSIONS | SUPPORTED_ARCHIVE_IMAGE_EXTENSIONS
            )
            raise ArchiveExtractionError(
                "Archive does not contain supported files "
                f"({', '.join(supported_files)})"
            )

        documents, document_errors = _extract_document_texts_from_paths(
            docs[:MAX_ARCHIVE_DOCUMENTS],
            extract_dir=extract_dir,
        )
        images, image_errors = _read_archive_images(
            image_paths[:MAX_ARCHIVE_IMAGES],
            extract_dir=extract_dir,
        )
        skipped_images_count = (
            max(0, len(image_paths) - MAX_ARCHIVE_IMAGES)
            + len(image_errors)
        )

        if not documents and not images:
            errors = document_errors + image_errors
            error_details = "; ".join(errors) if errors else "no readable files"
            raise ArchiveExtractionError(
                "Archive files could not be parsed. "
                f"Details: {error_details}"
            )

        return ArchiveContents(
            documents=documents,
            images=images,
            skipped_images_count=skipped_images_count,
        )


def _extract_document_texts_from_paths(
    docs: list[Path],
    extract_dir: Path,
) -> tuple[list[tuple[str, str]], list[str]]:
    extracted: list[tuple[str, str]] = []
    errors: list[str] = []

    for path in docs:
        relative_name = path.relative_to(extract_dir).as_posix()
        try:
            text = extract_document_text(path)
            if text.strip():
                extracted.append((relative_name, text))
            else:
                errors.append(f"{relative_name}: empty text")
        except Exception as exc:
            errors.append(f"{relative_name}: {exc}")

    return extracted, errors


def _read_archive_images(
    image_paths: list[Path],
    extract_dir: Path,
) -> tuple[list[ArchiveImage], list[str]]:
    images: list[ArchiveImage] = []
    errors: list[str] = []

    for path in image_paths:
        relative_name = path.relative_to(extract_dir).as_posix()
        try:
            if path.stat().st_size > MAX_ARCHIVE_IMAGE_BYTES:
                errors.append(f"{relative_name}: image is too large")
                continue
            images.append(
                ArchiveImage(
                    relative_name=relative_name,
                    file_name=path.name,
                    data=path.read_bytes(),
                )
            )
        except Exception as exc:
            errors.append(f"{relative_name}: {exc}")

    return images, errors


def _extract_archive(archive_path: Path, output_dir: Path) -> None:
    errors: list[str] = []

    if archive_path.suffix.lower() == ".zip":
        try:
            _extract_zip_via_python(archive_path=archive_path, output_dir=output_dir)
            return
        except Exception as exc:
            errors.append(f"zipfile: {exc}")

    commands = _build_extraction_commands(archive_path=archive_path, output_dir=output_dir)
    if not commands:
        if errors:
            raise ArchiveExtractionError(
                f"Не удалось распаковать архив. Подробности: {'; '.join(errors)}"
            )
        raise ArchiveExtractionError(
            "No archive extractor found. Install one of: unrar, 7z, bsdtar, unar"
        )

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
        "Make sure the archive is not encrypted or corrupted. "
        f"Details: {'; '.join(errors)}"
    )


def _extract_zip_via_python(archive_path: Path, output_dir: Path) -> None:
    with zipfile.ZipFile(str(archive_path), "r") as zf:
        output_root = output_dir.resolve()
        for member in zf.infolist():
            target_path = (output_dir / member.filename).resolve()
            try:
                target_path.relative_to(output_root)
            except ValueError as exc:
                raise ArchiveExtractionError(
                    f"Unsafe path in zip archive: {member.filename}"
                ) from exc
        zf.extractall(str(output_dir))


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
