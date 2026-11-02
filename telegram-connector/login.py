"""
One-time login for the Telegram connector. Run it ON YOUR SERVER:

    docker compose run --rm telegram python login.py

It asks for your api_id, api_hash, phone number, the code Telegram sends you,
and your two-step password if you have one. Secret things are typed hidden.
Nothing here ever goes to Claude. The result is saved in /data/account.json,
which only this connector can read.
"""

import asyncio
import json
import os
from getpass import getpass
from pathlib import Path

from telethon import TelegramClient, utils
from telethon.sessions import StringSession

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
ACCOUNT_FILE = DATA_DIR / "account.json"


async def main():
    print("\n=== Telegram login (one time) ===")
    print("Get api_id and api_hash at https://my.telegram.org -> API development tools.\n")
    if ACCOUNT_FILE.exists():
        again = input("You're already logged in. Log in again and replace it? (y/N): ")
        if again.strip().lower() != "y":
            print("Nothing changed.")
            return

    api_id = int(input("api_id (numbers only): ").strip())
    api_hash = getpass("api_hash (hidden as you type, then press Enter): ").strip()
    phone = input("Your phone number with country code, e.g. +905551234567: ").strip()

    client = TelegramClient(
        StringSession(), api_id, api_hash,
        device_model="Claude connector", system_version="Linux server", app_version="1.0",
    )
    await client.start(
        phone=lambda: phone,
        code_callback=lambda: input("Code Telegram just sent you (look in your Telegram app): ").strip(),
        password=lambda: getpass("Two-step verification password (hidden): "),
    )
    me = await client.get_me()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNT_FILE.write_text(json.dumps({
        "api_id": api_id,
        "api_hash": api_hash,
        "session": client.session.save(),
    }))
    os.chmod(ACCOUNT_FILE, 0o600)
    await client.disconnect()

    print(f"\nDone! Logged in as {utils.get_display_name(me)}.")
    print("Now start the connector with:  docker compose up -d --build\n")


if __name__ == "__main__":
    asyncio.run(main())
