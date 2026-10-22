"""Verifies Supabase access tokens and builds the current user from their hospital membership.

The hospital scope always comes from the user's own membership, never from the request
(constitution, Tenant isolation). Privileged roles require MFA (aal2) (FR-008).
"""

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.core.db import user_transaction
from app.core.errors import AppError


@dataclass(frozen=True)
class CurrentUser:
    user_id: str
    hospital_id: int
    display_name: str
    roles: frozenset[str]
    permissions: frozenset[str]
    privileged: bool
    mfa_satisfied: bool
    claims: dict[str, Any] = field(repr=False, compare=False)

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise AppError(403, "FORBIDDEN", "You do not have permission for this action.")


@lru_cache
def _jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=600)


def verify_token(token: str, settings: Settings) -> dict[str, Any]:
    if not settings.supabase_url:
        raise AppError(503, "AUTH_NOT_CONFIGURED", "Sign-in is not configured on the server.")
    options = {"require": ["exp", "sub", "aud", "iss"]}
    try:
        if settings.supabase_jwt_mode == "secret":
            if not settings.supabase_jwt_secret:
                raise AppError(
                    503, "AUTH_NOT_CONFIGURED", "Sign-in is not configured on the server."
                )
            key: Any = settings.supabase_jwt_secret
            algorithms = ["HS256"]
        else:
            jwks_url = f"{settings.jwt_issuer}/.well-known/jwks.json"
            key = _jwks_client(jwks_url).get_signing_key_from_jwt(token).key
            algorithms = ["RS256", "ES256"]
        return jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience=settings.supabase_jwt_audience,
            issuer=settings.jwt_issuer,
            options=options,
        )
    except AppError:
        raise
    except jwt.ExpiredSignatureError as exc:
        raise AppError(401, "UNAUTHENTICATED", "Your session has expired. Sign in again.") from exc
    except jwt.PyJWKClientConnectionError as exc:
        raise AppError(503, "AUTH_UNAVAILABLE", "Sign-in service is unreachable.") from exc
    except jwt.PyJWTError as exc:
        raise AppError(401, "UNAUTHENTICATED", "Sign-in is not valid.") from exc


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AppError(401, "UNAUTHENTICATED", "Sign in to continue.")
    return token


def load_user(claims: dict[str, Any]) -> CurrentUser:
    """Reads membership, roles and permissions as the user themself (RLS applies)."""
    with user_transaction(claims) as cur:
        cur.execute(
            "select hospital_id, display_name from public.hospital_members"
            " where user_id = auth.uid() and active order by hospital_id"
        )
        memberships = cur.fetchall()
        if not memberships:
            raise AppError(403, "NO_MEMBERSHIP", "Your account is not active in any hospital.")
        if len(memberships) > 1:
            # One hospital is served now (D-006); choosing between hospitals is not built yet.
            raise AppError(403, "MULTIPLE_HOSPITALS", "Your account belongs to several hospitals.")
        hospital_id = memberships[0]["hospital_id"]
        cur.execute(
            "select r.code, r.privileged from public.user_roles ur"
            " join public.roles r on r.hospital_id = ur.hospital_id and r.code = ur.role_code"
            " where ur.hospital_id = %s and ur.user_id = auth.uid()",
            (hospital_id,),
        )
        roles = cur.fetchall()
        cur.execute(
            "select distinct rp.permission_code from public.user_roles ur"
            " join public.role_permissions rp"
            "   on rp.hospital_id = ur.hospital_id and rp.role_code = ur.role_code"
            " where ur.hospital_id = %s and ur.user_id = auth.uid()",
            (hospital_id,),
        )
        permissions = frozenset(r["permission_code"] for r in cur.fetchall())

    privileged = any(r["privileged"] for r in roles)
    return CurrentUser(
        user_id=str(claims["sub"]),
        hospital_id=hospital_id,
        display_name=memberships[0]["display_name"],
        roles=frozenset(r["code"] for r in roles),
        permissions=permissions,
        privileged=privileged,
        mfa_satisfied=claims.get("aal") == "aal2",
        claims=claims,
    )


def current_user(request: Request, settings: Settings = Depends(get_settings)) -> CurrentUser:
    claims = verify_token(bearer_token(request), settings)
    user = load_user(claims)
    if user.privileged and not user.mfa_satisfied and not settings.demo_mode:
        raise AppError(
            403, "MFA_REQUIRED", "Your role requires multi-factor sign-in. Complete MFA and retry."
        )
    return user
