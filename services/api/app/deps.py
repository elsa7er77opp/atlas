"""
Project Atlas - request dependencies
AT-26-L1-002  |  FR-22, FR-24

The authorisation control lives here, and nowhere else. A route either declares
a required role or it is public, and there is no third option: `require_role`
is a dependency, so an endpoint that forgets it fails the FR-24 verification
test rather than quietly permitting everything.

Roles are ordered, not a set of independent flags. Report 8.7 defines
administrator as a superset of operator, and operator as a superset of viewer,
so a numeric rank expresses the model directly. A permission matrix would be
the right structure if the roles were orthogonal. They are not.
"""

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db, security
from .logging_setup import log

_RANK = {"viewer": 1, "operator": 2, "administrator": 3}

# auto_error=False so a missing header produces our own 401 with a consistent
# body, rather than FastAPI's default 403 for an absent credential.
_bearer = HTTPBearer(auto_error=False)


@dataclass
class CurrentUser:
    id: int
    username: str
    role: str

    def outranks(self, required: str) -> bool:
        return _RANK.get(self.role, 0) >= _RANK[required]


def _unauthorised(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    if creds is None or not creds.credentials:
        raise _unauthorised("authentication required")

    claims = security.decode_token(creds.credentials, expected_type="access")
    if claims is None:
        raise _unauthorised("invalid or expired token")

    # The token is signed, so the claims are authentic. They are not
    # necessarily CURRENT: an account deactivated two minutes ago still holds a
    # valid token for up to fifteen. One lookup per request closes that window,
    # and it is the cheapest possible one - a single indexed primary key read.
    row = db.fetch_one(
        "SELECT u.id, u.username, u.is_active, r.name AS role "
        "FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id = %s",
        (int(claims["sub"]),))

    if row is None or not row["is_active"]:
        log("warning", "token presented for missing or inactive account",
            user_id=claims.get("sub"), path=request.url.path)
        raise _unauthorised("account is not active")

    # The role is read from the database, not from the token, so a role change
    # takes effect immediately rather than at the next refresh. The token claim
    # remains useful for logging what the client believed it held.
    user = CurrentUser(id=row["id"], username=row["username"], role=row["role"])
    request.state.user = user
    return user


def require_role(minimum: str):
    """
    Returns a dependency enforcing `minimum`.

    Usage: `dependencies=[Depends(require_role("operator"))]` on the router, or
    `user: CurrentUser = Depends(require_role("administrator"))` where the
    handler needs the identity as well.
    """
    if minimum not in _RANK:
        raise ValueError(f"unknown role {minimum!r}")

    def _check(request: Request,
               user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not user.outranks(minimum):
            # Logged as a warning, not an error: a refused request is the
            # control working. FR-18.
            log("warning", "authorisation refused",
                username=user.username, role=user.role,
                required=minimum, method=request.method,
                path=request.url.path)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires role {minimum} or higher")
        return user

    return _check


def client_ip(request: Request) -> str:
    """
    Behind a reverse proxy the peer address is the proxy. X-Forwarded-For is
    accepted only because provisional parameter A1 places this on a trusted
    laboratory network; on an exposed deployment the header is attacker-
    controlled and must be replaced by a trusted-proxy configuration.
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
