from typing import Any

from fastapi import APIRouter, Depends

from app.schemas import AyudanteQuestionRequest
from app.security import AuthContext, require_tenant_read
from app.services import ayudante as ayudante_svc

router = APIRouter(prefix="/api/v1/ayudante", tags=["El Ayudante (Asesor Determinístico)"])


@router.get("")
async def get_briefing(
    ctx: AuthContext = Depends(require_tenant_read),
) -> dict[str, Any]:
    return await ayudante_svc.build_ayudante_briefing(ctx)


@router.post("/ask")
async def ask_ayudante(
    data: AyudanteQuestionRequest,
    ctx: AuthContext = Depends(require_tenant_read),
) -> dict[str, Any]:
    return await ayudante_svc.answer_ayudante_question(ctx, data.question)
