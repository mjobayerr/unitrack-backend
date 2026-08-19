import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import UserRole, UserStatus


class StudentRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    student_id_no: str = Field(min_length=1, max_length=64)
    department: str | None = None
    batch: str | None = None
    phone: str | None = None


class HelperRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    phone: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    """The refresh token to kill. The access token comes from the Bearer header.

    Optional because a client that has already lost its refresh token should
    still be able to end the session it can prove it holds.
    """

    refresh_token: str | None = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    # Seconds until `access_token` expires. Present so the response matches the
    # OAuth 2.0 token-response shape (RFC 6749 §5.1) — clients that already
    # speak OAuth need no special-casing, and ours stops hardcoding 15 minutes.
    expires_in: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    name: str
    role: UserRole
    status: UserStatus


class StudentProfileOut(BaseModel):
    """The student-specific half of a profile, collected at registration.

    Only present for student accounts; a helper or admin has no such row.
    """

    model_config = ConfigDict(from_attributes=True)

    student_id_no: str
    department: str | None = None
    batch: str | None = None


class MeOut(BaseModel):
    """The signed-in account, with everything registration captured.

    A superset of `UserOut` — it adds `phone` and, for students, the nested
    profile — so any client reading the old fields is unaffected. `student` is
    null for non-students rather than the fields being absent, which keeps the
    shape stable regardless of role.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    name: str
    role: UserRole
    status: UserStatus
    phone: str | None = None
    student: StudentProfileOut | None = None


class ResendVerification(BaseModel):
    """Ask for the confirmation link again.

    Only an address. The account cannot be logged into yet — that is the whole
    problem it solves — so there is no session to authenticate this with.
    """

    email: EmailStr


class ForgotPassword(BaseModel):
    """Ask for a password-reset link.

    Only an address, because the caller has by definition lost the credential
    that would authenticate anything more.
    """

    email: EmailStr


class ResetPassword(BaseModel):
    """Set a new password using the token from the emailed link.

    `token` is the whole authentication — holding it proves control of the
    mailbox. `password` mirrors the registration constraints so the rules a
    student met at signup are the rules they meet here.
    """

    token: str
    password: str = Field(min_length=8, max_length=128)


class ProfileUpdate(BaseModel):
    """Fields a signed-in user may change about themselves.

    Only what the backend actually stores and a person owns: their display name
    and phone. Email is the login identity and is left out on purpose — changing
    it is a re-verification flow, not a text edit. `phone` distinguishes "clear
    it" (explicit null) from "leave it" (absent) via `model_fields_set`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
