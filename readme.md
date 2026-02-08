## Offers Announcement Bot (Telegram)

This bot lets you announce new offers once and then lets customers check
current stock themselves. It also removes old offers when they sell out.

### Features

- Post a simple announcement like:
  `Hey! I have X in right now. Y available at $Z. LMK if interested.`
- `/stock` command for customers to see what's available.
- Admin commands to add, update, and mark offers sold out.
- Sold out items are removed from the stock list and (optionally) the
  original announcement message is deleted.

### Quick Start

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Set environment variables:

   ```bash
   export TELEGRAM_BOT_TOKEN="YOUR_BOT_TOKEN"
   export ADMIN_USER_IDS="123456789"
   # Optional: channel or group ID to post announcements
   export ANNOUNCE_CHAT_ID="-1001234567890"
   # Optional: override the default ending text
   export CONTACT_TEXT="LMK if interested."
   ```

3. Run the bot:

   ```bash
   python bot.py
   ```

### Commands

**Customers**

- `/stock` - show current offers

**Admin**

- `/add Name | qty | price`
  - Example: `/add Blue Dream | 5 | 25`
- `/setqty <id> <qty>`
  - Example: `/setqty 12 3`
  - If `qty` is `0`, the offer is marked sold out
- `/setprice <id> <price>`
  - Example: `/setprice 12 30`
- `/soldout <id>` or `/remove <id>`
- `/announce <id>` - re-send the announcement

### Notes

- If `ANNOUNCE_CHAT_ID` is set, the bot posts announcements there and tries
  to delete that message when you mark an offer sold out. The bot needs admin
  rights in that chat to delete messages.
- Offers are stored in a local SQLite database (`offers.db` by default).
  You can change the path via `OFFERS_DB_PATH`.
