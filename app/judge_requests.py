"""Retry idempotent judge operations without discarding running verification."""
import time
from fastapi import HTTPException
from .config import get_settings

TRANSIENT = {429, 502, 503, 504}


def request_with_retry(request, *args, check, on_wait=lambda: None, **kwargs):
    deadline = time.monotonic() + get_settings().judge_request_retry_seconds
    delay, notified = 1.0, False
    while True:
        check()
        try:
            return request(*args, **kwargs)
        except HTTPException as error:
            if error.status_code not in TRANSIENT or time.monotonic() >= deadline:
                raise
            if not notified:
                on_wait()
                notified = True
            # Keep cancellation and the job deadline responsive during backoff.
            until = min(deadline, time.monotonic() + delay)
            while time.monotonic() < until:
                check()
                time.sleep(max(0.0, min(0.1, until - time.monotonic())))
            check()
            if time.monotonic() >= deadline:
                raise
            delay = min(5.0, delay * 2)
