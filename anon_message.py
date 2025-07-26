import telegram
from state import group_rooms, global_nicknames, user_nicknames, nickname_counter, user_profiles, user_group
from database import save_profile, load_profile, delete_profile as db_delete_profile, init_db, save_room_settings, load_room_settings, ban_user_in_room, save_global_nick, save_room_link, load_room_link, restore_all_users, restore_rooms, clear_room_link, remove_room_member, save_room_member
import re
from telegram import Update, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
from telegram.constants import ParseMode
import os
from dotenv import load_dotenv
import time
import random
import string
from datetime import time as dt_time
from datetime import datetime, timedelta

# Загрузка переменных окружения
load_dotenv("db.env")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Очередь на поиск и пары чатов
waiting_users = set()  # список chat_id
chat_pairs = {}  # {chat_id: partner_chat_id}
user_interests: dict[int, set[str]] = {}
last_seen = {}

# Приватные ссылки: {link_code: chat_id}
private_links = {}
link_owners = {}  # {chat_id: link_code}

# Спам-контроль
SPAM_LIMIT = 15
SPAM_INTERVAL = 10  # секунд
BLOCK_DURATION = 600  # секунд
message_timestamps = {}
blocked_users = {}

# Альбомы: {chat_id: {"media": [...], "timeout": Job, "caption": str}}
pending_albums = {}
ALBUM_TIMEOUT = 10  # секунд
bot_username = "Djbsyshsb_bot"

# Групповые комнаты
user_states = {}
custom_nicknames = {}  # {chat_id: nickname}
GROUP_LIFETIME = 86400  # 24 часа

def is_active_hours():
    now = time.localtime()
    return dt_time(9, 0) <= dt_time(now.tm_hour, now.tm_min) <= dt_time(23, 0)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    now = datetime.now()
    online_users = sum(1 for t in last_seen.values() if now - t < timedelta(minutes=10))
    searching = len(waiting_users)

    await message.reply_text(
        f"👥 Онлайн: {online_users}\n🔎 В поиске: {searching}"
    )

def is_moderator(chat_id, context):
    code = user_group.get(chat_id)
    if not code:
        return False
    room = group_rooms.get(code)
    if not room:
        return False
    return room.get("moderator") == chat_id


def get_stats_text():
    now = datetime.now()
    online_users = sum(1 for t in last_seen.values() if now - t < timedelta(minutes=10))
    searching = len(waiting_users)
    return f"👥 Онлайн: {online_users}\n🔎 В поиске: {searching}\n"

#################################Комната
async def create_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    with_mod = 'mod' in args
    chat_id = update.effective_chat.id

    message = (
        update.message
        or (update.callback_query.message if update.callback_query else None)
    )

    # Проверка активности
    if is_user_busy(chat_id, context):
        if message:
            await message.reply_text("Сначала завершите текущую активность (/stop).")
        return

    if context.user_data.get("profile_creating"):
        if message:
            await message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        if message:
            await message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    # Генерация комнаты
    code = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    created = time.time()

    group_rooms[code] = {
        "members": {},
        "moderator": chat_id if with_mod else None,
        "muted": set(),
        "banned": set(),
        "with_moderation": with_mod,
        "welcome": None,
        "is_open": True,
        "is_private": True,
        "description": "",
        "created": created,
    }
    nickname_counter[code] = 0

    # === Сохраняем в БД ===
    from database import save_room_settings
    save_room_settings(
        room_code=code,
        welcome_message=None,
        short_description="",
        created=created,
        moderator_id=chat_id if with_mod else None,
        is_open=True,
        is_private=True
    )

    link = f"https://t.me/{bot_username}?start=group_{code}"

    if message:
        await message.reply_text(
            f"🔗 Ссылка на {'модерируемую ' if with_mod else ''}групповую комнату:\n{link}"
        )
    await send_main_menu(update, context)


async def make_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    message = update.message or update.callback_query.message

    code = user_group.get(chat_id)
    if not code or code not in group_rooms:
        await update.message.reply_text("Вы не находитесь в комнате.")
        return

    room = group_rooms[code]
    room["is_private"] = True
    await update.message.reply_text("Комната теперь скрыта из общего списка.")


async def make_public(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    message = update.message or update.callback_query.message

    code = user_group.get(chat_id)
    if not code or code not in group_rooms:
        await update.message.reply_text("Вы не находитесь в комнате.")
        return

    room = group_rooms[code]
    room["is_private"] = False
    await update.message.reply_text("Комната теперь видна в общем списке.")

    if not room or room.get("moderator") != chat_id:
        await message.reply_text("У вас нет прав модератора.")
        return


async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    if not message:
        return

    chat_id = message.chat_id

    code = user_group.get(chat_id)
    if not code:
        await message.reply_text("Вы не находитесь в комнате.")
        return

    room = group_rooms.get(code)
    if not room or room.get("moderator") != chat_id:
        await message.reply_text("Только модератор может делать объявления.")
        return

    if not context.args:
        await message.reply_text("Введите текст объявления, например:\n/announce Сегодня обсуждаем философию.")
        return

    announcement = "📢 Объявление:\n" + " ".join(context.args)

    for uid in room["members"]:
        if uid == chat_id:
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=announcement)
        except:
            pass

    await message.reply_text("Объявление отправлено всем участникам комнаты.")


async def set_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    room_code = user_group.get(chat_id)

    if not room_code:
        await update.message.reply_text("Вы не находитесь в комнате.")
        return

    room = group_rooms.get(room_code)
    if not room or room.get("moderator") != chat_id:
        await update.message.reply_text("Только модератор может менять описание.")
        return

    if not context.args:
        await update.message.reply_text("Введите описание: /set_description [текст]")
        return

    description = " ".join(context.args).strip()
    room["description"] = description[:100]  # ограничим длину

    # Сохраняем и welcome и description
    welcome = room.get("welcome") or ""
    save_room_settings(room_code, welcome, room["description"])

    await update.message.reply_text("Описание комнаты обновлено.")


async def list_active_rooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверка активности пользователя
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message
    if not message:
        return

    chat_id = message.chat_id

    if not group_rooms:
        await message.reply_text("Пока нет активных комнат.")
        return

    now = time.time()
    text = ""
    count = 0

    for code, room in group_rooms.items():
        if room.get("is_private"):
            continue
        if now - room["created"] > GROUP_LIFETIME:
            continue
        if not room["members"]:
            continue

        members_count = len(room["members"])
        is_mod = room.get("with_moderation", False)
        mod_tag = "👮" if is_mod else "👥"
        link = f"https://t.me/{bot_username}?start=group_{code}"
        room_info = load_room_settings(code)
        description = room_info.get("short_description", "") if room_info else ""

        text += f"{mod_tag} [Комната {count+1}]({link}) — {members_count} чел.\n"
        if description:
            text += f"_Описание_: {description}\n"
        text += "\n"

        count += 1

    if count == 0:
        await message.reply_text("Сейчас нет доступных комнат.")
        return

    await message.reply_text("📃 Список активных комнат:\n\n" + text, parse_mode=ParseMode.MARKDOWN)

async def join_group(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    if is_user_busy(chat_id, context):
        await update.message.reply_text("Сначала завершите текущую активность (/stop).")
        return

    room = group_rooms.get(code)
    if not room:
        await update.message.reply_text("Комната не существует.")
        return

    if not room.get("is_open", True):
        await update.message.reply_text("Эта комната сейчас закрыта для новых участников.")
        return

    welcome = room.get("welcome")
    if welcome:
        await safe_send(context.bot, chat_id, "send_message", text=welcome)

    if chat_id in room['banned']:
        await update.message.reply_text("Вы были забанены в этой комнате.")
        return

    if chat_id in room['members']:
        await update.message.reply_text("Вы уже в этой комнате.")
        return

    await leave_group(chat_id, context)

    # === Выбор ника ===
    nickname = user_nicknames.get(chat_id)

    if not nickname:
        # Пытаемся восстановить глобальный ник из базы
        from database import load_global_nick  # импорт в случае, если не вверху
        nickname = load_global_nick(chat_id)
        if nickname:
            user_nicknames[chat_id] = nickname
            global_nicknames[chat_id] = nickname

    if not nickname:
        link = load_room_link(chat_id)
        if link:
            nickname = link["nickname_in_room"]

    if not nickname:
        if code not in nickname_counter:
            nickname_counter[code] = 0
        nickname_counter[code] += 1
        nickname = f"Аноним №{nickname_counter[code]}"

    # === Добавление участника ===
    room['members'][chat_id] = nickname
    user_group[chat_id] = code
    custom_nicknames[chat_id] = nickname

    save_room_member(code, chat_id, nickname)
    save_room_link(chat_id, code, code, nickname)

    # Сообщение модератору
    if room.get("moderator") == chat_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🛡 Вы — модератор этой комнаты.\n"
                "Вы можете удалять участников, писать объявления и управлять приватностью.\n"
                "Доступные команды: /mod - узнать команды.\n"
                "Более детально в /help (читать 'модератор комнаты')"
            )
        )

    # Оповещаем других
    for uid in list(room['members']):
        if uid != chat_id:
            await safe_send(context.bot, uid, "send_message", text=f"{nickname} присоединился.")

    names = "\n".join(room["members"].values())
    mod_note = "\n(С модерацией)" if room.get("with_moderation") else ""
    await update.message.reply_text(f"Вы присоединились к группе.{mod_note}\nСписок участников:\n{names}")

    # Отправка анкеты другим
    profile = user_profiles.get(chat_id)
    if profile:
        text = profile.get('text', '')
        media_id = profile.get('media_id')
        media_type = profile.get('media_type')
        for uid in list(room['members']):
            if uid != chat_id:
                if media_id:
                    if media_type == 'photo':
                        await safe_send(context.bot, uid, "send_photo", photo=media_id,
                                        caption=f"Анкета участника:\n{text}")
                    elif media_type == 'video':
                        await safe_send(context.bot, uid, "send_video", video=media_id,
                                        caption=f"Анкета участника:\n{text}")
                elif text:
                    await safe_send(context.bot, uid, "send_message", text=f"Анкета участника:\n{text}")



async def mod_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message
    chat_id = message.chat_id
    code = user_group.get(chat_id)
    room = group_rooms.get(code)
    if not room or room.get("moderator") != chat_id:
        await update.message.reply_text("У вас нет прав модератора.")
        return
    await update.message.reply_text(
        "/kick <ник> — выгнать\n"
        "/mute <ник> — замутить\n"
        "/unmute <ник> — размутить\n"
        "/ban <ник> — забанить навсегда\n"
        "/delete_group — удалить комнату"
    )


async def kick_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, action="kick")


async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, action="mute")


async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, action="unmute")


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, action="ban")


async def delete_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    code = user_group.get(chat_id)
    room = group_rooms.get(code)
    if not room or room.get("moderator") != chat_id:
        await update.message.reply_text("У вас нет прав модератора.")
        return
    for uid in list(room['members']):
        await safe_send(context.bot, uid, "send_message", text="Комната была удалена модератором.")
        user_group.pop(uid, None)
        custom_nicknames.pop(uid, None)
    group_rooms.pop(code, None)
    await update.message.reply_text("Комната удалена.")


async def set_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    code = user_group.get(chat_id)
    if not code or code not in group_rooms:
        await update.message.reply_text("Вы не находитесь в комнате.")
        return

    room = group_rooms[code]
    if room.get("moderator") != chat_id:
        await update.message.reply_text("Только модератор может установить приветствие.")
        return

    if not context.args:
        await update.message.reply_text("Укажите приветственное сообщение. Пример:\n/set_welcome Добро пожаловать...")
        return

    welcome_text = update.message.text.split(maxsplit=1)[1].strip()
    room["welcome"] = welcome_text
    await update.message.reply_text("Приветственное сообщение обновлено.")
    save_room_settings(code, room["welcome"])

async def preview_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    code = user_group.get(chat_id)
    if not code or code not in group_rooms:
        await update.message.reply_text("Вы не в комнате.")
        return

    room = group_rooms[code]
    if chat_id != room.get("moderator"):
        await update.message.reply_text("Только модератор может просматривать приветствие.")
        return

    welcome = room.get("welcome")
    if welcome:
        await update.message.reply_text(f"Текущее приветствие:\n{welcome}")
    else:
        await update.message.reply_text("Приветственное сообщение не установлено.")


async def close_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    code = user_group.get(chat_id)
    if not code or code not in group_rooms:
        await message.reply_text("Вы не находитесь в комнате.")
        return

    room = group_rooms[code]
    if room.get("moderator") != chat_id:
        await message.reply_text("Только модератор может закрывать комнату.")
        return

    room["is_open"] = False
    await message.reply_text("Комната закрыта. Новые участники не смогут присоединиться.")


async def open_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    code = user_group.get(chat_id)
    if not code or code not in group_rooms:
        await message.reply_text("Вы не находитесь в комнате.")
        return

    room = group_rooms[code]
    if room.get("moderator") != chat_id:
        await message.reply_text("Только модератор может открыть комнату.")
        return

    room["is_open"] = True
    await message.reply_text("Комната снова открыта для присоединения.")


async def mod_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action):
    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    if context.user_data.get("profile_creating"):
        await message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    code = user_group.get(chat_id)
    room = group_rooms.get(code)

    if not room or room.get("moderator") != chat_id:
        await message.reply_text("У вас нет прав модератора.")
        return

    if not context.args:
        await message.reply_text("Укажите ник пользователя.")
        return

    target_nick = " ".join(context.args).strip().lower()
    target_id = None

    for uid, nick in room["members"].items():
        if nick.strip().lower() == target_nick:
            target_id = uid
            break

    if not target_id:
        await message.reply_text("Пользователь с таким ником не найден.")
        return

    if target_id == chat_id:
        await message.reply_text("Вы не можете применить это действие к себе.")
        return

    if action == "kick":
        room['members'].pop(target_id, None)
        user_group.pop(target_id, None)
        custom_nicknames.pop(target_id, None)
        await safe_send(context.bot, target_id, "send_message", text="Вы были удалены из комнаты модератором.")

    elif action == "mute":
        room.setdefault('muted', set())
        room['muted'].add(target_id)
        await safe_send(context.bot, target_id, "send_message", text="Вы были заглушены модератором.")

    elif action == "unmute":
        room.setdefault('muted', set())
        room['muted'].discard(target_id)
        await safe_send(context.bot, target_id, "send_message", text="Вы были размучены модератором.")

    elif action == "ban":
        room['banned'].add(target_id)
        ban_user_in_room(target_id, code)
        room['members'].pop(target_id, None)
        user_group.pop(target_id, None)
        custom_nicknames.pop(target_id, None)
        await safe_send(context.bot, target_id, "send_message", text="Вы были забанены в этой комнате модератором.")

    await update.message.reply_text(f"{action.capitalize()} успешно выполнено.")


async def change_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    if chat_id not in user_group:
        await message.reply_text("Вы не находитесь в групповой комнате.")
        return

    code = user_group[chat_id]
    room = group_rooms.get(code)
    if not room:
        await message.reply_text("Комната не найдена.")
        return

    new_nick = " ".join(context.args).strip()

    if len(new_nick) > 50:
        return await update.message.reply_text("Ник слишком длинный.")

    if not new_nick:
        await message.reply_text("Ник не может быть пустым.")
        return

    if new_nick in room['members'].values():
        await message.reply_text("Такой ник уже используется в комнате.")
        return

    old_nick = room['members'].get(chat_id, "Аноним")
    room['members'][chat_id] = new_nick

    await message.reply_text(f"Ваш ник изменён на: {new_nick}")
    for uid in room['members']:
        if uid != chat_id:
            await safe_send(context.bot, uid, "send_message", text=f"{old_nick} сменил ник на {new_nick}")

    updated_names = "\n".join([
        f"{nick}" for uid, nick in room["members"].items()
    ])


async def set_global_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    new_nick = " ".join(context.args).strip()

    if not new_nick:
        await update.message.reply_text("Ник не может быть пустым.")
        return

    if new_nick.lower().startswith("аноним"):
        await safe_send(context.bot, chat_id, "send_message", text="Нельзя использовать системный формат ника.")
        return

    if any(nick == new_nick for nick in user_nicknames.values()):
        await update.message.reply_text("Такой ник уже используется.")
        return

    user_nicknames[chat_id] = new_nick
    save_global_nick(chat_id, new_nick)  # ✅ Сохраняем ник в БД
    await update.message.reply_text(f"Ваш глобальный ник установлен: {new_nick}")


async def safe_send(bot, chat_id, method, **kwargs):
    try:
        return await getattr(bot, method)(chat_id=chat_id, **kwargs)
    except telegram.error.Forbidden:
        # пользователь заблокировал бота
        code = user_group.get(chat_id)
        if code and code in group_rooms:
            room = group_rooms[code]
            nickname = room["members"].pop(chat_id, "Аноним")
            user_group.pop(chat_id, None)
            custom_nicknames.pop(chat_id, None)

            for uid in list(room["members"]):
                await safe_send(bot, uid, "send_message", text=f"{nickname} покинул комнату.")

async def group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    message = update.message
    chat_id = message.chat_id
    last_seen[chat_id] = datetime.now()

    if chat_id not in user_group:
        return

    code = user_group[chat_id]
    room = group_rooms.get(code)
    if not room:
        return

    if ban_user_in_room(chat_id, code):
        await update.message.reply_text("Вы были забанены в этой комнате.")
        return

    if chat_id in room.get("muted", set()):
        await safe_send(context.bot, chat_id, "send_message", text="Вы замучены и не можете отправлять сообщения.")
        return

    nickname = room['members'].get(chat_id, "Аноним")
    suffix = f"\nОтправил {nickname}"

    # === Обработка личного сообщения по нику ===
    if message.text:
        msg = message.text.strip()

        # Поддержка @"ник с пробелами" или @ник
        match = re.match(r'^@(?:"([^"]+)"|(\S+))\s+(.+)', msg)
        if match:
            target_nick = match.group(1) or match.group(2)
            private_msg = match.group(3)

            target_id = None
            for uid, name in room['members'].items():
                if name.lower() == target_nick.lower():
                    target_id = uid
                    break

            if target_id and target_id != chat_id:
                await safe_send(context.bot, target_id, "send_message",
                                text=f"[Приват] {nickname}: {private_msg}")
                await safe_send(context.bot, chat_id, "send_message",
                                text=f"[Приват → {room['members'][target_id]}]: {private_msg}")
                return

    # --- Обработка альбома ---
    if message.media_group_id:
        if chat_id not in pending_albums:
            pending_albums[chat_id] = {
                "media": [],
                "timeout": None,
                "caption": None,
                "group_code": code,
                "sender_nickname": nickname
            }

        album = pending_albums[chat_id]
        if message.caption:
            album["caption"] = message.caption

        if message.photo:
            album["media"].append(InputMediaPhoto(message.photo[-1].file_id))
        elif message.video:
            album["media"].append(InputMediaVideo(message.video.file_id))
        elif message.document:
            album["media"].append(InputMediaDocument(message.document.file_id))

        jobs = context.job_queue.get_jobs_by_name(f"album_{chat_id}")
        for job in jobs:
            job.schedule_removal()

        album["timeout"] = context.job_queue.run_once(
            send_album_group, ALBUM_TIMEOUT, chat_id=chat_id, name=f"album_{chat_id}", data=code
        )
        return

    # --- Универсальная подпись ---
    caption_or_text = message.text or message.caption

    if caption_or_text:
        text_msg = f"{nickname}: {caption_or_text}"
        for uid in room['members']:
            if uid != chat_id:
                await safe_send(context.bot, uid, "send_message", text=text_msg)

    if message.photo:
        for uid in room['members']:
            if uid != chat_id:
                await safe_send(
                    context.bot, uid, "send_photo",
                    photo=message.photo[-1].file_id,
                    caption=f"{message.caption or ''}{suffix}"
                )

    elif message.video:
        for uid in room['members']:
            if uid != chat_id:
                await safe_send(
                    context.bot, uid, "send_video",
                    video=message.video.file_id,
                    caption=f"{message.caption or ''}{suffix}"
                )

    elif message.document:
        for uid in room['members']:
            if uid != chat_id:
                await safe_send(
                    context.bot, uid, "send_document",
                    document=message.document.file_id,
                    caption=f"{message.caption or ''}{suffix}"
                )

    elif message.voice:
        for uid in room['members']:
            if uid != chat_id:
                await safe_send(context.bot, uid, "send_voice", voice=message.voice.file_id)
                await safe_send(context.bot, uid, "send_message", text=suffix)

    elif message.sticker:
        for uid in room['members']:
            if uid != chat_id:
                await safe_send(context.bot, uid, "send_sticker", sticker=message.sticker.file_id)
                await safe_send(context.bot, uid, "send_message", text=suffix)

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    if chat_id not in user_group:
        await update.message.reply_text("Вы не находитесь в групповой комнате.")
        return

    code = user_group[chat_id]
    room = group_rooms.get(code)
    if not room:
        await update.message.reply_text("Комната не найдена.")
        return

    members = room.get("members", {})
    if not members:
        await message.reply_text("В этой комнате сейчас больше никого нет.")
        return

    names = "\n".join(members.values())
    await message.reply_text(f"Список участников:\n{names}")

async def send_album_group(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.chat_id
    code = job.data
    if chat_id not in pending_albums:
        return
    album_data = pending_albums.pop(chat_id)
    media = album_data["media"]
    nickname = album_data.get("sender_nickname", "Аноним")
    user_caption = album_data["caption"]
    nickname = album_data.get("sender_nickname", "Аноним")
    if user_caption:
        caption = f"{user_caption}\nОтправил {nickname}"
    else:
        caption = f"Отправил {nickname}"

    room = group_rooms.get(code)
    if not media or not room:
        return
    if caption:
        first = media[0]
        if isinstance(first, InputMediaPhoto):
            media[0] = InputMediaPhoto(media=first.media, caption=caption)
        elif isinstance(first, InputMediaVideo):
            media[0] = InputMediaVideo(media=first.media, caption=caption)
        elif isinstance(first, InputMediaDocument):
            media[0] = InputMediaDocument(media=first.media, caption=caption)
    for uid in list(room['members']):
        if uid != chat_id:
            await safe_send(context.bot, uid, "send_media_group", media=media)


#################################################################################

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message

    await message.reply_text(
        "🆘 <b>Инструкция</b>\n\n"
        "<b>Для всех пользователей:</b>\n"
        "/start – запустить бота\n"
        "/search – найти случайного собеседника\n"
        "/interests - написать свои интересы, чтобы найти такого же\n"
        "/stats - статистика кто сейчас в потске и активных пользователей\n"
        "/stop – завершить диалог\n"
        "/profile – создать анкету\n"
        "/my_profile – посмотреть свою анкету\n"
        "/delete_profile - удалить мою анкету\n"
        "/create_link – создать приватную ссылку для диалога\n\n"
        "<b>Группы:</b>\n"
        "/create_group – создать комнату\n"
        "/stop – покинуть комнату\n"
        "/nick – сменить свой ник\n"
        "/set_global_nick - глобальный ник\n"
        "/view_profile (ник) - посмотреть анкету другого пользователя\n"
        "/list_users – список участников\n\n"
        "<b>Модератор комнаты:</b>\n"
        "/ban, /mute, /unmute – управление участниками\n"
        "/set_welcome – установить приветствие\n"
        "/preview_welcome - посмотреть приветсвие\n"
        "/open_group, /close_group – закрыть или открыть комнату\n"
        "/make_private - не показывать комнату в списке активныъ комнат (автоматически при создании)\n"
        "/make_public - показывать в списке активных комнат\n"
        "/mod - команды модератора\n"
        "/kick – удалить пользователя из комнаты\n"
        "/announce - сделать обьявление для всех участников (модератор скрыт)\n\n"
        "https://github.com/UrPerv/-.git - ссылка на исходный код.\n"
        "https://t.me/Anonimnoe_Soobchenie_bot - анонимная обратная связь с разработчиком и админом.",
        parse_mode="HTML"
    )


async def anti_spam(update: Update) -> bool:
    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    now = time.time()

    if chat_id in blocked_users and now < blocked_users[chat_id]:
        try:
            await update.message.reply_text("Вы временно заблокированы за спам. Подождите немного.")
        except:
            pass
        return False
    elif chat_id in blocked_users:
        del blocked_users[chat_id]

    timestamps = message_timestamps.get(chat_id, [])
    timestamps = [t for t in timestamps if now - t < SPAM_INTERVAL]
    timestamps.append(now)
    message_timestamps[chat_id] = timestamps

    if len(timestamps) > SPAM_LIMIT:
        blocked_users[chat_id] = now + BLOCK_DURATION
        try:
            await update.message.reply_text("Вы были временно заблокированы за спам.")
        except:
            pass
        return False

    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    if context.args:
        arg = context.args[0]
        if arg.startswith("join_"):
            code = arg[5:]
            await join_via_code(update, context, code)
            return
        elif arg.startswith("group_"):
            code = arg[6:]
            await join_group(update, context, code)
            return

    keyboard = [
        [
            InlineKeyboardButton("🔍 Найти собеседника", callback_data="search"),
            InlineKeyboardButton("👥 Создать комнату", callback_data="create_group")
        ],
        [
            InlineKeyboardButton("📝 Анкета", callback_data="profile"),
            InlineKeyboardButton("📄 Моя анкета", callback_data="my_profile")
        ],
        [
            InlineKeyboardButton("🔗 Приватная ссылка", callback_data="create_link"),
            InlineKeyboardButton("🆘 Помощь", callback_data="help")
        ],
        [
            InlineKeyboardButton("📃 Активные комнаты", callback_data="list_rooms")
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await delete_previous_menu(update, context)

    text = get_stats_text() + "\nПривет! 👋 Добро пожаловать в приватную сеть.\nВыбери действие:"
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=reply_markup
    )
    context.user_data["menu_msg_id"] = msg.message_id

# Обработчик кнопок
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id

    # Проверка состояния анкеты или поиска
    if context.user_data.get("profile_creating"):
        await query.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await query.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    action = query.data

    # Выбор типа комнаты
    if action == "create_group":
        keyboard = [
            [
                InlineKeyboardButton("👥 Комната без модерации", callback_data="create_group_nomod"),
                InlineKeyboardButton("👮 Комната с модерацией", callback_data="create_group_mod")
            ],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]
        ]
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
        except:
            pass
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="Выберите тип комнаты:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data["menu_msg_id"] = msg.message_id
        return

    # Назад в главное меню
    if action == "back_to_menu":
        await send_main_menu(update, context)
        return

    # Создание комнаты
    if action in ["create_group_nomod", "create_group_mod"]:
        context.args = [] if action == "create_group_nomod" else ["mod"]
        await create_group(update, context)
        return

    # Прочие действия
    if action == "search":
        await search(update, context)
    elif action == "profile":
        await profile(update, context)
    elif action == "my_profile":
        await my_profile(update, context)
    elif action == "create_link":
        await create_link(update, context)
    elif action == "help":
        await help_command(update, context)
    elif action == "list_rooms":
        await list_active_rooms(update, context)



async def delete_previous_menu(update, context):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    chat_id = update.effective_chat.id
    msg_id = context.user_data.get("menu_msg_id")
    if msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except:
            pass


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    chat_id = update.effective_chat.id if isinstance(update, Update) else update

    keyboard = [
        [
            InlineKeyboardButton("🔍 Найти собеседника", callback_data="search"),
            InlineKeyboardButton("👥 Создать комнату", callback_data="create_group")
        ],
        [
            InlineKeyboardButton("📝 Анкета", callback_data="profile"),
            InlineKeyboardButton("📄 Моя анкета", callback_data="my_profile")
        ],
        [
            InlineKeyboardButton("🔗 Приватная ссылка", callback_data="create_link"),
            InlineKeyboardButton("🆘 Помощь", callback_data="help")
        ],
        [
            InlineKeyboardButton("📃 Активные комнаты", callback_data="list_rooms")
        ],

    ]

    try:
        if "menu_msg_id" in context.user_data:
            await context.bot.delete_message(chat_id=chat_id, message_id=context.user_data["menu_msg_id"])
    except:
        pass

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text = get_stats_text() + "\nПривет! 👋 Добро пожаловать в анонимный чат-бот.\nВыбери действие:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data["menu_msg_id"] = msg.message_id


############################### Анкета
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    if context.user_data.get("searching"):
        await message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    if is_user_busy(chat_id, context):
        await message.reply_text("Сначала завершите текущую активность (/stop).")
        return

    if is_user_busy(chat_id, context):
        await message.reply_text("Сначала завершите текущую активность (/stop).")
        return

    try:
        await message.delete()
    except:
        pass

    context.user_data['profile_creating'] = True  # ✅ правильный флаг

    await context.bot.send_message(
        chat_id=chat_id,
        text="Отправь сюда свою анкету (текст + фото/видео при желании).\n\nЧтобы выйти — закончите анкету"
    )


async def handle_profile_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = update.effective_user.id

    # Обрабатываем только если пользователь в режиме создания анкеты
    if not context.user_data.get("profile_creating"):
        return

    if message.text == "/stop":
        context.user_data.pop("profile_creating", None)
        sent = await stop(update, context)
        if not sent:
            await message.reply_text("Вы вышли из режима ввода анкеты.")
        return

    text = message.text or message.caption or ""
    media_id = None
    media_type = None

    if message.photo:
        media_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.video:
        media_id = message.video.file_id
        media_type = "video"

    # Сохраняем в SQLite
    save_profile(user_id, text.strip(), media_id, media_type)

    context.user_data.pop("profile_creating", None)
    await message.reply_text("Анкета сохранена!\n/delete_profile - удалить анкету")
    await send_main_menu(update, context)


async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id  # ← ВОТ ОН
    query = update.callback_query

    profile = load_profile(user_id)

    if profile:
        text = profile.get("text", "")
        media_id = profile.get("media_id")
        media_type = profile.get("media_type")

        if media_id:
            if media_type == "photo":
                await context.bot.send_photo(chat_id=chat_id, photo=media_id, caption=text)
            elif media_type == "video":
                await context.bot.send_video(chat_id=chat_id, video=media_id, caption=text)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text)
    else:
        await context.bot.send_message(chat_id=chat_id, text="У вас нет анкеты.")

    await send_main_menu(update, context)


async def delete_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    if context.user_data.get("searching"):
        await message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    if is_user_busy(chat_id, context):
        await message.reply_text("Сначала завершите текущую активность (/stop).")
        return

    # Проверка: есть ли анкета в БД
    profile = load_profile(chat_id)
    if profile:
        db_delete_profile(chat_id)
        await message.reply_text("Ваша анкета была удалена.")
    else:
        await message.reply_text("У вас нет анкеты для удаления.")

async def view_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    if context.user_data.get("profile_creating"):
        await message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    if chat_id not in user_group:
        await message.reply_text("Вы не находитесь в групповой комнате.")
        return

    if not context.args:
        await message.reply_text("Укажите ник пользователя. Пример:\n/view_profile Аноним №2")
        return

    code = user_group.get(chat_id)
    room = group_rooms.get(code)

    if not room:
        await message.reply_text("Комната не найдена.")
        return

    target_nick = " ".join(context.args).strip().lower()
    target_id = None

    for uid, nick in room["members"].items():
        if nick.lower() == target_nick:
            target_id = uid
            break

    if not target_id:
        await message.reply_text("Пользователь с таким ником не найден.")
        return

    profile = user_profiles.get(target_id) or load_profile(target_id)
    if not profile:
        await message.reply_text("У этого пользователя нет анкеты.")
        return

    text = profile.get("text", "")
    media_id = profile.get("media_id")
    media_type = profile.get("media_type")

    if media_id:
        if media_type == 'photo':
            await context.bot.send_photo(chat_id=chat_id, photo=media_id, caption=f"\n{text}")
        elif media_type == 'video':
            await context.bot.send_video(chat_id=chat_id, video=media_id, caption=f"\n{text}")
    else:
        await message.reply_text(f"\n{text}")

#####################################################################################################

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    online_users = sum(1 for t in last_seen.values() if now - t < timedelta(minutes=10))
    searching = len(waiting_users)

    message = update.message or update.callback_query.message
    if not message:
        return

    chat_id = message.chat_id

    # Проверка занятости
    if is_user_busy(chat_id, context):
        await message.reply_text("Сначала завершите текущую активность (/stop).")
        return

    # Уже ищет?
    if context.user_data.get("searching"):
        await message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    # Удаляем старую ссылку, если была
    if chat_id in link_owners:
        code = link_owners.pop(chat_id)
        private_links.pop(code, None)

    # Устанавливаем флаг поиска
    context.user_data["searching"] = True

    try:
        await message.delete()
    except:
        pass

    # Удаляем себя из очереди ДО цикла
    waiting_users.discard(chat_id)

    # === СЛУЧАЙНЫЙ ПОИСК ===
    for uid in list(waiting_users):  # обязательно копия
        if not is_user_busy(uid, context):
            waiting_users.remove(uid)
            await start_chat(chat_id, uid, context)
            return

    # Если никого не нашли — добавляем себя обратно в очередь
    waiting_users.add(chat_id)

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            get_stats_text() +
            "\nОжидаем второго пользователя...\n\n/stop - Остановить поиск\n/stats - статистика"
        )
    )



async def set_interests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    await message.reply_text("Введите свои интересы через запятую (например: книги, игры, искусство):")
    context.user_data["awaiting_interests"] = True

async def create_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    if chat_id in user_group:
        await message.reply_text("Вы находитесь в групповой комнате. Сначала используйте /stop, чтобы выйти.")
        return

    try:
        msg_id = context.user_data.get("menu_msg_id")
        if msg_id:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except:
        pass

    if chat_id in chat_pairs:
        await context.bot.send_message(chat_id=chat_id, text="Вы уже в чате. Завершите текущий диалог с помощью /stop.")
        return

    if chat_id in link_owners:
        code = link_owners[chat_id]
    else:
        code = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        private_links[code] = chat_id
        link_owners[chat_id] = code

    link = f"https://t.me/{bot_username}?start=join_{code}"

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"Отправьте эту ссылку тому, с кем хотите поговорить:\n{link}\n\nСсылка одноразовая. Чтобы отменить — /cancel_link")
    )

    await send_main_menu(chat_id, context)


async def join_via_code(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    if chat_id in chat_pairs:
        await update.message.reply_text("Вы уже в чате. Сначала используйте /stop.")
        return

    if code not in private_links:
        await update.message.reply_text("Ссылка недействительна или уже использована.")
        return

    partner_id = private_links.pop(code)
    link_owners.pop(partner_id, None)

    if partner_id == chat_id:
        await update.message.reply_text("Нельзя подключиться к самому себе.")
        return

    if is_user_busy(partner_id, context):
        await update.message.reply_text("Пользователь сейчас занят (в группе, диалоге, поиске или создаёт анкету).")
        return

    if is_user_busy(chat_id, context):
        await update.message.reply_text("Вы сейчас заняты. Завершите текущую активность командой /stop.")
        return

    # Прерываем создание анкеты, если оно было
    context.user_data.pop("profile_creating", None)
    await start_chat(chat_id, partner_id, context)


async def cancel_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    if chat_id in link_owners:
        code = link_owners.pop(chat_id)
        private_links.pop(code, None)
        await update.message.reply_text("Ссылка отменена.")
    else:
        await update.message.reply_text("У вас нет активной ссылки.")


async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await update.message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    if is_user_busy(chat_id, context):
        await update.message.reply_text("Сначала завершите текущую активность (/stop).")
        return

    if chat_id in chat_pairs:
        await update.message.reply_text("Вы уже в чате. Сначала используйте /stop.")
        return

    if not context.args:
        await update.message.reply_text("Укажите ссылку после команды. Пример: /join 123abcXYZ")
        return

    code = context.args[0]
    if code not in private_links:
        await update.message.reply_text("Ссылка недействительна или уже использована.")
        return

    partner_id = private_links.pop(code)
    link_owners.pop(partner_id, None)

    if partner_id == chat_id:
        await update.message.reply_text("Нельзя подключиться к самому себе.")
        return

    await start_chat(chat_id, partner_id, context)


async def start_chat(chat_id, partner_id, context):
    chat_pairs[chat_id] = partner_id
    chat_pairs[partner_id] = chat_id

    await safe_send(context.bot, chat_id, "send_message", text="Собеседник найден! Вы можете начинать анонимный чат.")
    await safe_send(context.bot, partner_id, "send_message",
                    text="Собеседник найден! Вы можете начинать анонимный чат.")

    for sender_id, receiver_id in [(chat_id, partner_id), (partner_id, chat_id)]:
        profile = load_profile(sender_id)

        if profile:
            text = profile.get('text', '')
            media_id = profile.get('media_id')
            media_type = profile.get('media_type')

            if media_id:
                if media_type == 'photo':
                    await safe_send(context.bot, receiver_id, "send_photo", photo=media_id,
                                    caption=f"Анкета собеседника:\n{text}")
                elif media_type == 'video':
                    await safe_send(context.bot, receiver_id, "send_video", video=media_id,
                                    caption=f"Анкета собеседника:\n{text}")
            elif text:
                await safe_send(context.bot, receiver_id, "send_message", text=f"Анкета собеседника:\n{text}")


async def next_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    chat_id = message.chat_id

    if context.user_data.get("profile_creating"):
        await update.message.reply_text("Сейчас вы создаёте анкету. Завершите или отмените с помощью /stop.")
        return

    if context.user_data.get("searching"):
        await message.reply_text("Сейчас идёт поиск. Чтобы остановить — используйте /stop.")
        return

    if chat_id in user_group:
        await update.message.reply_text("Вы находитесь в групповой комнате. Используйте /stop, чтобы выйти.")
        return
    await stop(update, context)
    await search(update, context)


async def leave_group(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    code = user_group.pop(chat_id, None)
    if not code:
        return

    room = group_rooms.get(code)
    if not room:
        return

    nickname = custom_nicknames.pop(chat_id, "Аноним")
    room['members'].pop(chat_id, None)

    # Удаляем из базы данных
    remove_room_member(code, chat_id)
    clear_room_link(chat_id)

    # Уведомляем других участников
    for uid in list(room['members']):
        await safe_send(context.bot, uid, "send_message", text=f"{nickname} покинул комнату.")



async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message or update.callback_query.message
    if not message:
        return False
    chat_id = message.chat_id

    if chat_id in chat_pairs:
        partner_id = chat_pairs.pop(chat_id)
        chat_pairs.pop(partner_id, None)
        await safe_send(context.bot, partner_id, "send_message", text="Собеседник завершил диалог.")
        await update.message.reply_text("Вы завершили диалог.")
        await send_main_menu(update, context)
        return True


    elif chat_id in waiting_users:

        if chat_id in waiting_users:
            waiting_users.remove(chat_id)

        context.user_data.pop("searching", None)

        await message.reply_text("Вы вышли из очереди поиска.")
        await send_main_menu(update, context)
        return True


    elif chat_id in user_group:
        await leave_group(chat_id, context)
        await message.reply_text("Вы вышли из групповой комнаты.")
        await send_main_menu(update, context)
        return True

    return False  # ничего не нашли


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    chat_id = message.chat_id
    last_seen[chat_id] = datetime.now()

    # === Спам фильтр ===
    if not await anti_spam(update):
        return

    # === Анкета ===
    if context.user_data.get('awaiting') == 'profile_text':
        text = message.caption or message.text or ''
        media_id = None
        media_type = None

        if message.photo:
            media_id = message.photo[-1].file_id
            media_type = 'photo'
        elif message.video:
            media_id = message.video.file_id
            media_type = 'video'

        user_profiles[chat_id] = {
            'text': text.strip(),
            'media_id': media_id,
            'media_type': media_type
        }

        context.user_data['awaiting'] = None
        await message.reply_text("Анкета сохранена! Теперь можете использовать /search.")
        return

    # === Если пользователь в групповой комнате ===
    if chat_id in user_group:
        await group_message(update, context)
        return

    # === Если не в диалоге ===
    if chat_id not in chat_pairs:
        await message.reply_text("Вы не находитесь в диалоге. Используйте /search или /create_link.")
        return

    # === Передача сообщения партнёру ===
    partner_id = chat_pairs[chat_id]

    # === Обработка альбома ===
    if message.media_group_id:
        if chat_id not in pending_albums:
            pending_albums[chat_id] = {"media": [], "timeout": None, "caption": None}

        media_group = pending_albums[chat_id]["media"]
        if message.caption:
            pending_albums[chat_id]["caption"] = message.caption

        if message.photo:
            media_group.append(InputMediaPhoto(message.photo[-1].file_id))
        elif message.video:
            media_group.append(InputMediaVideo(message.video.file_id))

        jobs = context.job_queue.get_jobs_by_name(f"album_{chat_id}")
        for job in jobs:
            job.schedule_removal()

        pending_albums[chat_id]["timeout"] = context.job_queue.run_once(
            send_album, ALBUM_TIMEOUT, chat_id=chat_id, name=f"album_{chat_id}", data=partner_id
        )
        return

    # === Остальные типы сообщений ===
    if message.text:
        await safe_send(context.bot, partner_id, "send_message", text=message.text)
    elif message.sticker:
        await safe_send(context.bot, partner_id, "send_sticker", sticker=message.sticker.file_id)
    elif message.photo:
        await safe_send(context.bot, partner_id, "send_photo", photo=message.photo[-1].file_id, caption=message.caption)
    elif message.voice:
        await safe_send(context.bot, partner_id, "send_voice", voice=message.voice.file_id)
    elif message.video:
        await safe_send(context.bot, partner_id, "send_video", video=message.video.file_id, caption=message.caption)
    elif message.document:
        await safe_send(context.bot, partner_id, "send_document", document=message.document.file_id, caption=message.caption)



async def send_album(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.chat_id
    partner_id = job.data

    if chat_id not in pending_albums:
        return

    album_data = pending_albums.pop(chat_id)
    media = album_data["media"]
    caption = album_data["caption"] or f"Отправил {user_nicknames}"

    if not media:
        return

    if caption:
        first = media[0]
        if isinstance(first, InputMediaPhoto):
            media[0] = InputMediaPhoto(media=first.media, caption=caption)
        elif isinstance(first, InputMediaVideo):
            media[0] = InputMediaVideo(media=first.media, caption=caption)
    await context.bot.send_media_group(chat_id=partner_id, media=media)


def is_user_busy(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return (
            context.user_data.get("profile_creating")
            or context.user_data.get("searching")
            or chat_id in chat_pairs
            or chat_id in user_group
    )


async def universal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    chat_id = update.message.chat_id

    if context.user_data.get("awaiting_interests"):
        message = update.message
        if message.text:
            text = message.text.lower()
            interests = set(i.strip() for i in text.split(",") if i.strip())
            user_interests[chat_id] = interests
            context.user_data.pop("awaiting_interests")
            await message.reply_text("Интересы сохранены.")
        else:
            await message.reply_text("Пожалуйста, введите интересы текстом.")
        return

    if not await anti_spam(update):
        return

    if context.user_data.get("profile_creating"):
        await handle_profile_text(update, context)
        return

    if chat_id in user_group:
        await group_message(update, context)
        return

    if chat_id in chat_pairs:
        await handle_message(update, context)
        return

    await update.message.reply_text("Вы не находитесь в чате. Используйте /search или /create_link.")

if __name__ == "__main__":
    init_db()
    restore_rooms()
    restore_all_users()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # === КОМАНДЫ (ПОРЯДОК ВАЖЕН) ===
    # Основные
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("interests", set_interests))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("stop", stop))  # до MessageHandler!
    app.add_handler(CommandHandler("next", next_chat))
    app.add_handler(CommandHandler("stats", stats))

    # Профили
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("my_profile", my_profile))
    app.add_handler(CommandHandler("delete_profile", delete_profile))
    app.add_handler(CommandHandler("view_profile", view_profile))
    app.add_handler(CommandHandler("create_link", create_link))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("cancel_link", cancel_link))

    # Группы и ники
    app.add_handler(CommandHandler("create_group", create_group))
    app.add_handler(CommandHandler("rooms", list_active_rooms))
    app.add_handler(CommandHandler("list_users", list_users))
    app.add_handler(CommandHandler("nick", change_nickname))
    app.add_handler(CommandHandler("set_global_nick", set_global_nick))
    app.add_handler(CommandHandler("description", set_description))

    # Модераторские
    app.add_handler(CommandHandler("mod", mod_commands))
    app.add_handler(CommandHandler("kick", kick_user))
    app.add_handler(CommandHandler("mute", mute_user))
    app.add_handler(CommandHandler("unmute", unmute_user))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("delete_group", delete_group))
    app.add_handler(CommandHandler("set_welcome", set_welcome))
    app.add_handler(CommandHandler("preview_welcome", preview_welcome))
    app.add_handler(CommandHandler("close_group", close_group))
    app.add_handler(CommandHandler("open_group", open_group))
    app.add_handler(CommandHandler("make_private", make_private))
    app.add_handler(CommandHandler("make_public", make_public))
    app.add_handler(CommandHandler("announce", announce))

    # Обработчик кнопок
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, universal_handler))

    # === АНКЕТА — только если пользователь сейчас вводит ===
    app.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.VIDEO,
        handle_profile_text
    ))

    # === ВСЕ ОСТАЛЬНЫЕ СООБЩЕНИЯ ===


    print("Приватная сеть запущена!")
    app.run_polling()  #


