from datetime import datetime, timezone
from typing import Any
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, IndexModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    def __init__(self, uri: str, db_name: str) -> None:
        self._client = AsyncIOMotorClient(uri)
        self.db: AsyncIOMotorDatabase = self._client[db_name]
        self.users = self.db["users"]
        self.country_prices = self.db["country_prices"]
        self.accounts = self.db["accounts"]
        self.orders = self.db["orders"]
        self.recharge_requests = self.db["recharge_requests"]
        self.fam_deposits = self.db["fam_deposits"]  # UPI (Auto) deposits
        self.used_refs = self.db["used_refs"]  # UTRs already used (unique _id = one use only)

    async def ensure_indexes(self) -> None:
        await self.users.create_indexes(
            [
                IndexModel([("user_id", ASCENDING)], unique=True),
            ]
        )
        await self.country_prices.create_indexes(
            [
                IndexModel([("server", ASCENDING), ("country_code", ASCENDING)], unique=True),
            ]
        )
        await self.accounts.create_indexes(
            [
                IndexModel([("phone_number", ASCENDING)], unique=True),
                IndexModel([("server", ASCENDING), ("country_code", ASCENDING), ("status", ASCENDING)]),
                IndexModel([("locked_until", ASCENDING)]),
            ]
        )
        await self.orders.create_indexes(
            [
                IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
                IndexModel([("status", ASCENDING), ("expires_at", ASCENDING)]),
            ]
        )
        await self.recharge_requests.create_indexes(
            [
                IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
                IndexModel([("status", ASCENDING)]),
            ]
        )

        await self.fam_deposits.create_indexes(
            [
                IndexModel([("user_id", ASCENDING), ("status", ASCENDING)]),
            ]
        )

    async def claim(self, coll, _id: Any, from_status: str, to_status: str, **fields: Any) -> bool:
        """Move a document between statuses exactly once. True only for the caller that won."""
        res = await coll.update_one(
            {"_id": _id, "status": from_status},
            {"$set": {"status": to_status, **fields}},
        )
        return res.modified_count == 1

    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> dict[str, Any]:
        now = utc_now()
        doc = await self.users.find_one_and_update(
            {"user_id": user_id},
            {
                "$set": {
                    "username": username or "",
                    "first_name": first_name or "User",
                    "last_seen": now,
                },
                "$setOnInsert": {
                    "user_id": user_id,
                    "balance": 0,
                    "created_at": now,
                },
            },
            upsert=True,
            return_document=True,
        )
        return doc

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        return await self.users.find_one({"user_id": user_id})

    async def get_balance(self, user_id: int) -> int:
        user = await self.get_user(user_id)
        if not user:
            return 0
        return int(user.get("balance", 0))

    async def add_balance(self, user_id: int, amount: int) -> int:
        doc = await self.users.find_one_and_update(
            {"user_id": user_id},
            {"$inc": {"balance": amount}},
            return_document=True,
        )
        return int(doc["balance"]) if doc else 0

    async def deduct_balance_atomic(self, user_id: int, amount: int) -> tuple[bool, int]:
        if amount <= 0:
            bal = await self.get_balance(user_id)
            return True, bal
        updated = await self.users.find_one_and_update(
            {"user_id": user_id, "balance": {"$gte": amount}},
            {"$inc": {"balance": -amount}},
            return_document=True,
        )
        if updated is None:
            current_bal = await self.get_balance(user_id)
            return False, current_bal
        return True, int(updated["balance"])

    async def close(self) -> None:
        self._client.close()
