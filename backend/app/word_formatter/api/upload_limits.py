"""Upload and text-size guards for word formatter endpoints."""

from fastapi import HTTPException, UploadFile, status

from app.config import settings

READ_CHUNK_SIZE = 1024 * 1024


def max_upload_bytes() -> int:
    max_size_mb = max(settings.MAX_UPLOAD_FILE_SIZE_MB, 1)
    return max_size_mb * 1024 * 1024


async def read_limited_upload(file: UploadFile) -> bytes:
    limit = max_upload_bytes()
    size = 0
    chunks = []

    content_length = file.headers.get("content-length") if file.headers else None
    if content_length:
        try:
            if int(content_length) > limit:
                raise_upload_too_large()
        except ValueError:
            pass

    while True:
        chunk = await file.read(READ_CHUNK_SIZE)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise_upload_too_large()
        chunks.append(chunk)

    return b"".join(chunks)


def validate_text_size(text: str) -> None:
    max_chars = max(settings.MAX_TEXT_INPUT_CHARS, 1)
    if len(text or "") > max_chars:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Text exceeds the limit of {max_chars} characters",
        )


def raise_upload_too_large() -> None:
    raise HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail=f"File exceeds the limit of {settings.MAX_UPLOAD_FILE_SIZE_MB} MB",
    )
