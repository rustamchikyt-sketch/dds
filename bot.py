import asyncio
import html
import importlib.util
import logging
import os
import random
import sqlite3
import subprocess
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent


# ---------------- .env И АВТОУСТАНОВКА БИБЛИОТЕК ----------------
def load_env_file(path: Path) -> None:
    """Читает файл .env (строки вида KEY=value) в переменные окружения.
    Уже заданные в системе переменные имеют приоритет."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


MIN_AIOGRAM = (3, 27)   # с этой версии у кнопок есть цвет фона (style)


def aiogram_is_recent() -> bool:
    try:
        from importlib.metadata import version

        parts = version("aiogram").split(".")
        return (int(parts[0]), int(parts[1])) >= MIN_AIOGRAM
    except Exception:
        return False


def ensure_dependencies() -> None:
    """Если нужных библиотек нет - ставит их из requirements.txt."""
    needed = {"aiogram": "aiogram", "tzdata": "tzdata"}
    if all(importlib.util.find_spec(mod) for mod in needed) and aiogram_is_recent():
        return
    print("Не хватает библиотек или aiogram устарел, устанавливаю из requirements.txt ...")
    cmd = [sys.executable, "-m", "pip", "install", "-U", "-r", str(BASE_DIR / "requirements.txt")]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        # на случай системного Python с защитой от установки
        result = subprocess.run(cmd + ["--break-system-packages"])
    if result.returncode != 0:
        raise SystemExit(
            "Не удалось установить библиотеки автоматически. "
            "Выполни вручную: pip install -r requirements.txt"
        )
    importlib.invalidate_caches()


load_env_file(BASE_DIR / ".env")
ensure_dependencies()

from zoneinfo import ZoneInfo  # noqa: E402

from aiogram import Bot, Dispatcher, F  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.exceptions import TelegramBadRequest  # noqa: E402
from aiogram.filters import Command, CommandStart  # noqa: E402
from aiogram.types import BotCommand, CallbackQuery, FSInputFile, Message, User  # noqa: E402
from aiogram.utils.keyboard import InlineKeyboardBuilder  # noqa: E402

# ---------------- НАСТРОЙКИ ----------------
TOKEN = os.getenv("BOT_TOKEN")  # берётся из .env или из переменных окружения
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Kaliningrad"))  # по этому времени сбрасывается "день"
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "artem.db"))
IMAGES_DIR = BASE_DIR / "images"

START_SIZE = 10          # стартовый размер Артёма (см)
GROW_CHANCE = 0.70       # шанс, что Артём вырастет (иначе уменьшится)
GROW_RANGE = (1, 10)     # на сколько может вырасти
SHRINK_RANGE = (1, 5)    # на сколько может уменьшиться
MIN_SIZE = 1             # ниже этого не опускается
TOP_LIMIT = 20           # сколько игроков показывать в /stats

# Дуэли
DEFAULT_BET = 1          # ставка по умолчанию (см)
MAX_BET = 20             # максимальная ставка (см)
DUEL_ACCEPT_TIMEOUT = 120  # сколько секунд ждать, пока соперник примет вызов
RPS_TIMEOUT = 60         # сколько секунд на выбор в "камень-ножницы-бумага"
THROW_TIMEOUT = 60       # сколько секунд на нажатие кнопки «Бросить»
MAX_ROUNDS = 3           # сколько раз переигрывать при ничьей, потом ничья засчитывается
DICE_WAIT = 4.5          # пауза, пока крутится анимация кубика/мяча (сек)

# Папки с картинками (кладёшь свои файлы внутрь):
#   images/grow      - когда Артём вырос
#   images/shrink    - когда Артём уменьшился
#   images/same      - когда размер не изменился (если вдруг)
#   images/stats     - для команды /stats
#   images/wait      - когда игрок уже играл сегодня
#   images/duel      - когда началась дуэль
#   images/duel_win  - когда в дуэли определился победитель
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# -------------------------------------------

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()


# ---------------- БАЗА ДАННЫХ ----------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                chat_id      INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                name         TEXT    NOT NULL,
                size         INTEGER NOT NULL,
                last_play    TEXT,
                plays        INTEGER NOT NULL DEFAULT 0,
                duel_wins    INTEGER NOT NULL DEFAULT 0,
                duel_losses  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        # миграция для баз, созданных до появления дуэлей
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(players)")}
        for col in ("duel_wins", "duel_losses"):
            if col not in cols:
                conn.execute(f"ALTER TABLE players ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def user_name(u: User) -> str:
    return html.escape(f"@{u.username}" if u.username else u.full_name)


def ensure_player(conn: sqlite3.Connection, chat_id: int, user: User) -> sqlite3.Row:
    """Возвращает игрока; если его ещё нет в этом чате - создаёт со стартовым размером."""
    conn.execute(
        """
        INSERT INTO players (chat_id, user_id, name, size, last_play, plays)
        VALUES (?, ?, ?, ?, NULL, 0)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET name = excluded.name
        """,
        (chat_id, user.id, user_name(user), START_SIZE),
    )
    return conn.execute(
        "SELECT * FROM players WHERE chat_id = ? AND user_id = ?", (chat_id, user.id)
    ).fetchone()


# ---------------- КАРТИНКИ ----------------
def pick_image(kind: str) -> Path | None:
    folder = IMAGES_DIR / kind
    if not folder.is_dir():
        return None
    files = [f for f in folder.iterdir() if f.suffix.lower() in IMG_EXT]
    return random.choice(files) if files else None


async def reply_with_image(message: Message, text: str, kind: str, reply: bool = True) -> None:
    """Отправляет сообщение с картинкой из папки images/<kind>. Если картинок нет - просто текст.
    reply=True - ответом на сообщение, reply=False - обычным сообщением в чат."""
    send_text = message.reply if reply else message.answer
    send_photo = message.reply_photo if reply else message.answer_photo
    send_anim = message.reply_animation if reply else message.answer_animation

    img = pick_image(kind)
    if img is None:
        await send_text(text)
        return

    file = FSInputFile(img)
    long_text = len(text) > 1000  # лимит подписи к фото - 1024 символа
    caption = None if long_text else text

    if img.suffix.lower() == ".gif":
        await send_anim(file, caption=caption)
    else:
        await send_photo(file, caption=caption)
    if long_text:
        await message.answer(text)


async def safe_edit(msg: Message | None, text: str, kb=None) -> None:
    """Редактирует текст сообщения и кнопки (kb=None - кнопки убираются), не падает при ошибке."""
    if msg is None:
        return
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


# ---------------- ИГРОВАЯ ЛОГИКА ----------------
def roll_delta() -> int:
    if random.random() < GROW_CHANCE:
        return random.randint(*GROW_RANGE)
    return -random.randint(*SHRINK_RANGE)


def rank_in_chat(conn: sqlite3.Connection, chat_id: int, user_id: int) -> int:
    rows = conn.execute(
        "SELECT user_id FROM players WHERE chat_id = ? ORDER BY size DESC, plays DESC",
        (chat_id,),
    ).fetchall()
    for i, r in enumerate(rows, start=1):
        if r["user_id"] == user_id:
            return i
    return 0


# ---------------- ДУЭЛИ: ИГРЫ ----------------
# ключ -> (эмодзи, название, псевдонимы для команды; первый псевдоним показывается в справке)
GAMES: dict[str, dict] = {
    "dice": {"emoji": "🎲", "title": "Кубики", "aliases": ["кубик", "кубики", "куб", "dice"]},
    "darts": {"emoji": "🎯", "title": "Дартс", "aliases": ["дартс", "darts"]},
    "bowling": {"emoji": "🎳", "title": "Боулинг", "aliases": ["боулинг", "bowling"]},
    "basket": {"emoji": "🏀", "title": "Баскетбол", "aliases": ["баскетбол", "basket", "basketball"]},
    "football": {"emoji": "⚽", "title": "Футбол", "aliases": ["футбол", "football"]},
    "slots": {"emoji": "🎰", "title": "Слоты", "aliases": ["слоты", "слот", "slots", "казино"]},
    "rps": {"emoji": "✊", "title": "Камень-ножницы-бумага", "aliases": ["кнб", "rps", "камень"]},
}
RANDOM_ALIASES = {"random", "рандом", "случайная", "любая"}

SLOT_SYMBOLS = ["BAR", "🍇", "🍋", "7️⃣"]

RPS_CHOICES = {
    "rock": ("✊", "Камень"),
    "scissors": ("✌️", "Ножницы"),
    "paper": ("✋", "Бумага"),
}
RPS_STYLES = {"rock": "primary", "scissors": "danger", "paper": "success"}
RPS_BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}


def find_game(token: str) -> str | None:
    token = token.lower()
    if token in RANDOM_ALIASES:
        return "random"
    for key, info in GAMES.items():
        if token == key or token in info["aliases"]:
            return key
    return None


def game_title(game: str) -> str:
    if game == "random":
        return "🎰 Случайная игра"
    info = GAMES[game]
    return f"{info['emoji']} {info['title']}"


def duel_help_text() -> str:
    games = "\n".join(f"{info['emoji']} {info['title']}" for info in GAMES.values())
    return (
        "⚔️ <b>Дуэль</b>\n"
        "Напиши /duel - откроется меню с кнопками: выбери игру и ставку, "
        "потом «Бросить вызов». Принять может любой в чате.\n"
        "Чтобы вызвать конкретного человека, ответь командой /duel на его сообщение.\n"
        "Быстрый вариант: <code>/duel кубик 5</code> - игра и ставка выбраны заранее.\n\n"
        f"<b>Игры:</b>\n{games}\n\n"
        f"Ставка от 1 до {MAX_BET} см. Проигравший отдаёт сантиметры победителю."
    )


def slot_reels(value: int) -> list[int]:
    x = value - 1
    return [x % 4, (x // 4) % 4, (x // 16) % 4]


def slot_score(value: int) -> float:
    reels = slot_reels(value)
    counts = Counter(reels)
    symbol, top = counts.most_common(1)[0]
    if top == 3:
        return 10 + symbol          # три одинаковых, 7️⃣7️⃣7️⃣ - лучший
    if top == 2:
        return 3 + symbol / 10      # пара
    return 0


def dice_score(game: str, value: int) -> float:
    if game == "slots":
        return slot_score(value)
    if game == "basket":
        return 1 if value >= 4 else 0     # 4-5 - попал
    if game == "football":
        return 1 if value >= 3 else 0     # 3-5 - гол
    return value                           # кубик, дартс, боулинг: чем больше, тем лучше


def dice_describe(game: str, value: int) -> str:
    if game == "slots":
        return " ".join(SLOT_SYMBOLS[i] for i in slot_reels(value))
    if game == "basket":
        return "попал 🔥" if value >= 4 else "мимо"
    if game == "football":
        return "гол!" if value >= 3 else "мимо"
    if game == "darts":
        return "в яблочко 🎯" if value == 6 else str(value)
    if game == "bowling":
        return "страйк 💥" if value == 6 else str(value)
    return str(value)


# ---------------- ДУЭЛИ: СОСТОЯНИЕ ----------------
BET_CHOICES = [b for b in (1, 3, 5, 10, 20) if b <= MAX_BET]


@dataclass
class Duel:
    id: str
    chat_id: int
    challenger_id: int
    challenger_name: str
    opponent_id: int | None = None      # None - открытый вызов, принять может любой
    opponent_name: str | None = None
    game: str = "random"                # ключ игры или "random"
    bet: int = DEFAULT_BET
    msg: Message | None = None          # сообщение-меню (его же редактируем)
    state: str = "setup"                # setup -> pending -> running -> done / declined / expired / cancelled
    step: str = "game"                  # шаг меню: game -> bet -> confirm
    token: int = 0                      # для сброса таймера ожидания
    rps_open: bool = False
    picks: dict[int, str] = field(default_factory=dict)
    throw_open: bool = False
    throw_emoji: str = ""
    throws: dict[int, int] = field(default_factory=dict)
    thrown: set[int] = field(default_factory=set)
    round_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def opponent_label(self) -> str:
        return self.opponent_name or "любой желающий"


DUELS: dict[str, Duel] = {}
BUSY: set[tuple[int, int]] = set()      # (chat_id, user_id) - кто сейчас в дуэли
BG_TASKS: set[asyncio.Task] = set()


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    BG_TASKS.add(task)
    task.add_done_callback(BG_TASKS.discard)


def release(duel: Duel) -> None:
    BUSY.discard((duel.chat_id, duel.challenger_id))
    if duel.opponent_id is not None:
        BUSY.discard((duel.chat_id, duel.opponent_id))
    DUELS.pop(duel.id, None)


def apply_duel_result(chat_id: int, winner_id: int, loser_id: int, bet: int) -> tuple[int, int]:
    """Переносит ставку от проигравшего к победителю. Возвращает новые размеры (победитель, проигравший)."""
    with db() as conn:
        conn.execute(
            "UPDATE players SET size = size + ?, duel_wins = duel_wins + 1 "
            "WHERE chat_id = ? AND user_id = ?",
            (bet, chat_id, winner_id),
        )
        conn.execute(
            "UPDATE players SET size = MAX(?, size - ?), duel_losses = duel_losses + 1 "
            "WHERE chat_id = ? AND user_id = ?",
            (MIN_SIZE, bet, chat_id, loser_id),
        )
        w = conn.execute(
            "SELECT size FROM players WHERE chat_id = ? AND user_id = ?", (chat_id, winner_id)
        ).fetchone()["size"]
        l = conn.execute(
            "SELECT size FROM players WHERE chat_id = ? AND user_id = ?", (chat_id, loser_id)
        ).fetchone()["size"]
    return w, l


def get_size(chat_id: int, user_id: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT size FROM players WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ).fetchone()
    return row["size"] if row else START_SIZE


def max_bet_for(size: int) -> int:
    return max(0, size - MIN_SIZE)


# ---------------- ДУЭЛИ: ЭКРАНЫ С КНОПКАМИ ----------------
def bet_style(bet: int) -> str:
    """Цвет фона кнопки ставки: мелкая - зелёная, средняя - синяя, крупная - красная."""
    idx = BET_CHOICES.index(bet) if bet in BET_CHOICES else 0
    return ("success", "success", "primary", "danger", "danger")[min(idx, 4)]


def setup_text(duel: Duel) -> str:
    who = f"👤 Соперник: <b>{duel.opponent_label}</b>"
    if duel.step == "game":
        return (
            f"⚔️ <b>Дуэль · шаг 1 из 3</b>\n"
            f"{duel.challenger_name}, выбери игру:\n\n{who}"
        )
    if duel.step == "bet":
        return (
            f"⚔️ <b>Дуэль · шаг 2 из 3</b>\n"
            f"🎮 Игра: <b>{game_title(duel.game)}</b>\n\n"
            f"{duel.challenger_name}, теперь выбери ставку.\n"
            f"Чем больше ставка, тем больше сантиметров на кону 😈\n\n{who}"
        )
    return (
        f"⚔️ <b>Дуэль · шаг 3 из 3</b>\n"
        f"Всё готово, осталось бросить вызов!\n\n"
        f"🎮 Игра: <b>{game_title(duel.game)}</b>\n"
        f"💰 Ставка: <b>{duel.bet} см</b>\n{who}"
    )


def setup_kb(duel: Duel):
    kb = InlineKeyboardBuilder()
    if duel.step == "game":
        for key in [*GAMES, "random"]:
            kb.button(
                text=game_title(key),
                callback_data=f"duel:g:{duel.id}:{key}",
                style="success" if key == "random" else "primary",
            )
        kb.button(text="❌ Отмена", callback_data=f"duel:no:{duel.id}", style="danger")
        rows = [2] * ((len(GAMES) + 2) // 2)
        rows[-1] = (len(GAMES) + 1) - 2 * (len(rows) - 1)
        kb.adjust(*rows, 1)
    elif duel.step == "bet":
        for b in BET_CHOICES:
            mark = "✅ " if duel.bet == b else ""
            kb.button(text=f"{mark}💰 {b} см", callback_data=f"duel:b:{duel.id}:{b}", style=bet_style(b))
        kb.button(text="⬅️ Назад", callback_data=f"duel:back:{duel.id}")
        kb.button(text="❌ Отмена", callback_data=f"duel:no:{duel.id}", style="danger")
        n = len(BET_CHOICES)
        kb.adjust(*([3, n - 3] if n > 3 else [n]), 2)
    else:
        kb.button(text="⚔️ Бросить вызов!", callback_data=f"duel:go:{duel.id}", style="success")
        kb.button(text="⬅️ Изменить", callback_data=f"duel:back:{duel.id}")
        kb.button(text="❌ Отмена", callback_data=f"duel:no:{duel.id}", style="danger")
        kb.adjust(1, 2)
    return kb.as_markup()


def pending_text(duel: Duel) -> str:
    if duel.opponent_id is None:
        head = f"⚔️ {duel.challenger_name} бросает вызов всему чату!"
        tail = "Принять может любой, кроме вызывающего."
    else:
        head = f"⚔️ {duel.challenger_name} вызывает {duel.opponent_name} на дуэль!"
        tail = f"{duel.opponent_name}, принимаешь?"
    return (
        f"{head}\n"
        f"🎮 Игра: {game_title(duel.game)}\n"
        f"💰 Ставка: <b>{duel.bet} см</b>\n\n"
        f"{tail} Время на ответ: {DUEL_ACCEPT_TIMEOUT} сек."
    )


def pending_kb(duel: Duel):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять вызов", callback_data=f"duel:yes:{duel.id}", style="success")
    kb.button(
        text="❌ Отказаться" if duel.opponent_id else "🚫 Отменить",
        callback_data=f"duel:no:{duel.id}",
        style="danger",
    )
    kb.adjust(2)
    return kb.as_markup()


async def expire_duel(duel: Duel, token: int) -> None:
    await asyncio.sleep(DUEL_ACCEPT_TIMEOUT)
    if duel.token != token or duel.state not in ("setup", "pending"):
        return
    was_setup = duel.state == "setup"
    duel.state = "expired"
    release(duel)
    if was_setup:
        text = f"⌛ {duel.challenger_name} так и не настроил дуэль. Отменена."
    elif duel.opponent_id is None:
        text = f"⌛ Никто не принял вызов {duel.challenger_name}. Дуэль отменена."
    else:
        text = f"⌛ {duel.opponent_name} не ответил на вызов {duel.challenger_name}. Дуэль отменена."
    await safe_edit(duel.msg, text)


def arm_timer(duel: Duel) -> None:
    duel.token += 1
    spawn(expire_duel(duel, duel.token))


# ---------------- ДУЭЛИ: ХОД ИГРЫ ----------------
async def play_dice_game(duel: Duel, game: str) -> tuple[str | None, str]:
    """Игра на Telegram-эмодзи: каждый игрок сам жмёт кнопку «Бросить»."""
    emoji = GAMES[game]["emoji"]
    a, b = duel.challenger_name, duel.opponent_name
    line = ""
    for rnd in range(1, MAX_ROUNDS + 1):
        duel.throws.clear()
        duel.thrown.clear()
        duel.round_event.clear()
        duel.throw_emoji = emoji

        kb = InlineKeyboardBuilder()
        kb.button(text=f"{emoji} Бросить!", callback_data=f"duel:throw:{duel.id}", style="success")
        prompt = await duel.msg.answer(
            f"{emoji} Раунд {rnd}: {a} и {b}, жмите кнопку, чтобы бросить!\n"
            f"У вас {THROW_TIMEOUT} сек.",
            reply_markup=kb.as_markup(),
        )
        duel.throw_open = True
        try:
            await asyncio.wait_for(duel.round_event.wait(), THROW_TIMEOUT)
        except asyncio.TimeoutError:
            pass
        duel.throw_open = False
        try:
            await prompt.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

        va = duel.throws.get(duel.challenger_id)
        vb = duel.throws.get(duel.opponent_id)
        if va is None and vb is None:
            return None, "⌛ Оба не бросили вовремя"
        if va is None:
            return "b", f"⌛ {a} не бросил вовремя"
        if vb is None:
            return "a", f"⌛ {b} не бросил вовремя"

        await asyncio.sleep(DICE_WAIT)  # дать доиграть анимации
        line = f"{a}: {dice_describe(game, va)}\n{b}: {dice_describe(game, vb)}"
        sa, sb = dice_score(game, va), dice_score(game, vb)
        if sa > sb:
            return "a", line
        if sb > sa:
            return "b", line
        if rnd < MAX_ROUNDS:
            await duel.msg.answer(f"🤝 Ничья!\n{line}\n\nБросаем ещё раз!")
            await asyncio.sleep(1)
    return None, line


async def play_rps(duel: Duel) -> tuple[str | None, str]:
    a, b = duel.challenger_name, duel.opponent_name
    line = ""
    for rnd in range(1, MAX_ROUNDS + 1):
        duel.picks.clear()
        duel.round_event.clear()

        kb = InlineKeyboardBuilder()
        for key, (emo, title) in RPS_CHOICES.items():
            kb.button(text=f"{emo} {title}", callback_data=f"duel:rps:{duel.id}:{key}", style=RPS_STYLES[key])
        kb.adjust(3)
        prompt = await duel.msg.answer(
            f"✊✌️✋ Раунд {rnd}: {a} и {b}, делайте выбор!\n"
            f"Выбор скрыт, пока не выберут оба. У вас {RPS_TIMEOUT} сек.",
            reply_markup=kb.as_markup(),
        )
        duel.rps_open = True
        try:
            await asyncio.wait_for(duel.round_event.wait(), RPS_TIMEOUT)
        except asyncio.TimeoutError:
            pass
        duel.rps_open = False
        try:
            await prompt.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

        pa = duel.picks.get(duel.challenger_id)
        pb = duel.picks.get(duel.opponent_id)
        if pa is None and pb is None:
            return None, "⌛ Оба не сделали выбор вовремя"
        if pa is None:
            return "b", f"⌛ {a} не сделал выбор вовремя"
        if pb is None:
            return "a", f"⌛ {b} не сделал выбор вовремя"

        line = f"{a}: {RPS_CHOICES[pa][0]} {RPS_CHOICES[pa][1]}\n{b}: {RPS_CHOICES[pb][0]} {RPS_CHOICES[pb][1]}"
        if pa == pb:
            if rnd < MAX_ROUNDS:
                await duel.msg.answer(f"🤝 Одинаковый выбор!\n{line}\n\nЕщё раунд!")
                continue
            return None, line
        return ("a" if RPS_BEATS[pa] == pb else "b"), line
    return None, line


async def finish_duel(duel: Duel, winner: str | None, line: str, game: str) -> None:
    a, b = duel.challenger_name, duel.opponent_name
    if winner is None:
        await duel.msg.answer(f"🤝 <b>Ничья!</b>\n{line}\n\nРазмеры Артёмов не меняются.")
        return

    if winner == "a":
        win_id, win_name, lose_id, lose_name = duel.challenger_id, a, duel.opponent_id, b
    else:
        win_id, win_name, lose_id, lose_name = duel.opponent_id, b, duel.challenger_id, a

    w_size, l_size = apply_duel_result(duel.chat_id, win_id, lose_id, duel.bet)
    text = (
        f"🏆 <b>{win_name} побеждает в дуэли!</b>\n"
        f"{game_title(game)}\n{line}\n\n"
        f"📈 {win_name}: +{duel.bet} см → <b>{w_size} см</b>\n"
        f"📉 {lose_name}: -{duel.bet} см → <b>{l_size} см</b>"
    )
    await reply_with_image(duel.msg, text, "duel_win", reply=False)


async def run_duel(duel: Duel) -> None:
    try:
        game = duel.game if duel.game != "random" else random.choice(list(GAMES))
        a, b = duel.challenger_name, duel.opponent_name
        await reply_with_image(
            duel.msg,
            f"⚔️ <b>Дуэль началась!</b>\n{a} VS {b}\n{game_title(game)} · 💰 ставка {duel.bet} см",
            "duel",
            reply=False,
        )
        await asyncio.sleep(1)
        if game == "rps":
            winner, line = await play_rps(duel)
        else:
            winner, line = await play_dice_game(duel, game)
        await finish_duel(duel, winner, line, game)
    except Exception:
        logging.exception("Дуэль %s сломалась", duel.id)
        try:
            await duel.msg.answer("😵 Что-то пошло не так, дуэль отменена. Размеры не изменились.")
        except Exception:
            pass
    finally:
        duel.state = "done"
        release(duel)


# ---------------- КОМАНДЫ ----------------
@dp.message(CommandStart())
@dp.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Я бот, где вы растите <b>Артёма</b> и соревнуетесь в чате!\n\n"
        "/artem - вырастить Артёма (раз в день)\n"
        "/stats - статистика игроков этого чата\n"
        "/duel - дуэль с мини-играми (ответом на сообщение соперника)\n\n"
        "Добавь меня в группу и играйте. Артём может как вырасти, так и уменьшиться 😈\n\n"
        + duel_help_text()
    )


@dp.message(Command("artem", "grow"))
async def cmd_artem(message: Message) -> None:
    if message.from_user is None:
        return
    chat_id = message.chat.id
    user_id = message.from_user.id
    name = user_name(message.from_user)
    now_day = today()

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM players WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()

        if row is None:
            size = START_SIZE
            plays = 0
        else:
            size = row["size"]
            plays = row["plays"]
            if row["last_play"] == now_day:
                await reply_with_image(
                    message,
                    f"⏳ {name}, ты сегодня уже растил Артёма!\n"
                    f"Сейчас он <b>{size} см</b>. Приходи завтра.",
                    "wait",
                )
                return

        delta = roll_delta()
        new_size = max(MIN_SIZE, size + delta)
        real_delta = new_size - size

        conn.execute(
            """
            INSERT INTO players (chat_id, user_id, name, size, last_play, plays)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                name = excluded.name,
                size = excluded.size,
                last_play = excluded.last_play,
                plays = excluded.plays
            """,
            (chat_id, user_id, name, new_size, now_day, plays + 1),
        )
        place = rank_in_chat(conn, chat_id, user_id)

    if real_delta > 0:
        text = f"📈 {name}, твой Артём вырос на <b>{real_delta} см</b>!"
        kind = "grow"
    elif real_delta < 0:
        text = f"📉 {name}, твой Артём уменьшился на <b>{abs(real_delta)} см</b> 😢"
        kind = "shrink"
    else:
        text = f"😐 {name}, Артём не изменился."
        kind = "same"

    text += f"\nТеперь он <b>{new_size} см</b>.\n🏆 Место в чате: <b>{place}</b>"
    await reply_with_image(message, text, kind)


@dp.message(Command("stats", "top"))
async def cmd_stats(message: Message) -> None:
    chat_id = message.chat.id
    with db() as conn:
        rows = conn.execute(
            """
            SELECT name, size, plays, duel_wins, duel_losses FROM players
            WHERE chat_id = ?
            ORDER BY size DESC, plays DESC
            LIMIT ?
            """,
            (chat_id, TOP_LIMIT),
        ).fetchall()

    if not rows:
        await message.reply("Тут пока никто не растил Артёма. Начни с /artem!")
        return

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["📊 <b>Статистика Артёмов в этом чате:</b>\n"]
    for i, r in enumerate(rows, start=1):
        mark = medals.get(i, f"{i}.")
        lines.append(
            f"{mark} {r['name']} - <b>{r['size']} см</b> "
            f"(игр: {r['plays']}, дуэли: {r['duel_wins']}🏆/{r['duel_losses']}💀)"
        )

    await reply_with_image(message, "\n".join(lines), "stats")


@dp.message(Command("duel"))
async def cmd_duel(message: Message) -> None:
    me = message.from_user
    if me is None:
        return
    if message.chat.type == "private":
        await message.answer("⚔️ Дуэли работают только в групповых чатах. Добавь меня в группу!")
        return

    target = message.reply_to_message.from_user if message.reply_to_message else None
    if target is not None and (target.is_bot or target.id == me.id):
        await message.reply("С собой и с ботами драться нельзя 🙃")
        return

    # необязательные аргументы: /duel кубик 5 - заранее выбирают игру и ставку
    bet, game = DEFAULT_BET, "random"
    bet_given = game_given = False
    for tok in (message.text or "").split()[1:]:
        if tok.isdigit():
            bet, bet_given = int(tok), True
        else:
            found = find_game(tok)
            if found is None:
                await message.reply(f"Не знаю игру «{html.escape(tok)}».\n\n{duel_help_text()}")
                return
            game, game_given = found, True
    if not 1 <= bet <= MAX_BET:
        await message.reply(f"Ставка должна быть от 1 до {MAX_BET} см.")
        return

    chat_id = message.chat.id
    if (chat_id, me.id) in BUSY:
        await message.reply("Ты уже участвуешь в дуэли. Доиграй её сначала!")
        return
    if target is not None and (chat_id, target.id) in BUSY:
        await message.reply(f"{user_name(target)} сейчас в другой дуэли. Подожди.")
        return

    with db() as conn:
        ensure_player(conn, chat_id, me)
        if target is not None:
            ensure_player(conn, chat_id, target)

    duel = Duel(
        id=uuid.uuid4().hex[:8],
        chat_id=chat_id,
        challenger_id=me.id,
        challenger_name=user_name(me),
        opponent_id=target.id if target else None,
        opponent_name=user_name(target) if target else None,
        game=game,
        bet=bet,
        step="confirm" if (game_given and bet_given) else ("bet" if game_given else "game"),
    )
    DUELS[duel.id] = duel
    BUSY.add((chat_id, me.id))
    duel.msg = await message.answer(setup_text(duel), reply_markup=setup_kb(duel))
    arm_timer(duel)


@dp.callback_query(F.data.startswith("duel:"))
async def cb_duel(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) < 3:
        await cb.answer()
        return
    action, duel_id = parts[1], parts[2]
    duel = DUELS.get(duel_id)
    if duel is None:
        await cb.answer("Эта дуэль уже неактуальна.", show_alert=True)
        return
    uid = cb.from_user.id
    is_challenger = uid == duel.challenger_id

    # ---- настройка по шагам: игра -> ставка -> подтверждение (только вызывающий) ----
    if action in ("g", "b", "back"):
        if not is_challenger:
            await cb.answer("Это меню настраивает тот, кто вызывает на дуэль.", show_alert=True)
            return
        if duel.state != "setup":
            await cb.answer("Вызов уже отправлен.", show_alert=True)
            return
        if action == "back":
            duel.step = {"confirm": "bet", "bet": "game"}.get(duel.step, "game")
        else:
            value = parts[3] if len(parts) == 4 else ""
            if action == "g" and (value == "random" or value in GAMES):
                duel.game = value
                duel.step = "bet"
            elif action == "b" and value.isdigit() and int(value) in BET_CHOICES:
                duel.bet = int(value)
                duel.step = "confirm"
            else:
                await cb.answer()
                return
        await safe_edit(duel.msg, setup_text(duel), setup_kb(duel))
        await cb.answer()
        return

    # ---- отправка вызова ----
    if action == "go":
        if not is_challenger:
            await cb.answer("Это меню настраивает тот, кто вызывает на дуэль.", show_alert=True)
            return
        if duel.state != "setup":
            await cb.answer("Вызов уже отправлен.", show_alert=True)
            return
        if duel.step != "confirm":
            await cb.answer("Сначала выбери игру и ставку.", show_alert=True)
            return
        mine = max_bet_for(get_size(duel.chat_id, duel.challenger_id))
        if mine < duel.bet:
            await cb.answer(f"Твой Артём слишком мал для такой ставки (максимум {mine} см).", show_alert=True)
            return
        if duel.opponent_id is not None:
            if (duel.chat_id, duel.opponent_id) in BUSY:
                await cb.answer("Соперник сейчас в другой дуэли.", show_alert=True)
                return
            theirs = max_bet_for(get_size(duel.chat_id, duel.opponent_id))
            if theirs < duel.bet:
                await cb.answer(
                    f"Артём у {duel.opponent_name} слишком мал для такой ставки (максимум {theirs} см).",
                    show_alert=True,
                )
                return
            BUSY.add((duel.chat_id, duel.opponent_id))
        duel.state = "pending"
        await safe_edit(duel.msg, pending_text(duel), pending_kb(duel))
        await cb.answer("Вызов отправлен!")
        arm_timer(duel)
        return

    # ---- отмена / отказ ----
    if action == "no":
        if duel.state not in ("setup", "pending"):
            await cb.answer("Дуэль уже началась.", show_alert=True)
            return
        is_opponent = duel.opponent_id is not None and uid == duel.opponent_id
        if not (is_challenger or is_opponent):
            await cb.answer("Это не твоя дуэль.", show_alert=True)
            return
        duel.state = "declined"
        release(duel)
        if is_opponent:
            text = f"🏳️ {duel.opponent_name} отказался от дуэли с {duel.challenger_name}."
        else:
            text = f"🚫 {duel.challenger_name} отменил дуэль."
        await safe_edit(duel.msg, text)
        await cb.answer()
        return

    # ---- принять вызов ----
    if action == "yes":
        if duel.state != "pending":
            await cb.answer("Сейчас принять нельзя.", show_alert=True)
            return
        if is_challenger:
            await cb.answer("Нельзя принять собственный вызов 🙃", show_alert=True)
            return
        if duel.opponent_id is not None and uid != duel.opponent_id:
            await cb.answer("Принять вызов может только тот, кого вызвали.", show_alert=True)
            return
        if duel.opponent_id is None and (duel.chat_id, uid) in BUSY:
            await cb.answer("Ты уже участвуешь в другой дуэли.", show_alert=True)
            return

        with db() as conn:
            ensure_player(conn, duel.chat_id, cb.from_user)
        mine = max_bet_for(get_size(duel.chat_id, uid))
        if mine < duel.bet:
            await cb.answer(f"Твой Артём слишком мал для такой ставки (максимум {mine} см).", show_alert=True)
            return
        if max_bet_for(get_size(duel.chat_id, duel.challenger_id)) < duel.bet:
            duel.state = "cancelled"
            release(duel)
            await safe_edit(duel.msg, "🚫 Артём вызывающего стал слишком мал для этой ставки. Дуэль отменена.")
            await cb.answer()
            return

        if duel.opponent_id is None:
            duel.opponent_id = uid
            BUSY.add((duel.chat_id, uid))
        duel.opponent_name = user_name(cb.from_user)
        duel.state = "running"
        await safe_edit(
            duel.msg,
            f"⚔️ {duel.opponent_name} принял вызов {duel.challenger_name}! "
            f"{game_title(duel.game)} · 💰 {duel.bet} см",
        )
        await cb.answer("Дуэль начинается!")
        spawn(run_duel(duel))
        return

    # ---- бросок кубика / мяча ----
    if action == "throw":
        if uid not in (duel.challenger_id, duel.opponent_id):
            await cb.answer("Это не твоя дуэль.", show_alert=True)
            return
        if duel.state != "running" or not duel.throw_open:
            await cb.answer("Сейчас бросать не нужно.", show_alert=True)
            return
        if uid in duel.thrown:
            await cb.answer("Ты уже бросил, ждём соперника.", show_alert=True)
            return
        duel.thrown.add(uid)
        await cb.answer()
        await duel.msg.answer(f"{duel.throw_emoji} {user_name(cb.from_user)} бросает!")
        sent = await duel.msg.answer_dice(emoji=duel.throw_emoji)
        duel.throws[uid] = sent.dice.value
        if len(duel.throws) == 2:
            duel.round_event.set()
        return

    # ---- камень-ножницы-бумага ----
    if action == "rps" and len(parts) == 4:
        choice = parts[3]
        if uid not in (duel.challenger_id, duel.opponent_id):
            await cb.answer("Это не твоя дуэль.", show_alert=True)
            return
        if duel.state != "running" or not duel.rps_open or choice not in RPS_CHOICES:
            await cb.answer("Сейчас выбор не нужен.", show_alert=True)
            return
        if uid in duel.picks:
            await cb.answer("Ты уже сделал выбор, ждём соперника.", show_alert=True)
            return
        duel.picks[uid] = choice
        await cb.answer(f"Ты выбрал: {RPS_CHOICES[choice][0]} {RPS_CHOICES[choice][1]}")
        if len(duel.picks) == 2:
            duel.round_event.set()
        return

    await cb.answer()


BOT_COMMANDS = [
    BotCommand(command="artem", description="🍆 Вырастить Артёма (раз в день)"),
    BotCommand(command="stats", description="📊 Статистика игроков чата"),
    BotCommand(command="duel", description="⚔️ Дуэль (ответом на сообщение соперника)"),
    BotCommand(command="help", description="❓ Справка и список игр"),
]


async def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "Не найден токен бота. Создай файл .env рядом с bot.py (скопируй .env.example) "
            "и впиши туда: BOT_TOKEN=твой_токен"
        )
    init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # список команд, который Telegram показывает при вводе "/" (для всех чатов)
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception:
        logging.exception("Не удалось установить список команд")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
