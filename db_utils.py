import aiosqlite
import logging

DB_NAME = 'suggestion_bot.db'

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY
            )
        ''')
        await db.commit()
        logging.info("Database initialized.")

async def is_user_blocked(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)) as cursor:
            result = await cursor.fetchone()
            return result is not None

async def block_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute("INSERT OR IGNORE INTO blocked_users (user_id) VALUES (?)", (user_id,))
            await db.commit()
            logging.info(f"User {user_id} added to block list in DB.")
        except Exception as e:
            logging.error(f"Failed to block user {user_id} in DB: {e}")

async def unban_user(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            cursor = await db.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))
            await db.commit()
            if cursor.rowcount > 0:
                logging.info(f"User {user_id} unblocked in DB.")
                return True
            else:
                logging.info(f"User {user_id} was not found in the block list to unban.")
                return False
        except Exception as e:
            logging.error(f"Failed to unban user {user_id} in DB: {e}")
            return False