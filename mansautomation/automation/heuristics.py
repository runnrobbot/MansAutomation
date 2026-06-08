"""Field-mapping heuristics that map adaptive DOM context to canonical fields."""

from __future__ import annotations

from dataclasses import dataclass, field

from mansautomation.core.models import FieldKey


@dataclass(frozen=True, slots=True)
class FieldHeuristic:
    """Defines patterns that identify a canonical field by DOM hints."""

    key: FieldKey
    keywords: tuple[str, ...]
    excludes: tuple[str, ...] = ()
    type_hints: tuple[str, ...] = ()
    autocomplete_tokens: tuple[str, ...] = ()
    weight: float = 1.0


HEURISTICS: tuple[FieldHeuristic, ...] = (
    FieldHeuristic(
        key=FieldKey.PASSWORD,
        keywords=("password", "passwd", "kata sandi", "sandi", "kontrasena"),
        type_hints=("password",),
        autocomplete_tokens=("current-password", "new-password"),
        weight=1.6,
    ),
    FieldHeuristic(
        key=FieldKey.LOGIN_EMAIL,
        keywords=(
            "login email",
            "sign in email",
            "account email",
            "email login",
            "email atau",
            "email or",
            "username",
        ),
        type_hints=("email",),
        autocomplete_tokens=("username",),
        weight=1.2,
    ),
    FieldHeuristic(
        key=FieldKey.EMAIL,
        keywords=("email", "e-mail", "mail address", "correo", "courriel"),
        type_hints=("email",),
        autocomplete_tokens=("email",),
        weight=1.4,
    ),
    FieldHeuristic(
        key=FieldKey.PHONE,
        keywords=("phone", "mobile", "tel", "telephone", "contact number", "whatsapp"),
        type_hints=("tel",),
        autocomplete_tokens=("tel", "tel-national"),
        weight=1.2,
    ),
    FieldHeuristic(
        key=FieldKey.FULL_NAME,
        keywords=("full name", "name on card", "your name", "complete name", "cardholder"),
        excludes=("user name", "username", "company", "business"),
        autocomplete_tokens=("name", "cc-name"),
    ),
    FieldHeuristic(
        key=FieldKey.FIRST_NAME,
        keywords=("first name", "given name", "forename", "firstname"),
        autocomplete_tokens=("given-name",),
    ),
    FieldHeuristic(
        key=FieldKey.LAST_NAME,
        keywords=("last name", "surname", "family name", "lastname"),
        autocomplete_tokens=("family-name",),
    ),
    FieldHeuristic(
        key=FieldKey.ADDRESS_LINE1,
        keywords=("address", "street", "address line 1", "addr1", "shipping address"),
        excludes=("address line 2", "addr2", "apt", "suite", "email"),
        autocomplete_tokens=("address-line1", "street-address"),
    ),
    FieldHeuristic(
        key=FieldKey.ADDRESS_LINE2,
        keywords=("address line 2", "addr2", "apt", "apartment", "suite", "unit"),
        autocomplete_tokens=("address-line2",),
    ),
    FieldHeuristic(
        key=FieldKey.CITY,
        keywords=("city", "town", "locality"),
        autocomplete_tokens=("address-level2",),
    ),
    FieldHeuristic(
        key=FieldKey.STATE,
        keywords=("state", "province", "region", "county"),
        autocomplete_tokens=("address-level1",),
    ),
    FieldHeuristic(
        key=FieldKey.POSTAL_CODE,
        keywords=("postal", "post code", "postcode", "zip", "zip code", "pin code"),
        autocomplete_tokens=("postal-code",),
    ),
    FieldHeuristic(
        key=FieldKey.COUNTRY,
        keywords=("country", "nation"),
        autocomplete_tokens=("country", "country-name"),
    ),
    FieldHeuristic(
        key=FieldKey.DATE_OF_BIRTH,
        keywords=("date of birth", "birth date", "dob", "birthday"),
        type_hints=("date",),
        autocomplete_tokens=("bday",),
    ),
    FieldHeuristic(
        key=FieldKey.GENDER,
        keywords=("gender", "sex"),
        autocomplete_tokens=("sex",),
    ),
    FieldHeuristic(
        key=FieldKey.BANK_ACCOUNT,
        keywords=("account number", "iban", "bank account", "checking number"),
        excludes=("routing", "swift", "card number"),
    ),
    FieldHeuristic(
        key=FieldKey.BANK_NAME,
        keywords=("bank name", "issuing bank", "financial institution"),
    ),
    FieldHeuristic(
        key=FieldKey.BANK_ROUTING,
        keywords=("routing number", "aba", "swift", "bic"),
    ),
    FieldHeuristic(
        key=FieldKey.ID_NUMBER,
        keywords=("identification", "id number", "passport", "national id", "ssn"),
    ),
    FieldHeuristic(
        key=FieldKey.COMPANY,
        keywords=("company", "organisation", "organization", "business name", "employer"),
        autocomplete_tokens=("organization",),
    ),
)


@dataclass(slots=True)
class FieldClassification:
    key: FieldKey
    score: float
    reasons: list[str] = field(default_factory=list)
