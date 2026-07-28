"""
Общий слой чтения постов из мессенджера МАКС + цикл кросспостинга.
=================================================================
Используется обоими публикаторами: max_to_ok_crosspost.py (Одноклассники)
и max_to_vk_crosspost.py (ВКонтакте).

Переменные окружения:
  MAX_BOT_TOKEN       - токен бота в МАКС
  MAX_CHAT_ID         - ID канала/чата МАКС, откуда читаем посты
  LOG_LEVEL           - INFO (прод) или DEBUG (пишет RAW-тела сообщений)
  STATE_DIR           - каталог для файлов состояния
  BACKFILL_ON_EMPTY   - 1 = залить старые посты при пустом состоянии
  BACKFILL_COUNT      - сколько последних постов залить при бэкфилле (по умолчанию 3)
  EBOI                - 1/true = принудительный бэкфилл при каждом старте,
                        даже если состояние не пустое (ручной перенос старых постов)
"""

import json
import logging
import os
import time
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import requests

MAX_API = "https://platform-api.max.ru"

log = logging.getLogger(__name__)

STATE_DIR = os.environ.get("STATE_DIR", ".")


def env_flag(name: str) -> bool:
    """Булев флаг из окружения: 1/true/yes/on (имя проверяется в обоих регистрах)."""
    value = os.environ.get(name) or os.environ.get(name.lower()) or ""
    return value.strip().lower() in ("1", "true", "yes", "on")


def multi_target() -> bool:
    """CROSSPOST_TARGET=all — обе площадки в одном процессе, каждая в своём потоке."""
    return os.environ.get("CROSSPOST_TARGET", "").strip().lower() in ("all", "both")


_stream_handler_added = False


def setup_logging(log_file: str, target: str = "") -> None:
    """Настроить логирование в stdout и в файл внутри STATE_DIR.

    Вызывается каждым публикатором при импорте. В режиме CROSSPOST_TARGET=all
    оба публикатора живут в одном процессе, поэтому:
      - обработчик stdout добавляется только один раз (иначе строки задвоятся);
      - в формат попадает имя потока (ok/vk), чтобы строки площадок различались;
      - файловый обработчик пишет только строки своего потока, так что
        crosspost.log и crosspost_vk.log остаются раздельными.
    """
    global _stream_handler_added
    os.makedirs(STATE_DIR, exist_ok=True)
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    multi = multi_target()

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s" if multi
        else "%(asctime)s [%(levelname)s] %(message)s"
    )

    if not _stream_handler_added:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
        _stream_handler_added = True

    file_handler = logging.FileHandler(os.path.join(STATE_DIR, log_file), encoding="utf-8")
    file_handler.setFormatter(formatter)
    if multi and target:
        file_handler.addFilter(lambda record, name=target: record.threadName == name)
    root.addHandler(file_handler)


# ─── Состояние ────────────────────────────────────────────────────────────────

class State:
    """ID уже перенесённых постов и marker long polling на диске.

    Имена файлов зависят от площадки: OK и VK ведут своё состояние независимо,
    чтобы один и тот же пост попал в обе соцсети.
    """

    def __init__(self, posted_ids_file: str, marker_file: str):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.posted_ids_path = os.path.join(STATE_DIR, posted_ids_file)
        self.marker_path = os.path.join(STATE_DIR, marker_file)
        self.posted_ids = self._load_posted_ids()

    def _load_posted_ids(self) -> set:
        if os.path.exists(self.posted_ids_path):
            with open(self.posted_ids_path, encoding="utf-8") as f:
                try:
                    return set(json.load(f))
                except (ValueError, TypeError):
                    pass
        return set()

    def add_posted(self, msg_id: str) -> None:
        self.posted_ids.add(msg_id)
        with open(self.posted_ids_path, "w", encoding="utf-8") as f:
            json.dump(list(self.posted_ids), f)

    def load_marker(self) -> Optional[int]:
        if os.path.exists(self.marker_path):
            with open(self.marker_path) as f:
                try:
                    return int(f.read().strip())
                except ValueError:
                    pass
        return None

    def save_marker(self, marker: int) -> None:
        with open(self.marker_path, "w") as f:
            f.write(str(marker))


# ─── MAX API ──────────────────────────────────────────────────────────────────

class MaxSource:
    """Клиент бота МАКС: long polling и разбор постов канала."""

    def __init__(self, token: Optional[str] = None, chat_id: Optional[int] = None):
        self.token = token or os.environ["MAX_BOT_TOKEN"]
        self.chat_id = chat_id if chat_id is not None else int(os.environ["MAX_CHAT_ID"])

    def _headers(self) -> dict:
        return {"Authorization": self.token, "Content-Type": "application/json"}

    def get_updates(self, marker: Optional[int] = None, timeout: int = 20) -> dict:
        """Long Polling: получить новые события из канала МАКС."""
        params = {"timeout": timeout, "limit": 100}
        if marker is not None:
            params["marker"] = marker
        resp = requests.get(
            f"{MAX_API}/updates", headers=self._headers(), params=params, timeout=timeout + 5
        )
        resp.raise_for_status()
        return resp.json()

    def get_messages(self, limit: int = 3) -> list[dict]:
        """Получить последние сообщения канала МАКС (не через long polling)."""
        params = {"chat_id": self.chat_id, "count": limit}
        resp = requests.get(f"{MAX_API}/messages", headers=self._headers(), params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return data.get("messages", [])
        return data

    def extract_post_from_message(self, message: dict) -> Optional[tuple[str, dict]]:
        """Извлечь пост из объекта сообщения (прямой запрос /messages)."""
        # Тело сообщения может лежать в "body" либо прямо в message.
        body = message.get("body") or message
        msg_id = str(
            message.get("mid") or message.get("id") or message.get("message_id")
            or body.get("mid") or body.get("id") or ""
        )
        post = parse_body(body)
        if not post["text"] and not post["photo_urls"] and not post["videos"]:
            return None
        return msg_id, post

    def extract_post_from_update(self, update: dict) -> Optional[tuple[str, dict]]:
        """Извлечь пост из события long polling (только message_created в нашем канале)."""
        if update.get("update_type") != "message_created":
            return None

        message = update.get("message", {})
        if message.get("recipient", {}).get("chat_id") != self.chat_id:
            return None

        body = message.get("body", {})
        # mid хранится внутри body, не в message
        msg_id = str(message.get("id") or message.get("message_id") or body.get("mid") or "")
        post = parse_body(body)
        if not post["text"] and not post["photo_urls"] and not post["videos"]:
            return None
        return msg_id, post


def parse_body(body: dict) -> dict:
    """
    Разобрать body сообщения МАКС.
    Возвращает dict: text, markup, photo_urls, videos.
    """
    log.debug("RAW body: %s", json.dumps(body, ensure_ascii=False))

    text: str = body.get("text", "")
    markup: list = body.get("markup", [])  # аннотации форматирования от МАКС

    photo_urls: list[str] = []
    # Каждое видео: {"payload_id": str|None, "url_id": str|None, "url": str|None}
    # payload_id — внутренний stream ID из MAX, url_id — параметр id= из URL CDN,
    # url — прямая ссылка для скачивания и перезаливки в соцсеть.
    videos: list[dict] = []

    for attachment in body.get("attachments", []):
        att_type = attachment.get("type")
        payload = attachment.get("payload", {})
        log.debug("Attachment type=%s payload=%s", att_type, json.dumps(payload, ensure_ascii=False))

        if att_type == "image":
            url = payload.get("url") or payload.get("photo_url")
            if url:
                photo_urls.append(url)
        elif att_type == "video":
            vid_url = payload.get("url") or payload.get("video_url")
            payload_id = str(payload["id"]) if payload.get("id") else None

            url_id = None
            if vid_url:
                qs = parse_qs(urlparse(vid_url).query)
                raw = qs.get("id", [None])[0]
                if raw and raw != payload_id:
                    url_id = raw

            log.debug("Видео payload_id=%s url_id=%s url=%s", payload_id, url_id, vid_url)
            videos.append({"payload_id": payload_id, "url_id": url_id, "url": vid_url})

    return {"text": text, "markup": markup, "photo_urls": photo_urls, "videos": videos}


# ─── Форматирование текста ────────────────────────────────────────────────────

def _char_bold(c: str) -> str:
    """Конвертировать символ в математический жирный Unicode (блок U+1D400)."""
    if "A" <= c <= "Z":
        return chr(0x1D400 + ord(c) - ord("A"))
    if "a" <= c <= "z":
        return chr(0x1D41A + ord(c) - ord("a"))
    if "0" <= c <= "9":
        return chr(0x1D7CE + ord(c) - ord("0"))
    return c


def _char_italic(c: str) -> str:
    """Конвертировать символ в математический курсивный Unicode (блок U+1D434)."""
    if "A" <= c <= "Z":
        return chr(0x1D434 + ord(c) - ord("A"))
    if "a" <= c <= "z":
        # 'h' занят планковской постоянной ℎ U+210E
        return "ℎ" if c == "h" else chr(0x1D44E + ord(c) - ord("a"))
    return c


_PRIORITY = {
    "heading": 0,
    "bold": 1, "strong": 1,
    "italic": 2, "em": 2,
    "strikethrough": 3,
    "underline": 4,
}


def apply_markup_to_text(text: str, markup: list) -> str:
    """
    Применить аннотации форматирования MAX к тексту через Unicode-символы.
    Ни OK, ни VK не принимают разметку в теле поста, поэтому форматирование
    встраивается прямо в строку.

      bold / strong / heading → математический жирный (U+1D400)
      italic / em             → математический курсив (U+1D434)
      strikethrough           → комбинирующее зачёркивание (U+0336)
      underline               → комбинирующее подчёркивание (U+0332)
      link                    → текст без изменений (URL теряется)
    """
    if not markup or not text:
        return text

    # Массив стилей по символам (при перекрытии побеждает более приоритетный)
    styles: list[str | None] = [None] * len(text)
    for ann in sorted(markup, key=lambda a: _PRIORITY.get(a.get("type", "").lower(), 99)):
        ann_type = ann.get("type", "").lower()
        if ann_type not in _PRIORITY:
            continue
        start = ann.get("from", 0)
        end = min(start + ann.get("length", 0), len(text))
        for i in range(start, end):
            if styles[i] is None:
                styles[i] = ann_type

    result = []
    for c, style in zip(text, styles):
        if style in ("bold", "strong", "heading"):
            result.append(_char_bold(c))
        elif style in ("italic", "em"):
            result.append(_char_italic(c))
        elif style == "strikethrough":
            result.append(c + "̶")
        elif style == "underline":
            result.append(c + "̲")
        else:
            result.append(c)

    return "".join(result)


# ─── Основной цикл ────────────────────────────────────────────────────────────

def run_loop(source: MaxSource, state: State, publish: Callable[[dict], str], target: str) -> None:
    """
    Long Polling из МАКС → публикация через publish(post) -> id опубликованного поста.

    marker последнего обработанного обновления и ID уже перенесённых постов
    хранятся в State, чтобы рестарт не приводил к дублям.

    Бэкфилл (перенос уже опубликованных в МАКС постов) выключен по умолчанию:
      BACKFILL_ON_EMPTY=1 — только при первом запуске, пока состояние пустое;
      EBOI=true           — принудительно при каждом старте, даже если состояние
                            не пустое (ручной режим: включил, перезапустил, выключил).
    Сколько постов забирать — BACKFILL_COUNT (по умолчанию 3).
    """
    marker = state.load_marker()

    log.info("Запуск кросспостинга МАКС → %s (канал %d)", target, source.chat_id)

    _run_backfill(source, state, publish, target)

    while True:
        try:
            data = source.get_updates(marker=marker)
            updates = data.get("updates", [])
            new_marker = data.get("marker")

            for update in updates:
                result = source.extract_post_from_update(update)
                if result:
                    _publish_one(*result, state, publish, target)

            if new_marker:
                marker = new_marker
                state.save_marker(marker)

            if not updates:
                time.sleep(0.5)

        except requests.exceptions.Timeout:
            pass  # нормально для long polling
        except KeyboardInterrupt:
            log.info("Остановлено вручную.")
            break
        except Exception as e:
            log.error("Неожиданная ошибка: %s. Пауза 10 сек...", e)
            time.sleep(10)


def _run_backfill(
    source: MaxSource, state: State, publish: Callable[[dict], str], target: str
) -> None:
    """Залить последние BACKFILL_COUNT постов канала, если бэкфилл включён.

    Уже перенесённые посты отсеиваются по posted_ids, поэтому повторный запуск
    с EBOI=true не создаёт дублей — доедет только то, чего ещё нет в соцсети.
    """
    forced = env_flag("EBOI")
    count = int(os.environ.get("BACKFILL_COUNT", "3"))

    if not forced and not (not state.posted_ids and env_flag("BACKFILL_ON_EMPTY")):
        if not state.posted_ids:
            log.info("Состояние пустое, бэкфилл выключен (EBOI/BACKFILL_ON_EMPTY). Переносим только новые посты.")
        return

    if count <= 0:
        log.warning("Бэкфилл включён, но BACKFILL_COUNT=%d — пропускаем.", count)
        return

    log.info(
        "Бэкфилл %s: забираем последние %d поста(ов) из канала МАКС...",
        "принудительный (EBOI)" if forced else "при пустом состоянии",
        count,
    )
    try:
        messages = source.get_messages(limit=count)
    except Exception as e:
        log.error("Ошибка при загрузке постов для бэкфилла: %s", e)
        return

    log.info("Получено сообщений из МАКС: %d", len(messages))
    published = 0
    for message in reversed(messages):  # от старого к новому
        result = source.extract_post_from_message(message)
        if not result:
            continue
        if _publish_one(*result, state, publish, target):
            published += 1
            time.sleep(1)  # пауза между постами, чтобы не упереться в лимиты API
    log.info("Бэкфилл завершён: опубликовано %d пост(ов).", published)


def _publish_one(
    msg_id: str, post: dict, state: State, publish: Callable[[dict], str], target: str
) -> bool:
    """Опубликовать один пост и запомнить его ID. Возвращает True при успехе."""
    if msg_id and msg_id in state.posted_ids:
        log.debug("Пост %s уже перенесён, пропускаем.", msg_id)
        return False

    log.info(
        "Публикуем пост %s в %s: текст=%r, фото=%d, видео=%d, markup=%d",
        msg_id,
        target,
        post["text"][:60] if post["text"] else "",
        len(post["photo_urls"]),
        len(post.get("videos", [])),
        len(post.get("markup", [])),
    )
    try:
        published_id = publish(post)
        log.info("Пост опубликован в %s, id=%s", target, published_id)
        if msg_id:
            state.add_posted(msg_id)
        return True
    except Exception as e:
        log.error("Ошибка публикации в %s: %s", target, e)
        return False
