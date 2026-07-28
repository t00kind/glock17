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

import max_source
from max_source import MaxSource, State, apply_markup_to_text

load_dotenv()

max_source.setup_logging("crosspost.log", "ok")
log = logging.getLogger(__name__)

# ─── Конфиг ──────────────────────────────────────────────────────────────────

OK_APP_ID      = os.environ["OK_APP_ID"]
OK_APP_KEY     = os.environ["OK_APP_KEY"]       # публичный ключ
OK_APP_SECRET  = os.environ["OK_APP_SECRET"]    # секретный ключ
OK_ACCESS_TOKEN = os.environ["OK_ACCESS_TOKEN"]
OK_GROUP_ID    = os.environ["OK_GROUP_ID"]

OK_API  = "https://api.ok.ru/fb.do"


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


# ─── Точка входа ──────────────────────────────────────────────────────────────


def publish(post: dict) -> str:
    return ok_post_to_group(
        post["text"],
        post.get("markup", []),
        post["photo_urls"],
        post.get("videos", []),
    )


def run_crosspost() -> None:
    source = MaxSource()
    state = State("posted_ids.json", "marker.txt")
    max_source.run_loop(source, state, publish, f"OK (группа {OK_GROUP_ID})")


if __name__ == "__main__":
    run_crosspost()
