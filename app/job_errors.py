"""Keep validation messages readable in background jobs and saved history."""
import ast
import re
from fastapi import HTTPException


def exception_message(error):
    if isinstance(error, HTTPException):
        detail = error.detail
        if isinstance(detail, dict) and isinstance(detail.get("message"), str):
            return detail["message"]
        if isinstance(detail, str):
            return detail
        return "요청 내용을 확인한 뒤 다시 시도해 주세요."
    return str(error)


def failure_result(error):
    result = {"summary": "작업을 완료하지 못했습니다.",
              "error": exception_message(error)[:32768]}
    if isinstance(error, HTTPException) and isinstance(error.detail, dict):
        code = error.detail.get("code")
        if isinstance(code, str):
            result["error_code"] = code
    return result


def readable_result(result):
    """Normalize legacy HTTPException strings without changing stored evidence."""
    raw = result.get("error")
    if not isinstance(raw, str) or len(raw) > 32768:
        return result
    match = re.fullmatch(r"[45]\d\d: (\{.*\})", raw, re.DOTALL)
    if not match:
        return result
    try:
        detail = ast.literal_eval(match[1])
    except (ValueError, SyntaxError, RecursionError):
        return result
    if not isinstance(detail, dict) or not isinstance(detail.get("message"), str):
        return result
    return {**result, **failure_result(HTTPException(status_code=400, detail=detail))}
