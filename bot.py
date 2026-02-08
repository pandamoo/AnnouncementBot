from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable, Optional, Set, Tuple

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from announcement_stock_bot import DEFAULT_THRESHOLD_MB, generate_announcement
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


def _read_float_env(name: str, default: float) -> float:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        LOGGER.warning("%s must be a number. Using %.2f.", name, default)
        return default


UPLOAD_THRESHOLD_MB = _read_float_env(
    "UPLOAD_THRESHOLD_MB", DEFAULT_THRESHOLD_MB
)
CATBOX_USERHASH = os.environ.get("CATBOX_USERHASH")

FLOW_KEY = "flow"
FLOW_DATA_KEY = "flow_data"

FLOW_ADD_NAME = "add_name"
FLOW_ADD_QTY = "add_qty"
FLOW_ADD_PRICE = "add_price"
FLOW_SET_QTY_ID = "set_qty_id"
FLOW_SET_QTY_VALUE = "set_qty_value"
FLOW_SET_PRICE_ID = "set_price_id"
FLOW_SET_PRICE_VALUE = "set_price_value"
FLOW_SOLD_OUT_ID = "sold_out_id"
FLOW_ANNOUNCE_ID = "announce_id"
FLOW_UPLOAD_WAIT_FILE = "upload_wait_file"

SETTING_ANNOUNCE_CHAT_ID = "announce_chat_id"

MENU_STOCK = "Stock"
MENU_ADD = "Add offer"
MENU_SET_QTY = "Update qty"
MENU_SET_PRICE = "Update price"
MENU_SOLD_OUT = "Sold out"
MENU_ANNOUNCE = "Re-announce"
MENU_SET_ANNOUNCE = "Set announce chat"
MENU_UPLOAD = "Upload file"
MENU_HELP = "Help"
MENU_MENU = "Menu"
MENU_CANCEL = "Cancel"

MENU_LABELS = {
    MENU_STOCK,
    MENU_ADD,
    MENU_SET_QTY,
    MENU_SET_PRICE,
    MENU_SOLD_OUT,
    MENU_ANNOUNCE,
    MENU_SET_ANNOUNCE,
    MENU_UPLOAD,
    MENU_HELP,
    MENU_MENU,
    MENU_CANCEL,
}


def _get_store(context: ContextTypes.DEFAULT_TYPE) -> OfferStore:
    store = context.application.bot_data.get("store")
    if not store:
        raise RuntimeError("OfferStore not initialized")
    return store


def _is_admin(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    if not ADMIN_USER_IDS:
        return False
    return user_id in ADMIN_USER_IDS


def _build_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(MENU_STOCK), KeyboardButton(MENU_HELP)],
    ]
    if is_admin:
        rows.extend(
            [
                [KeyboardButton(MENU_ADD), KeyboardButton(MENU_SET_QTY)],
                [KeyboardButton(MENU_SET_PRICE), KeyboardButton(MENU_SOLD_OUT)],
                [
                    KeyboardButton(MENU_ANNOUNCE),
                    KeyboardButton(MENU_SET_ANNOUNCE),
                ],
                [KeyboardButton(MENU_UPLOAD)],
            ]
        )
    rows.append([KeyboardButton(MENU_MENU), KeyboardButton(MENU_CANCEL)])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Tap a button or type a command.",
    )


def _set_flow(
    context: ContextTypes.DEFAULT_TYPE, flow: str, data: Optional[dict] = None
) -> None:
    context.user_data[FLOW_KEY] = flow
    if data is None:
        context.user_data.setdefault(FLOW_DATA_KEY, {})
    else:
        context.user_data[FLOW_DATA_KEY] = data


def _clear_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(FLOW_KEY, None)
    context.user_data.pop(FLOW_DATA_KEY, None)


def _current_flow(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    return context.user_data.get(FLOW_KEY)


def _flow_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault(FLOW_DATA_KEY, {})


def _safe_filename(name: Optional[str], fallback: str) -> str:
    if not name:
        name = fallback
    name = Path(name).name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return safe or fallback


def _parse_upload_caption(
    caption: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    if not caption:
        return None, None
    trimmed = caption.strip()
    lowered = trimmed.lower()
    for prefix in ("display:", "display=", "display ", "count:", "count=", "count "):
        if lowered.startswith(prefix):
            value = trimmed[len(prefix) :].strip()
            return None, value or None
    for prefix in ("header:", "header=", "header "):
        if lowered.startswith(prefix):
            value = trimmed[len(prefix) :].strip()
            return value or None, None
    return trimmed, None

async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    if not ADMIN_USER_IDS:
        await update.effective_message.reply_text(
            "Admins are not configured. Set ADMIN_USER_IDS to enable admin commands."
        )
        return False
    if not _is_admin(user_id):
        await update.effective_message.reply_text(
            "Not authorized. Ask the owner to add you as an admin."
        )
        return False
    return True


def _announcement_chat_id(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    store = _get_store(context)
    stored_value = store.get_setting(SETTING_ANNOUNCE_CHAT_ID)
    if stored_value:
        try:
            return int(stored_value)
        except ValueError:
            LOGGER.warning(
                "Invalid stored announce chat id: %s", stored_value
            )
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


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_admin = _is_admin(update.effective_user.id if update.effective_user else None)
    menu = _build_menu(is_admin)
    lines = [
        "Welcome! Tap a button below to get started.",
        "You can also type /help for details.",
    ]
    if is_admin:
        lines.extend(
            [
                "",
                "Admin tip: use Upload file to post sample announcements.",
                "Send a document with an optional caption to set the header.",
                "Use Set announce chat in the target group to post there.",
            ]
        )
    await update.effective_message.reply_text(
        "\n".join(lines), reply_markup=menu
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_menu(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_admin = _is_admin(update.effective_user.id if update.effective_user else None)
    lines = [
        "Customer commands:",
        "/stock - show current offers",
        "/menu - show the button menu",
    ]
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
                "/setannounce [chat_id] - set announcement chat",
                "/upload - start file upload flow",
                "/cancel - exit the current step",
                "",
                "You can also use the menu buttons for guided prompts.",
            ]
        )
    await update.effective_message.reply_text(
        "\n".join(lines), reply_markup=_build_menu(is_admin)
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_flow(context)
    is_admin = _is_admin(update.effective_user.id if update.effective_user else None)
    await update.effective_message.reply_text(
        "Canceled. You're back at the main menu.",
        reply_markup=_build_menu(is_admin),
    )


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


async def _create_offer_and_announce(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    name: str,
    quantity: int,
    price: str,
) -> None:
    store = _get_store(context)
    offer = store.add_offer(name=name, quantity=quantity, price=price)

    announce_chat_id = _announcement_chat_id(update, context)
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


async def _send_offer_announcement(
    update: Update, context: ContextTypes.DEFAULT_TYPE, offer_id: int
) -> bool:
    store = _get_store(context)
    offer = store.get_offer(offer_id)
    if not offer or not offer.active:
        await update.effective_message.reply_text(
            "Offer not found or inactive."
        )
        return False

    announce_chat_id = _announcement_chat_id(update, context)
    announcement = _build_announcement(offer)
    try:
        sent = await context.bot.send_message(
            chat_id=announce_chat_id, text=announcement
        )
        store.attach_announcement(offer.id, sent.chat.id, sent.message_id)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Announcement send failed: %s", exc)
        await update.effective_message.reply_text(str(exc))
        return False

    await update.effective_message.reply_text(
        f"Announced #{offer.id}."
    )
    return True

async def add_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    _clear_flow(context)
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
    await _create_offer_and_announce(update, context, name, quantity, price)


async def set_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    _clear_flow(context)
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
    _clear_flow(context)
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
    _clear_flow(context)
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
    _clear_flow(context)
    payload = _command_text(update.effective_message.text)
    if not payload:
        await update.effective_message.reply_text("Usage: /announce <id>")
        return
    try:
        offer_id = _parse_offer_id(payload)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await _send_offer_announcement(update, context, offer_id)


async def set_announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    _clear_flow(context)
    payload = _command_text(update.effective_message.text)
    chat_id: Optional[int]
    if payload:
        try:
            chat_id = int(payload.strip())
        except ValueError:
            await update.effective_message.reply_text(
                "Chat id must be a number. Example: /setannounce -1001234567890"
            )
            return
    else:
        if update.effective_chat is None:
            await update.effective_message.reply_text(
                "Use this command in the target chat or provide a chat id."
            )
            return
        chat_id = update.effective_chat.id

    store = _get_store(context)
    store.set_setting(SETTING_ANNOUNCE_CHAT_ID, str(chat_id))
    if update.effective_chat and update.effective_chat.id == chat_id:
        await update.effective_message.reply_text(
            "This chat is now set for announcements."
        )
    else:
        await update.effective_message.reply_text(
            f"Announcement chat set to {chat_id}."
        )


async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    _clear_flow(context)
    _set_flow(context, FLOW_UPLOAD_WAIT_FILE, {})
    await update.effective_message.reply_text(
        "\n".join(
            [
                "Send the file as a document to upload it.",
                "Optional: add a caption to use as the announcement header.",
                "Tip: use 'display: 2.5M' to show an original count.",
                "Send /cancel to stop.",
            ]
        )
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    flow = _current_flow(context)
    if flow and flow != FLOW_UPLOAD_WAIT_FILE:
        await update.effective_message.reply_text(
            "You're in the middle of another step. Send /cancel to stop."
        )
        return

    message = update.effective_message
    document = message.document if message else None
    if not document:
        return

    custom_header, display_count = _parse_upload_caption(message.caption)
    file_name = _safe_filename(document.file_name, "upload.txt")
    await update.effective_message.reply_text(
        "Got it. Uploading and building the announcement now."
    )
    if update.effective_chat:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING
        )

    messages: list[str] = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        file_path = Path(tmp_dir) / file_name
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=str(file_path))
        try:
            messages = generate_announcement(
                files=[file_path],
                custom_header=custom_header,
                display_count=display_count,
                threshold_mb=UPLOAD_THRESHOLD_MB,
                catbox_userhash=CATBOX_USERHASH,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("File announcement failed: %s", exc)
            await update.effective_message.reply_text(
                f"Upload failed: {exc}"
            )
            _clear_flow(context)
            return

    announce_chat_id = _announcement_chat_id(update, context)
    for outgoing in messages:
        await context.bot.send_message(chat_id=announce_chat_id, text=outgoing)
    if announce_chat_id != update.effective_chat.id:
        await update.effective_message.reply_text(
            "Uploaded and announced in the target chat."
        )
    _clear_flow(context)


async def _start_add_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await _require_admin(update, context):
        return
    _clear_flow(context)
    _set_flow(context, FLOW_ADD_NAME, {})
    await update.effective_message.reply_text(
        "Let's add a new offer. Send the item name."
    )


async def _start_set_qty_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await _require_admin(update, context):
        return
    _clear_flow(context)
    _set_flow(context, FLOW_SET_QTY_ID, {})
    await update.effective_message.reply_text(
        "Send the offer ID to update its quantity."
    )


async def _start_set_price_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await _require_admin(update, context):
        return
    _clear_flow(context)
    _set_flow(context, FLOW_SET_PRICE_ID, {})
    await update.effective_message.reply_text(
        "Send the offer ID to update its price."
    )


async def _start_sold_out_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await _require_admin(update, context):
        return
    _clear_flow(context)
    _set_flow(context, FLOW_SOLD_OUT_ID, {})
    await update.effective_message.reply_text(
        "Send the offer ID to mark it sold out."
    )


async def _start_announce_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await _require_admin(update, context):
        return
    _clear_flow(context)
    _set_flow(context, FLOW_ANNOUNCE_ID, {})
    await update.effective_message.reply_text(
        "Send the offer ID to re-announce it."
    )


async def _handle_flow_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    flow = _current_flow(context)
    if not flow:
        return
    if not _is_admin(update.effective_user.id if update.effective_user else None):
        _clear_flow(context)
        await update.effective_message.reply_text(
            "Not authorized. Ask the owner to add you as an admin."
        )
        return

    data = _flow_data(context)
    if flow == FLOW_ADD_NAME:
        name = text.strip()
        if not name:
            await update.effective_message.reply_text(
                "Please send a name for the offer."
            )
            return
        data["name"] = name
        _set_flow(context, FLOW_ADD_QTY, data)
        await update.effective_message.reply_text(
            "Great. Now send the quantity (whole number)."
        )
        return

    if flow == FLOW_ADD_QTY:
        try:
            quantity = parse_quantity(text)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        if quantity <= 0:
            await update.effective_message.reply_text(
                "Quantity must be greater than zero."
            )
            return
        data["quantity"] = quantity
        _set_flow(context, FLOW_ADD_PRICE, data)
        await update.effective_message.reply_text(
            "Almost done. Send the price (number)."
        )
        return

    if flow == FLOW_ADD_PRICE:
        try:
            price = normalize_price(text)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        await _create_offer_and_announce(
            update,
            context,
            data.get("name", "").strip(),
            int(data.get("quantity", 0)),
            price,
        )
        _clear_flow(context)
        return

    if flow == FLOW_SET_QTY_ID:
        try:
            offer_id = _parse_offer_id(text)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        store = _get_store(context)
        if not store.get_offer(offer_id):
            await update.effective_message.reply_text(
                "Offer not found. Send a valid offer ID."
            )
            return
        data["offer_id"] = offer_id
        _set_flow(context, FLOW_SET_QTY_VALUE, data)
        await update.effective_message.reply_text(
            "Send the new quantity (0 to mark sold out)."
        )
        return

    if flow == FLOW_SET_QTY_VALUE:
        try:
            quantity = parse_quantity(text)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        offer_id = int(data.get("offer_id", 0))
        store = _get_store(context)
        offer = store.get_offer(offer_id)
        if not offer:
            await update.effective_message.reply_text(
                "Offer not found. Send a valid offer ID."
            )
            _set_flow(context, FLOW_SET_QTY_ID, {})
            return
        if quantity == 0:
            await _mark_sold_out(update, context, offer)
            _clear_flow(context)
            return
        store.update_quantity(offer_id, quantity)
        store.set_active(offer_id, True)
        await update.effective_message.reply_text(
            f"Updated #{offer_id} quantity to {quantity}."
        )
        _clear_flow(context)
        return

    if flow == FLOW_SET_PRICE_ID:
        try:
            offer_id = _parse_offer_id(text)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        store = _get_store(context)
        if not store.get_offer(offer_id):
            await update.effective_message.reply_text(
                "Offer not found. Send a valid offer ID."
            )
            return
        data["offer_id"] = offer_id
        _set_flow(context, FLOW_SET_PRICE_VALUE, data)
        await update.effective_message.reply_text(
            "Send the new price."
        )
        return

    if flow == FLOW_SET_PRICE_VALUE:
        try:
            price = normalize_price(text)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        offer_id = int(data.get("offer_id", 0))
        store = _get_store(context)
        if not store.get_offer(offer_id):
            await update.effective_message.reply_text(
                "Offer not found. Send a valid offer ID."
            )
            _set_flow(context, FLOW_SET_PRICE_ID, {})
            return
        store.update_price(offer_id, price)
        await update.effective_message.reply_text(
            f"Updated #{offer_id} price to ${price}."
        )
        _clear_flow(context)
        return

    if flow == FLOW_SOLD_OUT_ID:
        try:
            offer_id = _parse_offer_id(text)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        store = _get_store(context)
        offer = store.get_offer(offer_id)
        if not offer:
            await update.effective_message.reply_text(
                "Offer not found. Send a valid offer ID."
            )
            return
        await _mark_sold_out(update, context, offer)
        _clear_flow(context)
        return

    if flow == FLOW_ANNOUNCE_ID:
        try:
            offer_id = _parse_offer_id(text)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        success = await _send_offer_announcement(update, context, offer_id)
        if success:
            _clear_flow(context)
        return

    if flow == FLOW_UPLOAD_WAIT_FILE:
        await update.effective_message.reply_text(
            "Please send a file as a document, or /cancel to stop."
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    text = message.text.strip()

    if text == MENU_CANCEL:
        await cancel(update, context)
        return

    flow = _current_flow(context)
    if flow:
        if text == MENU_HELP:
            await help_command(update, context)
            return
        if text == MENU_MENU:
            await show_menu(update, context)
            return
        if text in MENU_LABELS:
            await update.effective_message.reply_text(
                "You're in the middle of a step. Send /cancel to stop."
            )
            return
        await _handle_flow_text(update, context, text)
        return

    if text == MENU_MENU:
        await show_menu(update, context)
        return
    if text == MENU_HELP:
        await help_command(update, context)
        return
    if text == MENU_STOCK:
        await stock(update, context)
        return
    if text == MENU_ADD:
        await _start_add_flow(update, context)
        return
    if text == MENU_SET_QTY:
        await _start_set_qty_flow(update, context)
        return
    if text == MENU_SET_PRICE:
        await _start_set_price_flow(update, context)
        return
    if text == MENU_SOLD_OUT:
        await _start_sold_out_flow(update, context)
        return
    if text == MENU_ANNOUNCE:
        await _start_announce_flow(update, context)
        return
    if text == MENU_SET_ANNOUNCE:
        await set_announce(update, context)
        return
    if text == MENU_UPLOAD:
        await upload_command(update, context)
        return

    await text_stock_trigger(update, context)


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
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(CommandHandler("list", stock))
    app.add_handler(CommandHandler("add", add_offer))
    app.add_handler(CommandHandler("setqty", set_quantity))
    app.add_handler(CommandHandler("setprice", set_price))
    app.add_handler(CommandHandler("soldout", sold_out))
    app.add_handler(CommandHandler("remove", sold_out))
    app.add_handler(CommandHandler("announce", announce))
    app.add_handler(CommandHandler("setannounce", set_announce))
    app.add_handler(CommandHandler("upload", upload_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    LOGGER.info("Starting offers bot polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
