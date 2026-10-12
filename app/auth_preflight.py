"""Read-only deployment probe: an unsigned-in caller must be rejected normally."""

from fastapi import HTTPException
from starlette.requests import Request
from .shared_auth import call


def main():
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 0)})
    try:
        call(request, "me")
    except HTTPException as error:
        if (
            error.status_code == 401
            and error.detail.get("code") == "authentication_required"
        ):
            print("Central shared authentication signature and TLS verified.")
            return
    except Exception:
        pass
    raise SystemExit(
        "Central shared authentication preflight failed; keep the current release and check the private configuration."
    )


if __name__ == "__main__":
    main()
