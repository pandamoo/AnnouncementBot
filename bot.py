from __future__ import annotations

import logging
import os
from typing import Iterable, Optional, Set

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from offers_db import Offer, OfferStore, normalize_price, parse_quantity

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=LOG_LEVEL,
)
LOGGER = logging.getLogger("offers-bot")


def _parse_admin_ids(value: Optional[str]) -> Set[int]:
    if not value:
        return set()
    ids = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        ids.add(int(item))
    return ids


ADMIN_USER_IDS = _parse_admin_ids(os.environ.get("ADMIN_USER_IDS"))
ANNOUNCE_CHAT_ID = os.environ.get("ANNOUNCE_CHAT_ID")
CONTACT_TEXT = os.environ.get("CONTACT_TEXT", "LMK if interested.")


def _get_store(context: ContextTypes.DEFAULT_TYPE) -> OfferStore:
    store = context.application.bot_data.get("store")
    if not store:
        raise RuntimeError("OfferStore not initialized")
    return store


def _is_admin(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    if not ADMIN_USER_IDS:
        return True
    return user_id in ADMIN_USER_IDS


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id):
        await update.effective_message.reply_text(
            "Not authorized. Ask the owner to add you as an admin."
        )
        return False
    return True


def _announcement_chat_id(update: Update) -> int:
    if ANNOUNCE_CHAT_ID:
        return int(ANNOUNCE_CHAT_ID.strip())
    if update.effective_chat is None:
        raise RuntimeError("No chat available for announcement")
    return update.effective_chat.id


def _build_announcement(offer: Offer) -> str:
    return (
        f"Hey! I have {offer.name} in right now. "
        f"{offer.quantity} available at ${offer.price}. {CONTACT_TEXT}"
    )


def _format_offer_line(offer: Offer) -> str:
    return f"#{offer.id} - {offer.name} — {offer.quantity} @ ${offer.price}"


def _format_offers(offers: Iterable[Offer]) -> str:
    lines = ["Current stock:"]
    lines.extend(_format_offer_line(offer) for offer in offers)
    return "\n".join(lines)


def _command_text(text: Optional[str]) -> str:
    if not text:
        return ""
    parts = text.split(" ", 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _parse_offer_id(text: str) -> int:
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError("Offer id must be a number") from exc


def _parse_add_payload(payload: str) -> tuple[str, int, str]:
    parts = [part.strip() for part in payload.split("|")]
    if len(parts) != 3:
        raise ValueError("Expected three values: name | quantity | price")
    name, quantity_text, price_text = parts
    if not name:
        raise ValueError("Name is required")
    quantity = parse_quantity(quantity_text)
    if quantity <= 0:
        raise ValueError("Quantity must be greater than zero")
    price = normalize_price(price_text)
    return name, quantity, price


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Hey! Use /stock to see what's available right now."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_admin = _is_admin(update.effective_user.id if update.effective_user else None)
    lines = ["Customer commands:", "/stock - show current offers"]
    if is_admin:
        lines.extend(
            [
                "",
                "Admin commands:",
                "/add Name | qty | price",
                "/setqty <id> <qty>",
                "/setprice <id> <price>",
                "/soldout <id>",
                "/announce <id>",
            ]
        )
    await update.effective_message.reply_text("\n".join(lines))


async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = _get_store(context)
    offers = list(store.list_offers(active_only=True))
    if not offers:
        await update.effective_message.reply_text("All sold out right now.")
        return
    await update.effective_message.reply_text(_format_offers(offers))


async def text_stock_trigger(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    text = message.text.strip().lower()
    if text in {"stock", "offers", "list"}:
        await stock(update, context)


async def add_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    payload = _command_text(update.effective_message.text)
    if not payload:
        await update.effective_message.reply_text(
            "Usage: /add Name | qty | price"
        )
        return
    try:
        name, quantity, price = _parse_add_payload(payload)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    store = _get_store(context)
    offer = store.add_offer(name=name, quantity=quantity, price=price)

    announce_chat_id = _announcement_chat_id(update)
    announcement = _build_announcement(offer)
    try:
        sent = await context.bot.send_message(
            chat_id=announce_chat_id, text=announcement
        )
        store.attach_announcement(offer.id, sent.chat.id, sent.message_id)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Announcement send failed: %s", exc)
        await update.effective_message.reply_text(
            f"Added offer #{offer.id}. Announcement failed: {exc}"
        )
        return

    if announce_chat_id == update.effective_chat.id:
        await update.effective_message.reply_text(f"Added offer #{offer.id}.")
    else:
        await update.effective_message.reply_text(
            f"Added offer #{offer.id} and announced it."
        )


async def set_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    payload = _command_text(update.effective_message.text)
    if not payload:
        await update.effective_message.reply_text(
            "Usage: /setqty <id> <qty>"
        )
        return
    parts = payload.split()
    if len(parts) != 2:
        await update.effective_message.reply_text(
            "Usage: /setqty <id> <qty>"
        )
        return
    try:
        offer_id = _parse_offer_id(parts[0])
        quantity = parse_quantity(parts[1])
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    store = _get_store(context)
    offer = store.get_offer(offer_id)
    if not offer:
        await update.effective_message.reply_text("Offer not found.")
        return

    if quantity == 0:
        await _mark_sold_out(update, context, offer)
        return

    store.update_quantity(offer_id, quantity)
    store.set_active(offer_id, True)
    await update.effective_message.reply_text(
        f"Updated #{offer_id} quantity to {quantity}."
    )


async def set_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    payload = _command_text(update.effective_message.text)
    if not payload:
        await update.effective_message.reply_text(
            "Usage: /setprice <id> <price>"
        )
        return
    parts = payload.split()
    if len(parts) != 2:
        await update.effective_message.reply_text(
            "Usage: /setprice <id> <price>"
        )
        return
    try:
        offer_id = _parse_offer_id(parts[0])
        price = normalize_price(parts[1])
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    store = _get_store(context)
    if not store.get_offer(offer_id):
        await update.effective_message.reply_text("Offer not found.")
        return
    store.update_price(offer_id, price)
    await update.effective_message.reply_text(
        f"Updated #{offer_id} price to ${price}."
    )


async def sold_out(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    payload = _command_text(update.effective_message.text)
    if not payload:
        await update.effective_message.reply_text("Usage: /soldout <id>")
        return
    try:
        offer_id = _parse_offer_id(payload)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    store = _get_store(context)
    offer = store.get_offer(offer_id)
    if not offer:
        await update.effective_message.reply_text("Offer not found.")
        return

    await _mark_sold_out(update, context, offer)


async def _mark_sold_out(
    update: Update, context: ContextTypes.DEFAULT_TYPE, offer: Offer
) -> None:
    store = _get_store(context)
    store.update_quantity(offer.id, 0)
    store.set_active(offer.id, False)
    deleted = False
    if offer.announce_chat_id and offer.announce_message_id:
        try:
            await context.bot.delete_message(
                chat_id=offer.announce_chat_id,
                message_id=offer.announce_message_id,
            )
            deleted = True
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Announcement delete failed: %s", exc)
    if deleted:
        await update.effective_message.reply_text(
            f"Marked #{offer.id} as sold out and removed the announcement."
        )
    else:
        await update.effective_message.reply_text(
            f"Marked #{offer.id} as sold out."
        )


async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    payload = _command_text(update.effective_message.text)
    if not payload:
        await update.effective_message.reply_text("Usage: /announce <id>")
        return
    try:
        offer_id = _parse_offer_id(payload)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    store = _get_store(context)
    offer = store.get_offer(offer_id)
    if not offer or not offer.active:
        await update.effective_message.reply_text(
            "Offer not found or inactive."
        )
        return

    announce_chat_id = _announcement_chat_id(update)
    announcement = _build_announcement(offer)
    try:
        sent = await context.bot.send_message(
            chat_id=announce_chat_id, text=announcement
        )
        store.attach_announcement(offer.id, sent.chat.id, sent.message_id)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Announcement send failed: %s", exc)
        await update.effective_message.reply_text(str(exc))
        return

    await update.effective_message.reply_text(
        f"Announced #{offer.id}."
    )


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")

    db_path = os.environ.get("OFFERS_DB_PATH", "offers.db")
    store = OfferStore(db_path)

    app = ApplicationBuilder().token(token).build()
    app.bot_data["store"] = store

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(CommandHandler("list", stock))
    app.add_handler(CommandHandler("add", add_offer))
    app.add_handler(CommandHandler("setqty", set_quantity))
    app.add_handler(CommandHandler("setprice", set_price))
    app.add_handler(CommandHandler("soldout", sold_out))
    app.add_handler(CommandHandler("remove", sold_out))
    app.add_handler(CommandHandler("announce", announce))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_stock_trigger))

    LOGGER.info("Starting offers bot polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
