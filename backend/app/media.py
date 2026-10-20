"""Range-aware audio serving.

Browsers need HTTP Range support to seek inside a recording. Starlette's
FileResponse does not guarantee it across versions, so this is explicit — and
it matters here: "jump to the timestamp" is the core interaction.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _media_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def audio_response(request: Request, path: Path, *, download_name: str | None = None) -> Response:
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audio file not found")

    file_size = path.stat().st_size
    media_type = _media_type(path)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=0, must-revalidate",
        "X-Content-Type-Options": "nosniff",
    }
    if download_name:
        headers["Content-Disposition"] = f'inline; filename="{download_name}"'

    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=media_type, headers=headers)

    match = _RANGE_RE.match(range_header.strip())
    if not match:
        return FileResponse(path, media_type=media_type, headers=headers)

    start_raw, end_raw = match.groups()
    if start_raw == "" and end_raw == "":
        return FileResponse(path, media_type=media_type, headers=headers)

    if start_raw == "":
        # suffix range: last N bytes
        length = int(end_raw)
        start = max(file_size - length, 0)
        end = file_size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else file_size - 1

    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        raise HTTPException(
            status_code=416,
            detail="requested range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    chunk_size = end - start + 1

    def iterator():
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = chunk_size
            while remaining > 0:
                data = fh.read(min(1 << 20, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers.update(
        {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(chunk_size),
        }
    )
    return StreamingResponse(iterator(), status_code=206, media_type=media_type, headers=headers)
