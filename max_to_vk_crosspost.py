"""
Кросспостинг из МАКС мессенджера в сообщество ВКонтакте
=======================================================
Зависимости: pip install requests python-dotenv

Переменные окружения (файл .env):
  MAX_BOT_TOKEN     - токен бота в МАКС
  MAX_CHAT_ID       - ID канала/чата МАКС, откуда читаем посты
  VK_ACCESS_TOKEN   - токен администратора сообщества
                      (нужны права wall, photos, video, groups, offline)
  VK_GROUP_ID       - числовой ID сообщества ВКонтакте (без минуса)
  VK_API_VERSION    - версия API (по умолчанию 5.199)
"""

import logging
import os
from typing import Optional

import requests
from dotenv import load_dotenv

import max_source
from max_source import MaxSource, State, apply_markup_to_text

load_dotenv()

max_source.setup_logging("crosspost_vk.log", "vk")
log = logging.getLogger(__name__)

# ─── Конфиг ──────────────────────────────────────────────────────────────────

VK_ACCESS_TOKEN = os.environ["VK_ACCESS_TOKEN"]
VK_GROUP_ID = str(os.environ["VK_GROUP_ID"]).lstrip("-")
VK_API_VERSION = os.environ.get("VK_API_VERSION", "5.199")

VK_API = "https://api.vk.com/method"

# owner_id стены сообщества — отрицательный ID группы
VK_WALL_OWNER_ID = f"-{VK_GROUP_ID}"

# ─── VK API ───────────────────────────────────────────────────────────────────


def vk_call(method: str, params: dict) -> dict:
    """Выполнить запрос к VK API. Бросает RuntimeError на ошибку API."""
    payload = {**params, "access_token": VK_ACCESS_TOKEN, "v": VK_API_VERSION}
    resp = requests.post(f"{VK_API}/{method}", data=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"VK API error {err.get('error_code')}: {err.get('error_msg')}")
    log.debug("%s → %s", method, data.get("response"))
    return data["response"]


def vk_upload_photo(image_url: str) -> Optional[str]:
    """
    Загрузить фото по URL на стену сообщества.
    Шаги: 1) скачать, 2) photos.getWallUploadServer, 3) загрузить,
          4) photos.saveWallPhoto.
    Возвращает attachment вида photo<owner_id>_<id> или None при ошибке.
    """
    try:
        img_data = requests.get(image_url, timeout=30).content
    except Exception as e:
        log.warning("Не удалось скачать фото %s: %s", image_url, e)
        return None

    try:
        upload_url = vk_call("photos.getWallUploadServer", {"group_id": VK_GROUP_ID})["upload_url"]
        upload_resp = requests.post(
            upload_url,
            files={"photo": ("photo.jpg", img_data, "image/jpeg")},
            timeout=60,
        )
        upload_resp.raise_for_status()
        uploaded = upload_resp.json()

        saved = vk_call("photos.saveWallPhoto", {
            "group_id": VK_GROUP_ID,
            "server": uploaded["server"],
            "photo": uploaded["photo"],
            "hash": uploaded["hash"],
        })
    except Exception as e:
        log.warning("Ошибка загрузки фото в VK: %s", e)
        return None

    if not saved:
        log.warning("VK не вернул сохранённое фото для %s", image_url)
        return None

    photo = saved[0]
    return f"photo{photo['owner_id']}_{photo['id']}"


def vk_upload_video(video_url: str, name: str = "Видео") -> Optional[str]:
    """
    Загрузить видео по URL в видеозаписи сообщества.
    Шаги: 1) скачать, 2) video.save, 3) залить файл на upload_url.
    Возвращает attachment вида video<owner_id>_<video_id> или None.

    wall_post=0: VK не должен публиковать видео отдельным постом — оно
    прикрепляется к общему посту через wall.post, иначе получится дубль.
    """
    try:
        log.info("Скачиваем видео: %s", video_url)
        vid_resp = requests.get(video_url, timeout=180, stream=True)
        vid_resp.raise_for_status()
        vid_data = vid_resp.content
        log.info("Видео скачано, размер: %d байт", len(vid_data))
    except Exception as e:
        log.warning("Не удалось скачать видео %s: %s", video_url, e)
        return None

    try:
        saved = vk_call("video.save", {
            "group_id": VK_GROUP_ID,
            "name": name,
            "wall_post": 0,
        })
        upload_resp = requests.post(
            saved["upload_url"],
            files={"video_file": ("video.mp4", vid_data, "video/mp4")},
            timeout=600,
        )
        upload_resp.raise_for_status()
        log.debug("Ответ загрузки видео: %s", upload_resp.text)
    except Exception as e:
        log.warning("Ошибка загрузки видео в VK: %s", e)
        return None

    return f"video{saved['owner_id']}_{saved['video_id']}"


def vk_post_to_group(post: dict) -> str:
    """
    Опубликовать пост в сообщество ВКонтакте одним wall.post.
    Возвращает ID созданной записи.

    Видео из МАКС не являются видеозаписями VK, поэтому каждое скачивается
    и заливается в сообщество, а в пост попадает как video-вложение.
    Разметку VK в тексте не поддерживает — она встраивается Unicode-символами.
    """
    attachments: list[str] = []

    for url in post["photo_urls"]:
        attachment = vk_upload_photo(url)
        if attachment:
            attachments.append(attachment)
        else:
            log.error("Не удалось загрузить фото: %s", url)

    for video in post.get("videos", []):
        url = video.get("url")
        if not url:
            log.warning("Нет URL для загрузки видео: %s", video)
            continue
        attachment = vk_upload_video(url)
        if attachment:
            attachments.append(attachment)
        else:
            log.error("Не удалось загрузить видео: %s", url)

    message = apply_markup_to_text(post["text"], post.get("markup", []))

    if not message and not attachments:
        raise ValueError("Нет контента для публикации в VK")

    params = {
        "owner_id": VK_WALL_OWNER_ID,
        "from_group": 1,
        "message": message,
    }
    if attachments:
        params["attachments"] = ",".join(attachments)

    result = vk_call("wall.post", params)
    return str(result.get("post_id"))


# ─── Точка входа ──────────────────────────────────────────────────────────────


def run_crosspost() -> None:
    source = MaxSource()
    state = State("posted_ids_vk.json", "marker_vk.txt")
    max_source.run_loop(source, state, vk_post_to_group, f"VK (сообщество {VK_GROUP_ID})")


if __name__ == "__main__":
    run_crosspost()
