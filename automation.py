"""
Скрипт для запуска по расписанию (GitHub Actions cron) — без постоянно
работающего сервера/бота.

Каждый запуск:
1. Забирает новые Telegram-апдейты (нажатия кнопок, ответы модератора)
   через getUpdates и обрабатывает их (публикует ответы в Threads).
2. Проверяет новые комментарии под постами в Threads → генерирует черновик
   ответа → шлёт карточку с кнопками в Telegram.
3. Если подошло время (раз в 2-3 часа) — генерирует и публикует новый пост
   в Threads на тему канала.

Всё состояние (последний обработанный update_id, обработанные реплаи,
время следующего автопоста) хранится в state.json рядом со скриптом —
GitHub Actions коммитит его обратно в репозиторий после каждого запуска,
так что между запусками ничего не теряется.

Секреты (TELEGRAM_BOT_TOKEN, THREADS_ACCESS_TOKEN, THREADS_USER_ID,
TELEGRAM_ADMIN_CHAT_ID, ANTHROPIC_API_KEY) хранятся в GitHub → Settings →
Secrets and variables → Actions — бесплатно и не попадают в код.
"""

import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["TELEGRAM_ADMIN_CHAT_ID"])
THREADS_ACCESS_TOKEN = os.environ["THREADS_ACCESS_TOKEN"]
THREADS_USER_ID = os.environ["THREADS_USER_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# Автопост раз в 2-3 часа (случайный разброс, чтобы не выглядело роботизированно)
AUTOPOST_MIN_HOURS = 2
AUTOPOST_MAX_HOURS = 3

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
THREADS_GRAPH_URL = "https://graph.threads.net/v1.0"
# Бесплатный тариф Google AI Studio (Gemini) — щедрая дневная квота,
# более чем достаточно для пары постов в день + ответов на комментарии.
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)
STATE_PATH = Path(__file__).parent / "state.json"


def ask_gemini(prompt: str) -> str:
    resp = httpx.post(
        GEMINI_URL,
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()

CHANNEL_CONTEXT = """
Ты ведёшь Threads-аккаунт YouTube-канала "Пыльные Хроники" — исторический
контент про великих князей 13-14 века (Гедимин, Ольгерд, Витовт, Миндовг и
т.д.). Тон: живой, увлечённый, без канцелярита.
"""


# ---------------------------- Состояние ----------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {
        "last_telegram_update_id": 0,
        "processed_reply_ids": [],
        "pending_drafts": {},  # telegram_message_id -> {reply_id, draft}
        "next_autopost_at": None,
    }


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ---------------------------- Telegram ----------------------------

def tg_send_message(text: str, keyboard: list | None = None) -> int:
    payload = {"chat_id": ADMIN_CHAT_ID, "text": text}
    if keyboard:
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    resp = httpx.post(f"{TELEGRAM_API}/sendMessage", data=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()["result"]["message_id"]


def tg_edit_message(message_id: int, text: str) -> None:
    httpx.post(
        f"{TELEGRAM_API}/editMessageText",
        data={"chat_id": ADMIN_CHAT_ID, "message_id": message_id, "text": text},
        timeout=30,
    )


def tg_answer_callback(callback_query_id: str) -> None:
    httpx.post(
        f"{TELEGRAM_API}/answerCallbackQuery",
        data={"callback_query_id": callback_query_id},
        timeout=30,
    )


def tg_get_updates(offset: int) -> list[dict]:
    resp = httpx.get(
        f"{TELEGRAM_API}/getUpdates",
        params={"offset": offset, "timeout": 0},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["result"]


# ---------------------------- Threads API ----------------------------

def fetch_own_posts() -> list[dict]:
    resp = httpx.get(
        f"{THREADS_GRAPH_URL}/{THREADS_USER_ID}/threads",
        params={
            "fields": "id,text,permalink,timestamp",
            "access_token": THREADS_ACCESS_TOKEN,
            "limit": 25,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def fetch_replies(post_id: str) -> list[dict]:
    resp = httpx.get(
        f"{THREADS_GRAPH_URL}/{post_id}/replies",
        params={
            "fields": "id,text,username,is_reply_owned_by_me",
            "access_token": THREADS_ACCESS_TOKEN,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def publish_threads_content(text: str, reply_to_id: str | None = None) -> None:
    data = {
        "media_type": "TEXT",
        "text": text,
        "access_token": THREADS_ACCESS_TOKEN,
    }
    if reply_to_id:
        data["reply_to_id"] = reply_to_id
    create = httpx.post(
        f"{THREADS_GRAPH_URL}/{THREADS_USER_ID}/threads", data=data, timeout=30
    )
    create.raise_for_status()
    creation_id = create.json()["id"]

    publish = httpx.post(
        f"{THREADS_GRAPH_URL}/{THREADS_USER_ID}/threads_publish",
        data={"creation_id": creation_id, "access_token": THREADS_ACCESS_TOKEN},
        timeout=30,
    )
    publish.raise_for_status()


# ---------------------------- ИИ-генерация ----------------------------

def generate_reply_draft(post_text: str, author: str, comment: str) -> str:
    prompt = f"""{CHANNEL_CONTEXT}
Пост: \"\"\"{post_text}\"\"\"
Комментарий от @{author}: \"\"\"{comment}\"\"\"

Напиши короткий живой ответ (1-3 предложения) и заверши его встречным
вопросом, который вовлекает в обсуждение. Разный вопрос каждый раз, без
шаблонов. Только текст ответа, без кавычек."""
    return ask_gemini(prompt)


def generate_new_post() -> str:
    prompt = f"""{CHANNEL_CONTEXT}
Напиши оригинальный пост для Threads (2-5 предложений) на тему великих
князей 13-14 века — интересный факт, провокационный вопрос или мини-история,
который вызовет обсуждение и лайки. Заверши вопросом к читателям. Только
текст поста."""
    return ask_gemini(prompt)


# ---------------------------- Основная логика ----------------------------

def process_telegram_updates(state: dict) -> None:
    updates = tg_get_updates(state["last_telegram_update_id"] + 1)
    for update in updates:
        state["last_telegram_update_id"] = update["update_id"]

        if "callback_query" in update:
            cq = update["callback_query"]
            tg_answer_callback(cq["id"])
            action, reply_id = cq["data"].split(":", 1)
            msg_id = str(cq["message"]["message_id"])
            pending = state["pending_drafts"].get(msg_id)
            if not pending:
                continue

            if action == "send":
                publish_threads_content(pending["draft"], reply_to_id=reply_id)
                tg_edit_message(cq["message"]["message_id"], cq["message"]["text"] + "\n\n✅ Отправлено.")
                state["pending_drafts"].pop(msg_id, None)
            elif action == "skip":
                tg_edit_message(cq["message"]["message_id"], cq["message"]["text"] + "\n\n⏭ Пропущено.")
                state["pending_drafts"].pop(msg_id, None)
            elif action == "edit":
                pending["awaiting_custom_text"] = True
                tg_edit_message(
                    cq["message"]["message_id"],
                    cq["message"]["text"] + "\n\n✏️ Пришли следующим сообщением свой текст ответа.",
                )

        elif "message" in update and "text" in update["message"]:
            text = update["message"]["text"]
            for msg_id, pending in list(state["pending_drafts"].items()):
                if pending.get("awaiting_custom_text"):
                    publish_threads_content(text, reply_to_id=pending["reply_id"])
                    tg_send_message("✅ Отправлено с твоим текстом.")
                    state["pending_drafts"].pop(msg_id, None)
                    break


def process_new_threads_comments(state: dict) -> None:
    for post in fetch_own_posts():
        for reply in fetch_replies(post["id"]):
            if reply.get("is_reply_owned_by_me"):
                continue
            if reply["id"] in state["processed_reply_ids"]:
                continue

            draft = generate_reply_draft(
                post.get("text", ""), reply.get("username", "?"), reply.get("text", "")
            )
            text = (
                f"💬 @{reply.get('username')}: «{reply.get('text')}»\n\n"
                f"🤖 Черновик:\n{draft}"
            )
            keyboard = [[
                {"text": "✅ Отправить", "callback_data": f"send:{reply['id']}"},
                {"text": "✏️ Свой текст", "callback_data": f"edit:{reply['id']}"},
                {"text": "⏭ Пропустить", "callback_data": f"skip:{reply['id']}"},
            ]]
            msg_id = tg_send_message(text, keyboard)
            state["pending_drafts"][str(msg_id)] = {"reply_id": reply["id"], "draft": draft}
            state["processed_reply_ids"].append(reply["id"])

    # не даём списку расти бесконечно
    state["processed_reply_ids"] = state["processed_reply_ids"][-500:]


def maybe_autopost(state: dict) -> None:
    now = datetime.now(timezone.utc)
    next_at = state.get("next_autopost_at")

    if next_at is None:
        # первый запуск — планируем следующий автопост, ничего не постим сейчас
        delay_hours = random.uniform(AUTOPOST_MIN_HOURS, AUTOPOST_MAX_HOURS)
        state["next_autopost_at"] = (now + timedelta(hours=delay_hours)).isoformat()
        return

    if now >= datetime.fromisoformat(next_at):
        post_text = generate_new_post()
        publish_threads_content(post_text)
        tg_send_message(f"📤 Автопост опубликован в Threads:\n\n{post_text}")
        delay_hours = random.uniform(AUTOPOST_MIN_HOURS, AUTOPOST_MAX_HOURS)
        state["next_autopost_at"] = (now + timedelta(hours=delay_hours)).isoformat()


def main() -> None:
    state = load_state()
    process_telegram_updates(state)
    process_new_threads_comments(state)
    maybe_autopost(state)
    save_state(state)


if __name__ == "__main__":
    main()
