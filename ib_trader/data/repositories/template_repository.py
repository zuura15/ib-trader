"""Repository for order templates."""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import scoped_session, Session

from ib_trader.data.models import OrderTemplate

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class OrderTemplateRepository:
    """SQLAlchemy repository for OrderTemplate persistence."""

    def __init__(self, session_factory: scoped_session) -> None:
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def create(self, template: OrderTemplate) -> OrderTemplate:
        """Insert a new template and return it."""
        s = self._session()
        s.add(template)
        s.commit()
        return template

    def get(self, template_id: str) -> OrderTemplate | None:
        """Return the template with the given ID, or None."""
        return (
            self._session()
            .query(OrderTemplate)
            .filter(OrderTemplate.id == template_id)
            .first()
        )

    def get_all(self) -> list[OrderTemplate]:
        """Return all templates ordered by label."""
        return (
            self._session()
            .query(OrderTemplate)
            .order_by(OrderTemplate.label)
            .all()
        )

    def delete(self, template_id: str) -> None:
        """Delete a template by ID."""
        s = self._session()
        t = s.query(OrderTemplate).filter(OrderTemplate.id == template_id).first()
        if t:
            s.delete(t)
            s.commit()
        else:
            logger.warning('{"event": "TEMPLATE_NOT_FOUND", "template_id": "%s"}',
                           template_id)
