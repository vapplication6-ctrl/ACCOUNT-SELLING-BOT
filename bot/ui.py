"""UI helpers: coloured buttons (with safe fallback) and fancy text fonts."""
import inspect

from pyrogram import enums
from pyrogram.types import InlineKeyboardButton as _PyroButton

# ---------------------------------------------------------------- fonts
_SMALL_CAPS = dict(
    zip(
        "abcdefghijklmnopqrstuvwxyz",
        "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ",
    )
)


def sc(text: str) -> str:
    """Small caps font: 'good quality acc' -> 'ɢᴏᴏᴅ ǫᴜᴀʟɪᴛʏ ᴀᴄᴄ'."""
    return "".join(_SMALL_CAPS.get(ch, ch) for ch in text.lower())


def bold(text: str) -> str:
    """Sans-serif bold font (works for A-Z, a-z, 0-9)."""
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(0x1D5D4 + ord(ch) - ord("A")))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D5EE + ord(ch) - ord("a")))
        elif "0" <= ch <= "9":
            out.append(chr(0x1D7EC + ord(ch) - ord("0")))
        else:
            out.append(ch)
    return "".join(out)


def iso_from_flag(flag: str) -> str:
    """🇦🇫 -> 'AF' (read straight from the flag emoji)."""
    letters = [chr(ord(ch) - 0x1F1E6 + 65) for ch in flag if 0x1F1E6 <= ord(ch) <= 0x1F1FF]
    return "".join(letters)[:2]


LINE = "━━━━━━━━━━━━━━━━━━━━"

# ---------------------------------------------------------------- buttons
_HAS_STYLE = "style" in inspect.signature(_PyroButton.__init__).parameters
_STYLE_ENUM = getattr(enums, "ButtonStyle", None)


def style_support() -> bool:
    return _HAS_STYLE


_DANGER_WORDS = ("back", "cancel", "reject", "❌", "⬅", "close", "delete", "remove")
_SUCCESS_WORDS = ("buy", "done", "approve", "✅", "get otp", "add stock", "confirm")


def _auto_style(text: str) -> str:
    t = text.lower()
    if any(w in t for w in _DANGER_WORDS):
        return "danger"
    if any(w in t for w in _SUCCESS_WORDS):
        return "success"
    return "primary"


def _style_candidates(name: str) -> list:
    cands = []
    if _STYLE_ENUM is not None:
        member = getattr(_STYLE_ENUM, name.upper(), None)
        if member is not None:
            cands.append(member)
    cands.append(name.lower())
    return cands


def InlineKeyboardButton(text: str, callback_data=None, *, style: str | None = None, **kwargs):  # noqa: N802
    """Drop-in for pyrogram's InlineKeyboardButton that adds a colour.

    Colours: 'primary' (blue), 'success' (green), 'danger' (red).
    If the installed library or the Telegram app does not support styles,
    the button is simply created without a colour, so nothing breaks.
    """
    if style is None:
        style = _auto_style(text)
    if _HAS_STYLE and style:
        for value in _style_candidates(style):
            try:
                return _PyroButton(text, callback_data=callback_data, style=value, **kwargs)
            except Exception:
                continue
    return _PyroButton(text, callback_data=callback_data, **kwargs)
