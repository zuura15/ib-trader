"""Order template CRUD endpoints.

GET /api/templates — list all templates
POST /api/templates — create a new template
DELETE /api/templates/{template_id} — delete a template
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ib_trader.api.deps import get_session_factory
from ib_trader.api.serializers import TemplateRequest, TemplateResponse
from ib_trader.data.models import OrderTemplate
from ib_trader.data.repositories.template_repository import OrderTemplateRepository

router = APIRouter(prefix="/api/templates", tags=["templates"])


def _now():
    return datetime.now(timezone.utc)


def _serialize_template(t) -> TemplateResponse:
    return TemplateResponse(
        id=t.id,
        label=t.label,
        symbol=t.symbol,
        side=t.side,
        quantity=str(t.quantity),
        order_type=t.order_type,
        price=str(t.price) if t.price is not None else None,
        broker=t.broker,
    )


@router.get("", response_model=list[TemplateResponse])
def list_templates(sf=Depends(get_session_factory)):
    repo = OrderTemplateRepository(sf)
    return [_serialize_template(t) for t in repo.get_all()]


@router.post("", response_model=TemplateResponse, status_code=201)
def create_template(body: TemplateRequest, sf=Depends(get_session_factory)):
    from decimal import Decimal
    repo = OrderTemplateRepository(sf)
    now = _now()
    template = OrderTemplate(
        label=body.label,
        symbol=body.symbol.upper(),
        side=body.side.upper(),
        quantity=Decimal(body.quantity),
        order_type=body.order_type.upper(),
        price=Decimal(body.price) if body.price else None,
        broker=body.broker,
        created_at=now,
        updated_at=now,
    )
    repo.create(template)
    return _serialize_template(template)


@router.delete("/{template_id}", status_code=204)
def delete_template(template_id: str, sf=Depends(get_session_factory)):
    repo = OrderTemplateRepository(sf)
    t = repo.get(template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found")
    repo.delete(template_id)
