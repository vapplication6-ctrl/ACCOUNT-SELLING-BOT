# Telegram Automated Account Seller Bot (Pyrogram bot + Telethon sessions + MongoDB)

A modular, admin-managed Telegram account selling bot built with **Pyrogram** and **Motor (async MongoDB)**, supporting **Server 1** and **Server 2**, live `777000` OTP extraction (one OTP per order, balance is cut when the OTP is delivered, optional 2FA password button), 20-minute order lock timers with automatic stock release, and post-login old session termination: on `Done` the bot first detects the buyer's new login and then logs out only the OLD devices (the buyer's new device is never touched), and full DM-based `/admin` controls without editing code or redeploying for stock/pricing updates.

## Environment Variables

Set these variables in your environment, `.env` file, Railway, or Heroku:

- `BOT_TOKEN` — Telegram Bot Token from `@BotFather`
- `API_ID` — Telegram API ID from `my.telegram.org`
- `API_HASH` — Telegram API Hash from `my.telegram.org`
- `MONGO_URI` — MongoDB connection string (e.g., MongoDB Atlas)
- `ADMINS` — Comma-separated Telegram User IDs allowed to use `/admin` (e.g., `123456789,987654321`)
- `DB_NAME` — Optional database name (default: `tg_account_seller`)
- `SUPPORT_USERNAME` — Optional support handle without `@` (default: `support`)

## Deployment

### GitHub -> Railway / Heroku
1. Extract the `.zip` archive and push the files to your GitHub repository.
2. Create a new project/app on **Railway** or **Heroku** connected to the GitHub repository.
3. Configure `BOT_TOKEN`, `API_ID`, `API_HASH`, `MONGO_URI`, and `ADMINS`.
4. Ensure the `worker` process from `Procfile` is enabled (`python -m bot`).

## Sessions (Telethon)

All account sessions are **Telethon string sessions**, created inside the bot: `/admin` -> Add Stock -> country code -> phone number -> OTP (-> 2FA password). If the account has 2FA, its password is saved with the number and the buyer gets a **2FA Password** button after the OTP. Only stock with a Telethon string works; Pyrogram strings saved by the old version must be re-added.

Note: Telegram only lets a session log out other devices once it is 24 hours old, so let new stock sit for 24 hours before selling it.

## Recharge options

Tapping **Recharge** (or sending `/recharge`) shows two options:

- **UPI (Manual)** — QR with **I've Paid** / **Cancel** buttons + payment screenshot, balance is credited after an admin approves.
- **UPI (Auto)** — fixed-amount QR, the user sends the **12-digit UTR** (or the FamPay `FMP...` id) and the balance is credited automatically after the payment is found in the FamPay Gmail inbox. Each UTR works only once.

UPI (Auto) turns on when `FAM_UPI`, `FAM_GMAIL` and `FAM_APPPASS` are all set (see `.env.example`).

> Note: verification goes through a third-party API (`FAM_API_BASE`) and the Gmail App Password is sent to it. Use a separate Gmail for FamPay if you can.

## User commands (shown in Telegram's Menu button)

`/start` `/buy` `/recharge` `/balance` `/orders` `/profile` `/help` `/support`

Admin commands (`/admin`, `/addbal`) are not listed in the menu.
