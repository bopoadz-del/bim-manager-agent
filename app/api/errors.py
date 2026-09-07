"""Typed errors.

Every failure leaves through one of these and carries a machine-readable
``code``. Nothing in this service returns 200 with an error described in the
body: a client that has to parse prose to find out whether its upload worked
will eventually get it wrong, and the failure will look like success in every
dashboard between here and the engineer.
"""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] | None = None


class ApiError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def body(self) -> ErrorBody:
        return ErrorBody(code=self.code, message=self.message, detail=self.detail)


class NotFound(ApiError):
    status_code = 404
    code = "not_found"


class Unauthorized(ApiError):
    status_code = 401
    code = "unauthorized"


class Forbidden(ApiError):
    status_code = 403
    code = "forbidden"


class BadRequest(ApiError):
    status_code = 400
    code = "bad_request"


class Conflict(ApiError):
    status_code = 409
    code = "conflict"


class UnprocessableModel(ApiError):
    """The upload was received and is not a model this service can judge."""

    status_code = 422
    code = "unprocessable_model"


class DependencyUnavailable(ApiError):
    status_code = 503
    code = "dependency_unavailable"


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body().model_dump())
