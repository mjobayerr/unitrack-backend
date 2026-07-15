import enum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BusStatus(enum.StrEnum):
    active = "active"
    inactive = "inactive"
    maintenance = "maintenance"


class Bus(Base):
    __tablename__ = "buses"

    reg_no: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=40)
    status: Mapped[BusStatus] = mapped_column(
        SAEnum(BusStatus, name="bus_status"), nullable=False, default=BusStatus.active
    )
