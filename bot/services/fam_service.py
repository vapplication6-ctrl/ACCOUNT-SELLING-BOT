"""UPI (Auto): FamPay payment verification through the subdict API (UTR only, no screenshot / AI).

How it works
  1. gen_qr_url() / qr_png()  -> fixed-amount UPI QR for the user to pay.
  2. classify()               -> tells a 12-digit UTR from a FamPay transaction id (FMP...).
  3. check()                  -> asks the API to look for that payment in the FamPay Gmail inbox.

The Gmail App Password travels in the request URL, so nothing here ever logs a URL or a
request - only the error type. (bot/__main__.py also silences httpx logging.)
"""
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

LOG = logging.getLogger("tg_account_seller_bot.fam")
IST = timezone(timedelta(hours=5, minutes=30))


def _amt(amount: float) -> str:
    return f"{float(amount):.2f}".rstrip("0").rstrip(".")


def classify(text) -> tuple[str, str] | None:
    """'301268197755' -> ('utr', ...) ; 'FMPIB5255631361' -> ('txnid', ...) ; anything else -> None."""
    t = re.sub(r"\s+", "", str(text or "")).upper()
    if re.fullmatch(r"\d{12}", t):
        return "utr", t
    if re.fullmatch(r"FMP[A-Z0-9]{6,20}", t):
        return "txnid", t
    return None


def parse_time(s) -> datetime | None:
    """'16-05-2026 23:50:12' (IST) -> aware UTC datetime."""
    txt = str(s or "").strip()
    cleaned = re.sub(r"\s+", " ", txt.replace("IST", "").replace(",", " ")).strip()
    for fmt in (
        "%d-%m-%Y %H:%M:%S",
        "%I:%M %p %d %B %Y",
        "%I:%M %p %d %b %Y",
        "%d %B %Y %I:%M %p",
    ):
        try:
            src = txt if fmt == "%d-%m-%Y %H:%M:%S" else cleaned
            return datetime.strptime(src, fmt).replace(tzinfo=IST).astimezone(timezone.utc)
        except Exception:
            continue
    return None


def _num(v) -> float | None:
    """'₹3.0' / '3' / 3.0 -> 3.0"""
    try:
        return float(re.sub(r"[^0-9.]", "", str(v)))
    except Exception:
        return None


def qr_png(upi: str, amount: float, name: str = "") -> bytes | None:
    """Own fixed-amount UPI QR (fallback when the API cannot make one)."""
    try:
        import qrcode
    except ImportError:
        return None
    uri = f"upi://pay?pa={quote(upi)}&pn={quote(name or 'OTP Bot')}&cu=INR&am={float(amount):.2f}"
    buf = io.BytesIO()
    qrcode.make(uri).save(buf, format="PNG")
    return buf.getvalue()


class FamService:
    def __init__(self, upi: str, gmail: str, apppass: str, name: str, api_base: str) -> None:
        self.upi = upi
        self.gmail = gmail
        self.apppass = apppass
        self.name = name
        self.api_base = api_base
        self._http: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.upi and self.gmail and self.apppass)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=True)
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    async def gen_qr_url(self, amount: float) -> str | None:
        """Fixed-amount QR image URL from the API, or None (caller then draws its own QR)."""
        try:
            resp = await self._client().get(
                f"{self.api_base}/genqr",
                params={"upi": self.upi, "amount": _amt(amount), "name": self.name},
            )
            j = resp.json()
            if j.get("status") == "success":
                return j.get("image_url") or j["data"]["qr_url"]
        except Exception as exc:
            LOG.warning("fam genqr failed: %s", type(exc).__name__)
        return None

    async def check(self, kind: str, ref: str, amount: float) -> dict:
        """kind = 'utr' | 'txnid'.
        Returns {"ok": True, "data": {...}} or {"ok": False, "reason": <below>, "found": .., "expected": ..}
        reason: not_found | mismatch | duplicate | auth | rate | error
        """
        params = {
            "mail": self.gmail.split("@")[0] + "@gmail",
            "apppass": self.apppass.replace(" ", ""),
            kind: ref,
            "amount": _amt(amount),
        }
        try:
            resp = await self._client().get(f"{self.api_base}/check", params=params)
        except Exception as exc:  # never log the exception text - it can contain the URL
            LOG.warning("fam check network error: %s", type(exc).__name__)
            return {"ok": False, "reason": "error"}
        try:
            j = resp.json()
            if not isinstance(j, dict):
                j = {}
        except Exception:
            j = {}
        if j.get("status") == "success" and isinstance(j.get("data"), dict):
            return {"ok": True, "data": j["data"]}
        # Real API answer: {"status":"found","utr":..,"amount":"₹3.0","sender_name":..,"date_time":"01:20 PM IST, 23 November 2026","transaction_id":..}
        if str(j.get("status", "")).lower() in ("found", "success") and (j.get("utr") or j.get("transaction_id")):
            got = _num(j.get("amount"))
            if got is not None and abs(got - float(amount)) > 0.01:
                LOG.info("fam check -> mismatch (asked %s, found %s)", amount, got)
                return {"ok": False, "reason": "mismatch", "found": got, "expected": float(amount)}
            LOG.info("fam check -> found (http %s)", resp.status_code)
            return {
                "ok": True,
                "data": {
                    "utr": j.get("utr"),
                    "transaction_id": j.get("transaction_id"),
                    "sender_name": j.get("sender_name"),
                    "payment_time_ist": j.get("date_time") or j.get("payment_time_ist"),
                    "amount": got,
                },
            }
        msg = f"{j.get('message', '')} {j.get('details', '')} {j.get('result', '')} {j.get('error', '')}".lower()
        code = resp.status_code
        if code in (401, 403) or any(w in msg for w in ("password", "authentication", "imap", "unauthor", "login", "credential", "gmail")):
            reason = "auth"
        elif "mismatch" in msg or ("expected" in j and "found" in j):
            reason = "mismatch"
        elif "duplicate" in msg or code == 409:
            reason = "duplicate"
        elif code == 404 or "not found" in msg:
            reason = "not_found"
        elif code == 429:
            reason = "rate"
        else:
            reason = "error"
        body = (resp.text or "")[:300].replace(self.apppass, "***").replace(self.apppass.replace(" ", ""), "***")
        LOG.info(
            "fam check -> %s (http %s) msg=%r details=%r body=%r",
            reason, code, str(j.get("message", ""))[:200], str(j.get("details", ""))[:200], body,
        )
        return {"ok": False, "reason": reason, "found": j.get("found"), "expected": j.get("expected")}
