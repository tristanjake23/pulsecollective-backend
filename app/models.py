from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Text = Annotated[str, Field(min_length=1, max_length=500)]
Money = Annotated[
    Decimal, Field(ge=0, max_digits=12, decimal_places=2, allow_inf_nan=False)
]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(Model):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    email: Annotated[str, Field(min_length=3, max_length=254)]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class Refresh(Model):
    refresh_token: Annotated[str, Field(min_length=1, max_length=4096)]


class Patient(Model):
    full_name: Annotated[str, Field(min_length=1, max_length=120)]
    date_of_birth: date | None = None
    sex: Literal["female", "male", "other", "prefer_not_to_say"] | None = None
    phone: Annotated[str, Field(max_length=30)] | None = None
    email: Annotated[str, Field(max_length=254)] | None = None
    address: Annotated[str, Field(max_length=500)] | None = None
    emergency_contact_name: Annotated[str, Field(max_length=120)] | None = None
    emergency_contact_phone: Annotated[str, Field(max_length=30)] | None = None
    emergency_contact_relationship: Annotated[str, Field(max_length=80)] | None = None
    allergies: Annotated[str, Field(max_length=1000)] | None = None
    medical_notes: Annotated[str, Field(max_length=4000)] | None = None
    doctor_id: UUID | None = None
    status: Literal["active", "follow_up", "inactive"] = "active"

    @field_validator("date_of_birth")
    @classmethod
    def valid_dob(cls, v):
        if v and v > date.today():
            raise ValueError("Date of birth cannot be in the future")
        return v


class Doctor(Model):
    profile_id: UUID | None = None
    full_name: Annotated[str, Field(min_length=1, max_length=120)]
    specialty: Text
    clinic_name: Text | None = None
    location: Text | None = None
    schedule: Text | None = None
    consultation_fee: Money = Decimal(0)
    photo_url: Annotated[str, Field(max_length=2000)] | None = None
    is_sample: bool = False
    is_published: bool = False
    is_archived: bool = False

    @field_validator("photo_url")
    @classmethod
    def photo(cls, v):
        if v and not (
            v.startswith("https://")
            or (
                v.startswith("/doctors/")
                and ".." not in v
                and "?" not in v
                and "#" not in v
            )
        ):
            raise ValueError("Photo URL must use HTTPS")
        return v


class Schedule(Model):
    doctor_id: UUID
    starts_at: datetime
    ends_at: datetime
    is_available: bool = True

    @model_validator(mode="after")
    def valid_range(self):
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("Timezone is required")
        if self.ends_at <= self.starts_at:
            raise ValueError("End must follow start")
        return self


class Appointment(Model):
    patient_id: UUID
    schedule_id: UUID
    reason: Annotated[str, Field(max_length=2000)] | None = None


class AppointmentUpdate(Model):
    status: Literal["scheduled", "confirmed", "cancelled", "no_show"]


class Consultation(Model):
    appointment_id: UUID
    clinical_notes: Annotated[str, Field(max_length=10000)] | None = None
    diagnosis: Annotated[str, Field(max_length=2000)] | None = None


class ConsultationUpdate(Model):
    clinical_notes: Annotated[str, Field(max_length=10000)] | None = None
    diagnosis: Annotated[str, Field(max_length=2000)] | None = None


class Medication(Model):
    medication_name: Text
    strength: Text | None = None
    dose: Text
    route: Text
    frequency: Text
    duration: Text
    quantity: (
        Annotated[
            Decimal, Field(gt=0, max_digits=12, decimal_places=2, allow_inf_nan=False)
        ]
        | None
    ) = None
    instructions: Annotated[str, Field(max_length=2000)] | None = None


class Prescription(Model):
    consultation_id: UUID
    instructions: Annotated[str, Field(max_length=4000)] | None = None
    items: Annotated[list[Medication], Field(min_length=1, max_length=20)]


class LineItem(Model):
    description: Text
    quantity: Annotated[
        Decimal, Field(gt=0, max_digits=10, decimal_places=2, allow_inf_nan=False)
    ]
    unit_price: Money


class Invoice(Model):
    patient_id: UUID
    due_date: date
    items: Annotated[list[LineItem], Field(min_length=1, max_length=50)]

    @field_validator("due_date")
    @classmethod
    def due(cls, v):
        if v < date.today():
            raise ValueError("Due date cannot precede issue date")
        return v


class Payment(Model):
    amount: Annotated[
        Decimal, Field(gt=0, max_digits=12, decimal_places=2, allow_inf_nan=False)
    ]
    method: Literal["cash", "card", "bank_transfer", "ewallet", "other"]
    reference: Annotated[str, Field(min_length=1, max_length=200)]


class TriageInput(Model):
    patient_id: UUID | None = None
    symptoms: Annotated[str, Field(min_length=1, max_length=2000)]
    duration: Annotated[str, Field(max_length=100)] = "Today"
    red_flags: Annotated[list[str], Field(max_length=10)] = []


class Signup(Model):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    full_name: Annotated[str, Field(min_length=2, max_length=120)]
    email: Annotated[str, Field(min_length=3, max_length=254)]
    password: Annotated[str, Field(min_length=12, max_length=128)]

    @field_validator("full_name")
    @classmethod
    def name_required(cls, value):
        if len(value.strip()) < 2:
            raise ValueError("Enter your full name")
        return value.strip()

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        import re

        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid email address")
        return value


class ResetPassword(Model):
    email: Annotated[str, Field(min_length=3, max_length=254)]
    _email = field_validator("email")(Signup.valid_email.__func__)


class PatientAccount(Model):
    contact_email: Annotated[str, Field(max_length=254)] | None = None

    @field_validator("contact_email")
    @classmethod
    def optional_email(cls, value):
        return Signup.valid_email(value) if value and value.strip() else None

    first_name: Annotated[str, Field(max_length=60)] | None = None
    middle_name: Annotated[str, Field(max_length=60)] | None = None
    last_name: Annotated[str, Field(max_length=60)] | None = None
    sex: Literal["female", "male", "other", "prefer_not_to_say"] | None = None
    full_name: Annotated[str, Field(min_length=2, max_length=120)]
    date_of_birth: date | None = None
    phone: Annotated[str, Field(max_length=30)] | None = None
    address: Annotated[str, Field(max_length=500)] | None = None
    _dob = field_validator("date_of_birth")(Patient.valid_dob.__func__)

    @field_validator("full_name")
    @classmethod
    def nonblank(cls, value):
        if len(value.strip()) < 2:
            raise ValueError("Enter your full name")
        return value
