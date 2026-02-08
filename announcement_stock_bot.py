from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Union

import requests
from catboxpy import CatboxClient

GOFILE_ENDPOINT = "https://upload.gofile.io/uploadfile"
DEFAULT_THRESHOLD_MB = 200.0

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileMetrics:
    filename: str
    total_lines: int
    valid_ulp: int
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)


@dataclass(frozen=True)
class UploadResult:
    host: str
    url: str
    success: bool
    error: Optional[str] = None


class GofileUploader:
    def __init__(self, endpoint: str = GOFILE_ENDPOINT) -> None:
        self.endpoint = endpoint
        self.guest_token: Optional[str] = None
        self.folder_id: Optional[str] = None

    def upload_once(self, file_path: Path) -> str:
        data: dict[str, str] = {}
        if self.guest_token:
            data["guestToken"] = self.guest_token
        if self.folder_id:
            data["folderId"] = self.folder_id

        with file_path.open("rb") as handle:
            response = requests.post(
                self.endpoint, data=data, files={"file": handle}
            )

        if not response.ok:
            raise RuntimeError(
                f"Gofile upload failed: {response.status_code} {response.text}"
            )

        payload = response.json()
        if payload.get("status") != "ok":
            raise RuntimeError(f"Gofile upload failed: {payload}")

        data_payload = payload.get("data") or {}
        guest_token = (
            data_payload.get("guestToken") or payload.get("guestToken")
        )
        folder_id = data_payload.get("folderId") or payload.get("folderId")
        if guest_token:
            self.guest_token = guest_token
        if folder_id:
            self.folder_id = folder_id

        url = (
            data_payload.get("downloadPage")
            or data_payload.get("downloadUrl")
            or data_payload.get("directLink")
        )
        if not url and data_payload.get("fileId"):
            url = f"https://gofile.io/d/{data_payload['fileId']}"
        if not url:
            raise RuntimeError("Gofile response missing download URL")
        return url


class AnnouncementUploader:
    def __init__(
        self,
        threshold_mb: float = DEFAULT_THRESHOLD_MB,
        catbox_userhash: Optional[str] = None,
    ) -> None:
        self.threshold_mb = threshold_mb
        self.catbox_client = CatboxClient(catbox_userhash)
        self.gofile_uploader = GofileUploader()

    def upload(self, file_path: Path, size_bytes: int) -> UploadResult:
        size_mb = size_bytes / (1024 * 1024)
        if size_mb <= self.threshold_mb:
            return self._upload_with_retry(
                host="Catbox",
                upload_fn=lambda: self.catbox_client.file_upload(
                    str(file_path)
                ),
            )
        return self._upload_with_retry(
            host="Gofile",
            upload_fn=lambda: self.gofile_uploader.upload_once(file_path),
        )

    def _upload_with_retry(self, host: str, upload_fn) -> UploadResult:
        try:
            url = upload_fn()
            return UploadResult(host=host, url=url, success=True)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("%s upload failed, retrying: %s", host, exc)
            try:
                url = upload_fn()
                return UploadResult(host=host, url=url, success=True)
            except Exception as retry_exc:  # noqa: BLE001
                error_text = str(retry_exc).replace("\n", " ").strip()
                return UploadResult(
                    host=host,
                    url=f"Upload failed: {error_text}",
                    success=False,
                    error=error_text,
                )


def generate_announcement(
    files: Union[Iterable[Union[str, Path]], str, Path],
    custom_header: Optional[str] = None,
    display_count: Optional[Union[int, str]] = None,
    threshold_mb: float = DEFAULT_THRESHOLD_MB,
    catbox_userhash: Optional[str] = None,
) -> list[str]:
    """
    Generate announcement messages for a list of files.

    Args:
        files: Iterable of file paths or a single file path.
        custom_header: Optional header override.
        display_count: Optional count for the original dataset scale.
        threshold_mb: Size threshold to route uploads to Catbox vs Gofile.
        catbox_userhash: Optional Catbox userhash for account uploads.
    """
    file_list = _normalize_files(files)
    uploader = AnnouncementUploader(
        threshold_mb=threshold_mb, catbox_userhash=catbox_userhash
    )
    messages: list[str] = []

    for file_path in file_list:
        metrics = _scan_file(file_path)
        upload_result = uploader.upload(file_path, metrics.size_bytes)
        header = _resolve_header(custom_header, display_count, metrics)
        messages.append(_build_message(header, metrics, upload_result))

    return messages


def _normalize_files(
    files: Union[Iterable[Union[str, Path]], str, Path]
) -> list[Path]:
    if isinstance(files, (str, Path)):
        file_iterable = [files]
    else:
        file_iterable = list(files)
    paths = [Path(item).expanduser() for item in file_iterable]
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if not path.is_file():
            raise ValueError(f"Not a file: {path}")
    return paths


def _scan_file(file_path: Path) -> FileMetrics:
    total_lines = 0
    valid_ulp = 0
    with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            total_lines += 1
            if ":" in line and "[NOT_SAVED]" not in line:
                valid_ulp += 1
    size_bytes = os.path.getsize(file_path)
    return FileMetrics(
        filename=file_path.name,
        total_lines=total_lines,
        valid_ulp=valid_ulp,
        size_bytes=size_bytes,
    )


def _resolve_header(
    custom_header: Optional[str],
    display_count: Optional[Union[int, str]],
    metrics: FileMetrics,
) -> str:
    if custom_header:
        return custom_header.replace("\n", " ").strip()
    if display_count is not None:
        display_text = _format_display_count(display_count)
        return (
            "Total lines on this are "
            f"{display_text}, but here is {metrics.total_lines:,}"
        )
    return "New Sample!"


def _format_display_count(display_count: Union[int, str]) -> str:
    if isinstance(display_count, int):
        return f"{display_count:,}"
    return str(display_count).strip()


def _build_message(
    header: str, metrics: FileMetrics, upload: UploadResult
) -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    success_text = "1/1" if upload.success else "0/1"
    return "\n".join(
        [
            header,
            f"File: {metrics.filename}",
            f"Valid ULP: {metrics.valid_ulp:,}",
            f"Valid Lines: {metrics.total_lines:,}",
            f"Size: {metrics.size_mb:.2f} MB",
            f"{upload.host}: {upload.url}",
            f"Success: {success_text}",
            f"Time: {timestamp}",
        ]
    )
