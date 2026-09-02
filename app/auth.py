from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, Callable

import jwt
from fastapi import Header, HTTPException, Request, Security
from fastapi.openapi.models import OAuthFlowClientCredentials, OAuthFlows
from fastapi.security import OAuth2


service_bearer = OAuth2(
    flows=OAuthFlows(
        clientCredentials=OAuthFlowClientCredentials(
            tokenUrl=f"{os.getenv('KEYCLOAK_ISSUER', 'https://auth.codestra.co/realms/codestra').rstrip('/')}/protocol/openid-connect/token",
            scopes={
                "communications.send": "Submit governed communication messages",
                "communications.read": "Read communication messages and history",
                "communications.cancel": "Cancel governed communication messages",
                "communications.templates.read": "Read communication templates",
                "communications.templates.write": "Manage communication templates",
                "communications.consent.read": "Read communication consent state",
                "communications.consent.write": "Manage communication consent state",
                "communications.suppression.read": "Read suppression state",
                "communications.suppression.write": "Manage suppression state",
                "communications.providers.read": "Read provider routing and health state",
                "communications.operations.read": "Read durable communication operations",
                "metrics.read": "Read private service metrics",
            },
        )
    ),
    scheme_name="serviceBearer",
    auto_error=False,
)


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    scopes: frozenset[str]
    client_id: str


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(status_code=503, detail="identity_configuration_unavailable")
    return value


@lru_cache(maxsize=4)
def _jwk_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_jwk_set=True, lifespan=300)


def _scope_set(claims: dict[str, Any]) -> frozenset[str]:
    values: set[str] = set()
    if isinstance(claims.get("scope"), str):
        values.update(claims["scope"].split())
    if isinstance(claims.get("permissions"), list):
        values.update(value for value in claims["permissions"] if isinstance(value, str))
    return frozenset(values)


async def authenticate(
    request: Request,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
    token: Annotated[str | None, Security(service_bearer)] = None,
) -> Principal:
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="bearer_token_required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    issuer = _required("KEYCLOAK_ISSUER").rstrip("/")
    audience = _required("KEYCLOAK_AUDIENCE")
    jwks_url = os.getenv(
        "KEYCLOAK_JWKS_URL", f"{issuer}/protocol/openid-connect/certs"
    ).strip()
    try:
        key = _jwk_client(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256", "PS256", "ES256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=401,
            detail="invalid_access_token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="identity_verification_unavailable") from exc

    tenant = claims.get("tenant_id", claims.get("tenant"))
    subject = claims.get("sub")
    client_id = claims.get("azp", claims.get("client_id"))
    allowed = {item.strip() for item in _required("KEYCLOAK_ALLOWED_CLIENT_IDS").split(",") if item.strip()}
    if not all(isinstance(value, str) and value.strip() for value in (tenant, subject, client_id)):
        raise HTTPException(status_code=403, detail="required_identity_claim_missing")
    if x_tenant_id is None or x_tenant_id != tenant:
        raise HTTPException(status_code=403, detail="tenant_mismatch")
    if client_id not in allowed:
        raise HTTPException(status_code=403, detail="client_not_authorized")
    principal = Principal(subject.strip(), tenant.strip(), _scope_set(claims), client_id.strip())
    request.state.principal = principal
    return principal


def require_scope(scope: str) -> Callable[..., Principal]:
    async def dependency(principal: Principal = Security(authenticate, scopes=[scope])) -> Principal:
        if scope not in principal.scopes:
            raise HTTPException(status_code=403, detail="required_scope_missing")
        return principal

    return dependency
