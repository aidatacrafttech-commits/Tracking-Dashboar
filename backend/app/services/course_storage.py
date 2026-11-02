"""Course file storage (S3). The browser uploads straight to the bucket with a presigned POST
whose policy pins the object key, content type and maximum size; the server then confirms
the object exists with the declared size and type. Downloads and playback use short-lived
presigned GET URLs. The bucket stays private; credentials never leave the server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.config import Settings
from app.models.enums import CourseAssetKind

logger = logging.getLogger(__name__)

MB = 1024 * 1024

# Allowed MIME types per kind, with the file extensions that may carry them.
ALLOWED_TYPES: dict[CourseAssetKind, dict[str, tuple[str, ...]]] = {
    CourseAssetKind.IMAGE: {
        "image/jpeg": (".jpg", ".jpeg"),
        "image/png": (".png",),
        "image/webp": (".webp",),
    },
    CourseAssetKind.VIDEO: {
        "video/mp4": (".mp4", ".m4v"),
        "video/webm": (".webm",),
        "video/quicktime": (".mov",),
    },
    CourseAssetKind.AUDIO: {
        "audio/mpeg": (".mp3",),
        "audio/mp4": (".m4a",),
        "audio/aac": (".aac",),
        "audio/ogg": (".ogg",),
        "audio/wav": (".wav",),
    },
    CourseAssetKind.SUBTITLE: {
        "text/vtt": (".vtt",),
        "application/x-subrip": (".srt",),
    },
    CourseAssetKind.DOCUMENT: {
        "application/pdf": (".pdf",),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (".docx",),
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": (".pptx",),
        "text/plain": (".txt",),
    },
}


@dataclass(frozen=True, slots=True)
class StoredObject:
    size_bytes: int
    content_type: str


class CourseStorage(Protocol):
    def presigned_upload(self, key: str, content_type: str, max_bytes: int) -> dict[str, Any]: ...

    def head(self, key: str) -> StoredObject | None: ...

    def signed_url(self, key: str, *, download_name: str | None = None) -> str: ...


class S3CourseStorage:
    def __init__(self, settings: Settings) -> None:
        import boto3  # noqa: PLC0415 - only when course storage is configured
        from botocore.config import Config  # noqa: PLC0415

        assert settings.course_assets_bucket  # noqa: S101 - checked by build_course_storage
        self.bucket = settings.course_assets_bucket
        self.ttl = settings.course_assets_url_ttl_seconds
        credentials: dict[str, str] = {}
        if settings.course_assets_access_key_id and settings.course_assets_secret_access_key:
            key_id = settings.course_assets_access_key_id.get_secret_value()
            secret = settings.course_assets_secret_access_key.get_secret_value()
            credentials = {"aws_access_key_id": key_id, "aws_secret_access_key": secret}
        self.client = boto3.client(
            "s3",
            region_name=settings.course_assets_region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
            **credentials,
        )

    def presigned_upload(self, key: str, content_type: str, max_bytes: int) -> dict[str, Any]:
        return self.client.generate_presigned_post(  # type: ignore[no-any-return]
            Bucket=self.bucket,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, max_bytes],
            ],
            ExpiresIn=self.ttl,
        )

    def head(self, key: str) -> StoredObject | None:
        from botocore.exceptions import ClientError  # noqa: PLC0415

        try:
            meta = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return StoredObject(int(meta["ContentLength"]), str(meta.get("ContentType", "")))

    def signed_url(self, key: str, *, download_name: str | None = None) -> str:
        params: dict[str, str] = {"Bucket": self.bucket, "Key": key}
        if download_name:
            safe = "".join(c for c in download_name if c.isalnum() or c in "._- ")[:120] or "file"
            params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
        return self.client.generate_presigned_url(  # type: ignore[no-any-return]
            "get_object", Params=params, ExpiresIn=self.ttl
        )


def build_course_storage(settings: Settings) -> CourseStorage | None:
    """None when no bucket is configured (uploads are then refused; video URLs still work)."""
    if not settings.course_assets_bucket:
        return None
    try:
        return S3CourseStorage(settings)
    except Exception:  # pragma: no cover - misconfiguration must not stop startup
        logger.exception("course storage could not be initialised")
        return None
