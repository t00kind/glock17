"""
Кросспостинг из МАКС мессенджера в группу Одноклассников
=========================================================
Зависимости: pip install requests python-dotenv

Переменные окружения (файл .env):
  MAX_BOT_TOKEN       - токен бота в МАКС (из BotFather)
  MAX_CHAT_ID         - ID канала/чата МАКС, откуда читаем посты
  OK_APP_ID           - ID приложения Одноклассников
  OK_APP_KEY          - Публичный ключ приложения
  OK_APP_SECRET       - Секретный ключ приложения
  OK_ACCESS_TOKEN     - OAuth access_token пользователя-администратора группы
  OK_GROUP_ID         - ID группы в Одноклассниках (числовой)
"""

import hashlib
import json
import logging
import os
import time
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# Каталог для файлов состояния (в Docker монтируется как volume, чтобы
# дедупликация и marker переживали рестарт контейнера).
STATE_DIR = os.environ.get("STATE_DIR", ".")
os.makedirs(STATE_DIR, exist_ok=True)

POSTED_IDS_FILE = os.path.join(STATE_DIR, "posted_ids.json")
MARKER_FILE = os.path.join(STATE_DIR, "marker.txt")
LOG_FILE = os.path.join(STATE_DIR, "crosspost.log")


def load_posted_ids() -> set:
    if os.path.exists(POSTED_IDS_FILE):
        with open(POSTED_IDS_FILE, encoding="utf-8") as f:
            try:
                return set(json.load(f))
            except (ValueError, TypeError):
                pass
    return set()


def save_posted_ids(ids: set) -> None:
    with open(POSTED_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(ids), f)


# Уровень логирования управляется из окружения (по умолчанию INFO для прода).
# DEBUG пишет RAW-тела сообщений и ответы OK API — включать только для отладки.
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─── Конфиг ──────────────────────────────────────────────────────────────────

MAX_BOT_TOKEN = os.environ["MAX_BOT_TOKEN"]
MAX_CHAT_ID   = int(os.environ["MAX_CHAT_ID"])

OK_APP_ID      = os.environ["OK_APP_ID"]
OK_APP_KEY     = os.environ["OK_APP_KEY"]       # публичный ключ
OK_APP_SECRET  = os.environ["OK_APP_SECRET"]    # секретный ключ
OK_ACCESS_TOKEN = os.environ["OK_ACCESS_TOKEN"]
OK_GROUP_ID    = os.environ["OK_GROUP_ID"]

MAX_API = "https://platform-api.max.ru"
OK_API  = "https://api.ok.ru/fb.do"

# ─── MAX API ──────────────────────────────────────────────────────────────────

def max_headers() -> dict:
    return {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}


def max_get_updates(marker: Optional[int] = None, timeout: int = 20) -> dict:
    """Long Polling: получить новые события из канала МАКС."""
    params = {"timeout": timeout, "limit": 100}
    if marker is not None:
        params["marker"] = marker
    resp = requests.get(f"{MAX_API}/updates", headers=max_headers(), params=params, timeout=timeout + 5)
    resp.raise_for_status()
    return resp.json()


def max_get_messages(limit: int = 3) -> list[dict]:
    """Получить последние сообщения из канала МАКС (не через long polling)."""
    params = {"chat_id": MAX_CHAT_ID, "count": limit}
    resp = requests.get(f"{MAX_API}/messages", headers=max_headers(), params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    # API возвращает {"messages": [...]} или просто список
    if isinstance(data, dict):
        return data.get("messages", [])
    return data


def _parse_body(body: dict) -> dict:
    """
    Разобрать body сообщения МАКС.
    Возвращает dict: text, markup, photo_urls, videos.
    Логирует полное тело на уровне DEBUG для диагностики.
    """
    log.debug("RAW body: %s", json.dumps(body, ensure_ascii=False))

    text: str = body.get("text", "")
    markup: list = body.get("markup", [])  # аннотации форматирования от МАКС

    photo_urls: list[str] = []
    # Список видео: каждый элемент {"payload_id": str|None, "url_id": str|None, "url": str|None}
    # payload_id  — payload["id"] из MAX (внутренний stream ID, часто не совпадает с OK video ID)
    # url_id      — параметр id= из URL на okcdn.ru (другой числовой ID)
    # url         — прямая ссылка для скачивания и перезагрузки
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

            # Извлечь id= из URL-параметров (отличается от payload["id"])
            url_id = None
            if vid_url:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(vid_url).query)
                raw = qs.get("id", [None])[0]
                if raw and raw != payload_id:
                    url_id = raw

            log.debug("Видео payload_id=%s url_id=%s url=%s", payload_id, url_id, vid_url)
            videos.append({"payload_id": payload_id, "url_id": url_id, "url": vid_url})

    return {"text": text, "markup": markup, "photo_urls": photo_urls, "videos": videos}


def extract_post_from_message(message: dict) -> Optional[tuple[str, dict]]:
    """
    Извлечь данные поста из объекта сообщения МАКС (прямой запрос /messages).
    Возвращает (message_id, post_dict) или None.
    """
    # В /messages API тело сообщения может быть либо в ключе "body",
    # либо сам message является телом (поля text/attachments на верхнем уровне).
    body = message.get("body") or message
    msg_id = str(
        message.get("mid") or message.get("id") or message.get("message_id")
        or body.get("mid") or body.get("id") or ""
    )
    post = _parse_body(body)

    if not post["text"] and not post["photo_urls"] and not post["videos"]:
        return None

    return msg_id, post


def extract_post_from_update(update: dict) -> Optional[tuple[str, dict]]:
    """
    Извлечь данные поста из события МАКС.
    Нас интересуют события message_created в нашем канале.
    Возвращает (message_id, post_dict) или None.
    """
    if update.get("update_type") != "message_created":
        return None

    message = update.get("message", {})
    recipient = message.get("recipient", {})

    # Фильтруем только сообщения из нашего канала
    if recipient.get("chat_id") != MAX_CHAT_ID:
        return None

    body = message.get("body", {})
    # mid хранится внутри body (не в message), берём оттуда
    msg_id = str(message.get("id") or message.get("message_id") or body.get("mid") or "")
    post = _parse_body(body)

    if not post["text"] and not post["photo_urls"] and not post["videos"]:
        return None

    return msg_id, post


# ─── Одноклассники API ────────────────────────────────────────────────────────

def ok_sign_params(params: dict) -> str:
    """
    Вычислить sig для запроса к OK API.
    Алгоритм: MD5( sorted(key=value) + MD5(access_token + app_secret) )
    """
    token_digest = hashlib.md5(f"{OK_ACCESS_TOKEN}{OK_APP_SECRET}".encode()).hexdigest()
    sorted_pairs = "".join(f"{k}={v}" for k, v in sorted(params.items()))
    sig = hashlib.md5(f"{sorted_pairs}{token_digest}".encode()).hexdigest()
    return sig


def ok_call(method: str, extra_params: dict) -> dict:
    """Выполнить запрос к REST API Одноклассников."""
    params = {
        "application_key": OK_APP_KEY,
        "method": method,
        "format": "json",
        **extra_params,
    }
    params["sig"] = ok_sign_params(params)
    params["access_token"] = OK_ACCESS_TOKEN

    resp = requests.post(OK_API, data=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "error_code" in data:
        raise RuntimeError(f"OK API error {data['error_code']}: {data.get('error_msg')}")
    return data


def ok_upload_photo(image_url: str) -> Optional[str]:
    """
    Загрузить фото по URL во временное хранилище OK и получить token.
    Шаги: 1) скачать фото, 2) получить upload URL, 3) загрузить, 4) сохранить.
    """
    # 1. Скачать файл
    try:
        img_data = requests.get(image_url, timeout=15).content
    except Exception as e:
        log.warning("Не удалось скачать фото %s: %s", image_url, e)
        return None

    # 2. Получить URL для загрузки
    upload_info = ok_call("photosV2.getUploadUrl", {"count": "1", "gid": OK_GROUP_ID})
    upload_url = upload_info.get("upload_url")
    if not upload_url:
        log.warning("Не получен upload_url от OK API")
        return None

    # 3. Загрузить
    upload_resp = requests.post(
        upload_url,
        files={"pic1": ("photo.jpg", img_data, "image/jpeg")},
        timeout=30,
    )
    upload_resp.raise_for_status()
    upload_data = upload_resp.json()

    # 4. Сохранить и вернуть token
    photos = upload_data.get("photos", {})
    for _key, photo_obj in photos.items():
        token = photo_obj.get("token")
        if token:
            return token

    log.warning("Не удалось получить photo token из ответа OK: %s", upload_data)
    return None


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


def apply_markup_to_text(text: str, markup: list) -> str:
    """
    Применить аннотации форматирования MAX к тексту через Unicode-символы.
    OK REST API не поддерживает markup в attachment, поэтому встраиваем
    форматирование прямо в строку.

    Поддерживаемые типы:
      bold / strong / heading → математический жирный (U+1D400)
      italic / em             → математический курсив (U+1D434)
      strikethrough           → комбинирующее зачёркивание (U+0336)
      underline               → комбинирующее подчёркивание (U+0332)
      link                    → текст без изменений (URL теряется, но текст сохраняется)
    """
    if not markup or not text:
        return text

    # Приоритет стилей при перекрытии (меньше — важнее)
    _PRIORITY = {
        "heading": 0,
        "bold": 1, "strong": 1,
        "italic": 2, "em": 2,
        "strikethrough": 3,
        "underline": 4,
    }

    # Массив стилей по символам (берём стиль с наивысшим приоритетом)
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


def ok_upload_video(video_url: str, filename: str = "video.mp4") -> Optional[str]:
    """
    Загрузить видео по URL в группу OK.
    Шаги: 1) скачать видео, 2) получить upload URL (video.getUploadUrl),
           3) загрузить файл, 4) финализировать (video.update).
    Возвращает video_id (str) или None при ошибке.

    Полученный video_id прикрепляется в mediatopic.post как
    {"type": "movie", "list": [{"id": video_id}]}.

    Важно:
      - НЕ передавать gid в video.getUploadUrl: загрузка видео в группу
        автоматически публикует его в ленту группы (отдельный пост).
        Затем mediatopic.post создаёт второй пост → дубль. Поэтому
        заливаем в личные видео бота, а в группу прикрепляем через
        mediatopic.post.
      - НЕ передавать attachment_type — это вызывает ошибку прав при
        video.update.
    """
    # 1. Скачать файл
    try:
        log.info("Скачиваем видео: %s", video_url)
        vid_resp = requests.get(video_url, timeout=120, stream=True)
        vid_resp.raise_for_status()
        vid_data = vid_resp.content
        log.info("Видео скачано, размер: %d байт", len(vid_data))
    except Exception as e:
        log.warning("Не удалось скачать видео %s: %s", video_url, e)
        return None

    # 2. Получить URL для загрузки
    # Не передаём gid/post_form: с gid OK сам публикует видео в ленту группы
    # (получаем дубль). Заливаем в личные видео, в группу прикрепим через
    # mediatopic.post. Не передаём attachment_type — это вызывает ошибку 10.
    try:
        upload_info = ok_call("video.getUploadUrl", {
            "file_name": filename,
            "file_size": str(len(vid_data)),
        })
        log.debug("video.getUploadUrl response: %s", upload_info)
    except Exception as e:
        log.warning("Не удалось получить upload URL для видео: %s", e)
        return None

    upload_url = upload_info.get("upload_url")
    video_id = upload_info.get("video_id")
    if not upload_url or not video_id:
        log.warning("Не получен upload_url или video_id: %s", upload_info)
        return None

    # 3. Загрузить файл на полученный URL
    try:
        upload_resp = requests.post(
            upload_url,
            files={"file": (filename, vid_data, "video/mp4")},
            timeout=180,
        )
        upload_resp.raise_for_status()
        log.debug("Ответ загрузки видео: %s", upload_resp.text)
    except Exception as e:
        log.warning("Ошибка загрузки видео в OK: %s", e)
        return None

    # 4. Финализировать загрузку
    try:
        ok_call("video.update", {"vid": str(video_id)})
        log.debug("video.update выполнен для video_id=%s", video_id)
    except Exception as e:
        log.warning("video.update завершился с ошибкой (возможно видео уже готово): %s", e)

    # 5. Дождаться окончания транскодинга (OK обрабатывает видео асинхронно).
    # Не критично, но снижает риск «битого» эмбеда в момент публикации.
    _wait_video_ready(str(video_id))

    return str(video_id)


def _wait_video_ready(video_id: str, attempts: int = 12, delay: float = 5.0) -> None:
    """Опросить video.get и подождать, пока видео выйдет из статуса PROCESSING.

    Не бросает исключений: если не дождались — всё равно пытаемся опубликовать
    (OK покажет плейсхолдер обработки, который позже сам разрешится).
    """
    for i in range(attempts):
        try:
            info = ok_call("video.get", {"vids": video_id, "fields": "video.status"})
            videos = info.get("videos") or []
            status = (videos[0].get("status") if videos else None) or ""
            log.debug("Статус видео %s: %s (попытка %d)", video_id, status, i + 1)
            if status and status.upper() not in ("PROCESSING", "UPLOADING"):
                return
        except Exception as e:
            log.debug("video.get не удался (попытка %d): %s", i + 1, e)
        time.sleep(delay)
    log.warning("Видео %s не завершило обработку за отведённое время, публикуем как есть", video_id)


def _upload_all_videos(videos: list[dict]) -> list[dict]:
    """Загрузить все видео через ok_upload_video.

    Возвращает media-items для mediatopic.post: одно вложение
    {"type": "movie", "list": [{"id": ...}, ...]} со всеми загруженными
    видео (форма для своих, не-репостных видео). Если ни одно не загрузилось —
    пустой список.
    """
    ids = []
    for vid in videos:
        url = vid.get("url")
        if not url:
            log.warning("Нет URL для загрузки видео: %s", vid)
            continue
        uploaded_id = ok_upload_video(url)
        if uploaded_id:
            ids.append({"id": uploaded_id})
        else:
            log.error("Не удалось загрузить видео: %s", url)
    return [{"type": "movie", "list": ids}] if ids else []


def ok_post_to_group(text: str, markup: list, photo_urls: list[str], videos: list[dict]) -> str:
    """
    Опубликовать пост в группу Одноклассников.
    Возвращает ID созданного медиатопика.

    Видео из MAX не являются OK-видео (movie-reshare всегда падает с
    errors.movie.not.available), поэтому каждое видео заливается в OK через
    ok_upload_video и прикрепляется как {"type": "movie", "list": [...]}.
    Весь пост (текст + фото + видео) публикуется одним mediatopic.post.

    Разметка текста (markup) не поддерживается OK REST API, текст публикуется as-is.
    """
    media_items: list[dict] = []

    if text:
        display_text = apply_markup_to_text(text, markup)
        media_items.append({"type": "text", "text": display_text})

    photo_list = []
    for url in photo_urls:
        token = ok_upload_photo(url)
        if token:
            photo_list.append({"id": token})
    if photo_list:
        media_items.append({"type": "photo", "list": photo_list})

    # Видео всегда заливаем сами (MAX-видео нельзя репостнуть через movie-reshare)
    media_items += _upload_all_videos(videos)

    if not media_items:
        raise ValueError("Нет контента для публикации в OK")

    attachment = json.dumps({"media": media_items}, ensure_ascii=False)
    result = ok_call("mediatopic.post", {
        "type": "GROUP_THEME",
        "gid": OK_GROUP_ID,
        "attachment": attachment,
    })
    return str(result)


# ─── Основной цикл ────────────────────────────────────────────────────────────

def crosspost_post(msg_id: str, post: dict, posted_ids: set) -> bool:
    """Опубликовать пост в OK и сохранить его ID. Возвращает True при успехе."""
    if msg_id and msg_id in posted_ids:
        log.debug("Пост %s уже перенесён, пропускаем.", msg_id)
        return False
    log.info(
        "Публикуем пост %s: текст=%r, фото=%d, видео=%d, markup=%d",
        msg_id,
        post["text"][:60] if post["text"] else "",
        len(post["photo_urls"]),
        len(post.get("videos", [])),
        len(post.get("markup", [])),
    )
    try:
        topic_id = ok_post_to_group(
            post["text"],
            post.get("markup", []),
            post["photo_urls"],
            post.get("videos", []),
        )
        log.info("Пост опубликован в OK, topic_id=%s", topic_id)
        if msg_id:
            posted_ids.add(msg_id)
            save_posted_ids(posted_ids)
        return True
    except Exception as e:
        log.error("Ошибка публикации в OK: %s", e)
        return False


def run_crosspost():
    """
    Основной цикл: Long Polling из МАКС → публикация в OK.
    Хранит marker последнего обработанного обновления в файле marker.txt.
    Хранит ID уже перенесённых постов в posted_ids.json.
    При первом запуске бэкфилл управляется переменными BACKFILL_ON_EMPTY/
    BACKFILL_COUNT (по умолчанию выключен, чтобы прод-старт не залил старые посты).
    """
    marker: Optional[int] = None

    if os.path.exists(MARKER_FILE):
        with open(MARKER_FILE) as f:
            try:
                marker = int(f.read().strip())
            except ValueError:
                pass

    posted_ids = load_posted_ids()

    log.info("Запуск кросспостинга МАКС → Одноклассники (канал %d → группа %s)", MAX_CHAT_ID, OK_GROUP_ID)

    # Бэкфилл при пустом состоянии: по умолчанию ВЫКЛЮЧЕН (на проде иначе
    # зальёт старые посты в реальную группу). Включается BACKFILL_ON_EMPTY=1.
    backfill_on = os.environ.get("BACKFILL_ON_EMPTY", "0").lower() in ("1", "true", "yes")
    backfill_count = int(os.environ.get("BACKFILL_COUNT", "3"))
    if not posted_ids and backfill_on and backfill_count > 0:
        log.info("Нет перенесённых постов. Загружаем последние %d поста(ов) из канала МАКС...", backfill_count)
        try:
            recent_messages = max_get_messages(limit=backfill_count)
            for message in reversed(recent_messages):  # от старого к новому
                result = extract_post_from_message(message)
                if result:
                    msg_id, post = result
                    crosspost_post(msg_id, post, posted_ids)
                    time.sleep(1)  # небольшая пауза между постами
        except Exception as e:
            log.error("Ошибка при загрузке начальных постов: %s", e)
    elif not posted_ids:
        log.info("Состояние пустое, бэкфилл выключен (BACKFILL_ON_EMPTY=0). Переносим только новые посты.")

    while True:
        try:
            data = max_get_updates(marker=marker)
            updates = data.get("updates", [])
            new_marker = data.get("marker")

            for update in updates:
                result = extract_post_from_update(update)
                if result:
                    msg_id, post = result
                    crosspost_post(msg_id, post, posted_ids)

            if new_marker:
                marker = new_marker
                with open(MARKER_FILE, "w") as f:
                    f.write(str(marker))

            if not updates:
                # Long Polling вернул пустой ответ — сразу делаем следующий запрос
                time.sleep(0.5)

        except requests.exceptions.Timeout:
            # Нормально для Long Polling
            pass
        except KeyboardInterrupt:
            log.info("Остановлено вручную.")
            break
        except Exception as e:
            log.error("Неожиданная ошибка: %s. Пауза 10 сек...", e)
            time.sleep(10)


if __name__ == "__main__":
    run_crosspost()
