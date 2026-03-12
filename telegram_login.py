import asyncio
from telethon import TelegramClient

import os
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
SESSION_FILE = "/opt/swarmesh/swarmesh_telegram"

async def main():
    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await client.start(phone=os.getenv("TELEGRAM_PHONE", ""))
    me = await client.get_me()
    print(f"Logged in as: {me.first_name} (@{me.username})")
    print(f"Session saved to: {SESSION_FILE}.session")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
