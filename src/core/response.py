from typing import Any, Literal, Optional
from pydantic import BaseModel
from fastapi.responses import JSONResponse


class BaseResponse(BaseModel):
    status: Literal["success", "error"]
    message: Optional[str] = None
    data: Optional[Any] = None


def success(data: Any = None, message: str = "ok") -> dict:
    return BaseResponse(status="success", message=message, data=data).model_dump()


def error(message: str, status_code: int = 500) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=BaseResponse(status="error", message=message, data=None).model_dump(),
    )
