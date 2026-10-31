"""Authentication primitives for the assessment.

The static tokens are deliberately demo-only. Production deployments must validate
JWT signatures/claims or use OAuth introspection before trusting roles or tenants.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import Header, HTTPException, status
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    role: str


def authenticate(authorization: str | None) -> Principal:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid authorization header",
                            headers={"WWW-Authenticate": "Bearer"})

    token = token.strip()
    # Demo credentials are enabled only in development mode. In production,
    # configure real JWT/OAuth verification rather than relying on static tokens.
    demo_mode = os.getenv("APP_ENV", "development").lower() == "development"
    admin_token = os.getenv("ADMIN_TOKEN", "admin-token" if demo_mode else "")
    viewer_token = os.getenv("VIEWER_TOKEN", "viewer-token" if demo_mode else "")
    if token == admin_token:
        return Principal(subject="demo-admin",
                         tenant_id=os.getenv("ADMIN_TENANT_ID", "demo-tenant"),
                         role="admin")
    if token == viewer_token:
        return Principal(subject="demo-viewer",
                         tenant_id=os.getenv("VIEWER_TENANT_ID", "demo-tenant"),
                         role="viewer")
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid bearer token",
                        headers={"WWW-Authenticate": "Bearer"})


def current_principal(authorization: str | None = Header(default=None)) -> Principal:
    return authenticate(authorization)
