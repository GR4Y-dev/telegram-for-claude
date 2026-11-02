"""
One-time login for the AI assistant's OWN Telegram account (your second account,
not your main one). setup.sh runs it for you.

It asks for the api_id and api_hash (the same ones from my.telegram.org work), the
second account's phone number, the code Telegram sends, and its two-step password
if it has one. Nothing here ever goes to Claude. The result is saved in data/account.json.
"""

import asyncio
import json
import os
from getpass import getpass
from pathlib import Path

from telethon import TelegramClient, utils
from telethon.sessions import StringSession

ACCOUNT_FILE = Path(__file__).resolve().parent / "data" / "account.json"


async def main():
    print("\n=== Log in the assistant's Telegram account (your SECOND account) ===\n")
    if ACCOUNT_FILE.exists():
        again = input("Already logged in. Log in again and replace it? (y/N): ")
        if again.strip().lower() != "y":
            print("Nothing changed.")
            return

    api_id = int(input("api_id (numbers only): ").strip())
    api_hash = getpass("api_hash (hidden as you type, then press Enter): ").strip()
    phone = input("The SECOND account's phone number with country code: ").strip()

    client = TelegramClient(
        StringSession(), api_id, api_hash,
        device_model="AI assistant", system_version="Linux server", app_version="1.0",
    )
    await client.start(
        phone=lambda: phone,
        code_callback=lambda: input("Code Telegram just sent to the second account: ").strip(),
        password=lambda: getpass("Its two-step verification password (hidden): "),
    )
    me = await client.get_me()

    ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNT_FILE.write_text(json.dumps({
        "api_id": api_id,
        "api_hash": api_hash,
        "session": client.session.save(),
        "id": me.id,
        "username": me.username,
    }))
    os.chmod(ACCOUNT_FILE, 0o600)
    await client.disconnect()
    print(f"\nDone! The assistant is logged in as {utils.get_display_name(me)}.\n")


if __name__ == "__main__":
    asyncio.run(main())
