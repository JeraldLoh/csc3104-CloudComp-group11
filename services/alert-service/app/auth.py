"""Who is calling the Alert Service.

Same rules as services/journey-service/app/auth.py, so the caregiver app's one "Act as" box (and later
one Cognito login) works for both services.

AUTH_MODE picks how (config.py):
  off      nobody is checked; authenticate() returns None and the access rules are skipped.
  dev      trust an `X-Dev-User: G-003` header. Local testing only: anyone can claim to be anyone.
  cognito  verify the `Authorization: Bearer <jwt>` header against the Cognito user pool.

A verified caller is a `Caller(id, role)`: the contract ID from one claim (default `custom:userId`, e.g. G-003)
and the role from another (default `cognito:groups`, containing `caregiver` or `traveller`). The ID's prefix
has to agree with the role (G- caregiver, U- traveller), so a mislabelled account is refused rather than trusted.

Errors carry a reason for the log only; the caller just gets "missing or invalid login token".
"""
import re
from dataclasses import dataclass
from typing import Callable, Literal, Mapping

import jwt

from app.config import Settings

ROLE_PREFIX = {"caregiver": "G-", "traveller": "U-"}
ID_PATTERN = re.compile(r"^[GU]-[A-Za-z0-9]{1,40}$")  # contracts/README.md: U- traveller, G- caregiver
MAX_TOKEN_CHARS = 8192  # real Cognito tokens are ~1-2 KB; refuse anything silly


class Unauthorized(Exception):
    """No valid login. The message is for the log, never for the caller."""


class Forbidden(Exception):
    """Logged in, but not allowed to do this."""


class AuthUnavailable(Exception):
    """Couldn't check the login (the signing keys couldn't be fetched). Not the caller's fault."""


@dataclass(frozen=True)
class Caller:
    id: str
    role: Literal["caregiver", "traveller"]

    @property
    def is_caregiver(self) -> bool:
        return self.role == "caregiver"


def _caller(user_id, role: str) -> Caller:
    if not isinstance(user_id, str) or not ID_PATTERN.match(user_id):
        raise Unauthorized("the user id is missing or not in the G-/U- format")
    if not user_id.startswith(ROLE_PREFIX[role]):
        raise Unauthorized(f"id prefix does not match the {role} role")
    return Caller(user_id, role)  # type: ignore[arg-type]


class Authenticator:
    def __init__(self, settings: Settings, key_resolver: Callable[[str], object] | None = None):
        """`key_resolver(token)` returns the public key that should have signed `token`; tests inject their own."""
        self._s = settings
        self._mode = settings.auth_mode
        if self._mode not in ("off", "dev", "cognito"):
            raise RuntimeError(f"AUTH_MODE must be off, dev or cognito, not {self._mode!r}")
        if self._mode == "cognito":
            missing = [name for name, value in (("COGNITO_REGION", settings.cognito_region),
                                                ("COGNITO_USER_POOL_ID", settings.cognito_user_pool_id),
                                                ("COGNITO_CLIENT_ID", settings.cognito_client_id)) if not value]
            if missing:
                raise RuntimeError(f"AUTH_MODE=cognito needs {', '.join(missing)}")  # fail at startup, not per request
            if settings.auth_token_use not in ("id", "access"):
                raise RuntimeError("AUTH_TOKEN_USE must be id or access")
            self._issuer = f"https://cognito-idp.{settings.cognito_region}.amazonaws.com/{settings.cognito_user_pool_id}"
            self._resolver = key_resolver or self._jwks_resolver(
                settings.cognito_jwks_url or f"{self._issuer}/.well-known/jwks.json")

    @property
    def enabled(self) -> bool:
        return self._mode != "off"

    def authenticate(self, headers: Mapping[str, str]) -> Caller | None:
        """None when auth is off; otherwise the caller, or Unauthorized / AuthUnavailable."""
        if self._mode == "off":
            return None
        if self._mode == "dev":
            user = (headers.get("x-dev-user") or "").strip()
            for role, prefix in ROLE_PREFIX.items():
                if user.startswith(prefix):
                    return _caller(user, role)
            raise Unauthorized("no valid X-Dev-User header")
        return self._verify(self._bearer(headers))

    # --- cognito ----------------------------------------------------------------------

    @staticmethod
    def _bearer(headers: Mapping[str, str]) -> str:
        scheme, _, token = (headers.get("authorization") or "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise Unauthorized("no Bearer token")
        if len(token) > MAX_TOKEN_CHARS:
            raise Unauthorized("token is far too long")
        return token

    @staticmethod
    def _jwks_resolver(url: str) -> Callable[[str], object]:
        client = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=5)

        def resolve(token: str):
            try:
                return client.get_signing_key_from_jwt(token).key
            except jwt.PyJWKClientConnectionError as e:
                raise AuthUnavailable(f"can't fetch the signing keys: {e}") from e
            except jwt.PyJWTError as e:
                raise Unauthorized(f"no matching signing key ({type(e).__name__})") from e
        return resolve

    def _verify(self, token: str) -> Caller:
        s = self._s
        use_id = s.auth_token_use == "id"
        try:
            key = self._resolver(token)
            claims = jwt.decode(
                token, key,
                algorithms=["RS256"],  # never "none", never HS256 signed with the public key
                issuer=self._issuer,
                audience=s.cognito_client_id if use_id else None,
                options={"require": ["exp", "iss"] + (["aud"] if use_id else []), "verify_aud": use_id},
            )
        except (AuthUnavailable, Unauthorized):
            raise
        except jwt.PyJWTError as e:
            raise Unauthorized(f"token rejected ({type(e).__name__})") from e

        if claims.get("token_use") != s.auth_token_use:
            raise Unauthorized(f"expected a {s.auth_token_use} token")
        if not use_id and claims.get("client_id") != s.cognito_client_id:
            raise Unauthorized("token was issued to a different app client")

        groups = claims.get(s.auth_role_claim)
        groups = [groups] if isinstance(groups, str) else groups
        if not isinstance(groups, list):
            raise Unauthorized("no role claim")
        roles = [role for role, group in (("caregiver", s.auth_caregiver_group), ("traveller", s.auth_traveller_group))
                 if group in groups]
        if len(roles) != 1:
            raise Unauthorized("the account must be in exactly one of the caregiver and traveller groups")
        return _caller(claims.get(s.auth_id_claim), roles[0])
