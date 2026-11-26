from datetime import timedelta
from typing import Any
from bson import ObjectId
from bot.config import MAX_OTP_ATTEMPTS, ORDER_LOCK_SECONDS
from bot.database import Database, utc_now


class StockService:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_country_price(self, server: str, country_code: str) -> int | None:
        doc = await self._db.country_prices.find_one(
            {"server": server, "country_code": country_code}
        )
        if not doc:
            return None
        return int(doc["price"])

    async def set_country_price(
        self,
        server: str,
        country_code: str,
        country_name: str,
        flag: str,
        price: int,
    ) -> None:
        await self._db.country_prices.update_one(
            {"server": server, "country_code": country_code},
            {
                "$set": {
                    "server": server,
                    "country_code": country_code,
                    "country_name": country_name,
                    "flag": flag,
                    "price": int(price),
                    "updated_at": utc_now(),
                }
            },
            upsert=True,
        )

    async def add_account_stock(
        self,
        server: str,
        country_code: str,
        country_name: str,
        flag: str,
        phone_number: str,
        session_string: str,
        added_by: int,
        two_fa_password: str | None = None,
    ) -> tuple[bool, str]:
        existing = await self._db.accounts.find_one({"phone_number": phone_number})
        if existing:
            old_status = existing.get("status")
            # A number that was already sold (or died: frozen/deleted/expired) can be added again with the new session.
            if old_status in ("sold", "frozen", "deleted", "expired"):
                await self._db.accounts.update_one(
                    {"_id": existing["_id"]},
                    {
                        "$set": {
                            "server": server,
                            "country_code": country_code,
                            "country_name": country_name,
                            "flag": flag,
                            "session_string": session_string,
                            "two_fa_password": two_fa_password or None,
                            "status": "available",
                            "locked_by": None,
                            "locked_until": None,
                            "active_order_id": None,
                            "added_by": added_by,
                            "added_at": utc_now(),
                            "sold_to": None,
                            "sold_at": None,
                            "dead_reason": None,
                            "dead_at": None,
                        },
                        "$inc": {"times_restocked": 1},
                    },
                )
                return True, (
                    f"Account `{phone_number}` re-added to `{server}` ({flag} {country_name}). "
                    f"(It was previously marked {old_status}.)"
                )
            return False, f"Number `{phone_number}` already exists in stock."

        await self._db.accounts.insert_one(
            {
                "server": server,
                "country_code": country_code,
                "country_name": country_name,
                "flag": flag,
                "phone_number": phone_number,
                "session_string": session_string,
                "two_fa_password": two_fa_password or None,
                "status": "available",  # available | locked | sold
                "locked_by": None,
                "locked_until": None,
                "active_order_id": None,
                "added_by": added_by,
                "added_at": utc_now(),
                "sold_to": None,
                "sold_at": None,
            }
        )
        return True, f"Account `{phone_number}` added to `{server}` ({flag} {country_name})."

    async def list_server_countries(self, server: str) -> list[dict[str, Any]]:
        price_docs = await self._db.country_prices.find({"server": server}).to_list(length=200)
        price_map = {doc["country_code"]: doc for doc in price_docs}

        pipeline = [
            {"$match": {"server": server, "status": "available"}},
            {
                "$group": {
                    "_id": "$country_code",
                    "count": {"$sum": 1},
                    "country_name": {"$first": "$country_name"},
                    "flag": {"$first": "$flag"},
                }
            },
        ]
        avail_rows = await self._db.accounts.aggregate(pipeline).to_list(length=200)
        avail_map = {row["_id"]: row for row in avail_rows}

        all_codes = sorted(set(price_map.keys()) | set(avail_map.keys()))
        result: list[dict[str, Any]] = []
        for code in all_codes:
            p_doc = price_map.get(code, {})
            a_doc = avail_map.get(code, {})
            avail_count = int(a_doc.get("count", 0))
            if avail_count <= 0:
                continue
            price_val = p_doc.get("price")
            if price_val is None:
                continue
            result.append(
                {
                    "country_code": code,
                    "country_name": p_doc.get("country_name") or a_doc.get("country_name") or code,
                    "flag": p_doc.get("flag") or a_doc.get("flag") or "🌍",
                    "price": int(price_val),
                    "available_stock": avail_count,
                }
            )
        return result

    async def list_admin_server_countries(self, server: str) -> list[dict[str, Any]]:
        price_docs = await self._db.country_prices.find({"server": server}).to_list(length=200)
        price_map = {doc["country_code"]: doc for doc in price_docs}

        pipeline = [
            {"$match": {"server": server}},
            {
                "$group": {
                    "_id": {"country_code": "$country_code", "status": "$status"},
                    "count": {"$sum": 1},
                    "country_name": {"$first": "$country_name"},
                    "flag": {"$first": "$flag"},
                }
            },
        ]
        rows = await self._db.accounts.aggregate(pipeline).to_list(length=500)
        stats_by_code: dict[str, dict[str, Any]] = {}
        for row in rows:
            code = row["_id"]["country_code"]
            st = row["_id"]["status"]
            entry = stats_by_code.setdefault(
                code,
                {
                    "country_code": code,
                    "country_name": row.get("country_name") or code,
                    "flag": row.get("flag") or "🌍",
                    "available": 0,
                    "locked": 0,
                    "sold": 0,
                },
            )
            entry[st] = int(row.get("count", 0))

        codes = sorted(set(price_map.keys()) | set(stats_by_code.keys()))
        items: list[dict[str, Any]] = []
        for code in codes:
            p_doc = price_map.get(code, {})
            s_doc = stats_by_code.get(code, {})
            if p_doc.get("price") is None and not (s_doc.get("available", 0) or s_doc.get("locked", 0)):
                continue
            items.append(
                {
                    "country_code": code,
                    "country_name": p_doc.get("country_name") or s_doc.get("country_name") or code,
                    "flag": p_doc.get("flag") or s_doc.get("flag") or "🌍",
                    "price": p_doc.get("price"),
                    "available": s_doc.get("available", 0),
                    "locked": s_doc.get("locked", 0),
                    "sold": s_doc.get("sold", 0),
                }
            )
        return items

    async def get_country_numbers(self, server: str, country_code: str, limit: int = 50) -> list[dict[str, Any]]:
        cursor = self._db.accounts.find(
            {"server": server, "country_code": country_code},
            {"session_string": 0, "two_fa_password": 0},
        ).limit(limit)
        return await cursor.to_list(length=limit)

    async def create_locked_order(
        self,
        user_id: int,
        server: str,
        country_code: str,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        price = await self.get_country_price(server, country_code)
        if price is None:
            return False, "Price is not configured for this country yet.", None

        balance = await self._db.get_balance(user_id)
        if balance < price:
            return (
                False,
                f"Insufficient balance. Required: ₹{price}, Your Balance: ₹{balance}. Use Recharge first.",
                None,
            )

        now = utc_now()
        lock_until = now + timedelta(seconds=ORDER_LOCK_SECONDS)

        order_doc: dict[str, Any] = {
            "user_id": user_id,
            "server": server,
            "country_code": country_code,
            "country_name": "",
            "flag": "🌍",
            "phone_number": "",
            "account_id": None,
            "price": price,
            "status": "locked",  # locked | otp_sent | completed | expired | cancelled
            "balance_deducted": False,
            "otp_attempts_used": 0,
            "max_otp_attempts": MAX_OTP_ATTEMPTS,
            "new_device_logged_in": False,
            "last_otp": None,
            "last_otp_msg_id": None,
            "created_at": now,
            "expires_at": lock_until,
            "completed_at": None,
        }
        inserted = await self._db.orders.insert_one(order_doc)
        order_id = inserted.inserted_id

        account = await self._db.accounts.find_one_and_update(
            {
                "server": server,
                "country_code": country_code,
                "status": "available",
            },
            {
                "$set": {
                    "status": "locked",
                    "locked_by": user_id,
                    "locked_until": lock_until,
                    "active_order_id": order_id,
                }
            },
            sort=[("added_at", 1)],
            return_document=True,
        )

        if account is None:
            await self._db.orders.update_one(
                {"_id": order_id},
                {"$set": {"status": "cancelled", "completed_at": now}},
            )
            return False, "Sorry, no available number left in stock for this country.", None

        updated_order = await self._db.orders.find_one_and_update(
            {"_id": order_id},
            {
                "$set": {
                    "account_id": account["_id"],
                    "phone_number": account["phone_number"],
                    "country_name": account.get("country_name", country_code),
                    "flag": account.get("flag", "🌍"),
                }
            },
            return_document=True,
        )
        return True, "Number reserved for 20 minutes.", updated_order

    async def get_order(self, order_id_str: str) -> dict[str, Any] | None:
        try:
            oid = ObjectId(order_id_str)
        except Exception:
            return None
        return await self._db.orders.find_one({"_id": oid})

    async def get_account_for_order(self, order: dict[str, Any]) -> dict[str, Any] | None:
        acc_id = order.get("account_id")
        if not acc_id:
            return None
        return await self._db.accounts.find_one({"_id": acc_id})

    async def mark_otp_delivered_and_charge(
        self,
        order_id: ObjectId,
        user_id: int,
        price: int,
        otp_code: str,
        msg_id: int | None,
        baseline_hashes: list[int] | None = None,
    ) -> tuple[bool, str, int]:
        """Balance is cut here, at the moment the OTP is handed to the buyer."""
        order = await self._db.orders.find_one({"_id": order_id})
        already_charged = bool(order and order.get("balance_deducted"))

        new_balance = await self._db.get_balance(user_id)
        if not already_charged:
            ok, bal_after = await self._db.deduct_balance_atomic(user_id, price)
            if not ok:
                return False, f"Insufficient balance (₹{bal_after}) to complete OTP delivery.", bal_after
            new_balance = bal_after

        await self._db.orders.update_one(
            {"_id": order_id},
            {
                "$set": {
                    "status": "otp_sent",
                    "balance_deducted": True,
                    "last_otp": otp_code,
                    "last_otp_msg_id": msg_id,
                    "otp_delivered_at": utc_now(),
                    "baseline_auth_hashes": baseline_hashes,
                },
                "$inc": {"otp_attempts_used": 1},
            },
        )
        return True, "OTP delivered.", new_balance

    async def mark_order_done(self, order_id: ObjectId, account_id: ObjectId, user_id: int) -> None:
        now = utc_now()
        await self._db.orders.update_one(
            {"_id": order_id},
            {
                "$set": {
                    "status": "completed",
                    "new_device_logged_in": True,
                    "completed_at": now,
                }
            },
        )
        await self._db.accounts.update_one(
            {"_id": account_id},
            {
                "$set": {
                    "status": "sold",
                    "sold_to": user_id,
                    "sold_at": now,
                    "locked_by": None,
                    "locked_until": None,
                    "active_order_id": None,
                }
            },
        )

    async def expire_timed_out_orders(self) -> list[dict[str, Any]]:
        now = utc_now()
        expired_orders = await self._db.orders.find(
            {
                "status": {"$in": ["locked", "otp_sent"]},
                "expires_at": {"$lte": now},
            }
        ).to_list(length=100)

        processed: list[dict[str, Any]] = []
        for order in expired_orders:
            res = await self._db.orders.find_one_and_update(
                {"_id": order["_id"], "status": {"$in": ["locked", "otp_sent"]}},
                {"$set": {"status": "expired", "completed_at": now}},
                return_document=True,
            )
            if res is None:
                continue

            if res.get("account_id"):
                await self._db.accounts.update_one(
                    {"_id": res["account_id"], "status": "locked"},
                    {
                        "$set": {
                            "status": "available",
                            "locked_by": None,
                            "locked_until": None,
                            "active_order_id": None,
                        }
                    },
                )
            processed.append(res)
        return processed

    async def get_status_summary(self) -> dict[str, Any]:
        total_users = await self._db.users.count_documents({})
        avail_count = await self._db.accounts.count_documents({"status": "available"})
        locked_count = await self._db.accounts.count_documents({"status": "locked"})
        sold_count = await self._db.accounts.count_documents({"status": "sold"})
        total_accounts = avail_count + locked_count + sold_count
        completed_orders = await self._db.orders.count_documents({"status": "completed"})
        return {
            "total_users": total_users,
            "total_accounts": total_accounts,
            "available": avail_count,
            "locked": locked_count,
            "sold": sold_count,
            "completed_orders": completed_orders,
        }

    async def get_purchase_history(self, limit: int = 15) -> list[dict[str, Any]]:
        cursor = self._db.orders.find({"status": "completed"}).sort("completed_at", -1).limit(limit)
        return await cursor.to_list(length=limit)

    async def get_user_orders(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        cursor = self._db.orders.find({"user_id": user_id}).sort("created_at", -1).limit(limit)
        return await cursor.to_list(length=limit)

    # ---------------- ACCOUNT HEALTH (frozen / deleted / expired) ----------------
    async def list_scannable_accounts(self) -> list[dict[str, Any]]:
        """Unsold numbers sitting in stock (these are the ones worth checking)."""
        cursor = self._db.accounts.find({"status": "available", "session_string": {"$nin": [None, ""]}})
        return await cursor.to_list(length=5000)

    async def mark_account_dead(self, account_id: ObjectId, status: str, reason: str) -> dict[str, Any] | None:
        """Moves an unsold account out of the sale list. Returns the doc, or None if it was no longer available."""
        return await self._db.accounts.find_one_and_update(
            {"_id": account_id, "status": "available"},
            {"$set": {"status": status, "dead_reason": reason, "dead_at": utc_now()}},
            return_document=True,
        )

    async def list_dead_accounts(self, statuses: list[str], limit: int = 40) -> tuple[list[dict[str, Any]], int]:
        query = {"status": {"$in": statuses}}
        total = await self._db.accounts.count_documents(query)
        cursor = self._db.accounts.find(query, {"session_string": 0, "two_fa_password": 0}).sort("dead_at", -1).limit(limit)
        return await cursor.to_list(length=limit), total

    async def clear_dead_accounts(self, statuses: list[str]) -> int:
        res = await self._db.accounts.delete_many({"status": {"$in": statuses}})
        return int(res.deleted_count)

    # ---------------- REMOVE STOCK (admin) ----------------
    _REMOVABLE = ["available", "frozen", "deleted", "expired"]

    async def remove_country_stock(self, server: str, country_code: str) -> dict[str, int]:
        """Deletes every unsold number of a country on a server (locked / sold ones are kept)."""
        res = await self._db.accounts.delete_many(
            {"server": server, "country_code": country_code, "status": {"$in": self._REMOVABLE}}
        )
        locked = await self._db.accounts.count_documents(
            {"server": server, "country_code": country_code, "status": "locked"}
        )
        sold = await self._db.accounts.count_documents(
            {"server": server, "country_code": country_code, "status": "sold"}
        )
        if locked == 0:
            await self._db.country_prices.delete_one({"server": server, "country_code": country_code})
        return {"removed": int(res.deleted_count), "locked": locked, "sold": sold}

    async def remove_number(self, server: str, country_code: str, phone_number: str) -> tuple[bool, str]:
        doc = await self._db.accounts.find_one(
            {"server": server, "country_code": country_code, "phone_number": phone_number},
            {"session_string": 0, "two_fa_password": 0},
        )
        if not doc:
            return False, f"Number `{phone_number}` was not found in this country's stock."
        status = doc.get("status")
        if status == "locked":
            return False, f"`{phone_number}` is reserved by a buyer right now, so it cannot be removed."
        if status == "sold":
            return False, f"`{phone_number}` is already sold (kept for records), so it cannot be removed."
        res = await self._db.accounts.delete_one({"_id": doc["_id"], "status": {"$in": self._REMOVABLE}})
        if res.deleted_count != 1:
            return False, f"`{phone_number}` just changed status, please try again."
        return True, f"`{phone_number}` removed from stock."
