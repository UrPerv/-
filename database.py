import sqlite3
from state import group_rooms, global_nicknames, user_nicknames, nickname_counter, user_group, custom_nicknames
from cryptography.fernet import Fernet
import os
from dotenv import load_dotenv

load_dotenv("db.env")
DB_PATH = "bot.db"
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY").encode()
fernet = Fernet(ENCRYPTION_KEY)

# Временное хранилище для восстановления в памяти

# Инициализация базы
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY,
            text BLOB,
            media_id BLOB,
            media_type TEXT
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS global_profiles (
            user_id INTEGER PRIMARY KEY,
            nickname BLOB
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS room_links (
            user_id INTEGER PRIMARY KEY,
            room_code TEXT,
            last_active_room TEXT,
            nickname_in_room BLOB
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS room_settings (
            room_code TEXT PRIMARY KEY,
            welcome_message TEXT,
            short_description TEXT,
            moderator_id INTEGER,
            created REAL,
            is_open INTEGER,
            is_private INTEGER
        )

        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER,
            room_code TEXT,
            PRIMARY KEY (user_id, room_code)
        )
        
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS room_members (
            room_code TEXT,
            user_id INTEGER,
            nickname TEXT,
            PRIMARY KEY (room_code, user_id)
        )
        """)


# Шифрование текста
def encrypt(data: str | None) -> bytes:
    if data is None:
        return b""
    return fernet.encrypt(data.encode())


# Расшифровка текста
def decrypt(token: bytes | None) -> str:
    if not token:
        return ""
    return fernet.decrypt(token).decode()

#######
# Сохранение анкеты
def save_profile(user_id: int, text: str, media_id: str, media_type: str):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        INSERT OR REPLACE INTO profiles (user_id, text, media_id, media_type)
        VALUES (?, ?, ?, ?)
        """, (user_id, encrypt(text), encrypt(media_id), media_type))

# Загрузка анкеты
def load_profile(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT text, media_id, media_type FROM profiles WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if not row:
            return None
        return {
            "text": decrypt(row[0]),
            "media_id": decrypt(row[1]),
            "media_type": row[2]
        }

# Удаление анкеты
def delete_profile(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM profiles WHERE user_id = ?", (user_id,))
#############################################################################################
########################################################
# Глобальный профиль (ник)
def save_global_nick(user_id: int, nickname: str):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        INSERT OR REPLACE INTO global_profiles (user_id, nickname)
        VALUES (?, ?)
        """, (user_id, encrypt(nickname)))

def load_global_nick(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT nickname FROM global_profiles WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        return decrypt(row[0]) if row else None

########################################################################################################

# Привязка к комнате (ссылки, последнее местоположение, ник в комнате)
def save_room_link(user_id: int, room_code: str, last_active_room: str, nickname_in_room: str):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        INSERT OR REPLACE INTO room_links (user_id, room_code, last_active_room, nickname_in_room)
        VALUES (?, ?, ?, ?)
        """, (user_id, room_code, last_active_room, encrypt(nickname_in_room)))

def load_room_link(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT room_code, last_active_room, nickname_in_room FROM room_links WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if not row:
            return None
        return {
            "room_code": row[0],
            "last_active_room": row[1],
            "nickname_in_room": decrypt(row[2])
        }

def clear_room_link(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM room_links WHERE user_id = ?", (user_id,))


# Сохранение настроек комнаты
def save_room_settings(room_code: str, welcome_message: str, short_description: str, created: float,
                       moderator_id: int = None, is_open: bool = True, is_private: bool = True):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO room_settings (
                room_code,
                welcome_message,
                short_description,
                moderator_id,
                created,
                is_open,
                is_private
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            room_code,
            welcome_message,
            short_description,
            moderator_id,
            created,
            int(is_open),
            int(is_private)
        ))


# Загрузка настроек комнаты
def load_room_settings(room_code: str):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT welcome_message, short_description, created, moderator_id, is_open, is_private
            FROM room_settings
            WHERE room_code = ?
        """, (room_code,))
        row = c.fetchone()
        if not row:
            return None
        return {
            "welcome_message": row[0],
            "short_description": row[1],
            "created": row[2],
            "moderator_id": row[3],
            "is_open": bool(row[4]),
            "is_private": bool(row[5]),
        }


# Блокировка пользователя в комнате
def ban_user_in_room(user_id: int, room_code: str):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        INSERT OR REPLACE INTO banned_users (user_id, room_code)
        VALUES (?, ?)
        """, (user_id, room_code))

# Проверка бана
def is_user_banned(user_id: int, room_code: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM banned_users WHERE user_id = ? AND room_code = ?", (user_id, room_code))
        return c.fetchone() is not None

# Восстановление комнат из БД
def restore_rooms():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT room_code, welcome_message, short_description, created, moderator_id, is_open, is_private
            FROM room_settings
        """)
        for row in c.fetchall():
            room_code, welcome, desc, created, moderator_id, is_open, is_private = row
            group_rooms[room_code] = {
                "members": {},
                "moderator": moderator_id,
                "muted": set(),
                "banned": set(),
                "with_moderation": moderator_id is not None,
                "welcome": welcome,
                "is_open": bool(is_open),
                "is_private": bool(is_private),
                "description": desc,
                "created": created,
            }
            nickname_counter[room_code] = 0

            # Восстановление участников
            for uid, nick in restore_room_members(room_code):
                group_rooms[room_code]["members"][uid] = nick
                user_group[uid] = room_code
                custom_nicknames[uid] = nickname_counter


def save_room_member(room_code: str, user_id: int, nickname: str):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        INSERT OR REPLACE INTO room_members (room_code, user_id, nickname)
        VALUES (?, ?, ?)
        """, (room_code, user_id, nickname))

def remove_room_member(room_code: str, user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        DELETE FROM room_members WHERE room_code = ? AND user_id = ?
        """, (room_code, user_id))

def restore_room_members(room_code: str):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        SELECT user_id, nickname FROM room_members WHERE room_code = ?
        """, (room_code,))
        return c.fetchall()

#########################################################################

# Восстановление пользовательских состояний
def restore_user_state(user_id: int):
    link = load_room_link(user_id)
    if link:
        user_group[user_id] = link["room_code"]
    global_nick = load_global_nick(user_id)
    if global_nick:
        global_nicknames[user_id] = global_nick
        user_nicknames[user_id] = global_nick  # <-- ЭТО ВАЖНО

def restore_all_users():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM room_links")
        all_ids = c.fetchall()
        for (user_id,) in all_ids:
            try:
                restore_user_state(user_id)
            except Exception as e:
                print(f"[!] Не удалось восстановить пользователя {user_id}: {e}")
