import secrets
from hmac import compare_digest

from fastapi import Request
from fastapi.responses import RedirectResponse

from .config import settings


ADMIN_AUTH_KEY = "admin_authenticated"
ADMIN_USER_KEY = "admin_username"
CSRF_TOKEN_KEY = "csrf_token"


def is_admin_authenticated(request: Request) -> bool:
    return request.session.get(ADMIN_AUTH_KEY) is True


def verify_admin_credentials(username: str, password: str) -> bool:
    return compare_digest(username, settings.admin_username) and compare_digest(
        password, settings.admin_password
    )


def login_admin(request: Request, username: str) -> None:
    request.session[ADMIN_AUTH_KEY] = True
    request.session[ADMIN_USER_KEY] = username
    request.session.pop(CSRF_TOKEN_KEY, None)


def logout_admin(request: Request) -> None:
    request.session.clear()


def require_admin_or_redirect(request: Request) -> RedirectResponse | None:
    if is_admin_authenticated(request):
        return None
    return RedirectResponse(url="/admin/login", status_code=303)


def issue_csrf_token(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    request.session[CSRF_TOKEN_KEY] = token
    return token


def validate_csrf_token(request: Request, provided_token: str | None) -> bool:
    saved_token = request.session.get(CSRF_TOKEN_KEY)
    if not saved_token or not provided_token:
        return False
    return compare_digest(saved_token, provided_token)
