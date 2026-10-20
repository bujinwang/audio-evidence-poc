"""PoC access control.

This is deliberately simple and clearly marked: identity arrives as headers from
the reverse proxy (or the demo UI). A real deployment replaces `get_actor` with
OIDC/JWT verification and per-case authorisation — nothing else in the app has
to change, because every route depends on the permission checks below rather
than on header parsing directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, status

from .config import get_settings

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"read", "upload", "retry", "edit", "verify", "audit", "export"},
    "investigator": {"read", "upload", "export"},
    "reviewer": {"read", "edit", "verify", "audit", "export"},
    "viewer": {"read"},
}

ALL_PERMISSIONS = sorted({p for perms in ROLE_PERMISSIONS.values() for p in perms})


@dataclass(frozen=True)
class Actor:
    name: str
    role: str

    @property
    def permissions(self) -> set[str]:
        return ROLE_PERMISSIONS.get(self.role, set())

    def can(self, permission: str) -> bool:
        return permission in self.permissions


def get_actor(
    x_actor: str | None = Header(default=None, alias="X-Actor"),
    x_role: str | None = Header(default=None, alias="X-Role"),
) -> Actor:
    settings = get_settings()
    name = (x_actor or "").strip() or settings.dev_default_actor
    role = (x_role or "").strip().lower() or settings.dev_default_role

    if not settings.allow_anonymous and not x_actor:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: supply X-Actor / X-Role (PoC stand-in for SSO).",
        )
    if role not in ROLE_PERMISSIONS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Unknown role '{role}'. Known roles: {', '.join(sorted(ROLE_PERMISSIONS))}.",
        )
    return Actor(name=name, role=role)


def require(permission: str):
    """Route dependency factory: `Depends(require("edit"))`."""
    from fastapi import Depends  # local import keeps module import cheap

    def _dependency(actor: Actor = Depends(get_actor)) -> Actor:
        if not actor.can(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{actor.role}' may not perform '{permission}'.",
            )
        return actor

    return _dependency
