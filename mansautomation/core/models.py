"""Domain models for profiles, automation jobs, and workflow events."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def generate_id() -> str:
    """Public helper for callers that need to mint domain identifiers."""

    return _new_id()


class FieldKey(StrEnum):
    """Canonical field identifiers used by the autofill engine."""

    FULL_NAME = "full_name"
    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    EMAIL = "email"
    PHONE = "phone"
    PASSWORD = "password"
    LOGIN_EMAIL = "login_email"
    ADDRESS_LINE1 = "address_line1"
    ADDRESS_LINE2 = "address_line2"
    CITY = "city"
    STATE = "state"
    POSTAL_CODE = "postal_code"
    COUNTRY = "country"
    DATE_OF_BIRTH = "date_of_birth"
    GENDER = "gender"
    BANK_ACCOUNT = "bank_account"
    BANK_NAME = "bank_name"
    BANK_ROUTING = "bank_routing"
    ID_NUMBER = "id_number"
    COMPANY = "company"
    NOTES = "notes"
    CUSTOM = "custom"


class Address(BaseModel):
    line1: str = ""
    line2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = ""


class BankInfo(BaseModel):
    account_holder: str = ""
    bank_name: str = ""
    account_number: SecretStr | None = None
    routing_number: SecretStr | None = None
    iban: SecretStr | None = None
    swift: str = ""


class LoginCredentials(BaseModel):
    """Reusable site credentials stored encrypted at rest."""

    model_config = ConfigDict(extra="ignore")

    email: str = ""
    password: SecretStr | None = None
    site: str = ""

    def password_value(self) -> str | None:
        return self.password.get_secret_value() if self.password else None


class Attendee(BaseModel):
    """Represents a single attendee/ticket holder for events with multiple seats."""

    model_config = ConfigDict(extra="ignore")

    full_name: str
    email: EmailStr | None = None
    phone: str | None = None
    date_of_birth: str | None = None
    gender: str | None = None
    id_number: str | None = None
    custom: dict[str, str] = Field(default_factory=dict)


class Profile(BaseModel):
    """A reusable user profile / dataset entry used by autofill."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=_new_id)
    name: str
    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    email: EmailStr | None = None
    phone: str = ""
    date_of_birth: str | None = None
    gender: str | None = None
    company: str = ""
    notes: str = ""
    address: Address = Field(default_factory=Address)
    bank: BankInfo = Field(default_factory=BankInfo)
    login: LoginCredentials = Field(default_factory=LoginCredentials)
    attendees: list[Attendee] = Field(default_factory=list)
    custom_fields: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def touch(self) -> None:
        self.updated_at = _utcnow()

    def value_for(self, key: FieldKey) -> str | None:
        """Return the stringified value associated with a canonical field key."""

        match key:
            case FieldKey.FULL_NAME:
                return self.full_name or " ".join(filter(None, [self.first_name, self.last_name])) or None
            case FieldKey.FIRST_NAME:
                if self.first_name:
                    return self.first_name
                return self.full_name.split(" ", 1)[0] if self.full_name else None
            case FieldKey.LAST_NAME:
                if self.last_name:
                    return self.last_name
                if self.full_name and " " in self.full_name:
                    return self.full_name.rsplit(" ", 1)[1]
                return None
            case FieldKey.EMAIL:
                return str(self.email) if self.email else None
            case FieldKey.LOGIN_EMAIL:
                return self.login.email or (str(self.email) if self.email else None)
            case FieldKey.PASSWORD:
                return self.login.password_value()
            case FieldKey.PHONE:
                return self.phone or None
            case FieldKey.ADDRESS_LINE1:
                return self.address.line1 or None
            case FieldKey.ADDRESS_LINE2:
                return self.address.line2 or None
            case FieldKey.CITY:
                return self.address.city or None
            case FieldKey.STATE:
                return self.address.state or None
            case FieldKey.POSTAL_CODE:
                return self.address.postal_code or None
            case FieldKey.COUNTRY:
                return self.address.country or None
            case FieldKey.DATE_OF_BIRTH:
                return self.date_of_birth or None
            case FieldKey.GENDER:
                return self.gender or None
            case FieldKey.BANK_ACCOUNT:
                return self.bank.account_number.get_secret_value() if self.bank.account_number else None
            case FieldKey.BANK_NAME:
                return self.bank.bank_name or None
            case FieldKey.BANK_ROUTING:
                return self.bank.routing_number.get_secret_value() if self.bank.routing_number else None
            case FieldKey.COMPANY:
                return self.company or None
            case FieldKey.NOTES:
                return self.notes or None
            case FieldKey.ID_NUMBER:
                # ID number lives on attendee records by default
                return None
            case FieldKey.CUSTOM:
                return None


class WorkflowStatus(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    HUMAN_REQUIRED = "human_required"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class WorkflowEventLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class WorkflowEvent(BaseModel):
    """A single observable event emitted by the runner."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=_new_id)
    timestamp: datetime = Field(default_factory=_utcnow)
    level: WorkflowEventLevel = WorkflowEventLevel.INFO
    status: WorkflowStatus = WorkflowStatus.RUNNING
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class WorkflowJob(BaseModel):
    """A queued automation job."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=_new_id)
    plugin_id: str
    profile_id: str
    target_url: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
