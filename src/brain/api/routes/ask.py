"""Ask endpoint (thin router over AskService)."""

from fastapi import APIRouter, Depends

from brain import schemas
from brain.api.deps import get_ask_service
from brain.api.routes.search import to_result_read
from brain.services.ask import AskService

router = APIRouter(tags=["ask"])


@router.post("/ask", response_model=schemas.AskAnswer)
def ask(
    data: schemas.AskQuestion,
    service: AskService = Depends(get_ask_service),
) -> schemas.AskAnswer:
    """Answer a question from the most relevant memory items, with citations."""
    result = service.ask(data.question)
    sources = [to_result_read(hit) for hit in result.sources]
    return schemas.AskAnswer(answer=result.answer, cited_ids=result.cited_ids, sources=sources)
