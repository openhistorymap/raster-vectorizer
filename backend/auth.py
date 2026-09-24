"""Single-user HTTP basic auth (env-configured)."""
from __future__ import annotations

import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

# auto_error=False so we can return "open" when no credentials are configured.
_security = HTTPBasic(auto_error=False)


def _expected() -> tuple[str, str] | None:
    user = os.environ.get("OFM_EDITOR_USER")
    pw = os.environ.get("OFM_EDITOR_PASSWORD")
    if not user or not pw:
        return None
    return user, pw


def require_user(
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> str:
    """Reject unless credentials match OFM_EDITOR_USER/OFM_EDITOR_PASSWORD env.

    If the env vars are unset, the service is open (useful for local dev /
    docker compose without secrets).  Set both env vars to enable auth.
    """
    expected = _expected()
    if expected is None:
        return (credentials.username if credentials else None) or "anonymous"
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="credentials required",
            headers={"WWW-Authenticate": "Basic"},
        )
    exp_user, exp_pw = expected
    if not (
        secrets.compare_digest(credentials.username.encode(), exp_user.encode())
        and secrets.compare_digest(credentials.password.encode(), exp_pw.encode())
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
