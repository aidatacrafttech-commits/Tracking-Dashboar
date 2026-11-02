from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Provider


class ProviderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_all(self) -> list[Provider]:
        return list(self.session.scalars(select(Provider).order_by(Provider.display_name)))

    def get_by_slug(self, slug: str) -> Provider | None:
        return self.session.scalar(select(Provider).where(Provider.slug == slug))
