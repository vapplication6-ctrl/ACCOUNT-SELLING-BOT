import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

SERVERS = {
    "server_1": "Server 1",
    "server_2": "Server 2",
}

SERVER_TAGS = {
    "server_1": "【ɢᴏᴏᴅ ǫᴜᴀʟɪᴛʏ ᴀᴄᴄ】",
    "server_2": "【ꜱᴘᴀᴍ ᴀᴄᴄ】",
}

# Force-Subscribe links (order matters: FSUB_IDS env must list one chat id per link, same order)
FSUB_LINKS = [
    "https://t.me/+fcwScAoJs2s1ZjVk",
    "https://t.me/+lZBThtdO_IliNjNk",
    "https://t.me/silentchannel828282",
]

ORDER_LOCK_SECONDS = 20 * 60  # 20 minutes
MAX_OTP_ATTEMPTS = 1  # one OTP per order (no resend)
EXPIRY_SWEEP_INTERVAL_SECONDS = 15
HEALTH_CHECK_INTERVAL_SECONDS = 60 * 60  # re-check unsold stock sessions every hour
HEALTH_CHECK_ACCOUNT_DELAY_SECONDS = 1.5  # pause between accounts to stay clear of flood limits


def _parse_admins(raw: str | None) -> set[int]:
    if not raw:
        return set()
    admins: set[int] = set()
    for part in raw.split(","):
        cleaned = part.strip()
        if cleaned.isdigit():
            admins.add(int(cleaned))
    return admins


@dataclass(frozen=True)
class Config:
    bot_token: str
    api_id: int
    api_hash: str
    mongo_uri: str
    db_name: str
    admins: set[int]
    support_username: str
    # ---- UPI (Auto): FamPay verification. Turned on only when all three FAM_* values are set.
    fam_upi: str = ""
    fam_gmail: str = ""
    fam_apppass: str = ""
    fam_name: str = "OTP Bot"
    fam_api_base: str = "https://subdict.qzz.io"
    min_deposit: int = 20
    max_deposit: int = 5000
    deposit_expiry_min: int = 15
    utr_max_fails: int = 5
    utr_block_min: int = 10
    fsub_links: tuple = ()
    fsub_ids: tuple = ()

    @property
    def fam_enabled(self) -> bool:
        return bool(self.fam_upi and self.fam_gmail and self.fam_apppass)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(float(raw)) if raw else default
    except ValueError:
        return default


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    api_id_raw = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()
    mongo_uri = os.getenv("MONGO_URI", "").strip()
    db_name = os.getenv("DB_NAME", "tg_account_seller").strip() or "tg_account_seller"
    admins = _parse_admins(os.getenv("ADMINS", ""))
    support_username = os.getenv("SUPPORT_USERNAME", "support").strip().lstrip("@")

    if not bot_token:
        raise RuntimeError("Missing required environment variable: BOT_TOKEN")
    if not api_id_raw.isdigit():
        raise RuntimeError("Missing or invalid environment variable: API_ID")
    if not api_hash:
        raise RuntimeError("Missing required environment variable: API_HASH")
    if not mongo_uri:
        raise RuntimeError("Missing required environment variable: MONGO_URI")

    return Config(
        bot_token=bot_token,
        api_id=int(api_id_raw),
        api_hash=api_hash,
        mongo_uri=mongo_uri,
        db_name=db_name,
        admins=admins,
        support_username=support_username,
        fam_upi=os.getenv("FAM_UPI", "").strip(),
        fam_gmail=os.getenv("FAM_GMAIL", "").strip().replace("@gmail.com", ""),
        fam_apppass=os.getenv("FAM_APPPASS", "").replace(" ", "").strip(),
        fam_name=os.getenv("FAM_NAME", "").strip() or "OTP Bot",
        fam_api_base=(os.getenv("FAM_API_BASE", "").strip() or "https://subdict.qzz.io").rstrip("/"),
        min_deposit=_int_env("MIN_DEPOSIT", 20),
        max_deposit=_int_env("MAX_DEPOSIT", 5000),
        deposit_expiry_min=_int_env("DEPOSIT_EXPIRY_MIN", 15),
        utr_max_fails=_int_env("UTR_MAX_FAILS", 5),
        utr_block_min=_int_env("UTR_BLOCK_MIN", 10),
        fsub_links=tuple(FSUB_LINKS),
        fsub_ids=tuple(x.strip() for x in os.getenv("FSUB_IDS", "").split(",") if x.strip()),
    )
