from fastapi import HTTPException


def api_error(status: int, code: str, message: str, round_id: str | None = None) -> HTTPException:
    detail: dict[str, str] = {"code": code, "message": message}
    if round_id:
        detail["round_id"] = round_id
    return HTTPException(status_code=status, detail=detail)
