"""Editable prompts + the user memory file (thin router over PromptStore).

Every LLM system prompt is registered in brain.services.prompts with its
code default; an edit appends a version row (history kept, never
rewritten), reset appends a back-to-default row, restore copies an older
version forward. Lookups at use time fall back to the code default.

GET  /prompts                          every key: overridden?, version, updated_at
GET  /prompts/memory-file              the memory file (same shape as one prompt)
PUT  /prompts/memory-file              save the memory file (<= 8000 chars; '' = none)
GET  /prompts/{key}                    current + default text, placeholders, history
PUT  /prompts/{key}                    save a new version (validated)
POST /prompts/{key}/reset              back to the code default
POST /prompts/{key}/restore/{version}  copy an older version forward

Error mapping: unknown key / version -> 404; a text that breaks the
template contract (placeholders dropped or added, malformed braces, over
the length limit) -> 422 with a readable ``detail``; a concurrent save of
the same key -> 409.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from brain import schemas
from brain.db.session import get_db
from brain.services.prompts import (
    MEMORY_FILE_KEY,
    PromptConflictError,
    PromptStore,
    PromptValidationError,
    get_spec,
)

router = APIRouter(prefix="/prompts", tags=["prompts"])


def _store(db: Session = Depends(get_db)) -> PromptStore:
    return PromptStore(db)


def _known(key: str) -> str:
    try:
        get_spec(key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown prompt '{key}'") from None
    return key


def _detail(store: PromptStore, key: str) -> schemas.PromptDetail:
    return schemas.PromptDetail.model_validate(store.describe(key))


def _save(store: PromptStore, key: str, text: str) -> schemas.PromptDetail:
    try:
        store.save(key, text)
    except PromptValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except PromptConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _detail(store, key)


@router.get("", response_model=schemas.PromptList)
def list_prompts(store: PromptStore = Depends(_store)) -> schemas.PromptList:
    """Every registered key (prompts first, then the memory file)."""
    return schemas.PromptList(
        prompts=[schemas.PromptSummary.model_validate(row) for row in store.summaries()]
    )


@router.get("/memory-file", response_model=schemas.PromptDetail)
def get_memory_file(store: PromptStore = Depends(_store)) -> schemas.PromptDetail:
    """The user memory file (text '' = empty, nothing added to prompts)."""
    return _detail(store, MEMORY_FILE_KEY)


@router.put("/memory-file", response_model=schemas.PromptDetail)
def put_memory_file(
    data: schemas.PromptTextUpdate, store: PromptStore = Depends(_store)
) -> schemas.PromptDetail:
    """Save the memory file; 422 over the length limit."""
    return _save(store, MEMORY_FILE_KEY, data.text)


@router.get("/{key}", response_model=schemas.PromptDetail)
def get_prompt(key: str, store: PromptStore = Depends(_store)) -> schemas.PromptDetail:
    """One key: current text, default text, placeholders, history (newest first)."""
    return _detail(store, _known(key))


@router.put("/{key}", response_model=schemas.PromptDetail)
def put_prompt(
    key: str, data: schemas.PromptTextUpdate, store: PromptStore = Depends(_store)
) -> schemas.PromptDetail:
    """Save a new version; 422 when the text breaks the placeholder contract.

    Saving the current text again is a no-op (no new version); saving the
    exact code default is recorded as a reset.
    """
    return _save(store, _known(key), data.text)


@router.post("/{key}/reset", response_model=schemas.PromptDetail)
def reset_prompt(key: str, store: PromptStore = Depends(_store)) -> schemas.PromptDetail:
    """Back to the code default (no-op when already there); history kept."""
    try:
        store.reset(_known(key))
    except PromptConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _detail(store, key)


@router.post("/{key}/restore/{version}", response_model=schemas.PromptDetail)
def restore_prompt(
    key: str, version: int, store: PromptStore = Depends(_store)
) -> schemas.PromptDetail:
    """Make an older version current again (copied forward as a new version)."""
    try:
        store.restore(_known(key), version)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except PromptValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except PromptConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _detail(store, key)
