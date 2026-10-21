import re
import uuid

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class SharedStateUnavailable(RuntimeError):
    """Distributed coordination backend is unavailable for correctness-critical work."""


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", str(uuid.uuid4()))


def problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = get_request_id(request)
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        headers={"X-Request-ID": request_id, **(headers or {})},
        content={
            "type": f"urn:Grace:problem:{code}",
            "code": code,
            "title": title,
            "detail": detail,
            "request_id": request_id,
        },
    )


async def http_problem_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else "The request could not be completed."
    response_headers = dict(exc.headers or {})
    specific_code = response_headers.pop("X-Problem-Code", None)
    code = specific_code or {
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "invalid_request",
        429: "rate_limited",
    }.get(exc.status_code, "request_failed")
    title = {
        401: "Authentication required",
        403: "Forbidden",
        404: "Not found",
        409: "Conflict",
        422: "Invalid request",
        429: "Too many requests",
    }.get(exc.status_code, "Request failed")
    return problem_response(
        request,
        status=exc.status_code,
        code=code,
        title=title,
        detail=detail,
        headers=response_headers,
    )


async def validation_problem_handler(request: Request, _exc: RequestValidationError):
    return problem_response(
        request,
        status=422,
        code="validation_error",
        title="Invalid request",
        detail="One or more request fields are invalid.",
    )


async def internal_problem_handler(request: Request, _exc: Exception):
    return problem_response(
        request,
        status=500,
        code="internal_error",
        title="Internal server error",
        detail="The request could not be completed.",
    )


async def rate_limit_problem_handler(request: Request, exc: Exception):
    retry_after = getattr(exc, "detail", None)
    headers = {}
    if isinstance(retry_after, int):
        headers["Retry-After"] = str(retry_after)
    return problem_response(
        request,
        status=429,
        code="rate_limited",
        title="Too many requests",
        detail="Please wait before trying again.",
        headers=headers,
    )


def request_id_for_header(value: str | None) -> str:
    if value and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return str(uuid.uuid4())


async def shared_state_unavailable_handler(request: Request, _exc: SharedStateUnavailable):
    return problem_response(
        request,
        status=503,
        code="shared_state_unavailable",
        title="Service temporarily unavailable",
        detail="A distributed coordination service is temporarily unavailable. Please try again shortly.",
        headers={"Retry-After": "5"},
    )
