import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.password import compute_check
from telethon.tl.functions.account import (
    GetAuthorizationsRequest,
    GetPasswordRequest,
    GetPasswordSettingsRequest,
    ResetAuthorizationRequest,
)
from telethon.tl.functions.help import GetAppConfigRequest

OTP_NEAR_WORD_REGEX = re.compile(r"(?i)code\D{0,12}(\d{4,8})")
OTP_ANY_REGEX = re.compile(r"\b(\d{4,8})\b")


def extract_otp(body: str) -> str | None:
    """Pulls the login code out of a 777000 message. 'New login' notices (dates, times) are ignored."""
    text = body or ""
    lowered = text.lower()
    if "new login" in lowered or "we detected a login" in lowered:
        return None
    match = OTP_NEAR_WORD_REGEX.search(text) or OTP_ANY_REGEX.search(text)
    return match.group(1) if match else None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class LoginSessionHandle:
    """Holds an active Telethon login flow while awaiting OTP or 2FA password."""

    def __init__(self, client: TelegramClient, phone_number: str, phone_code_hash: str) -> None:
        self.client = client
        self.phone_number = phone_number
        self.phone_code_hash = phone_code_hash

    async def close_quietly(self) -> None:
        try:
            if self.client.is_connected():
                await self.client.disconnect()
        except Exception:
            pass


class SessionService:
    def __init__(self, api_id: int, api_hash: str) -> None:
        self._api_id = api_id
        self._api_hash = api_hash

    def _build_client(self, session_string: str | None = None) -> TelegramClient:
        kwargs: dict[str, Any] = {
            "receive_updates": False,
            "flood_sleep_threshold": 0,  # raise FloodWaitError at once instead of sleeping silently
        }
        return TelegramClient(StringSession(session_string or ""), self._api_id, self._api_hash, **kwargs)

    @staticmethod
    async def _disconnect(client: TelegramClient) -> None:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass

    # ---------------------------------------------------------------- login (Add Stock)
    @staticmethod
    def _describe_delivery(sent) -> str:
        """Tells the admin WHERE Telegram sent the login code (app chat, SMS, call...)."""
        kind = type(getattr(sent, "type", None)).__name__
        if kind == "SentCodeTypeApp":
            return "✅ Code sent INSIDE Telegram - check the 'Telegram' (777000) chat of that account."
        if kind == "SentCodeTypeSms":
            return (
                "📩 Code sent by SMS to the phone number - it will NOT appear in the Telegram chat. "
                "(Telegram does this when the number has no active Telegram login.)"
            )
        if kind in ("SentCodeTypeCall", "SentCodeTypeFlashCall", "SentCodeTypeMissedCall"):
            return "📞 Code is delivered by a phone call, not inside Telegram."
        if kind.startswith("SentCodeTypeEmail") or kind.startswith("SentCodeTypeFragment"):
            return f"⚠️ Code delivery type: {kind} - it will not appear in the Telegram chat."
        return f"Code requested (delivery: {kind or 'unknown'})."

    async def start_login(self, owner_id: int, phone_number: str) -> tuple[bool, str, LoginSessionHandle | None]:
        client = self._build_client()
        try:
            await client.connect()
            sent = await client.send_code_request(phone_number)
            handle = LoginSessionHandle(
                client=client,
                phone_number=phone_number,
                phone_code_hash=sent.phone_code_hash,
            )
            return True, self._describe_delivery(sent), handle
        except FloodWaitError as exc:
            await self._disconnect(client)
            return False, f"Telegram FloodWait: retry after {exc.seconds}s.", None
        except PhoneNumberInvalidError:
            await self._disconnect(client)
            return False, "Invalid phone number format. Include country code (e.g. +919876543210).", None
        except Exception as exc:
            await self._disconnect(client)
            return False, f"Failed to send login code: {exc}", None

    async def import_session(self, session_string: str) -> tuple[bool, str, str | None, bool]:
        """Checks a Telethon string session. Returns (ok, message, phone_with_plus, has_2fa)."""
        session_string = (session_string or "").strip()
        try:
            client = self._build_client(session_string)
        except Exception:
            return False, "Invalid session string (Telethon StringSession needed).", None, False
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return False, "This session is not logged in (expired or terminated).", None, False
            me = await client.get_me()
            if not me or not getattr(me, "phone", None):
                return False, "Could not read the phone number of this session.", None, False
            pwd = await client(GetPasswordRequest())
            return True, "Session is valid.", "+" + str(me.phone).lstrip("+"), bool(pwd.has_password)
        except FloodWaitError as exc:
            return False, f"Telegram FloodWait: retry after {exc.seconds}s.", None, False
        except Exception as exc:
            return False, f"Invalid or unusable session string: {type(exc).__name__}", None, False
        finally:
            await self._disconnect(client)

    async def verify_2fa_for_session(self, session_string: str, password: str) -> tuple[bool, str]:
        """Checks that `password` really is the 2FA password of an already logged-in session."""
        client = self._build_client(session_string.strip())
        try:
            await client.connect()
            pwd = await client(GetPasswordRequest())
            await client(GetPasswordSettingsRequest(password=compute_check(pwd, password)))
            return True, "2FA password is correct."
        except PasswordHashInvalidError:
            return False, "Wrong 2FA password. Try again (or send `skip`)."
        except FloodWaitError as exc:
            return False, f"Telegram FloodWait: retry after {exc.seconds}s."
        except Exception as exc:
            return False, f"Could not verify the 2FA password: {type(exc).__name__}"
        finally:
            await self._disconnect(client)

    async def submit_code(self, handle: LoginSessionHandle, code: str) -> tuple[str, str | None]:
        """Returns ('logged_in', session_string), ('need_2fa', None), or ('error', message)."""
        cleaned_code = "".join(ch for ch in code.strip() if ch.isdigit())
        if not cleaned_code:
            return "error", "Please enter a valid numeric OTP."
        try:
            await handle.client.sign_in(
                phone=handle.phone_number,
                code=cleaned_code,
                phone_code_hash=handle.phone_code_hash,
            )
            session_str = handle.client.session.save()
            await handle.close_quietly()
            return "logged_in", session_str
        except SessionPasswordNeededError:
            return "need_2fa", "Two-step verification password required."
        except PhoneCodeInvalidError:
            return "error", "Invalid OTP code. Please try again."
        except PhoneCodeExpiredError:
            await handle.close_quietly()
            return "error", "OTP expired. Please restart /admin -> Add Stock."
        except FloodWaitError as exc:
            return "error", f"FloodWait: retry after {exc.seconds}s."
        except Exception as exc:
            await handle.close_quietly()
            return "error", f"Sign-in failed: {exc}"

    async def submit_2fa_password(self, handle: LoginSessionHandle, password: str) -> tuple[bool, str]:
        if not password.strip():
            return False, "2FA password cannot be empty."
        try:
            await handle.client.sign_in(password=password.strip())
            session_str = handle.client.session.save()
            await handle.close_quietly()
            return True, session_str
        except PasswordHashInvalidError:
            await handle.close_quietly()
            return False, "2FA verification failed: wrong password."
        except Exception as exc:
            await handle.close_quietly()
            return False, f"2FA verification failed: {exc}"

    # ---------------------------------------------------------------- OTP (buyer)
    async def _recent_777000(self, client: TelegramClient, limit: int = 10) -> list:
        try:
            return [m async for m in client.iter_messages(777000, limit=limit)]
        except ValueError:
            # entity not cached yet on a fresh in-memory session: load dialogs once, then retry
            await client.get_dialogs(limit=20)
            return [m async for m in client.iter_messages(777000, limit=limit)]

    async def _auth_hashes(self, client: TelegramClient) -> list[int] | None:
        """Hashes of every OTHER logged-in device (the bot's own session is flagged `current`)."""
        try:
            res = await client(GetAuthorizationsRequest())
            return [int(a.hash) for a in res.authorizations if not a.current]
        except Exception:
            return None

    async def fetch_otp_from_777000(
        self,
        session_string: str,
        not_before: datetime | None = None,
    ) -> tuple[bool, str, str | None, int | None, list[int] | None]:
        """Reads the Telegram (777000) chat with the stored session and disconnects.

        Only messages newer than `not_before` count, so an old code (for example the one the admin
        used when adding the number) is never handed to a buyer.
        Returns (ok, status_message, otp_code, msg_id, other_device_hashes_before_login).
        """
        client = self._build_client(session_string)
        not_before = _as_utc(not_before)
        try:
            await client.connect()
            messages = await self._recent_777000(client)
            if not messages:
                return False, "No messages found in Telegram (777000) yet.", None, None, None

            for msg in messages:  # newest first
                msg_date = _as_utc(getattr(msg, "date", None))
                if not_before and msg_date and msg_date < not_before:
                    continue
                code = extract_otp(msg.raw_text or "")
                if code:
                    baseline = await self._auth_hashes(client)
                    return True, "OTP fetched successfully from 777000.", code, msg.id, baseline

            return False, "No new login code has arrived yet.", None, None, None
        except FloodWaitError as exc:
            return False, f"Telegram FloodWait ({exc.seconds}s). Please wait and try again.", None, None, None
        except Exception as exc:
            return False, f"Unable to read 777000 right now: {exc}", None, None, None
        finally:
            await self._disconnect(client)

    # ---------------------------------------------------------------- cleanup after the buyer logs in
    async def cleanup_after_new_login(
        self,
        session_string: str,
        baseline_hashes: list[int] | None,
        delivered_at: datetime | None,
    ) -> tuple[str, str]:
        """Logs out the OLD devices, but only once the buyer's new login is detected.

        Returns (status, note) where status is 'no_new_login', 'cleaned' or 'cleanup_failed'.
        The buyer's own new device is never touched: old devices are removed one by one.
        """
        client = self._build_client(session_string)
        delivered_at = _as_utc(delivered_at)
        try:
            await client.connect()
            res = await client(GetAuthorizationsRequest())
            others = [a for a in res.authorizations if not a.current]
            baseline = set(baseline_hashes) if baseline_hashes is not None else None

            def _is_new(auth: Any) -> bool:
                if baseline is not None:
                    return int(auth.hash) not in baseline
                created = _as_utc(getattr(auth, "date_created", None))
                return bool(created and delivered_at and created >= delivered_at - timedelta(seconds=60))

            new_hashes = {int(a.hash) for a in others if _is_new(a)}
            if not new_hashes:
                return "no_new_login", "New login not detected yet."

            old = [a for a in others if int(a.hash) not in new_hashes]
            failures: list[str] = []
            for auth in old:
                try:
                    await client(ResetAuthorizationRequest(hash=auth.hash))
                    await asyncio.sleep(0.3)
                except FloodWaitError as exc:
                    failures.append(f"FloodWait ({exc.seconds}s)")
                    break
                except Exception as exc:
                    failures.append(getattr(exc, "message", None) or str(exc))

            if failures:
                reason = failures[0]
                if "FRESH" in reason.upper():
                    reason = "Telegram allows removing old devices only 24 hours after the stock session was created"
                return "cleanup_failed", f"Some old devices could not be logged out: {reason}."
            if not old:
                return "cleaned", "No other old devices were logged in."
            return "cleaned", f"{len(old)} old device(s) logged out. Your new login is safe."
        except FloodWaitError as exc:
            return "cleanup_failed", f"Could not check sessions due to FloodWait ({exc.seconds}s)."
        except Exception as exc:
            return "cleanup_failed", f"Session cleanup note: {exc}"
        finally:
            await self._disconnect(client)

    # ---------------------------------------------------------------- health check
    async def check_account_health(self, session_string: str) -> tuple[str, str]:
        """Returns (status, detail). status: ok | frozen | deleted | expired | unknown."""
        client = self._build_client(session_string)
        try:
            await client.connect()
            me = await client.get_me()
            if me is None:
                return "expired", "Session string is no longer valid (logged out / revoked)."
            if await self._is_frozen(client):
                return "frozen", "Account is frozen by Telegram."
            return "ok", ""
        except Exception as exc:
            kind = classify_account_error(exc)
            if kind:
                return kind
            return "unknown", f"{type(exc).__name__}: {exc}"
        finally:
            await self._disconnect(client)

    async def _is_frozen(self, client: TelegramClient) -> bool:
        """Frozen accounts carry a `freeze_since_date` value in Telegram's app config."""
        try:
            res = await client(GetAppConfigRequest(hash=0))
            cfg = getattr(res, "config", res)
            for item in getattr(cfg, "value", None) or []:
                if getattr(item, "key", "") == "freeze_since_date":
                    value = getattr(getattr(item, "value", None), "value", None)
                    return bool(value)
        except Exception as exc:
            if "FROZEN" in str(exc).upper() or "FROZEN" in str(getattr(exc, "message", "")).upper():
                return True
        return False


def classify_account_error(exc: Exception) -> tuple[str, str] | None:
    """Maps a Telegram error to ('frozen'|'deleted'|'expired', detail), or None if it is not an account problem."""
    name = type(exc).__name__.lower()
    if name.endswith("error"):
        name = name[: -len("error")]
    text = f"{exc} {getattr(exc, 'message', '')}".upper()
    if "FROZEN" in text or "frozen" in name:
        return "frozen", "Account is frozen by Telegram."
    if name == "userdeactivatedban" or "USER_DEACTIVATED_BAN" in text:
        return "deleted", "Account was banned/deactivated by Telegram."
    if name == "userdeactivated" or "USER_DEACTIVATED" in text:
        return "deleted", "Account was deleted."
    expired_names = {"authkeyunregistered", "sessionrevoked", "sessionexpired", "authkeyinvalid", "authkeyduplicated"}
    expired_texts = ("AUTH_KEY_UNREGISTERED", "SESSION_REVOKED", "SESSION_EXPIRED", "AUTH_KEY_INVALID", "AUTH_KEY_DUPLICATED")
    if name in expired_names or any(t in text for t in expired_texts):
        return "expired", "Session string is no longer valid (logged out / revoked)."
    return None
