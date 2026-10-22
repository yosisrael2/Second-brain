"""Paste-aware capture endpoint: parse a pasted text and create item(s).

POST /capture runs :func:`brain.services.paste.parse` on the payload, creates
one memory item per parsed chunk (whole thread by default, one per message
with ``split=true``), and returns the detected kind, the extraction summary,
and the created items. Detection never fails: unknown formats fall back to
plain notes. Zone-less times are interpreted in the ``TIMEZONE`` setting and
stored as aware UTC; explicit offsets are honored as given.
"""

from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, status

from brain import schemas
from brain.api.deps import get_ingest_service
from brain.config import get_settings
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
    ``occurred_at``/``meta`` filled from the extraction (aware UTC instants;
    raw source strings kept in ``meta``). Classification and indexing
    failures never block creation (same contract as POST /items).
    """
    zone = ZoneInfo(get_settings().timezone)
    parsed = parse_paste(data.text, kind_hint=data.kind_hint, split=data.split, tz=zone)
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
