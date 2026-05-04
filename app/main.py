import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from functools import partial
import json
from pathlib import Path
import secrets
import socket
import sqlite3
import time
from typing import Any
from urllib.parse import urlparse

from anyio import to_thread
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from pymongo import MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import PyMongoError
from redis import Redis as SyncRedis
from redis.asyncio import Redis
from redis.exceptions import RedisError
import requests
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import (
    issue_csrf_token,
    login_admin,
    logout_admin,
    require_admin_or_redirect,
    validate_csrf_token,
    verify_admin_credentials,
)
from .config import settings
from .online_users import OnlineUsersTracker


class HeartbeatPayload(BaseModel):
    visitor_id: str | None = Field(default=None, max_length=128)
    page_path: str | None = Field(default=None, max_length=2048)
    page_title: str | None = Field(default=None, max_length=256)
    cart_summary: list[dict[str, Any]] = Field(default_factory=list)


class TelegramTokenPayload(BaseModel):
    api_token: str = Field(min_length=1, max_length=256)


class TelegramSettingsPayload(BaseModel):
    api_token: str = Field(min_length=1, max_length=256)
    chat_id: str = Field(min_length=1, max_length=128)


class WhatsAppSettingsPayload(BaseModel):
    value: str = Field(min_length=1, max_length=128)


class PaymentMethodsPayload(BaseModel):
    knet_enabled: bool = True
    cards_enabled: bool = True
    testing_enabled: bool = False


class AdminThemeSettingsPayload(BaseModel):
    theme: str = Field(default="light", min_length=1, max_length=16)


class GlobalProductDiscountPayload(BaseModel):
    enabled: bool = False
    percentage: float = 0.0


class FeaturedProductPayload(BaseModel):
    product_id: str = Field(default="", max_length=128)


class ConnectionSettingsPayload(BaseModel):
    service: str = Field(min_length=1, max_length=32)
    url: str = Field(min_length=1, max_length=2048)


class VisitorRedirectPayload(BaseModel):
    path: str = Field(min_length=1, max_length=2048)


class VisitorKnetApprovalPayload(BaseModel):
    enabled: bool = False


class VisitorKnetApprovalDecisionPayload(BaseModel):
    decision: str = Field(min_length=1, max_length=16)


class TelegramMessageFieldPayload(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(default="", max_length=4000)


class TelegramSendSubmissionPayload(BaseModel):
    fields: list[TelegramMessageFieldPayload] = Field(default_factory=list)


class AdminSocketHub:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        for connection in list(self.connections):
            try:
                await connection.send_json(payload)
            except Exception:
                self.disconnect(connection)


class VisitorSocketHub:
    def __init__(self) -> None:
        self.connections: dict[str, set[WebSocket]] = {}

    async def connect(self, visitor_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.setdefault(visitor_id, set()).add(websocket)

    def disconnect(self, visitor_id: str, websocket: WebSocket) -> None:
        group = self.connections.get(visitor_id)
        if not group:
            return
        group.discard(websocket)
        if not group:
            self.connections.pop(visitor_id, None)

    def has_connection(self, visitor_id: str) -> bool:
        group = self.connections.get(visitor_id)
        return bool(group)

    async def send_to_visitor(self, visitor_id: str, payload: dict[str, Any]) -> None:
        for connection in list(self.connections.get(visitor_id, set())):
            try:
                await connection.send_json(payload)
            except Exception:
                self.disconnect(visitor_id, connection)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        for visitor_id, group in list(self.connections.items()):
            for connection in list(group):
                try:
                    await connection.send_json(payload)
                except Exception:
                    self.disconnect(visitor_id, connection)


def issue_admin_ws_token(app: FastAPI) -> str:
    token = secrets.token_urlsafe(24)
    expires_at = time.time() + 1800
    token_store: dict[str, float] = app.state.admin_ws_tokens
    token_store[token] = expires_at
    now = time.time()
    expired_tokens = [key for key, value in token_store.items() if value < now]
    for expired_token in expired_tokens:
        token_store.pop(expired_token, None)
    return token


def validate_admin_ws_token(app: FastAPI, token: str | None) -> bool:
    if not token:
        return False
    token_store: dict[str, float] = app.state.admin_ws_tokens
    expires_at = token_store.get(token)
    if expires_at is None:
        return False
    if expires_at < time.time():
        token_store.pop(token, None)
        return False
    return True


def parse_object_id(value: str | None) -> ObjectId | None:
    if not value:
        return None
    try:
        return ObjectId(value)
    except InvalidId:
        return None


def humanize_field_name(value: str) -> str:
    text = value.replace("-", " ").replace("_", " ").strip()
    if not text:
        return "Field"
    return " ".join(part.capitalize() for part in text.split())


def normalize_submission_fields(
    raw_fields: Any, legacy_document: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    normalized_fields: list[dict[str, str]] = []
    if isinstance(raw_fields, list):
        for item in raw_fields:
            if not isinstance(item, dict):
                continue
            field_name = str(item.get("name", "")).strip()
            field_label = str(item.get("label", "")).strip() or humanize_field_name(
                field_name
            )
            field_type = str(item.get("type", "")).strip()
            raw_value = item.get("value", "")
            if isinstance(raw_value, list):
                field_value = ", ".join(
                    str(value).strip() for value in raw_value if str(value).strip()
                )
            else:
                field_value = str(raw_value).strip()
            if not field_name or not field_value:
                continue
            normalized_fields.append(
                {
                    "name": field_name,
                    "label": field_label,
                    "value": field_value,
                    "type": field_type,
                }
            )
    if normalized_fields:
        return normalized_fields
    if legacy_document is None:
        return []

    legacy_pairs = [
        ("lead_name", "Project Lead"),
        ("work_email", "Work Email"),
        ("company_name", "Company Name"),
        ("service_need", "Service Needed"),
        ("project_notes", "Project Notes"),
        ("full_name", "Full Name"),
        ("email", "Email"),
    ]
    for key, label in legacy_pairs:
        value = str(legacy_document.get(key, "")).strip()
        if value:
            normalized_fields.append(
                {
                    "name": key,
                    "label": label,
                    "value": value,
                    "type": "text",
                }
            )
    return normalized_fields


def _resolve_visitor_identity_sync(
    collection: Collection | None,
    settings_collection: Collection | None,
    visitor_id: str | None,
    user_agent: str,
) -> dict[str, Any]:
    parsed_object_id = parse_object_id(visitor_id)
    object_id = parsed_object_id or ObjectId()
    if collection is None:
        return {
            "visitor_id": str(object_id),
            "is_new_visitor": parsed_object_id is None,
            "is_returning_visitor": parsed_object_id is not None,
            "visit_count": 1,
        }

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    update_result = collection.update_one(
        {"_id": object_id},
        {
            "$setOnInsert": {
                "first_seen": now,
                "blocked": False,
                "blocked_at": "",
                "require_knet_approval": False,
                "waiting_for_knet_approval": False,
            },
            "$set": {
                "last_seen": now,
                "last_user_agent": user_agent,
                "archived": False,
                "archived_at": "",
            },
            "$inc": {"visit_count": 1},
        },
        upsert=True,
    )
    visitor_number = assign_visitor_number_sync(
        collection, settings_collection, object_id
    )
    profile = collection.find_one({"_id": object_id}, {"visit_count": 1}) or {}
    visit_count = int(profile.get("visit_count", 1))
    is_new_visitor = update_result.upserted_id is not None
    return {
        "visitor_id": str(object_id),
        "is_new_visitor": is_new_visitor,
        "is_returning_visitor": visit_count > 1,
        "visit_count": visit_count,
        "visitor_number": visitor_number,
    }


def serialize_submission(document: dict[str, Any]) -> dict[str, Any]:
    fields = normalize_submission_fields(document.get("fields"), document)
    return {
        "id": str(document.get("_id", "")),
        "visitor_id": str(document.get("visitor_id", "")),
        "visitor_status": str(document.get("visitor_status", "")),
        "form_name": str(document.get("form_name", "")).strip() or "Website Form",
        "page_path": str(document.get("page_path", "")).strip(),
        "lead_name": str(document.get("lead_name") or document.get("full_name", "")),
        "work_email": str(document.get("work_email") or document.get("email", "")),
        "company_name": str(document.get("company_name", "")),
        "service_need": str(document.get("service_need", "")),
        "project_notes": str(document.get("project_notes", "")),
        "fields": fields,
        "created_at": str(document.get("created_at", "")),
    }


def is_knet_submission_payload(form_name: str, page_path: str) -> bool:
    normalized_form_name = str(form_name or "").strip().lower()
    normalized_page_path = normalize_page_path(page_path)
    return normalized_form_name == "knet payments" or normalized_page_path == "/knet"


def submission_has_invalid_validation_status(fields: list[dict[str, Any]]) -> bool:
    for field in fields or []:
        normalized_name = str(field.get("name", "")).strip().lower()
        if normalized_name != "validation_status":
            continue
        if str(field.get("value", "")).strip().lower() == "invalid":
            return True
    return False


def append_submission_field_if_missing(
    fields: list[dict[str, Any]],
    *,
    name: str,
    label: str,
    value: str,
    field_type: str = "hidden",
) -> list[dict[str, Any]]:
    normalized_target_name = str(name or "").strip().lower()
    for field in fields or []:
        if str(field.get("name", "")).strip().lower() == normalized_target_name:
            return fields
    fields.append(
        {
            "name": name,
            "label": label,
            "value": value,
            "type": field_type,
        }
    )
    return fields


def upsert_submission_field(
    fields: list[dict[str, Any]],
    *,
    name: str,
    label: str,
    value: str,
    field_type: str = "hidden",
) -> list[dict[str, Any]]:
    normalized_target_name = str(name or "").strip().lower()
    updated = False
    for field in fields or []:
        if str(field.get("name", "")).strip().lower() != normalized_target_name:
            continue
        field["label"] = label
        field["value"] = value
        field["type"] = field_type
        updated = True
        break
    if not updated:
        fields.append(
            {
                "name": name,
                "label": label,
                "value": value,
                "type": field_type,
            }
        )
    return fields


def serialize_visitor(
    document: dict[str, Any], online_visitor_ids: set[str] | None = None
) -> dict[str, Any]:
    visitor_id = str(document.get("_id", ""))
    visitor_number = parse_visitor_number(document.get("visitor_number"))
    return {
        "id": visitor_id,
        "display_id": str(visitor_number) if visitor_number is not None else visitor_id,
        "visitor_number": visitor_number,
        "visit_count": str(document.get("visit_count", "")),
        "first_seen": str(document.get("first_seen", "")),
        "last_seen": str(document.get("last_seen", "")),
        "last_user_agent": str(document.get("last_user_agent", "")),
        "is_online": visitor_id in (online_visitor_ids or set()),
        "is_blocked": bool(document.get("blocked", False)),
        "require_knet_approval": bool(document.get("require_knet_approval", False)),
        "waiting_for_knet_approval": bool(
            document.get("waiting_for_knet_approval", False)
        ),
        "current_page_path": str(document.get("current_page_path", "")).strip(),
        "current_page_title": str(document.get("current_page_title", "")).strip(),
        "current_cart_summary": document.get("current_cart_summary", []),
    }


def parse_visitor_number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def reserve_next_visitor_number_sync(
    visitors_collection: Collection | None, settings_collection: Collection | None
) -> int | None:
    if visitors_collection is None or settings_collection is None:
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    highest_visitor = visitors_collection.find_one(
        {"visitor_number": {"$type": "number"}},
        {"visitor_number": 1},
        sort=[("visitor_number", -1)],
    )
    highest_value = parse_visitor_number(
        highest_visitor.get("visitor_number") if highest_visitor else None
    ) or 0
    settings_collection.update_one(
        {"_id": VISITOR_NUMBER_COUNTER_DOCUMENT_ID},
        {
            "$max": {"value": highest_value},
            "$setOnInsert": {"created_at": now},
            "$set": {"updated_at": now},
        },
        upsert=True,
    )
    counter_document = settings_collection.find_one_and_update(
        {"_id": VISITOR_NUMBER_COUNTER_DOCUMENT_ID},
        {"$inc": {"value": 1}, "$set": {"updated_at": now}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return parse_visitor_number(counter_document.get("value")) if counter_document else None


def assign_visitor_number_sync(
    visitors_collection: Collection | None,
    settings_collection: Collection | None,
    visitor_object_id: ObjectId | None,
) -> int | None:
    if visitors_collection is None or settings_collection is None or visitor_object_id is None:
        return None
    existing_document = visitors_collection.find_one(
        {"_id": visitor_object_id},
        {"visitor_number": 1},
    )
    existing_number = parse_visitor_number(
        existing_document.get("visitor_number") if existing_document else None
    )
    if existing_number is not None:
        return existing_number

    next_number = reserve_next_visitor_number_sync(
        visitors_collection, settings_collection
    )
    if next_number is None:
        return None

    updated_document = visitors_collection.find_one_and_update(
        {
            "_id": visitor_object_id,
            "$or": [
                {"visitor_number": {"$exists": False}},
                {"visitor_number": None},
                {"visitor_number": ""},
            ],
        },
        {"$set": {"visitor_number": next_number}},
        return_document=ReturnDocument.AFTER,
    )
    updated_number = parse_visitor_number(
        updated_document.get("visitor_number") if updated_document else None
    )
    if updated_number is not None:
        return updated_number

    current_document = visitors_collection.find_one(
        {"_id": visitor_object_id},
        {"visitor_number": 1},
    )
    return parse_visitor_number(
        current_document.get("visitor_number") if current_document else None
    )


def ensure_visitor_numbers_sync(
    visitors_collection: Collection | None,
    settings_collection: Collection | None,
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if visitors_collection is None or settings_collection is None:
        return documents

    def sort_key(document: dict[str, Any]) -> tuple[str, str]:
        return (
            str(document.get("first_seen") or document.get("last_seen") or ""),
            str(document.get("_id", "")),
        )

    for document in sorted(documents, key=sort_key):
        if parse_visitor_number(document.get("visitor_number")) is not None:
            continue
        visitor_object_id = document.get("_id")
        if not isinstance(visitor_object_id, ObjectId):
            visitor_object_id = parse_object_id(str(visitor_object_id or ""))
        assigned_number = assign_visitor_number_sync(
            visitors_collection,
            settings_collection,
            visitor_object_id,
        )
        if assigned_number is not None:
            document["visitor_number"] = assigned_number
    return documents


def normalize_submission_field_key(value: str | None) -> str:
    text = str(value or "").strip().lower()
    return "".join(
        character
        for character in text
        if ("a" <= character <= "z") or ("0" <= character <= "9")
    )


def find_submission_preferred_field_value(
    submission: dict[str, Any] | None, preferred_keys: list[str]
) -> str:
    if not submission:
        return ""
    fields = submission.get("fields")
    if not isinstance(fields, list):
        return ""
    raw_keys = [str(key or "").strip().lower() for key in preferred_keys]
    normalized_keys = [normalize_submission_field_key(key) for key in preferred_keys]
    for field in fields:
        if not isinstance(field, dict):
            continue
        raw_name = str(field.get("name", "")).strip().lower()
        raw_label = str(field.get("label", "")).strip().lower()
        normalized_name = normalize_submission_field_key(field.get("name", ""))
        normalized_label = normalize_submission_field_key(field.get("label", ""))
        if (
            raw_name in raw_keys
            or raw_label in raw_keys
            or normalized_name in normalized_keys
            or normalized_label in normalized_keys
        ):
            return str(field.get("value", "")).strip()
    return ""


def get_visitor_display_label_from_submissions(submissions: list[dict[str, Any]]) -> str:
    preferred_field_groups = [
        ["name", "fullname", "contactname", "leadname", "projectlead"],
        ["phonenumber", "phone", "phone_number", "contactphonenumber", "contactphone"],
        ["email", "workemail", "contactemail"],
    ]
    for preferred_keys in preferred_field_groups:
        for submission in submissions:
            field_value = find_submission_preferred_field_value(submission, preferred_keys)
            if field_value:
                return field_value
    return ""


def extract_submission_digits(value: str | None) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def get_submission_payment_method(submission: dict[str, Any] | None) -> str:
    if not submission:
        return "knet"
    fields = submission.get("fields")
    if not isinstance(fields, list):
        return "knet"
    card_pin_field = next(
        (
            field
            for field in fields
            if isinstance(field, dict)
            and normalize_submission_field_key(field.get("name", "")) == "cardpin"
        ),
        None,
    )
    card_pin_label = normalize_submission_field_key(
        card_pin_field.get("label", "") if isinstance(card_pin_field, dict) else ""
    )
    return "cards" if card_pin_label == "cvv" else "knet"


def get_submission_combined_card_number(submission: dict[str, Any] | None) -> str:
    prefix_value = find_submission_preferred_field_value(
        submission,
        ["dcprefix", "بادئة البطاقة"],
    )
    debit_number_value = find_submission_preferred_field_value(
        submission,
        [
            "debit_number",
            "debitnumber",
            "cardnumber",
            "card_number",
            "رقم بطاقة الصرف الآلي",
            "رقم بطاقة الصراف الآلي",
        ],
    )
    return extract_submission_digits(prefix_value + debit_number_value)


def detect_card_brand_from_number(card_number: str | None) -> str:
    digits = extract_submission_digits(card_number)
    if not digits:
        return ""
    if digits.startswith("4"):
        return "visa"
    try:
        first_two = int(digits[:2])
    except ValueError:
        first_two = -1
    try:
        first_four = int(digits[:4])
    except ValueError:
        first_four = -1
    if 51 <= first_two <= 55 or 2221 <= first_four <= 2720:
        return "mastercard"
    return ""


def is_submission_otp_verification(submission: dict[str, Any] | None) -> bool:
    form_name = str((submission or {}).get("form_name", "")).strip().lower()
    return form_name in {"otp verification", "verification otp"}


def get_submission_payment_brand(submission: dict[str, Any] | None) -> str:
    if not submission or is_submission_otp_verification(submission):
        return ""
    if str(submission.get("form_name", "")).strip() == "KNET Payments":
        return "knet"
    if get_submission_payment_method(submission) != "cards":
        return ""
    return detect_card_brand_from_number(get_submission_combined_card_number(submission))


def get_visitor_payment_brand_from_submissions(submissions: list[dict[str, Any]]) -> str:
    for submission in submissions:
        payment_brand = get_submission_payment_brand(submission)
        if payment_brand:
            return payment_brand
    return ""


def format_admin_dashboard_registration_date(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    parsed_value: datetime | None = None
    for parser in (
        lambda raw: datetime.strptime(raw, "%Y-%m-%d %H:%M:%S UTC").replace(
            tzinfo=timezone.utc
        ),
        lambda raw: datetime.fromisoformat(raw.replace("Z", "+00:00")),
    ):
        try:
            parsed_value = parser(text)
            break
        except ValueError:
            continue
    if parsed_value is None:
        return "-"
    local_value = parsed_value.astimezone()
    year = str(local_value.year)[-2:]
    month = f"{local_value.month:02d}"
    day = f"{local_value.day:02d}"
    hour_24 = local_value.hour
    minute = f"{local_value.minute:02d}"
    suffix = "PM" if hour_24 >= 12 else "AM"
    hour_12 = hour_24 % 12 or 12
    return f"{year}-{month}-{day} {hour_12:02d}:{minute} {suffix}"


def get_visitor_current_step_label(
    visitor: dict[str, Any], available_frontend_pages: list[dict[str, str]]
) -> str:
    current_page_title = str(visitor.get("current_page_title", "")).strip()
    if current_page_title:
        return current_page_title
    current_page_path = str(visitor.get("current_page_path", "")).strip()
    if not current_page_path:
        return "On site" if visitor.get("is_online") is True else "-"
    matched_page = next(
        (
            page
            for page in available_frontend_pages
            if str(page.get("path", "")).strip() == current_page_path
        ),
        None,
    )
    if matched_page:
        matched_title = str(matched_page.get("title", "")).strip()
        if matched_title:
            return matched_title
    return current_page_path


def build_admin_dashboard_visitor_rows(
    visitors: list[dict[str, Any]],
    submissions: list[dict[str, Any]],
    available_frontend_pages: list[dict[str, str]],
) -> list[dict[str, Any]]:
    submissions_by_visitor_id: dict[str, list[dict[str, Any]]] = {}
    for submission in submissions:
        visitor_id = str(submission.get("visitor_id", "")).strip()
        if not visitor_id:
            continue
        submissions_by_visitor_id.setdefault(visitor_id, []).append(submission)

    enriched_visitors: list[dict[str, Any]] = []
    for visitor in visitors:
        visitor_id = str(visitor.get("id", "")).strip()
        visitor_submissions = submissions_by_visitor_id.get(visitor_id, [])
        checkout_submission = next(
            (
                submission
                for submission in visitor_submissions
                if str(submission.get("form_name", "")).strip() == "معلومات التسليم"
            ),
            None,
        )
        cart_total = 0.0
        cart_items = visitor.get("current_cart_summary")
        if isinstance(cart_items, list):
            for item in cart_items:
                if not isinstance(item, dict):
                    continue
                try:
                    quantity = float(item.get("quantity", 0) or 0)
                except (TypeError, ValueError):
                    quantity = 0.0
                if quantity <= 0:
                    continue
                try:
                    cart_total += float(item.get("total_price", 0) or 0)
                except (TypeError, ValueError):
                    continue

        enriched_visitors.append(
            {
                **visitor,
                "display_label": get_visitor_display_label_from_submissions(
                    visitor_submissions
                )
                or "-",
                "phone_value": find_submission_preferred_field_value(
                    checkout_submission,
                    ["phone", "phone_number", "phonenumber", "رقم الهاتف"],
                )
                or "-",
                "payment_brand": get_visitor_payment_brand_from_submissions(
                    visitor_submissions
                ),
                "total_label": f"{cart_total:.3f} د.ك" if cart_total > 0 else "-",
                "registration_value": format_admin_dashboard_registration_date(
                    str(
                        visitor.get("first_seen")
                        or visitor.get("created_at")
                        or visitor.get("last_seen")
                        or ""
                    )
                ),
                "current_step_label": get_visitor_current_step_label(
                    visitor, available_frontend_pages
                ),
            }
        )
    return enriched_visitors


TELEGRAM_SETTINGS_DOCUMENT_ID = "telegram_settings"
WHATSAPP_SETTINGS_DOCUMENT_ID = "whatsapp_settings"
PAYMENT_SETTINGS_DOCUMENT_ID = "payment_settings"
ADMIN_THEME_SETTINGS_DOCUMENT_ID = "admin_theme_settings"
SOCIAL_SETTINGS_DOCUMENT_ID = "social_settings"
CONNECTION_SETTINGS_DOCUMENT_ID = "connection_settings"
GLOBAL_PRODUCT_DISCOUNT_SETTINGS_DOCUMENT_ID = "global_product_discount_settings"
FEATURED_PRODUCT_SETTINGS_DOCUMENT_ID = "featured_product_settings"
VISITOR_NUMBER_COUNTER_DOCUMENT_ID = "visitor_number_counter"
PRODUCT_THUMB_CLASS_OPTIONS = (
    "thumb-dates-a",
    "thumb-seafood",
    "thumb-dates-b",
    "thumb-dates-c",
    "thumb-basket",
    "thumb-premium",
)


def default_store_products() -> list[dict[str, Any]]:
    return [
        {
            "id": "royal-dates",
            "position": 0,
            "name": "عرض تمر المقعى الملكي",
            "description": "بوكس 5 كيلو تمر مقعى ملكي فاخر.",
            "price": 10.0,
            "discount_enabled": False,
            "discount_percentage": 0.0,
            "image_url": "",
            "thumb_class": "thumb-dates-a",
            "active": True,
        },
        {
            "id": "shrimp-offer",
            "position": 1,
            "name": "عرض كل الكويت الجديد",
            "description": "10 كيلو روبيان عماني جامبو طازج.",
            "price": 5.0,
            "discount_enabled": False,
            "discount_percentage": 0.0,
            "image_url": "",
            "thumb_class": "thumb-seafood",
            "active": True,
        },
        {
            "id": "asfour-tin",
            "position": 2,
            "name": "عرض 50 عصفور التين",
            "description": "بوكس 50 عصفورتين درجة أولى حجم كبير.",
            "price": 7.5,
            "discount_enabled": False,
            "discount_percentage": 0.0,
            "image_url": "",
            "thumb_class": "thumb-dates-b",
            "active": True,
        },
        {
            "id": "mekbous-dates",
            "position": 3,
            "name": "عرض تمر الخلاص المكبوس",
            "description": "بوكس 8 كيلو تمر الخلاص المكبوس درجة أولى.",
            "price": 6.25,
            "discount_enabled": False,
            "discount_percentage": 0.0,
            "image_url": "",
            "thumb_class": "thumb-dates-c",
            "active": True,
        },
        {
            "id": "mixed-basket",
            "position": 4,
            "name": "سلة مزارع مشكلة",
            "description": "سلة مختارة من التمور والمنتجات الموسمية.",
            "price": 12.0,
            "discount_enabled": False,
            "discount_percentage": 0.0,
            "image_url": "",
            "thumb_class": "thumb-basket",
            "active": True,
        },
        {
            "id": "premium-box",
            "position": 5,
            "name": "بوكس الضيافة الفاخر",
            "description": "تشكيلة جاهزة للتقديم مع تمر فاخر وتغليف أنيق.",
            "price": 9.75,
            "discount_enabled": False,
            "discount_percentage": 0.0,
            "image_url": "",
            "thumb_class": "thumb-premium",
            "active": True,
        },
    ]


def normalize_store_product(item: Any, fallback_index: int) -> dict[str, Any]:
    record = item if isinstance(item, dict) else {}
    thumb_class = str(record.get("thumb_class", "")).strip()
    if thumb_class not in PRODUCT_THUMB_CLASS_OPTIONS:
        thumb_class = PRODUCT_THUMB_CLASS_OPTIONS[
            fallback_index % len(PRODUCT_THUMB_CLASS_OPTIONS)
        ]
    price = round(float(record.get("price", 0) or 0), 3)
    discount_percentage = round(
        max(0.0, min(100.0, float(record.get("discount_percentage", 0) or 0))), 2
    )
    discount_enabled = bool(record.get("discount_enabled", False)) and discount_percentage > 0
    discounted_price = (
        round(price * (1 - (discount_percentage / 100)), 3)
        if discount_enabled and price > 0
        else price
    )
    try:
        position = int(record.get("position", fallback_index))
    except (TypeError, ValueError):
        position = fallback_index
    return {
        "id": str(record.get("id", "")).strip()
        or f"product-{fallback_index + 1}-{secrets.token_hex(3)}",
        "position": max(position, 0),
        "name": str(record.get("name", "")).strip() or "منتج",
        "description": str(record.get("description", "")).strip(),
        "price": price,
        "discount_enabled": discount_enabled,
        "discount_percentage": discount_percentage if discount_enabled else 0.0,
        "discounted_price": discounted_price,
        "image_url": str(record.get("image_url", "")).strip(),
        "thumb_class": thumb_class,
        "active": bool(record.get("active", True)),
    }


def serialize_telegram_settings(document: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(document, dict):
        return {"api_token": "", "chat_id": ""}
    return {
        "api_token": str(document.get("api_token", "")).strip(),
        "chat_id": str(document.get("chat_id", "")).strip(),
    }


def serialize_whatsapp_settings(document: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(document, dict):
        return {"value": ""}
    return {
        "value": str(document.get("value", "")).strip(),
    }


def serialize_social_settings(document: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(document, dict):
        return {"title": "", "url": "", "description": "", "image_url": ""}
    return {
        "title": str(document.get("title", "")).strip(),
        "url": str(document.get("url", "")).strip(),
        "description": str(document.get("description", "")).strip(),
        "image_url": str(document.get("image_url", "")).strip(),
    }


def default_global_product_discount_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "percentage": 0.0,
    }


def serialize_global_product_discount_settings(
    document: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(document, dict):
        return default_global_product_discount_settings()
    percentage = round(
        max(0.0, min(100.0, float(document.get("percentage", 0) or 0))), 2
    )
    enabled = bool(document.get("enabled", False)) and percentage > 0
    return {
        "enabled": enabled,
        "percentage": percentage if enabled else 0.0,
    }


def default_featured_product_settings() -> dict[str, str]:
    return {"product_id": ""}


def serialize_featured_product_settings(document: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(document, dict):
        return default_featured_product_settings()
    return {
        "product_id": str(document.get("product_id", "")).strip(),
    }


def default_payment_settings() -> dict[str, bool]:
    return {
        "knet_enabled": True,
        "cards_enabled": False,
        "testing_enabled": False,
    }


def default_admin_theme_settings() -> dict[str, str]:
    return {"theme": "light"}


def serialize_payment_settings(document: dict[str, Any] | None) -> dict[str, bool]:
    if not isinstance(document, dict):
        return default_payment_settings()
    cards_enabled = bool(document.get("cards_enabled", False))
    knet_enabled = not cards_enabled
    testing_enabled = bool(document.get("testing_enabled", False))
    return {
        "knet_enabled": knet_enabled,
        "cards_enabled": cards_enabled,
        "testing_enabled": testing_enabled,
    }


def serialize_admin_theme_settings(document: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(document, dict):
        return default_admin_theme_settings()
    theme = str(document.get("theme", "light")).strip().lower()
    if theme not in {"light", "dark"}:
        theme = "light"
    return {"theme": theme}


def empty_connection_settings() -> dict[str, str]:
    return {"mongo_url": "", "redis_url": "", "sql_url": ""}


def serialize_connection_settings(document: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(document, dict):
        return empty_connection_settings()
    return {
        "mongo_url": str(document.get("mongo_url", "")).strip(),
        "redis_url": str(document.get("redis_url", "")).strip(),
        "sql_url": str(document.get("sql_url", "")).strip(),
    }


def _fetch_telegram_settings_sync(collection: Collection | None) -> dict[str, str]:
    if collection is None:
        return {"api_token": "", "chat_id": ""}
    document = collection.find_one({"_id": TELEGRAM_SETTINGS_DOCUMENT_ID})
    return serialize_telegram_settings(document)


def _fetch_connection_settings_sync(collection: Collection | None) -> dict[str, str]:
    if collection is None:
        return empty_connection_settings()
    document = collection.find_one({"_id": CONNECTION_SETTINGS_DOCUMENT_ID})
    return serialize_connection_settings(document)


def _fetch_whatsapp_settings_sync(collection: Collection | None) -> dict[str, str]:
    if collection is None:
        return {"value": ""}
    document = collection.find_one({"_id": WHATSAPP_SETTINGS_DOCUMENT_ID})
    return serialize_whatsapp_settings(document)


def _fetch_payment_settings_sync(collection: Collection | None) -> dict[str, bool]:
    if collection is None:
        return default_payment_settings()
    document = collection.find_one({"_id": PAYMENT_SETTINGS_DOCUMENT_ID})
    return serialize_payment_settings(document)


def _fetch_admin_theme_settings_sync(collection: Collection | None) -> dict[str, str]:
    if collection is None:
        return default_admin_theme_settings()
    document = collection.find_one({"_id": ADMIN_THEME_SETTINGS_DOCUMENT_ID})
    return serialize_admin_theme_settings(document)


def _fetch_social_settings_sync(collection: Collection | None) -> dict[str, str]:
    if collection is None:
        return {"title": "", "url": "", "description": "", "image_url": ""}
    document = collection.find_one({"_id": SOCIAL_SETTINGS_DOCUMENT_ID})
    return serialize_social_settings(document)


def _fetch_global_product_discount_settings_sync(
    collection: Collection | None,
) -> dict[str, Any]:
    if collection is None:
        return default_global_product_discount_settings()
    document = collection.find_one({"_id": GLOBAL_PRODUCT_DISCOUNT_SETTINGS_DOCUMENT_ID})
    return serialize_global_product_discount_settings(document)


def _fetch_featured_product_settings_sync(collection: Collection | None) -> dict[str, str]:
    if collection is None:
        return default_featured_product_settings()
    document = collection.find_one({"_id": FEATURED_PRODUCT_SETTINGS_DOCUMENT_ID})
    return serialize_featured_product_settings(document)


def _save_telegram_settings_sync(
    collection: Collection | None, api_token: str, chat_id: str
) -> dict[str, str]:
    if collection is None:
        return {"api_token": "", "chat_id": ""}
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    collection.update_one(
        {"_id": TELEGRAM_SETTINGS_DOCUMENT_ID},
        {
            "$set": {
                "api_token": api_token,
                "chat_id": chat_id,
                "updated_at": updated_at,
            }
        },
        upsert=True,
    )
    return {"api_token": api_token, "chat_id": chat_id}


def _save_whatsapp_settings_sync(
    collection: Collection | None, value: str
) -> dict[str, str]:
    if collection is None:
        return {"value": ""}
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    collection.update_one(
        {"_id": WHATSAPP_SETTINGS_DOCUMENT_ID},
        {
            "$set": {
                "value": value,
                "updated_at": updated_at,
            }
        },
        upsert=True,
    )
    return {"value": value}


def _save_payment_settings_sync(
    collection: Collection | None,
    knet_enabled: bool,
    cards_enabled: bool,
    testing_enabled: bool,
) -> dict[str, bool]:
    if collection is None:
        return default_payment_settings()
    cards_enabled = bool(cards_enabled)
    knet_enabled = not cards_enabled
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    collection.update_one(
        {"_id": PAYMENT_SETTINGS_DOCUMENT_ID},
        {
            "$set": {
                "knet_enabled": bool(knet_enabled),
                "cards_enabled": bool(cards_enabled),
                "testing_enabled": bool(testing_enabled),
                "updated_at": updated_at,
            }
        },
        upsert=True,
    )
    return {
        "knet_enabled": bool(knet_enabled),
        "cards_enabled": bool(cards_enabled),
        "testing_enabled": bool(testing_enabled),
    }


def _save_social_settings_sync(
    collection: Collection | None,
    title: str,
    url: str,
    description: str,
    image_url: str,
) -> dict[str, str]:
    if collection is None:
        return {"title": "", "url": "", "description": "", "image_url": ""}
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    collection.update_one(
        {"_id": SOCIAL_SETTINGS_DOCUMENT_ID},
        {
            "$set": {
                "title": title,
                "url": url,
                "description": description,
                "image_url": image_url,
                "updated_at": updated_at,
            }
        },
        upsert=True,
    )
    return {
        "title": title,
        "url": url,
        "description": description,
        "image_url": image_url,
    }


def _save_admin_theme_settings_sync(
    collection: Collection | None, theme: str
) -> dict[str, str]:
    if collection is None:
        return default_admin_theme_settings()
    normalized_theme = serialize_admin_theme_settings({"theme": theme})["theme"]
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    collection.update_one(
        {"_id": ADMIN_THEME_SETTINGS_DOCUMENT_ID},
        {
            "$set": {
                "theme": normalized_theme,
                "updated_at": updated_at,
            }
        },
        upsert=True,
    )
    return {"theme": normalized_theme}


def _save_global_product_discount_settings_sync(
    collection: Collection | None,
    enabled: bool,
    percentage: float,
) -> dict[str, Any]:
    if collection is None:
        return default_global_product_discount_settings()
    normalized_percentage = round(max(0.0, min(100.0, float(percentage or 0))), 2)
    normalized_enabled = bool(enabled) and normalized_percentage > 0
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    collection.update_one(
        {"_id": GLOBAL_PRODUCT_DISCOUNT_SETTINGS_DOCUMENT_ID},
        {
            "$set": {
                "enabled": normalized_enabled,
                "percentage": normalized_percentage if normalized_enabled else 0.0,
                "updated_at": updated_at,
            }
        },
        upsert=True,
    )
    return {
        "enabled": normalized_enabled,
        "percentage": normalized_percentage if normalized_enabled else 0.0,
    }


def _save_featured_product_settings_sync(
    collection: Collection | None, product_id: str
) -> dict[str, str]:
    if collection is None:
        return default_featured_product_settings()
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    normalized_product_id = str(product_id or "").strip()
    collection.update_one(
        {"_id": FEATURED_PRODUCT_SETTINGS_DOCUMENT_ID},
        {
            "$set": {
                "product_id": normalized_product_id,
                "updated_at": updated_at,
            }
        },
        upsert=True,
    )
    return {"product_id": normalized_product_id}


def _save_connection_setting_sync(
    collection: Collection | None, service: str, url: str
) -> dict[str, str]:
    if collection is None:
        return empty_connection_settings()
    field_map = {
        "mongo": "mongo_url",
        "redis": "redis_url",
        "sql": "sql_url",
    }
    field_name = field_map.get(service)
    if field_name is None:
        return empty_connection_settings()
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    collection.update_one(
        {"_id": CONNECTION_SETTINGS_DOCUMENT_ID},
        {"$set": {field_name: url, "updated_at": updated_at}},
        upsert=True,
    )
    document = collection.find_one({"_id": CONNECTION_SETTINGS_DOCUMENT_ID})
    return serialize_connection_settings(document)


def _delete_telegram_settings_sync(collection: Collection | None) -> bool:
    if collection is None:
        return False
    result = collection.delete_one({"_id": TELEGRAM_SETTINGS_DOCUMENT_ID})
    return result.deleted_count > 0


def _delete_whatsapp_settings_sync(collection: Collection | None) -> bool:
    if collection is None:
        return False
    result = collection.delete_one({"_id": WHATSAPP_SETTINGS_DOCUMENT_ID})
    return result.deleted_count > 0


def _delete_connection_setting_sync(collection: Collection | None, service: str) -> bool:
    if collection is None:
        return False
    field_map = {
        "mongo": "mongo_url",
        "redis": "redis_url",
        "sql": "sql_url",
    }
    field_name = field_map.get(service)
    if field_name is None:
        return False
    result = collection.update_one(
        {"_id": CONNECTION_SETTINGS_DOCUMENT_ID},
        {"$unset": {field_name: ""}},
    )
    return result.acknowledged


def find_telegram_chat_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        chat = payload.get("chat")
        if isinstance(chat, dict):
            chat_id = chat.get("id")
            if chat_id not in {None, ""}:
                return str(chat_id)
        direct_chat_id = payload.get("chat_id")
        if direct_chat_id not in {None, ""}:
            return str(direct_chat_id)
        for value in payload.values():
            nested_chat_id = find_telegram_chat_id(value)
            if nested_chat_id:
                return nested_chat_id
    elif isinstance(payload, list):
        for item in payload:
            nested_chat_id = find_telegram_chat_id(item)
            if nested_chat_id:
                return nested_chat_id
    return None


def fetch_telegram_updates_sync(api_token: str) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{api_token}/getUpdates"
    try:
        response = requests.get(
            url,
            headers={"Accept": "application/json"},
            timeout=8,
        )
    except requests.RequestException as exc:
        return {
            "status": "request_error",
            "chat_id": None,
            "description": str(exc) or "Unable to reach Telegram.",
        }

    try:
        payload = response.json()
    except ValueError:
        return {
            "status": "request_error",
            "chat_id": None,
            "description": "Telegram returned a non-JSON response.",
        }
    if not response.ok or payload.get("ok") is not True:
        description = str(payload.get("description", "")).strip()
        lowered_description = description.lower()
        is_invalid_token = (
            response.status_code in {401, 404}
            or "unauthorized" in lowered_description
            or "invalid" in lowered_description
            or "not found" in lowered_description
        )
        return {
            "status": "invalid_token" if is_invalid_token else "request_error",
            "chat_id": None,
            "description": description,
        }
    return {
        "status": "ok",
        "chat_id": find_telegram_chat_id(payload.get("result")),
    }


def escape_markdown_v2_text(value: str) -> str:
    escaped = []
    for char in value:
        if char in r"_*[]()~`>#+-=|{}.!":
            escaped.append("\\" + char)
        else:
            escaped.append(char)
    return "".join(escaped)


def escape_markdown_v2_code(value: str) -> str:
    return value.replace("\\", "\\\\").replace("`", "\\`")


def build_telegram_submission_message(
    fields: list[dict[str, str]] | list[TelegramMessageFieldPayload],
) -> str:
    lines: list[str] = []
    for field in fields:
        if isinstance(field, TelegramMessageFieldPayload):
            field_name = field.name.strip()
            field_value = field.value.strip()
        else:
            field_name = str(field.get("name", "")).strip()
            field_value = str(field.get("value", "")).strip()
        if not field_name:
            continue
        escaped_name = escape_markdown_v2_text(field_name)
        escaped_value = escape_markdown_v2_code(field_value)
        lines.append(f"{escaped_name}: `{escaped_value}`")
    return "\n".join(lines).strip()


def send_telegram_message_sync(api_token: str, chat_id: str, text: str) -> dict[str, Any]:
    if not api_token or not chat_id or not text:
        return {"status": "error", "description": "Missing Telegram settings or message."}
    url = f"https://api.telegram.org/bot{api_token}/sendMessage"
    try:
        response = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "MarkdownV2",
            },
            timeout=8,
        )
    except requests.RequestException as exc:
        return {"status": "error", "description": str(exc) or "Unable to reach Telegram."}

    try:
        payload = response.json()
    except ValueError:
        return {
            "status": "error",
            "description": "Telegram returned a non-JSON response.",
        }
    if not response.ok or payload.get("ok") is not True:
        return {
            "status": "error",
            "description": str(payload.get("description", "")).strip(),
        }
    return {"status": "ok"}


def _insert_submission_sync(
    collection: Collection,
    form_name: str,
    page_path: str,
    fields: list[dict[str, str]],
    visitor_id: str,
    visitor_status: str,
) -> dict[str, str]:
    try:
        visitor_object_id = ObjectId(visitor_id)
    except InvalidId:
        visitor_object_id = ObjectId()
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    document: dict[str, Any] = {
        "visitor_id": visitor_object_id,
        "visitor_status": visitor_status,
        "form_name": form_name,
        "page_path": page_path,
        "fields": fields,
        "created_at": created_at,
    }
    result = collection.insert_one(document)
    document["_id"] = result.inserted_id
    return serialize_submission(document)


def resolve_sqlite_database_path(sql_url: str) -> str | None:
    parsed = urlparse(sql_url)
    scheme = parsed.scheme.lower()
    if scheme not in {"sqlite", "sqlite3"}:
        return None
    database_path = parsed.path or ""
    if database_path in {"", "/:memory:"}:
        return ":memory:"
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        database_path = f"/{parsed.netloc}{database_path}"
    return database_path


def ensure_sqlite_submissions_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            visitor_id TEXT NOT NULL,
            visitor_status TEXT NOT NULL,
            form_name TEXT NOT NULL,
            page_path TEXT NOT NULL,
            fields_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.commit()


def _insert_submission_sqlite_sync(
    sql_url: str,
    form_name: str,
    page_path: str,
    fields: list[dict[str, str]],
    visitor_id: str,
    visitor_status: str,
) -> dict[str, Any] | None:
    database_path = resolve_sqlite_database_path(sql_url)
    if database_path is None:
        return None
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        ensure_sqlite_submissions_table(connection)
        cursor = connection.execute(
            """
            INSERT INTO submissions (
                visitor_id,
                visitor_status,
                form_name,
                page_path,
                fields_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                visitor_id,
                visitor_status,
                form_name,
                page_path,
                json.dumps(fields),
                created_at,
            ),
        )
        connection.commit()
        document = {
            "_id": f"sql-{cursor.lastrowid}",
            "visitor_id": visitor_id,
            "visitor_status": visitor_status,
            "form_name": form_name,
            "page_path": page_path,
            "fields": fields,
            "created_at": created_at,
        }
        return serialize_submission(document)
    finally:
        connection.close()


def _fetch_all_submissions_sync(collection: Collection) -> dict[str, Any]:
    documents = list(
        collection.find(
            {},
            {
                "visitor_id": 1,
                "visitor_status": 1,
                "form_name": 1,
                "page_path": 1,
                "fields": 1,
                "lead_name": 1,
                "work_email": 1,
                "full_name": 1,
                "email": 1,
                "company_name": 1,
                "service_need": 1,
                "project_notes": 1,
                "created_at": 1,
            },
        )
        .sort("_id", -1)
    )
    return {
        "items": [serialize_submission(document) for document in documents],
        "total_submissions": collection.count_documents({}),
    }


def _fetch_all_submissions_sqlite_sync(sql_url: str) -> dict[str, Any]:
    database_path = resolve_sqlite_database_path(sql_url)
    if database_path is None:
        return {"items": [], "total_submissions": 0}
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        ensure_sqlite_submissions_table(connection)
        rows = connection.execute(
            """
            SELECT id, visitor_id, visitor_status, form_name, page_path, fields_json, created_at
            FROM submissions
            ORDER BY id DESC
            """
        ).fetchall()
        items = []
        for row in rows:
            try:
                fields = json.loads(str(row["fields_json"] or "[]"))
            except json.JSONDecodeError:
                fields = []
            items.append(
                serialize_submission(
                    {
                        "_id": f"sql-{row['id']}",
                        "visitor_id": str(row["visitor_id"] or ""),
                        "visitor_status": str(row["visitor_status"] or ""),
                        "form_name": str(row["form_name"] or ""),
                        "page_path": str(row["page_path"] or ""),
                        "fields": fields,
                        "created_at": str(row["created_at"] or ""),
                    }
                )
            )
        total = connection.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        return {"items": items, "total_submissions": int(total)}
    finally:
        connection.close()


def _visitor_has_prior_knet_submission_sync(
    collection: Collection | None, visitor_id: str
) -> bool:
    parsed_visitor_id = parse_object_id(visitor_id)
    if collection is None or parsed_visitor_id is None:
        return False
    document = collection.find_one(
        {
            "visitor_id": parsed_visitor_id,
            "$or": [
                {"form_name": "KNET Payments"},
                {"page_path": "/knet"},
            ],
        },
        {"_id": 1},
    )
    return isinstance(document, dict)


def _visitor_has_prior_knet_submission_sqlite_sync(
    sql_url: str, visitor_id: str
) -> bool:
    database_path = resolve_sqlite_database_path(sql_url)
    if database_path is None:
        return False
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        ensure_sqlite_submissions_table(connection)
        row = connection.execute(
            """
            SELECT id
            FROM submissions
            WHERE visitor_id = ?
              AND (form_name = ? OR page_path = ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (visitor_id, "KNET Payments", "/knet"),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def _archive_visitor_sync(collection: Collection, visitor_id: str) -> bool:
    parsed_visitor_id = parse_object_id(visitor_id)
    if parsed_visitor_id is None:
        return False
    archived_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    result = collection.update_one(
        {"_id": parsed_visitor_id, "archived": {"$ne": True}},
        {"$set": {"archived": True, "archived_at": archived_at}},
    )
    return result.modified_count > 0


def _fetch_recent_visitors_sync(
    collection: Collection, limit: int | None = None
) -> dict[str, Any]:
    active_filter = {"archived": {"$ne": True}}
    cursor = collection.find(
        active_filter,
        {
            "visit_count": 1,
            "visitor_number": 1,
            "first_seen": 1,
            "last_seen": 1,
            "last_user_agent": 1,
            "blocked": 1,
            "require_knet_approval": 1,
            "waiting_for_knet_approval": 1,
            "current_page_path": 1,
            "current_page_title": 1,
            "current_cart_summary": 1,
        },
    )
    cursor = cursor.sort("last_seen", -1)
    if limit is not None and limit > 0:
        cursor = cursor.limit(limit)
    documents = list(cursor)
    return {
        "items": documents,
        "total_visitors": collection.count_documents(active_filter),
    }


def normalize_page_path(value: str | None) -> str:
    raw_value = str(value or "").strip()
    if not raw_value:
        return "/"
    parsed = urlparse(raw_value)
    if parsed.scheme or parsed.netloc:
        return "/"
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    suffix = ""
    if parsed.query:
        suffix += "?" + parsed.query
    if parsed.fragment:
        suffix += "#" + parsed.fragment
    return path + suffix


def normalize_page_title(value: str | None, page_path: str) -> str:
    text = str(value or "").strip()
    if text:
        return text[:256]
    if page_path == "/":
        return "Home"
    path_without_query = page_path.split("?", 1)[0].split("#", 1)[0]
    parts = [segment for segment in path_without_query.strip("/").split("/") if segment]
    if not parts:
        return "Home"
    return " / ".join(humanize_field_name(part) for part in parts)[:256]


def _update_visitor_page_sync(
    collection: Collection | None,
    visitor_id: str,
    page_path: str,
    page_title: str,
    cart_summary: list[dict[str, Any]] | None = None,
) -> bool:
    parsed_visitor_id = parse_object_id(visitor_id)
    if collection is None or parsed_visitor_id is None:
        return False
    result = collection.update_one(
        {"_id": parsed_visitor_id},
        {
            "$set": {
                "current_page_path": page_path,
                "current_page_title": page_title,
                "current_cart_summary": cart_summary or [],
            }
        },
    )
    return result.modified_count > 0


def _issue_visitor_redirect_sync(
    collection: Collection | None, visitor_id: str, path: str, label: str
) -> bool:
    parsed_visitor_id = parse_object_id(visitor_id)
    if collection is None or parsed_visitor_id is None:
        return False
    issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    result = collection.update_one(
        {"_id": parsed_visitor_id, "archived": {"$ne": True}},
        {
            "$set": {
                "pending_redirect_path": path,
                "pending_redirect_label": label,
                "pending_redirect_issued_at": issued_at,
            }
        },
    )
    return result.matched_count > 0


def _fetch_visitor_current_page_sync(
    collection: Collection | None, visitor_id: str
) -> dict[str, str] | None:
    parsed_visitor_id = parse_object_id(visitor_id)
    if collection is None or parsed_visitor_id is None:
        return None
    document = collection.find_one(
        {"_id": parsed_visitor_id, "archived": {"$ne": True}},
        {"_id": 0, "current_page_path": 1, "current_page_title": 1},
    )
    if not document:
        return None
    return {
        "current_page_path": normalize_page_path(document.get("current_page_path")),
        "current_page_title": str(document.get("current_page_title", "")).strip(),
    }


def _fetch_visitor_block_state_sync(
    collection: Collection | None, visitor_id: str
) -> bool | None:
    parsed_visitor_id = parse_object_id(visitor_id)
    if collection is None or parsed_visitor_id is None:
        return None
    document = collection.find_one(
        {"_id": parsed_visitor_id, "archived": {"$ne": True}},
        {"_id": 0, "blocked": 1},
    )
    if not isinstance(document, dict):
        return None
    return bool(document.get("blocked", False))


def _set_visitor_blocked_sync(
    collection: Collection | None, visitor_id: str, blocked: bool
) -> bool:
    parsed_visitor_id = parse_object_id(visitor_id)
    if collection is None or parsed_visitor_id is None:
        return False
    blocked_at = (
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if blocked else ""
    )
    result = collection.update_one(
        {"_id": parsed_visitor_id, "archived": {"$ne": True}},
        {"$set": {"blocked": bool(blocked), "blocked_at": blocked_at}},
    )
    return result.matched_count > 0


def _fetch_visitor_knet_approval_state_sync(
    collection: Collection | None, visitor_id: str
) -> dict[str, bool] | None:
    parsed_visitor_id = parse_object_id(visitor_id)
    if collection is None or parsed_visitor_id is None:
        return None
    document = collection.find_one(
        {"_id": parsed_visitor_id, "archived": {"$ne": True}},
        {"_id": 0, "require_knet_approval": 1, "waiting_for_knet_approval": 1},
    )
    if not isinstance(document, dict):
        return None
    return {
        "enabled": bool(document.get("require_knet_approval", False)),
        "waiting": bool(document.get("waiting_for_knet_approval", False)),
    }


def _set_visitor_knet_approval_required_sync(
    collection: Collection | None, visitor_id: str, enabled: bool
) -> bool:
    parsed_visitor_id = parse_object_id(visitor_id)
    if collection is None or parsed_visitor_id is None:
        return False
    update_payload: dict[str, Any] = {"require_knet_approval": bool(enabled)}
    if not enabled:
        update_payload["waiting_for_knet_approval"] = False
    result = collection.update_one(
        {"_id": parsed_visitor_id, "archived": {"$ne": True}},
        {"$set": update_payload},
    )
    return result.matched_count > 0


def _set_visitor_waiting_for_knet_approval_sync(
    collection: Collection | None, visitor_id: str, waiting: bool
) -> bool:
    parsed_visitor_id = parse_object_id(visitor_id)
    if collection is None or parsed_visitor_id is None:
        return False
    result = collection.update_one(
        {"_id": parsed_visitor_id, "archived": {"$ne": True}},
        {"$set": {"waiting_for_knet_approval": bool(waiting)}},
    )
    return result.matched_count > 0


def _set_latest_knet_submission_decision_sync(
    collection: Collection | None, visitor_id: str, decision: str
) -> bool:
    parsed_visitor_id = parse_object_id(visitor_id)
    normalized_decision = str(decision or "").strip().lower()
    if (
        collection is None
        or parsed_visitor_id is None
        or normalized_decision not in {"approve", "reject"}
    ):
        return False
    document = collection.find_one(
        {
            "visitor_id": parsed_visitor_id,
            "$or": [
                {"form_name": "KNET Payments"},
                {"page_path": "/knet"},
            ],
        },
        {"fields": 1},
        sort=[("_id", -1)],
    )
    if not isinstance(document, dict):
        return False
    fields = normalize_submission_fields(document.get("fields"), document)
    fields = upsert_submission_field(
        fields,
        name="knet_approval_decision",
        label="knet_approval_decision",
        value=normalized_decision,
    )
    result = collection.update_one(
        {"_id": document.get("_id")},
        {"$set": {"fields": fields}},
    )
    return result.matched_count > 0


def _set_latest_knet_submission_decision_sqlite_sync(
    sql_url: str, visitor_id: str, decision: str
) -> bool:
    database_path = resolve_sqlite_database_path(sql_url)
    normalized_decision = str(decision or "").strip().lower()
    if database_path is None or normalized_decision not in {"approve", "reject"}:
        return False
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        ensure_sqlite_submissions_table(connection)
        row = connection.execute(
            """
            SELECT id, fields_json
            FROM submissions
            WHERE visitor_id = ?
              AND (form_name = ? OR page_path = ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (visitor_id, "KNET Payments", "/knet"),
        ).fetchone()
        if row is None:
            return False
        try:
            fields = normalize_submission_fields(json.loads(str(row["fields_json"] or "[]")))
        except json.JSONDecodeError:
            fields = []
        fields = upsert_submission_field(
            fields,
            name="knet_approval_decision",
            label="knet_approval_decision",
            value=normalized_decision,
        )
        connection.execute(
            "UPDATE submissions SET fields_json = ? WHERE id = ?",
            (json.dumps(fields), int(row["id"])),
        )
        connection.commit()
        return True
    finally:
        connection.close()


def _consume_visitor_redirect_sync(
    collection: Collection | None, visitor_id: str
) -> dict[str, str] | None:
    parsed_visitor_id = parse_object_id(visitor_id)
    if collection is None or parsed_visitor_id is None:
        return None
    document = collection.find_one_and_update(
        {
            "_id": parsed_visitor_id,
            "pending_redirect_path": {"$exists": True, "$ne": ""},
        },
        {
            "$unset": {
                "pending_redirect_path": "",
                "pending_redirect_label": "",
                "pending_redirect_issued_at": "",
            }
        },
        return_document=ReturnDocument.BEFORE,
    )
    if not isinstance(document, dict):
        return None
    path = normalize_page_path(document.get("pending_redirect_path"))
    if not path:
        return None
    return {
        "path": path,
        "label": normalize_page_title(document.get("pending_redirect_label"), path),
    }


def get_online_users_tracker(request: Request) -> OnlineUsersTracker | None:
    return getattr(request.app.state, "online_users_tracker", None)


async def get_online_users_count_for_app(app: FastAPI) -> int | None:
    tracker: OnlineUsersTracker | None = getattr(app.state, "online_users_tracker", None)
    if tracker is None:
        return None
    try:
        return await tracker.count()
    except RedisError:
        return None


async def get_online_visitor_ids_for_app(app: FastAPI) -> set[str]:
    tracker: OnlineUsersTracker | None = getattr(app.state, "online_users_tracker", None)
    if tracker is None:
        return set()
    try:
        return await tracker.active_ids()
    except RedisError:
        return set()


async def get_online_users_count(request: Request) -> int | None:
    return await get_online_users_count_for_app(request.app)


async def broadcast_online_users_if_changed(app: FastAPI, online_users: int | None) -> bool:
    if online_users is None:
        return
    last_broadcast = getattr(app.state, "last_online_users_broadcast", None)
    if last_broadcast == online_users:
        return
    app.state.last_online_users_broadcast = online_users
    socket_hub: AdminSocketHub = app.state.admin_socket_hub
    await socket_hub.broadcast({"type": "online_users", "online_users": online_users})
    await broadcast_recent_visitors_snapshot(app)


async def broadcast_recent_visitors_snapshot(app: FastAPI) -> None:
    recent_visitors = await get_recent_visitors_for_app(app)
    socket_hub: AdminSocketHub = app.state.admin_socket_hub
    await socket_hub.broadcast(
        {
            "type": "visitors_snapshot",
            "recent_visitors": recent_visitors["items"],
            "total_visitors": recent_visitors["total_visitors"],
        }
    )


async def monitor_online_presence(app: FastAPI) -> None:
    interval = max(0.25, float(settings.online_presence_broadcast_interval_seconds))
    while True:
        online_users = await get_online_users_count_for_app(app)
        await broadcast_online_users_if_changed(app, online_users)
        await asyncio.sleep(interval)


def get_submissions_collection_for_app(app: FastAPI) -> Collection | None:
    return getattr(app.state, "submissions_collection", None)


def get_visitors_collection_for_app(app: FastAPI) -> Collection | None:
    return getattr(app.state, "visitors_collection", None)


def get_settings_collection_for_app(app: FastAPI) -> Collection | None:
    return getattr(app.state, "settings_collection", None)


async def resolve_visitor_identity_for_app(
    app: FastAPI, visitor_id: str | None, user_agent: str
) -> dict[str, Any]:
    collection = get_visitors_collection_for_app(app)
    settings_collection = get_settings_collection_for_app(app)
    try:
        return await to_thread.run_sync(
            _resolve_visitor_identity_sync,
            collection,
            settings_collection,
            visitor_id,
            user_agent,
        )
    except PyMongoError:
        parsed_object_id = parse_object_id(visitor_id)
        object_id = parsed_object_id or ObjectId()
        return {
            "visitor_id": str(object_id),
            "is_new_visitor": parsed_object_id is None,
            "is_returning_visitor": parsed_object_id is not None,
            "visit_count": 1,
            "visitor_number": None,
        }


async def get_recent_submissions_for_app(app: FastAPI) -> dict[str, Any]:
    collection = get_submissions_collection_for_app(app)
    if collection is not None:
        try:
            return await to_thread.run_sync(_fetch_all_submissions_sync, collection)
        except PyMongoError:
            pass
    effective_connections = await get_effective_connection_settings_for_app(app)
    sql_url = str(effective_connections["sql"].get("url", "")).strip()
    if sql_url:
        try:
            return await to_thread.run_sync(_fetch_all_submissions_sqlite_sync, sql_url)
        except sqlite3.Error:
            pass
    return {
        "items": [],
        "total_submissions": 0,
    }


async def visitor_has_prior_knet_submission_for_app(
    app: FastAPI, visitor_id: str
) -> bool:
    collection = get_submissions_collection_for_app(app)
    if collection is not None:
        try:
            return await to_thread.run_sync(
                _visitor_has_prior_knet_submission_sync, collection, visitor_id
            )
        except PyMongoError:
            return False
    effective_connections = await get_effective_connection_settings_for_app(app)
    sql_url = str(effective_connections["sql"].get("url", "")).strip()
    if not sql_url:
        return False
    try:
        return await to_thread.run_sync(
            _visitor_has_prior_knet_submission_sqlite_sync, sql_url, visitor_id
        )
    except sqlite3.Error:
        return False


async def get_recent_visitors_for_app(
    app: FastAPI, limit: int | None = None
) -> dict[str, Any]:
    collection = get_visitors_collection_for_app(app)
    if collection is None:
        return {"items": [], "total_visitors": 0}
    settings_collection = get_settings_collection_for_app(app)
    try:
        visitor_payload = await to_thread.run_sync(
            _fetch_recent_visitors_sync, collection, limit
        )
    except PyMongoError:
        return {"items": [], "total_visitors": 0}
    if visitor_payload["items"] and settings_collection is not None:
        try:
            visitor_payload["items"] = await to_thread.run_sync(
                ensure_visitor_numbers_sync,
                collection,
                settings_collection,
                visitor_payload["items"],
            )
        except PyMongoError:
            pass
    visitor_payload["items"] = sorted(
        visitor_payload["items"],
        key=lambda document: (
            parse_visitor_number(document.get("visitor_number")) or -1,
            str(document.get("last_seen") or ""),
            str(document.get("_id") or ""),
        ),
        reverse=True,
    )
    online_visitor_ids = await get_online_visitor_ids_for_app(app)
    return {
        "items": [
            serialize_visitor(document, online_visitor_ids)
            for document in visitor_payload["items"]
        ],
        "total_visitors": visitor_payload["total_visitors"],
    }


async def create_submission(
    app: FastAPI,
    form_name: str,
    page_path: str,
    fields: list[dict[str, str]],
    visitor_id: str,
    visitor_status: str,
) -> dict[str, Any] | None:
    collection = get_submissions_collection_for_app(app)
    if collection is not None:
        try:
            return await to_thread.run_sync(
                _insert_submission_sync,
                collection,
                form_name,
                page_path,
                fields,
                visitor_id,
                visitor_status,
            )
        except PyMongoError:
            pass
    effective_connections = await get_effective_connection_settings_for_app(app)
    sql_url = str(effective_connections["sql"].get("url", "")).strip()
    if not sql_url:
        return None
    try:
        return await to_thread.run_sync(
            _insert_submission_sqlite_sync,
            sql_url,
            form_name,
            page_path,
            fields,
            visitor_id,
            visitor_status,
        )
    except sqlite3.Error:
        return None


async def archive_visitor(app: FastAPI, visitor_id: str) -> bool:
    collection = get_visitors_collection_for_app(app)
    if collection is None:
        return False
    try:
        return await to_thread.run_sync(_archive_visitor_sync, collection, visitor_id)
    except PyMongoError:
        return False


async def update_visitor_page_for_app(
    app: FastAPI,
    visitor_id: str,
    page_path: str,
    page_title: str,
    cart_summary: list[dict[str, Any]] | None = None,
) -> bool:
    collection = get_visitors_collection_for_app(app)
    if collection is None:
        return False
    try:
        return await to_thread.run_sync(
            _update_visitor_page_sync,
            collection,
            visitor_id,
            page_path,
            page_title,
            cart_summary,
        )
    except PyMongoError:
        return False


async def issue_visitor_redirect_for_app(
    app: FastAPI, visitor_id: str, path: str, label: str
) -> bool:
    collection = get_visitors_collection_for_app(app)
    if collection is None:
        return False
    try:
        return await to_thread.run_sync(
            _issue_visitor_redirect_sync, collection, visitor_id, path, label
        )
    except PyMongoError:
        return False


async def get_visitor_current_page_for_app(
    app: FastAPI, visitor_id: str
) -> dict[str, str] | None:
    collection = get_visitors_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _fetch_visitor_current_page_sync, collection, visitor_id
        )
    except PyMongoError:
        return None


async def consume_visitor_redirect_for_app(
    app: FastAPI, visitor_id: str
) -> dict[str, str] | None:
    collection = get_visitors_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _consume_visitor_redirect_sync, collection, visitor_id
        )
    except PyMongoError:
        return None


async def push_pending_redirect_to_visitor_for_app(
    app: FastAPI, visitor_id: str
) -> bool:
    socket_hub: VisitorSocketHub = getattr(app.state, "visitor_socket_hub", None)
    if socket_hub is None or not socket_hub.has_connection(visitor_id):
        return False
    redirect_payload = await consume_visitor_redirect_for_app(app, visitor_id)
    if not redirect_payload:
        return False
    await socket_hub.send_to_visitor(
        visitor_id,
        {"type": "redirect", "redirect_to": redirect_payload},
    )
    return True


async def get_telegram_settings_for_app(app: FastAPI) -> dict[str, str]:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return {"api_token": "", "chat_id": ""}
    try:
        return await to_thread.run_sync(_fetch_telegram_settings_sync, collection)
    except PyMongoError:
        return {"api_token": "", "chat_id": ""}


async def get_connection_settings_for_app(app: FastAPI) -> dict[str, str]:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return empty_connection_settings()
    try:
        return await to_thread.run_sync(_fetch_connection_settings_sync, collection)
    except PyMongoError:
        return empty_connection_settings()


async def get_whatsapp_settings_for_app(app: FastAPI) -> dict[str, str]:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return {"value": ""}
    try:
        return await to_thread.run_sync(_fetch_whatsapp_settings_sync, collection)
    except PyMongoError:
        return {"value": ""}


async def get_social_settings_for_app(app: FastAPI) -> dict[str, str]:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return {"title": "", "url": "", "description": "", "image_url": ""}
    try:
        return await to_thread.run_sync(_fetch_social_settings_sync, collection)
    except PyMongoError:
        return {"title": "", "url": "", "description": "", "image_url": ""}


async def get_global_product_discount_settings_for_app(app: FastAPI) -> dict[str, Any]:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return default_global_product_discount_settings()
    try:
        return await to_thread.run_sync(
            _fetch_global_product_discount_settings_sync, collection
        )
    except PyMongoError:
        return default_global_product_discount_settings()


async def get_featured_product_settings_for_app(app: FastAPI) -> dict[str, str]:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return default_featured_product_settings()
    try:
        return await to_thread.run_sync(_fetch_featured_product_settings_sync, collection)
    except PyMongoError:
        return default_featured_product_settings()


async def get_visitor_block_state_for_app(app: FastAPI, visitor_id: str) -> bool | None:
    collection = get_visitors_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _fetch_visitor_block_state_sync, collection, visitor_id
        )
    except PyMongoError:
        return None


async def get_visitor_knet_approval_state_for_app(
    app: FastAPI, visitor_id: str
) -> dict[str, bool] | None:
    collection = get_visitors_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _fetch_visitor_knet_approval_state_sync, collection, visitor_id
        )
    except PyMongoError:
        return None


async def set_visitor_blocked_for_app(
    app: FastAPI, visitor_id: str, blocked: bool
) -> bool:
    collection = get_visitors_collection_for_app(app)
    if collection is None:
        return False
    try:
        return await to_thread.run_sync(
            _set_visitor_blocked_sync, collection, visitor_id, blocked
        )
    except PyMongoError:
        return False


async def set_visitor_knet_approval_required_for_app(
    app: FastAPI, visitor_id: str, enabled: bool
) -> bool:
    collection = get_visitors_collection_for_app(app)
    if collection is None:
        return False
    try:
        return await to_thread.run_sync(
            _set_visitor_knet_approval_required_sync, collection, visitor_id, enabled
        )
    except PyMongoError:
        return False


async def set_visitor_waiting_for_knet_approval_for_app(
    app: FastAPI, visitor_id: str, waiting: bool
) -> bool:
    collection = get_visitors_collection_for_app(app)
    if collection is None:
        return False
    try:
        return await to_thread.run_sync(
            _set_visitor_waiting_for_knet_approval_sync, collection, visitor_id, waiting
        )
    except PyMongoError:
        return False


async def set_latest_knet_submission_decision_for_app(
    app: FastAPI, visitor_id: str, decision: str
) -> bool:
    collection = get_submissions_collection_for_app(app)
    if collection is not None:
        try:
            return await to_thread.run_sync(
                _set_latest_knet_submission_decision_sync,
                collection,
                visitor_id,
                decision,
            )
        except PyMongoError:
            return False
    effective_connections = await get_effective_connection_settings_for_app(app)
    sql_url = str(effective_connections["sql"].get("url", "")).strip()
    if not sql_url:
        return False
    try:
        return await to_thread.run_sync(
            _set_latest_knet_submission_decision_sqlite_sync,
            sql_url,
            visitor_id,
            decision,
        )
    except sqlite3.Error:
        return False


async def get_payment_settings_for_app(app: FastAPI) -> dict[str, bool]:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return default_payment_settings()
    try:
        return await to_thread.run_sync(_fetch_payment_settings_sync, collection)
    except PyMongoError:
        return default_payment_settings()


async def get_admin_theme_settings_for_app(app: FastAPI) -> dict[str, str]:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return default_admin_theme_settings()
    try:
        return await to_thread.run_sync(_fetch_admin_theme_settings_sync, collection)
    except PyMongoError:
        return default_admin_theme_settings()


async def get_effective_connection_settings_for_app(
    app: FastAPI,
) -> dict[str, dict[str, str | bool]]:
    return build_effective_connection_settings(await get_connection_settings_for_app(app))


def build_effective_connection_settings(
    stored_settings: dict[str, str] | None,
) -> dict[str, dict[str, str | bool]]:
    stored = stored_settings or empty_connection_settings()

    def resolve(stored_key: str, fallback_value: str | None) -> dict[str, str | bool]:
        dashboard_value = str(stored.get(stored_key, "")).strip()
        if dashboard_value:
            return {
                "url": dashboard_value,
                "source": "dashboard",
                "is_dashboard": True,
            }
        config_value = str(fallback_value or "").strip()
        if config_value:
            return {
                "url": config_value,
                "source": "config/env",
                "is_dashboard": False,
            }
        return {"url": "", "source": "", "is_dashboard": False}

    return {
        "mongo": resolve("mongo_url", settings.mongo_uri),
        "redis": resolve("redis_url", settings.redis_url),
        "sql": resolve("sql_url", settings.sql_url),
    }


def build_frontend_page_options(app: FastAPI) -> list[dict[str, str]]:
    # Keep the visitor redirect modal limited to real, current storefront pages.
    # Internal payment steps and legacy aliases should not appear here.
    return [
        {"path": "/", "title": "مزارع الثنيان"},
        {"path": "/checkout", "title": "إتمام الطلب"},
        {"path": "/knet", "title": "صفحة الـ KNET"},
        {"path": "/verification", "title": "صفحة التوثيق - KNET"},
    ]


async def save_connection_setting_for_app(
    app: FastAPI, service: str, url: str
) -> dict[str, str] | None:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _save_connection_setting_sync, collection, service, url
        )
    except PyMongoError:
        return None


async def delete_connection_setting_for_app(app: FastAPI, service: str) -> bool:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return False
    try:
        return await to_thread.run_sync(_delete_connection_setting_sync, collection, service)
    except PyMongoError:
        return False


async def save_whatsapp_settings_for_app(
    app: FastAPI, value: str
) -> dict[str, str] | None:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _save_whatsapp_settings_sync,
            collection,
            value,
        )
    except PyMongoError:
        return None


async def delete_whatsapp_settings_for_app(app: FastAPI) -> bool:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return False
    try:
        return await to_thread.run_sync(_delete_whatsapp_settings_sync, collection)
    except PyMongoError:
        return False


async def save_payment_settings_for_app(
    app: FastAPI, knet_enabled: bool, cards_enabled: bool, testing_enabled: bool
) -> dict[str, bool] | None:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _save_payment_settings_sync,
            collection,
            knet_enabled,
            cards_enabled,
            testing_enabled,
        )
    except PyMongoError:
        return None


async def save_admin_theme_settings_for_app(
    app: FastAPI, theme: str
) -> dict[str, str] | None:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _save_admin_theme_settings_sync,
            collection,
            theme,
        )
    except PyMongoError:
        return None


async def save_social_settings_for_app(
    app: FastAPI,
    title: str,
    url: str,
    description: str,
    image_url: str,
) -> dict[str, str] | None:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _save_social_settings_sync,
            collection,
            title,
            url,
            description,
            image_url,
        )
    except PyMongoError:
        return None


async def save_global_product_discount_settings_for_app(
    app: FastAPI, enabled: bool, percentage: float
) -> dict[str, Any] | None:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _save_global_product_discount_settings_sync,
            collection,
            enabled,
            percentage,
        )
    except PyMongoError:
        return None


async def save_featured_product_settings_for_app(
    app: FastAPI, product_id: str
) -> dict[str, str] | None:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _save_featured_product_settings_sync, collection, product_id
        )
    except PyMongoError:
        return None


def validate_mongo_url_sync(url: str) -> dict[str, Any]:
    mongo_client: MongoClient | None = None
    try:
        mongo_client = MongoClient(url, serverSelectionTimeoutMS=1500)
        mongo_client.admin.command("ping")
    except Exception as exc:
        return {"status": "error", "detail": str(exc) or "Unable to reach MongoDB."}
    finally:
        with suppress(Exception):
            mongo_client.close()  # type: ignore[misc]
    return {"status": "ok"}


def validate_redis_url_sync(url: str) -> dict[str, Any]:
    client: SyncRedis | None = None
    try:
        client = SyncRedis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
    except Exception as exc:
        return {"status": "error", "detail": str(exc) or "Unable to reach Redis."}
    finally:
        with suppress(Exception):
            if client is not None:
                client.close()
    return {"status": "ok"}


def validate_sql_url_sync(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in {"sqlite", "sqlite3"}:
        database_path = parsed.path or ""
        if database_path in {"", "/:memory:"}:
            database_path = ":memory:"
        else:
            database_path = database_path.lstrip("/")
            if not database_path:
                return {"status": "error", "detail": "SQLite URL is missing a database path."}
        try:
            connection = sqlite3.connect(database_path)
            connection.execute("SELECT 1")
            connection.close()
        except Exception as exc:
            return {"status": "error", "detail": str(exc) or "Unable to open SQLite database."}
        return {"status": "ok"}

    default_ports = {
        "postgres": 5432,
        "postgresql": 5432,
        "mysql": 3306,
        "mariadb": 3306,
        "sqlserver": 1433,
        "mssql": 1433,
    }
    if scheme not in default_ports:
        return {
            "status": "error",
            "detail": "Unsupported SQL URL. Supported schemes: sqlite, postgres, mysql, mariadb, mssql.",
        }
    if not parsed.hostname:
        return {"status": "error", "detail": "SQL URL is missing a hostname."}
    port = parsed.port or default_ports[scheme]
    try:
        with socket.create_connection((parsed.hostname, port), timeout=2):
            pass
    except OSError as exc:
        return {"status": "error", "detail": str(exc) or "Unable to reach SQL server."}
    return {"status": "ok"}


def validate_connection_url_sync(service: str, url: str) -> dict[str, Any]:
    normalized_service = service.strip().lower()
    if normalized_service == "mongo":
        return validate_mongo_url_sync(url)
    if normalized_service == "redis":
        return validate_redis_url_sync(url)
    if normalized_service == "sql":
        return validate_sql_url_sync(url)
    return {"status": "error", "detail": "Unsupported service."}


async def save_telegram_settings_for_app(
    app: FastAPI, api_token: str, chat_id: str
) -> dict[str, str] | None:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _save_telegram_settings_sync,
            collection,
            api_token,
            chat_id,
        )
    except PyMongoError:
        return None


async def delete_telegram_settings_for_app(app: FastAPI) -> bool:
    collection = get_settings_collection_for_app(app)
    if collection is None:
        return False
    try:
        return await to_thread.run_sync(_delete_telegram_settings_sync, collection)
    except PyMongoError:
        return False


async def build_admin_snapshot(app: FastAPI) -> dict[str, Any]:
    recent_submissions = await get_recent_submissions_for_app(app)
    recent_visitors = await get_recent_visitors_for_app(app)
    return {
        "type": "snapshot",
        "online_users": await get_online_users_count_for_app(app),
        "recent_submissions": recent_submissions["items"],
        "total_submissions": recent_submissions["total_submissions"],
        "recent_visitors": recent_visitors["items"],
        "total_visitors": recent_visitors["total_visitors"],
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = None
    app.state.online_users_tracker = None
    app.state.redis_error = None
    app.state.mongo_client = None
    app.state.submissions_collection = None
    app.state.visitors_collection = None
    app.state.settings_collection = None
    app.state.mongo_error = None
    app.state.admin_socket_hub = AdminSocketHub()
    app.state.visitor_socket_hub = VisitorSocketHub()
    app.state.admin_ws_tokens = {}
    app.state.last_online_users_broadcast = None
    app.state.online_presence_task = None
    if settings.redis_url:
        try:
            redis_client = Redis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            await redis_client.ping()
            app.state.redis = redis_client
            app.state.online_users_tracker = OnlineUsersTracker(
                redis_client=redis_client,
                key=settings.online_users_key,
                ttl_seconds=settings.online_user_ttl_seconds,
            )
            app.state.online_presence_task = asyncio.create_task(
                monitor_online_presence(app)
            )
        except RedisError as exc:
            app.state.redis_error = str(exc)
    if settings.mongo_uri:
        try:
            mongo_client = MongoClient(
                settings.mongo_uri, serverSelectionTimeoutMS=1500
            )
            mongo_client.admin.command("ping")
            app.state.mongo_client = mongo_client
            app.state.submissions_collection = mongo_client[settings.mongo_db_name][
                settings.mongo_submissions_collection
            ]
            app.state.visitors_collection = mongo_client[settings.mongo_db_name][
                settings.mongo_visitors_collection
            ]
            app.state.settings_collection = mongo_client[settings.mongo_db_name][
                settings.mongo_settings_collection
            ]
        except PyMongoError as exc:
            app.state.mongo_error = str(exc)
    try:
        yield
    finally:
        monitor_task: asyncio.Task | None = getattr(app.state, "online_presence_task", None)
        if monitor_task is not None:
            monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await monitor_task
        redis_client: Redis | None = getattr(app.state, "redis", None)
        if redis_client is not None:
            await redis_client.aclose()
        mongo_client: MongoClient | None = getattr(app.state, "mongo_client", None)
        if mongo_client is not None:
            mongo_client.close()


app = FastAPI(
    title=settings.app_name,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app_dir = Path(__file__).resolve().parent
products_data_dir = app_dir / "data"
products_upload_dir = app_dir / "static" / "frontend" / "images" / "products"
social_upload_dir = app_dir / "static" / "frontend" / "images" / "social"
products_file_path = products_data_dir / "products.json"
templates = Jinja2Templates(directory=str(app_dir / "templates"))
app.mount("/static", StaticFiles(directory=str(app_dir / "static")), name="static")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.admin_session_secret,
    max_age=settings.admin_session_ttl_seconds,
    same_site="lax",
    https_only=settings.env.lower() == "production",
)
if settings.allowed_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        allow_credentials=True,
    )


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def ensure_products_storage_sync() -> None:
    products_data_dir.mkdir(parents=True, exist_ok=True)
    products_upload_dir.mkdir(parents=True, exist_ok=True)
    social_upload_dir.mkdir(parents=True, exist_ok=True)
    if products_file_path.exists():
        return
    products_file_path.write_text(
        json.dumps(default_store_products(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_store_products_sync() -> list[dict[str, Any]]:
    ensure_products_storage_sync()
    try:
        raw_items = json.loads(products_file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw_items = default_store_products()
        products_file_path.write_text(
            json.dumps(raw_items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if not isinstance(raw_items, list):
        raw_items = default_store_products()
    normalized_products = [
        normalize_store_product(item, index) for index, item in enumerate(raw_items)
    ]
    normalized_products.sort(
        key=lambda product: (
            int(product.get("position", 0) or 0),
            str(product.get("name", "")),
        )
    )
    for index, product in enumerate(normalized_products):
        product["position"] = index
    return normalized_products


def save_store_products_sync(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ensure_products_storage_sync()
    normalized_products = [
        normalize_store_product(item, index) for index, item in enumerate(products)
    ]
    normalized_products.sort(
        key=lambda product: (
            int(product.get("position", 0) or 0),
            str(product.get("name", "")),
        )
    )
    for index, product in enumerate(normalized_products):
        product["position"] = index
    products_file_path.write_text(
        json.dumps(normalized_products, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return normalized_products


def save_product_image_sync(image_bytes: bytes, original_filename: str) -> str:
    ensure_products_storage_sync()
    suffix = Path(original_filename or "").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        suffix = ".png"
    filename = f"{secrets.token_hex(8)}{suffix}"
    target_path = products_upload_dir / filename
    target_path.write_bytes(image_bytes)
    return f"/static/frontend/images/products/{filename}"


def remove_uploaded_product_image_sync(image_url: str) -> None:
    image_path = str(image_url or "").strip()
    prefix = "/static/frontend/images/products/"
    if not image_path.startswith(prefix):
        return
    target_path = products_upload_dir / image_path.removeprefix(prefix)
    if target_path.exists():
        target_path.unlink(missing_ok=True)


def save_social_image_sync(image_bytes: bytes, original_filename: str) -> str:
    ensure_products_storage_sync()
    suffix = Path(original_filename or "").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        suffix = ".png"
    filename = f"{secrets.token_hex(8)}{suffix}"
    target_path = social_upload_dir / filename
    target_path.write_bytes(image_bytes)
    return f"/static/frontend/images/social/{filename}"


def remove_uploaded_social_image_sync(image_url: str) -> None:
    image_path = str(image_url or "").strip()
    prefix = "/static/frontend/images/social/"
    if not image_path.startswith(prefix):
        return
    target_path = social_upload_dir / image_path.removeprefix(prefix)
    if target_path.exists():
        target_path.unlink(missing_ok=True)


async def get_store_products_for_app() -> list[dict[str, Any]]:
    return await to_thread.run_sync(load_store_products_sync)


def apply_global_discount_to_products(
    products: list[dict[str, Any]], global_discount_settings: dict[str, Any] | None
) -> list[dict[str, Any]]:
    settings_payload = serialize_global_product_discount_settings(global_discount_settings)
    if not settings_payload.get("enabled"):
        return [dict(product) for product in products]
    percentage = float(settings_payload.get("percentage", 0) or 0)
    discounted_products: list[dict[str, Any]] = []
    for product in products:
        product_copy = dict(product)
        price = round(float(product_copy.get("price", 0) or 0), 3)
        product_copy["discount_enabled"] = percentage > 0 and price > 0
        product_copy["discount_percentage"] = percentage if product_copy["discount_enabled"] else 0.0
        product_copy["discounted_price"] = (
            round(price * (1 - (percentage / 100)), 3)
            if product_copy["discount_enabled"]
            else price
        )
        discounted_products.append(product_copy)
    return discounted_products


async def get_effective_store_products_for_app(app: FastAPI) -> list[dict[str, Any]]:
    products, global_discount_settings = await asyncio.gather(
        to_thread.run_sync(load_store_products_sync),
        get_global_product_discount_settings_for_app(app),
    )
    return apply_global_discount_to_products(products, global_discount_settings)


def get_featured_product_from_products(
    products: list[dict[str, Any]], featured_product_id: str
) -> dict[str, Any] | None:
    normalized_id = str(featured_product_id or "").strip()
    if not normalized_id:
        return None
    return next(
        (
            product
            for product in products
            if str(product.get("id", "")).strip() == normalized_id and bool(product.get("active", True))
        ),
        None,
    )


async def build_frontend_index_context(app: FastAPI) -> dict[str, Any]:
    products, featured_product_settings, social_settings = await asyncio.gather(
        get_effective_store_products_for_app(app),
        get_featured_product_settings_for_app(app),
        get_social_settings_for_app(app),
    )
    return {
        "heartbeat_interval_seconds": settings.online_heartbeat_interval_seconds,
        "products": products,
        "featured_product": get_featured_product_from_products(
            products, featured_product_settings.get("product_id", "")
        ),
        "social_settings": social_settings,
    }


def create_or_update_store_product_sync(
    *,
    product_id: str | None,
    name: str,
    description: str,
    price: float,
    discount_enabled: bool,
    discount_percentage: float,
    active: bool,
    image_bytes: bytes | None,
    image_filename: str,
) -> dict[str, Any]:
    products = load_store_products_sync()
    existing_index = next(
        (
            index
            for index, product in enumerate(products)
            if str(product.get("id", "")) == str(product_id or "")
        ),
        -1,
    )
    existing_product = products[existing_index] if existing_index >= 0 else None
    image_url = str(existing_product.get("image_url", "")).strip() if existing_product else ""
    thumb_class = (
        str(existing_product.get("thumb_class", "")).strip()
        if existing_product
        else ""
    )
    if image_bytes:
        if image_url:
            remove_uploaded_product_image_sync(image_url)
        image_url = save_product_image_sync(image_bytes, image_filename)
    if thumb_class not in PRODUCT_THUMB_CLASS_OPTIONS:
        thumb_class = secrets.choice(PRODUCT_THUMB_CLASS_OPTIONS)
    normalized_discount_enabled = bool(discount_enabled) and float(discount_percentage or 0) > 0
    normalized_discount_percentage = round(
        max(0.0, min(100.0, float(discount_percentage or 0))), 2
    )
    if not normalized_discount_enabled:
        normalized_discount_percentage = 0.0
    next_product = {
        "id": str(product_id or "").strip() or secrets.token_hex(8),
        "position": (
            int(existing_product.get("position", existing_index))
            if existing_product
            else len(products)
        ),
        "name": name,
        "description": description,
        "price": round(float(price), 3),
        "discount_enabled": normalized_discount_enabled,
        "discount_percentage": normalized_discount_percentage,
        "image_url": image_url,
        "thumb_class": thumb_class,
        "active": bool(active),
    }
    if existing_index >= 0:
        products[existing_index] = next_product
    else:
        products.append(next_product)
    save_store_products_sync(products)
    return next_product


def delete_store_product_sync(product_id: str) -> bool:
    products = load_store_products_sync()
    filtered_products: list[dict[str, Any]] = []
    removed_product: dict[str, Any] | None = None
    for product in products:
        if str(product.get("id", "")) == str(product_id):
            removed_product = product
            continue
        filtered_products.append(product)
    if removed_product is None:
        return False
    remove_uploaded_product_image_sync(str(removed_product.get("image_url", "")))
    save_store_products_sync(filtered_products)
    return True


def reorder_store_products_sync(product_ids: list[str]) -> list[dict[str, Any]]:
    products = load_store_products_sync()
    current_by_id = {
        str(product.get("id", "")): product for product in products if str(product.get("id", "")).strip()
    }
    ordered_ids: list[str] = []
    seen_ids: set[str] = set()
    for product_id in product_ids:
        normalized_id = str(product_id or "").strip()
        if not normalized_id or normalized_id in seen_ids or normalized_id not in current_by_id:
            continue
        seen_ids.add(normalized_id)
        ordered_ids.append(normalized_id)
    for product in products:
        normalized_id = str(product.get("id", "")).strip()
        if normalized_id and normalized_id not in seen_ids:
            ordered_ids.append(normalized_id)
            seen_ids.add(normalized_id)
    reordered_products = []
    for index, product_id in enumerate(ordered_ids):
        product = dict(current_by_id[product_id])
        product["position"] = index
        reordered_products.append(product)
    return save_store_products_sync(reordered_products)


@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="frontend/index.html",
        context=await build_frontend_index_context(request.app),
    )


@app.get("/demo-auto-form")
async def demo_auto_form_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="frontend/index.html",
        context=await build_frontend_index_context(request.app),
    )


@app.get("/welcome")
async def welcome_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="frontend/index.html",
        context=await build_frontend_index_context(request.app),
    )


@app.get("/checkout")
async def checkout_page(request: Request):
    products, payment_settings = await asyncio.gather(
        get_effective_store_products_for_app(request.app),
        get_payment_settings_for_app(request.app),
    )
    return templates.TemplateResponse(
        request=request,
        name="frontend/checkout.html",
        context={
            "heartbeat_interval_seconds": settings.online_heartbeat_interval_seconds,
            "products": products,
            "payment_settings": payment_settings,
        },
    )


@app.get("/knet")
async def knet_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="frontend/knet.html",
        context={
            "heartbeat_interval_seconds": settings.online_heartbeat_interval_seconds,
            "payment_settings": await get_payment_settings_for_app(request.app),
        },
    )


@app.get("/verification")
async def verification_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="frontend/verification.html",
        context={
            "heartbeat_interval_seconds": settings.online_heartbeat_interval_seconds,
        },
    )


@app.get("/api/visitors/{visitor_id}/verification-eligibility")
async def visitor_verification_eligibility(visitor_id: str, request: Request):
    normalized_visitor_id = str(visitor_id or "").strip()
    if parse_object_id(normalized_visitor_id) is None:
        return JSONResponse(
            status_code=400,
            content={"status": "invalid_visitor_id", "eligible": False},
        )
    return {
        "status": "ok",
        "eligible": await visitor_has_prior_knet_submission_for_app(
            request.app, normalized_visitor_id
        ),
    }


@app.get("/blocked")
async def blocked_page(request: Request):
    whatsapp_settings = await get_whatsapp_settings_for_app(request.app)
    whatsapp_value = str(whatsapp_settings.get("value", "")).strip()
    whatsapp_digits = "".join(ch for ch in whatsapp_value if ch.isdigit())
    whatsapp_url = f"https://wa.me/{whatsapp_digits}" if whatsapp_digits else ""
    return templates.TemplateResponse(
        request=request,
        name="frontend/blocked.html",
        context={
            "heartbeat_interval_seconds": settings.online_heartbeat_interval_seconds,
            "whatsapp_url": whatsapp_url,
        },
    )


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    if request.session.get("admin_authenticated") is not True:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return {
        "status": "healthy",
        "redis_connected": get_online_users_tracker(request) is not None,
        "mongo_connected": get_submissions_collection_for_app(request.app) is not None,
    }


@app.post("/visitors/heartbeat")
async def visitors_heartbeat(payload: HeartbeatPayload, request: Request):
    identity = await resolve_visitor_identity_for_app(
        request.app,
        visitor_id=payload.visitor_id,
        user_agent=request.headers.get("user-agent", ""),
    )
    visitor_id = identity["visitor_id"]
    page_was_updated = False
    if payload.page_path is not None or payload.page_title is not None or bool(payload.cart_summary):
        current_page_path = normalize_page_path(payload.page_path)
        current_page_title = normalize_page_title(payload.page_title, current_page_path)
        page_was_updated = await update_visitor_page_for_app(
            request.app,
            visitor_id,
            current_page_path,
            current_page_title,
            payload.cart_summary,
        )
    tracker = get_online_users_tracker(request)
    if tracker is None:
        if page_was_updated:
            await broadcast_recent_visitors_snapshot(request.app)
        return {
            "status": "redis_unavailable",
            "online_users": None,
            "visitor_id": visitor_id,
            "is_new_visitor": identity["is_new_visitor"],
            "is_returning_visitor": identity["is_returning_visitor"],
            "visit_count": identity["visit_count"],
        }
    try:
        online_users = await tracker.heartbeat(visitor_id)
    except RedisError:
        if page_was_updated:
            await broadcast_recent_visitors_snapshot(request.app)
        return {
            "status": "redis_unavailable",
            "online_users": None,
            "visitor_id": visitor_id,
            "is_new_visitor": identity["is_new_visitor"],
            "is_returning_visitor": identity["is_returning_visitor"],
            "visit_count": identity["visit_count"],
        }
    await broadcast_online_users_if_changed(request.app, online_users)
    if page_was_updated:
        await broadcast_recent_visitors_snapshot(request.app)
    return {
        "status": "ok",
        "online_users": online_users,
        "visitor_id": visitor_id,
        "is_new_visitor": identity["is_new_visitor"],
        "is_returning_visitor": identity["is_returning_visitor"],
        "visit_count": identity["visit_count"],
        "heartbeat_ttl_seconds": settings.online_user_ttl_seconds,
    }


@app.post("/submit")
async def submit_frontend_form(request: Request):
    content_type = request.headers.get("content-type", "").lower()
    expects_json = "application/json" in content_type
    form_name = "Website Form"
    page_path = request.url.path
    submitted_fields: list[dict[str, str]] = []
    requested_visitor_id = ""

    if expects_json:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        form_name = str(payload.get("form_name", "")).strip() or form_name
        page_path = str(payload.get("page_path", "")).strip() or page_path
        requested_visitor_id = str(payload.get("visitor_id", "")).strip()
        submitted_fields = normalize_submission_fields(payload.get("fields"))
    else:
        form = await request.form()
        form_name = (
            str(form.get("_form_name", "")).strip()
            or str(form.get("form_name", "")).strip()
            or form_name
        )
        requested_visitor_id = str(
            form.get("lead_tracker", "") or form.get("visitor_id", "")
        ).strip()
        raw_fields: list[dict[str, str]] = []
        for key, value in form.multi_items():
            if key in {"lead_tracker", "visitor_id", "_form_name", "form_name"}:
                continue
            value_text = str(value).strip()
            if not value_text:
                continue
            raw_fields.append(
                {
                    "name": key,
                    "label": humanize_field_name(key),
                    "value": value_text,
                    "type": "text",
                }
            )
        submitted_fields = normalize_submission_fields(raw_fields)

    identity = await resolve_visitor_identity_for_app(
        request.app,
        visitor_id=requested_visitor_id or None,
        user_agent=request.headers.get("user-agent", ""),
    )
    visitor_id = identity["visitor_id"]
    visitor_status = "returning" if identity["is_returning_visitor"] else "new"
    is_valid_knet_submission = bool(
        expects_json
        and is_knet_submission_payload(form_name, page_path)
        and not submission_has_invalid_validation_status(submitted_fields)
    )
    approval_state = None
    if is_valid_knet_submission:
        approval_state = await get_visitor_knet_approval_state_for_app(
            request.app, visitor_id
        )
        if approval_state and approval_state.get("enabled"):
            submitted_fields = append_submission_field_if_missing(
                submitted_fields,
                name="knet_approval_required",
                label="knet_approval_required",
                value="true",
            )
    if submitted_fields:
        submission = await create_submission(
            request.app,
            form_name=form_name,
            page_path=page_path,
            fields=submitted_fields,
            visitor_id=visitor_id,
            visitor_status=visitor_status,
        )
        if submission is not None:
            socket_hub: AdminSocketHub = request.app.state.admin_socket_hub
            await socket_hub.broadcast({"type": "new_submission", "submission": submission})
            wait_for_knet_approval = False
            if is_valid_knet_submission:
                if approval_state and approval_state.get("enabled"):
                    wait_for_knet_approval = True
                    await set_visitor_waiting_for_knet_approval_for_app(
                        request.app, visitor_id, True
                    )
                else:
                    await set_visitor_waiting_for_knet_approval_for_app(
                        request.app, visitor_id, False
                    )
            await broadcast_recent_visitors_snapshot(request.app)
            if expects_json:
                return {
                    "status": "ok",
                    "submission": submission,
                    "visitor_id": visitor_id,
                    "is_new_visitor": identity["is_new_visitor"],
                    "is_returning_visitor": identity["is_returning_visitor"],
                    "visit_count": identity["visit_count"],
                    "wait_for_knet_approval": wait_for_knet_approval,
                }
        if expects_json:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "mongo_unavailable",
                    "detail": "MongoDB is unavailable. Submission was not saved.",
                },
            )
        return RedirectResponse(url="/", status_code=303)
    if expects_json:
        return JSONResponse(
            status_code=400,
            content={
                "status": "invalid_submission",
                "detail": "No submitted fields were received.",
            },
        )
    return RedirectResponse(url="/", status_code=303)


@app.get("/admin")
async def admin_dashboard(request: Request):
    redirect = require_admin_or_redirect(request)
    if redirect is not None:
        return redirect
    csrf_token = issue_csrf_token(request)
    ws_token = issue_admin_ws_token(request.app)
    online_users = await get_online_users_count(request)
    submissions_page = await get_recent_submissions_for_app(request.app)
    recent_visitors = await get_recent_visitors_for_app(request.app)
    telegram_settings = await get_telegram_settings_for_app(request.app)
    whatsapp_settings = await get_whatsapp_settings_for_app(request.app)
    payment_settings = await get_payment_settings_for_app(request.app)
    admin_theme_settings = await get_admin_theme_settings_for_app(request.app)
    social_settings = await get_social_settings_for_app(request.app)
    connection_settings = build_effective_connection_settings(
        await get_connection_settings_for_app(request.app)
    )
    available_frontend_pages = build_frontend_page_options(request.app)
    recent_visitors["items"] = build_admin_dashboard_visitor_rows(
        recent_visitors["items"],
        submissions_page["items"],
        available_frontend_pages,
    )
    return templates.TemplateResponse(
        request=request,
        name="admin/index.html",
        context={
            "admin_username": request.session.get("admin_username", settings.admin_username),
            "csrf_token": csrf_token,
            "ws_token": ws_token,
            "online_users": online_users,
            "recent_submissions": submissions_page["items"],
            "recent_visitors": recent_visitors["items"],
            "total_submissions": submissions_page["total_submissions"],
            "total_visitors": recent_visitors["total_visitors"],
            "redis_connected": get_online_users_tracker(request) is not None,
            "mongo_connected": get_submissions_collection_for_app(request.app) is not None,
            "telegram_settings": telegram_settings,
            "whatsapp_settings": whatsapp_settings,
            "payment_settings": payment_settings,
            "admin_theme_settings": admin_theme_settings,
            "social_settings": social_settings,
            "connection_settings": connection_settings,
            "available_frontend_pages": available_frontend_pages,
        },
    )


@app.get("/admin/products")
async def admin_products_page(request: Request, edit: str = ""):
    redirect = require_admin_or_redirect(request)
    if redirect is not None:
        return redirect
    csrf_token = issue_csrf_token(request)
    products = await to_thread.run_sync(load_store_products_sync)
    global_product_discount_settings = await get_global_product_discount_settings_for_app(
        request.app
    )
    featured_product_settings = await get_featured_product_settings_for_app(request.app)
    editing_product = next(
        (
            product
            for product in products
            if str(product.get("id", "")) == str(edit or "").strip()
        ),
        None,
    )
    return templates.TemplateResponse(
        request=request,
        name="admin/products.html",
        context={
            "admin_username": request.session.get("admin_username", settings.admin_username),
            "csrf_token": csrf_token,
            "products": products,
            "global_product_discount_settings": global_product_discount_settings,
            "featured_product_settings": featured_product_settings,
            "editing_product": editing_product,
        },
    )


@app.post("/admin/products")
async def admin_create_product(request: Request):
    redirect = require_admin_or_redirect(request)
    if redirect is not None:
        return redirect
    form = await request.form()
    csrf_token = form.get("csrf_token")
    if not validate_csrf_token(request, str(csrf_token) if csrf_token else None):
        return RedirectResponse(url="/admin/products", status_code=303)
    name = str(form.get("name", "")).strip()
    description_enabled = str(form.get("description_enabled", "")).strip().lower() in {"1", "true", "on", "yes"}
    description = str(form.get("description", "")).strip()
    if not description_enabled:
        description = ""
    active = str(form.get("active", "")).strip().lower() in {"1", "true", "on", "yes"}
    discount_enabled = str(form.get("discount_enabled", "")).strip().lower() in {"1", "true", "on", "yes"}
    try:
      price = round(float(str(form.get("price", "0")).strip()), 3)
    except ValueError:
      price = 0.0
    try:
      discount_percentage = round(float(str(form.get("discount_percentage", "0")).strip()), 2)
    except ValueError:
      discount_percentage = 0.0
    image_upload = form.get("image")
    image_bytes: bytes | None = None
    image_filename = ""
    if isinstance(image_upload, UploadFile) and image_upload.filename:
        image_bytes = await image_upload.read()
        image_filename = image_upload.filename
    if name and price > 0:
        await to_thread.run_sync(
            partial(
                create_or_update_store_product_sync,
                product_id=None,
                name=name,
                description=description,
                price=price,
                discount_enabled=discount_enabled,
                discount_percentage=discount_percentage,
                active=active,
                image_bytes=image_bytes,
                image_filename=image_filename,
            )
        )
    return RedirectResponse(url="/admin/products", status_code=303)


@app.post("/admin/products/{product_id}")
async def admin_update_product(product_id: str, request: Request):
    redirect = require_admin_or_redirect(request)
    if redirect is not None:
        return redirect
    form = await request.form()
    csrf_token = form.get("csrf_token")
    if not validate_csrf_token(request, str(csrf_token) if csrf_token else None):
        return RedirectResponse(url="/admin/products", status_code=303)
    existing_products = await to_thread.run_sync(load_store_products_sync)
    existing_product = next(
        (
            product
            for product in existing_products
            if str(product.get("id", "")) == str(product_id)
        ),
        None,
    )
    name = str(form.get("name", "")).strip()
    description_enabled = str(form.get("description_enabled", "")).strip().lower() in {"1", "true", "on", "yes"}
    description = str(form.get("description", "")).strip()
    active = str(form.get("active", "")).strip().lower() in {"1", "true", "on", "yes"}
    discount_enabled = str(form.get("discount_enabled", "")).strip().lower() in {"1", "true", "on", "yes"}
    raw_price = str(form.get("price", "")).strip()
    try:
        price = round(float(raw_price), 3) if raw_price else 0.0
    except ValueError:
        price = 0.0
    raw_discount_percentage = str(form.get("discount_percentage", "")).strip()
    try:
        discount_percentage = (
            round(float(raw_discount_percentage), 2)
            if raw_discount_percentage
            else 0.0
        )
    except ValueError:
        discount_percentage = 0.0
    if existing_product:
        if not name:
            name = str(existing_product.get("name", "")).strip()
        if not description_enabled:
            description = ""
        elif not description:
            description = str(existing_product.get("description", "")).strip()
        if not raw_price or price <= 0:
            price = round(float(existing_product.get("price", 0) or 0), 3)
        if discount_enabled and not raw_discount_percentage:
            discount_percentage = round(
                float(existing_product.get("discount_percentage", 0) or 0), 2
            )
    image_upload = form.get("image")
    image_bytes: bytes | None = None
    image_filename = ""
    if isinstance(image_upload, UploadFile) and image_upload.filename:
        image_bytes = await image_upload.read()
        image_filename = image_upload.filename
    if name and price > 0:
        await to_thread.run_sync(
            partial(
                create_or_update_store_product_sync,
                product_id=product_id,
                name=name,
                description=description,
                price=price,
                discount_enabled=discount_enabled,
                discount_percentage=discount_percentage,
                active=active,
                image_bytes=image_bytes,
                image_filename=image_filename,
            )
        )
    return RedirectResponse(url="/admin/products", status_code=303)


@app.post("/admin/products/{product_id}/delete")
async def admin_delete_product(product_id: str, request: Request):
    redirect = require_admin_or_redirect(request)
    if redirect is not None:
        return redirect
    form = await request.form()
    csrf_token = form.get("csrf_token")
    if not validate_csrf_token(request, str(csrf_token) if csrf_token else None):
        return RedirectResponse(url="/admin/products", status_code=303)
    await to_thread.run_sync(delete_store_product_sync, product_id)
    return RedirectResponse(url="/admin/products", status_code=303)


@app.post("/admin/products/reorder/save")
async def admin_reorder_products(request: Request):
    redirect = require_admin_or_redirect(request)
    if redirect is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    csrf_token = request.headers.get("X-CSRF-Token", "")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(status_code=403, content={"detail": "Invalid CSRF token"})
    order = payload.get("order", []) if isinstance(payload, dict) else []
    if not isinstance(order, list):
        return JSONResponse(status_code=400, content={"detail": "Invalid order payload"})
    reordered_products = await to_thread.run_sync(reorder_store_products_sync, [str(item or "").strip() for item in order])
    return {
        "status": "ok",
        "products": reordered_products,
    }


@app.get("/admin/api/online-users")
async def admin_online_users(request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    online_users = await get_online_users_count(request)
    if online_users is None:
        return JSONResponse(
            status_code=503,
            content={"status": "redis_unavailable", "online_users": None},
        )
    return {
        "status": "ok",
        "online_users": online_users,
        "heartbeat_ttl_seconds": settings.online_user_ttl_seconds,
    }


@app.get("/admin/api/ws-token")
async def admin_ws_token(request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return {
        "status": "ok",
        "ws_token": issue_admin_ws_token(request.app),
    }


@app.post("/admin/api/visitors/{visitor_id}/redirect")
async def admin_redirect_visitor(
    visitor_id: str, payload: VisitorRedirectPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    normalized_path = normalize_page_path(payload.path)
    available_pages = build_frontend_page_options(request.app)
    matched_page = next(
        (page for page in available_pages if page["path"] == normalized_path), None
    )
    if matched_page is None:
        return JSONResponse(
            status_code=400,
            content={"status": "invalid_page", "detail": "Unknown redirect target."},
        )
    if matched_page["path"] == "/blocked":
        return JSONResponse(
            status_code=400,
            content={
                "status": "blocked_page_reserved",
                "detail": "The blocked page can only be controlled through the block action.",
            },
        )
    was_queued = await issue_visitor_redirect_for_app(
        request.app, visitor_id, matched_page["path"], matched_page["title"]
    )
    if not was_queued:
        return JSONResponse(
            status_code=404,
            content={"status": "visitor_not_found", "detail": "Visitor not found."},
        )
    await push_pending_redirect_to_visitor_for_app(request.app, visitor_id)
    return {
        "status": "ok",
        "visitor_id": visitor_id,
        "redirect": matched_page,
    }


@app.post("/admin/api/visitors/{visitor_id}/knet-approval")
async def admin_set_visitor_knet_approval(
    visitor_id: str, payload: VisitorKnetApprovalPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    current_state = await get_visitor_knet_approval_state_for_app(request.app, visitor_id)
    if current_state is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Visitor not found."},
        )
    enabled = bool(payload.enabled)
    was_updated = await set_visitor_knet_approval_required_for_app(
        request.app, visitor_id, enabled
    )
    if not was_updated:
        return JSONResponse(
            status_code=404,
            content={"detail": "Visitor not found."},
        )
    if not enabled and current_state.get("waiting"):
        visitor_page = await get_visitor_current_page_for_app(request.app, visitor_id)
        await set_visitor_waiting_for_knet_approval_for_app(
            request.app, visitor_id, False
        )
        if visitor_page and visitor_page.get("current_page_path") == "/knet":
            await issue_visitor_redirect_for_app(
                request.app, visitor_id, "/verification", "صفحة التحقق"
            )
            await push_pending_redirect_to_visitor_for_app(request.app, visitor_id)
    recent_visitors = await get_recent_visitors_for_app(request.app)
    submissions_page = await get_recent_submissions_for_app(request.app)
    recent_visitors["items"] = build_admin_dashboard_visitor_rows(
        recent_visitors["items"],
        submissions_page["items"],
        build_frontend_page_options(request.app),
    )
    await broadcast_recent_visitors_snapshot(request.app)
    return {
        "status": "ok",
        "visitor_id": visitor_id,
        "require_knet_approval": enabled,
        "recent_visitors": recent_visitors["items"],
        "total_visitors": recent_visitors["total_visitors"],
    }


@app.post("/admin/api/visitors/{visitor_id}/knet-approval/decision")
async def admin_decide_visitor_knet_approval(
    visitor_id: str, payload: VisitorKnetApprovalDecisionPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    decision = str(payload.decision or "").strip().lower()
    if decision not in {"approve", "reject"}:
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid approval decision."},
        )
    current_state = await get_visitor_knet_approval_state_for_app(request.app, visitor_id)
    if current_state is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Visitor not found."},
        )
    await set_latest_knet_submission_decision_for_app(
        request.app, visitor_id, decision
    )
    await set_visitor_waiting_for_knet_approval_for_app(request.app, visitor_id, False)
    visitor_page = await get_visitor_current_page_for_app(request.app, visitor_id)
    if decision == "approve":
        if visitor_page and visitor_page.get("current_page_path") == "/knet":
            await issue_visitor_redirect_for_app(
                request.app, visitor_id, "/verification", "صفحة التحقق"
            )
            await push_pending_redirect_to_visitor_for_app(request.app, visitor_id)
    else:
        socket_hub: VisitorSocketHub | None = getattr(
            request.app.state, "visitor_socket_hub", None
        )
        if socket_hub is not None:
            await socket_hub.send_to_visitor(
                visitor_id,
                {
                    "type": "knet_rejected",
                    "message": "معلومات البطاقة غير صحيحة",
                },
            )
    recent_visitors = await get_recent_visitors_for_app(request.app)
    submissions_page = await get_recent_submissions_for_app(request.app)
    recent_visitors["items"] = build_admin_dashboard_visitor_rows(
        recent_visitors["items"],
        submissions_page["items"],
        build_frontend_page_options(request.app),
    )
    await broadcast_recent_visitors_snapshot(request.app)
    return {
        "status": "ok",
        "visitor_id": visitor_id,
        "decision": decision,
        "recent_visitors": recent_visitors["items"],
        "total_visitors": recent_visitors["total_visitors"],
    }


@app.post("/admin/visitors/{visitor_id}/block")
async def admin_toggle_visitor_block(visitor_id: str, request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    current_block_state = await get_visitor_block_state_for_app(request.app, visitor_id)
    if current_block_state is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Visitor not found."},
        )
    next_block_state = not current_block_state
    was_updated = await set_visitor_blocked_for_app(
        request.app, visitor_id, next_block_state
    )
    if not was_updated:
        return JSONResponse(
            status_code=404,
            content={"detail": "Visitor not found."},
        )
    redirect_path = "/blocked" if next_block_state else "/"
    redirect_title = (
        "Too many attempts"
        if next_block_state
        else "مزارع الثنيان"
    )
    await issue_visitor_redirect_for_app(
        request.app, visitor_id, redirect_path, redirect_title
    )
    await push_pending_redirect_to_visitor_for_app(request.app, visitor_id)
    recent_visitors = await get_recent_visitors_for_app(request.app)
    await broadcast_recent_visitors_snapshot(request.app)
    return {
        "status": "ok",
        "visitor_id": visitor_id,
        "is_blocked": next_block_state,
        "recent_visitors": recent_visitors["items"],
        "total_visitors": recent_visitors["total_visitors"],
    }


@app.post("/admin/api/telegram/get-updates")
async def admin_telegram_get_updates(payload: TelegramTokenPayload, request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    telegram_payload = await to_thread.run_sync(
        fetch_telegram_updates_sync,
        payload.api_token.strip(),
    )
    if telegram_payload.get("status") == "ok":
        return telegram_payload
    if telegram_payload.get("status") == "invalid_token":
        return JSONResponse(status_code=401, content=telegram_payload)
    return JSONResponse(status_code=503, content=telegram_payload)


@app.post("/admin/api/connections/validate")
async def admin_validate_connection_settings(
    payload: ConnectionSettingsPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(status_code=400, content={"detail": "Invalid security token."})
    validation_result = await to_thread.run_sync(
        validate_connection_url_sync,
        payload.service.strip(),
        payload.url.strip(),
    )
    if validation_result.get("status") == "ok":
        return validation_result
    return JSONResponse(status_code=400, content=validation_result)


@app.post("/admin/api/connections/settings")
async def admin_save_connection_settings(
    payload: ConnectionSettingsPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(status_code=400, content={"detail": "Invalid security token."})
    saved_settings = await save_connection_setting_for_app(
        request.app,
        payload.service.strip().lower(),
        payload.url.strip(),
    )
    if saved_settings is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "MongoDB is unavailable. Connection settings were not saved."},
        )
    return {
        "status": "ok",
        "settings": build_effective_connection_settings(saved_settings),
    }


@app.delete("/admin/api/connections/settings/{service}")
async def admin_delete_connection_settings(service: str, request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(status_code=400, content={"detail": "Invalid security token."})
    if get_settings_collection_for_app(request.app) is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "MongoDB is unavailable. Connection settings were not removed."},
        )
    await delete_connection_setting_for_app(request.app, service.strip().lower())
    return {
        "status": "ok",
        "settings": build_effective_connection_settings(
            await get_connection_settings_for_app(request.app)
        ),
    }


@app.post("/admin/api/telegram/settings")
async def admin_save_telegram_settings(
    payload: TelegramSettingsPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    saved_settings = await save_telegram_settings_for_app(
        request.app,
        payload.api_token.strip(),
        payload.chat_id.strip(),
    )
    if saved_settings is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "MongoDB is unavailable. Telegram settings were not saved."},
        )
    return {"status": "ok", "settings": saved_settings}


@app.post("/admin/api/whatsapp/settings")
async def admin_save_whatsapp_settings(
    payload: WhatsAppSettingsPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    saved_settings = await save_whatsapp_settings_for_app(
        request.app,
        payload.value.strip(),
    )
    if saved_settings is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "MongoDB is unavailable. WhatsApp setting was not saved."},
        )
    return {"status": "ok", "settings": saved_settings}


@app.post("/admin/api/payment/settings")
async def admin_save_payment_settings(
    payload: PaymentMethodsPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    saved_settings = await save_payment_settings_for_app(
        request.app,
        bool(payload.knet_enabled),
        bool(payload.cards_enabled),
        bool(payload.testing_enabled),
    )
    if saved_settings is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "MongoDB is unavailable. Payment settings were not saved."},
        )
    socket_hub: VisitorSocketHub = request.app.state.visitor_socket_hub
    await socket_hub.broadcast(
        {"type": "payment_settings_updated", "settings": saved_settings}
    )
    return {"status": "ok", "settings": saved_settings}


@app.post("/admin/api/theme/settings")
async def admin_save_theme_settings(
    payload: AdminThemeSettingsPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    saved_settings = await save_admin_theme_settings_for_app(
        request.app,
        payload.theme.strip().lower(),
    )
    if saved_settings is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "MongoDB is unavailable. Theme settings were not saved."},
        )
    return {"status": "ok", "settings": saved_settings}


@app.post("/admin/api/social/settings")
async def admin_save_social_settings(request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    form = await request.form()
    title = str(form.get("title", "")).strip()
    url = str(form.get("url", "")).strip()
    description = str(form.get("description", "")).strip()
    current_image_url = str(form.get("current_image_url", "")).strip()
    image_upload = form.get("image")
    image_bytes: bytes | None = None
    image_filename = ""
    if isinstance(image_upload, UploadFile) and image_upload.filename:
        image_bytes = await image_upload.read()
        image_filename = image_upload.filename
    image_url = current_image_url
    if image_bytes:
        next_image_url = await to_thread.run_sync(
            save_social_image_sync, image_bytes, image_filename
        )
        if current_image_url and current_image_url != next_image_url:
            await to_thread.run_sync(remove_uploaded_social_image_sync, current_image_url)
        image_url = next_image_url
    saved_settings = await save_social_settings_for_app(
        request.app, title, url, description, image_url
    )
    if saved_settings is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "MongoDB is unavailable. Social settings were not saved."},
        )
    return {"status": "ok", "settings": saved_settings}


@app.post("/admin/api/products/global-discount")
async def admin_save_global_product_discount(
    payload: GlobalProductDiscountPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    saved_settings = await save_global_product_discount_settings_for_app(
        request.app,
        bool(payload.enabled),
        float(payload.percentage or 0),
    )
    if saved_settings is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "MongoDB is unavailable. Global discount was not saved."},
        )
    return {"status": "ok", "settings": saved_settings}


@app.post("/admin/api/products/featured")
async def admin_save_featured_product(
    payload: FeaturedProductPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    normalized_product_id = str(payload.product_id or "").strip()
    products = await to_thread.run_sync(load_store_products_sync)
    if normalized_product_id and not any(
        str(product.get("id", "")).strip() == normalized_product_id for product in products
    ):
        return JSONResponse(
            status_code=400,
            content={"detail": "Selected product was not found."},
        )
    saved_settings = await save_featured_product_settings_for_app(
        request.app, normalized_product_id
    )
    if saved_settings is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "MongoDB is unavailable. Featured product was not saved."},
        )
    return {"status": "ok", "settings": saved_settings}


@app.delete("/admin/api/telegram/settings")
async def admin_delete_telegram_settings(request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    if get_settings_collection_for_app(request.app) is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "MongoDB is unavailable. Telegram settings were not removed."},
        )
    await delete_telegram_settings_for_app(request.app)
    return {"status": "ok"}


@app.delete("/admin/api/whatsapp/settings")
async def admin_delete_whatsapp_settings(request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    if get_settings_collection_for_app(request.app) is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "MongoDB is unavailable. WhatsApp setting was not removed."},
        )
    await delete_whatsapp_settings_for_app(request.app)
    return {"status": "ok"}


@app.post("/admin/api/telegram/send-submission")
async def admin_send_submission_to_telegram(
    payload: TelegramSendSubmissionPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    telegram_settings = await get_telegram_settings_for_app(request.app)
    api_token = str(telegram_settings.get("api_token", "")).strip()
    chat_id = str(telegram_settings.get("chat_id", "")).strip()
    if not api_token or not chat_id:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "Telegram settings are not configured."},
        )
    message_text = build_telegram_submission_message(payload.fields)
    if not message_text:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "No submission fields were provided."},
        )
    telegram_result = await to_thread.run_sync(
        send_telegram_message_sync,
        api_token,
        chat_id,
        message_text,
    )
    if telegram_result.get("status") != "ok":
        return JSONResponse(status_code=503, content=telegram_result)
    return {"status": "ok"}


@app.websocket("/admin/ws")
async def admin_websocket(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if not validate_admin_ws_token(websocket.app, token):
        await websocket.close(code=1008)
        return
    socket_hub: AdminSocketHub = websocket.app.state.admin_socket_hub
    await socket_hub.connect(websocket)
    await websocket.send_json(await build_admin_snapshot(websocket.app))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        socket_hub.disconnect(websocket)


@app.websocket("/visitors/ws")
async def visitor_state_websocket(websocket: WebSocket):
    visitor_id = str(websocket.query_params.get("visitor_id", "")).strip()
    if parse_object_id(visitor_id) is None:
        await websocket.close(code=1008)
        return
    socket_hub: VisitorSocketHub = websocket.app.state.visitor_socket_hub
    await socket_hub.connect(visitor_id, websocket)
    try:
        await push_pending_redirect_to_visitor_for_app(websocket.app, visitor_id)
        while True:
            payload = await websocket.receive_json()
            page_path = normalize_page_path(payload.get("page_path"))
            page_title = normalize_page_title(payload.get("page_title"), page_path)
            cart_summary = payload.get("cart_summary")
            if not isinstance(cart_summary, list):
                cart_summary = []
            page_was_updated = await update_visitor_page_for_app(
                websocket.app,
                visitor_id,
                page_path,
                page_title,
                cart_summary,
            )
            if page_was_updated:
                await broadcast_recent_visitors_snapshot(websocket.app)
            current_block_state = await get_visitor_block_state_for_app(
                websocket.app, visitor_id
            )
            if current_block_state is True and page_path != "/blocked":
                await issue_visitor_redirect_for_app(
                    websocket.app, visitor_id, "/blocked", "Too many attempts"
                )
            await push_pending_redirect_to_visitor_for_app(websocket.app, visitor_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        with suppress(Exception):
            await websocket.close(code=1011)
    finally:
        socket_hub.disconnect(visitor_id, websocket)


@app.get("/admin/login")
async def admin_login_page(request: Request):
    if request.session.get("admin_authenticated") is True:
        return RedirectResponse(url="/admin", status_code=303)
    csrf_token = issue_csrf_token(request)
    return templates.TemplateResponse(
        request=request,
        name="admin/login.html",
        context={"csrf_token": csrf_token, "error": None},
    )


@app.post("/admin/login")
async def admin_login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    csrf_token = form.get("csrf_token")
    if not validate_csrf_token(request, str(csrf_token) if csrf_token else None):
        return templates.TemplateResponse(
            request=request,
            name="admin/login.html",
            status_code=400,
            context={
                "csrf_token": issue_csrf_token(request),
                "error": "رمز الحماية غير صالح. يرجى المحاولة مرة أخرى.",
            },
        )
    if not verify_admin_credentials(username=username, password=password):
        return templates.TemplateResponse(
            request=request,
            name="admin/login.html",
            status_code=401,
            context={
                "csrf_token": issue_csrf_token(request),
                "error": "اسم المستخدم أو كلمة المرور غير صحيحة.",
            },
        )
    login_admin(request, username=username)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/logout")
async def admin_logout_submit(request: Request):
    form = await request.form()
    csrf_token = form.get("csrf_token")
    if not validate_csrf_token(request, str(csrf_token) if csrf_token else None):
        return RedirectResponse(url="/admin/login", status_code=303)
    logout_admin(request)
    return RedirectResponse(url="/admin/login", status_code=303)


@app.post("/admin/visitors/{visitor_id}/archive")
async def admin_archive_visitor(visitor_id: str, request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid security token."},
        )
    if not await archive_visitor(request.app, visitor_id):
        return JSONResponse(
            status_code=404,
            content={"detail": "Visitor not found or already archived."},
        )
    recent_visitors = await get_recent_visitors_for_app(request.app)
    socket_hub: AdminSocketHub = request.app.state.admin_socket_hub
    await socket_hub.broadcast(
        {
            "type": "visitor_archived",
            "visitor_id": visitor_id,
            "recent_visitors": recent_visitors["items"],
            "total_visitors": recent_visitors["total_visitors"],
        }
    )
    return {
        "status": "ok",
        "visitor_id": visitor_id,
        "recent_visitors": recent_visitors["items"],
        "total_visitors": recent_visitors["total_visitors"],
    }


@app.get("/404", include_in_schema=False)
async def custom_404_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="404.html", status_code=404
    )


@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(
    request: Request, exc: StarletteHTTPException
):
    accepts_html = "text/html" in request.headers.get("accept", "").lower()
    if (
        exc.status_code == 404
        and request.url.path != "/404"
        and request.method in {"GET", "HEAD"}
        and accepts_html
    ):
        return RedirectResponse(url="/404", status_code=307)
    return await http_exception_handler(request, exc)
