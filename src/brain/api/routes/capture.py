"""Paste-aware capture endpoint: parse a pasted text and create item(s).

POST /capture runs :func:`brain.services.paste.parse` on the payload, creates
one memory item per parsed chunk (whole thread by default, one per message
with ``split=true``), and returns the detected kind, the extraction summary,
and the created items. Detection never fails: unknown formats fall back to
plain notes.
"""

from fastapi import APIRouter, Depends, status

from brain import schemas
from brain.api.deps import get_ingest_service
from brain.services.ingest import IngestService
from brain.services.paste import parse as parse_paste

router = APIRouter(prefix="/capture", tags=["capture"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=schemas.CaptureResponse)
def capture_paste(
    data: schemas.CaptureRequest,
    service: IngestService = Depends(get_ingest_service),
) -> schemas.CaptureResponse:
    """Parse a pasted email/WhatsApp thread/calendar event/note and capture it.

    Items are created with ``source`` set to the detected kind and their
    ``occurred_at``/``meta`` filled from the extraction. Classification and
    indexing failures never block creation (same contract as POST /items).
    """
    parsed = parse_paste(data.text, kind_hint=data.kind_hint, split=data.split)
    items = [
        schemas.ItemRead.model_validate(
            service.create(
                schemas.ItemCreate(
                    raw_text=chunk.raw_text,
                    source=parsed.kind,
                    occurred_at=chunk.occurred_at,
                    meta=chunk.meta,
                )
            )
        )
        for chunk in parsed.chunks
    ]
    return schemas.CaptureResponse(kind=parsed.kind, extracted=parsed.extracted, items=items)
