"""Travellers' logins (issue 1.5). Travellers can't sign up: when a caregiver adds one, this makes their Cognito
account and returns a setup code (the account's password) for the caregiver to sign the phone in with.

The account's username is the traveller's U- ID, custom:userId is the same ID and it is in the traveller group, so
its tokens pass app/auth.py like a caregiver's. A new setup code replaces the old one and signs the traveller out
everywhere (a lost phone stops working once its token runs out, within the hour).
Only used with AUTH_MODE=cognito (cloudformation/careroute-cognito.yaml).
"""
import logging
import secrets

from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger("alert.logins")

# Easy to read out and type on a phone: no 0/o, 1/l/i. 12 characters is about 60 bits.
_LETTERS = "abcdefghjkmnpqrstuvwxyz"
_DIGITS = "23456789"


class LoginUnavailable(Exception):
    """Cognito couldn't be reached or refused. The message is for the log."""


def setup_code() -> str:
    """Like `k7mq-4rtx-9cwe`: lowercase letters and digits, at least one of each (the pool's password policy)."""
    while True:
        chars = "".join(secrets.choice(_LETTERS + _DIGITS) for _ in range(12))
        if any(c in _DIGITS for c in chars) and any(c in _LETTERS for c in chars):
            return "-".join(chars[i:i + 4] for i in range(0, 12, 4))


class TravellerLogins:
    def __init__(self, cognito, user_pool_id: str, group: str = "traveller"):
        self.cognito, self.pool, self.group = cognito, user_pool_id, group

    def issue(self, traveller_id: str) -> dict:
        """Create the traveller's account if it doesn't exist, give it a new setup code, and return
        {"username", "setupCode"}. Safe to repeat."""
        code = setup_code()
        try:
            try:
                self.cognito.admin_create_user(
                    UserPoolId=self.pool, Username=traveller_id, MessageAction="SUPPRESS",  # no email: they have none
                    UserAttributes=[{"Name": "custom:userId", "Value": traveller_id}])
            except ClientError as e:
                if e.response["Error"]["Code"] != "UsernameExistsException":
                    raise
                self.cognito.admin_user_global_sign_out(UserPoolId=self.pool, Username=traveller_id)
            # Permanent: no "choose a new password" step on the phone
            self.cognito.admin_set_user_password(UserPoolId=self.pool, Username=traveller_id, Password=code,
                                                 Permanent=True)
            self.cognito.admin_add_user_to_group(UserPoolId=self.pool, Username=traveller_id, GroupName=self.group)
        except (ClientError, BotoCoreError) as e:
            raise LoginUnavailable(f"can't set up the login of {traveller_id}: {e}") from e
        return {"username": traveller_id, "setupCode": code}

    def remove(self, traveller_id: str) -> None:
        """Delete the traveller's account, if there is one. Failures are only logged: the profile is already gone."""
        try:
            self.cognito.admin_delete_user(UserPoolId=self.pool, Username=traveller_id)
        except ClientError as e:
            if e.response["Error"]["Code"] != "UserNotFoundException":
                log.warning("can't delete the login of %s: %s", traveller_id, e)
        except BotoCoreError as e:
            log.warning("can't delete the login of %s: %s", traveller_id, e)
