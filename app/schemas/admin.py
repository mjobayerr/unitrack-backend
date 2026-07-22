import uuid

from pydantic import BaseModel, ConfigDict

from app.models.user import HelperStatus, UserStatus


class HelperOut(BaseModel):
    """A helper account as the admin panel sees it."""

    model_config = ConfigDict(from_attributes=True)

    helper_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    email: str
    phone: str | None
    helper_status: HelperStatus
    user_status: UserStatus
    approved_by: uuid.UUID | None
