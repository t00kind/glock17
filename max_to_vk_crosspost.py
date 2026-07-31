"""
Кросспостинг из МАКС мессенджера в сообщество ВКонтакте
=======================================================
Зависимости: pip install requests python-dotenv

Переменные окружения (файл .env):
  MAX_BOT_TOKEN     - токен бота в МАКС
  MAX_CHAT_ID       - ID канала/чата МАКС, откуда читаем посты
  VK_ACCESS_TOKEN   - ключ доступа сообщества (настройки группы → Работа с API).
                      Права: управление сообществом, фотографии, видео, стена.
                      Токены VK ID (vk2.a.*) не подходят: они выдаются только
                      для авторизации на сайтах, а методы API отвечают на них
                      ошибками 1051 и 15
  VK_REFRESH_TOKEN  - refresh-токен VK ID, если всё же используется он
  VK_APP_ID         - ID приложения VK ID, нужен для обновления токена
  VK_DEVICE_ID      - device_id от VK ID, нужен для обновления токена
  VK_GROUP_ID       - числовой ID сообщества ВКонтакте (без минуса)
  VK_API_VERSION    - версия API (по умолчанию 5.199)
  VK_RATE_LIMIT     - запросов в секунду к VK API (по умолчанию 3 — лимит ВК
                      для пользовательского токена)

Фотографии ключу сообщества заливаются через сервер загрузки сообщений:
штатный photos.getWallUploadServer таким ключом вызвать нельзя (ошибка 27).
Проверить доступные способы: python vk_probe.py
"""

import logging
import os
import threading
import time
from typing import Optional

import requests
from dotenv import load_dotenv

import max_source
import vk_token
from max_source import MaxSource, State, apply_markup_to_text

load_dotenv()

max_source.setup_logging("crosspost_vk.log", "vk")
log = logging.getLogger(__name__)

# ─── Конфиг ──────────────────────────────────────────────────────────────────

TOKENS = vk_token.get_store(max_source.STATE_DIR)
VK_GROUP_ID = str(os.environ["VK_GROUP_ID"]).lstrip("-")
VK_API_VERSION = os.environ.get("VK_API_VERSION", "5.199")

VK_API = "https://api.vk.com/method"

# owner_id стены сообщества — отрицательный ID группы
VK_WALL_OWNER_ID = f"-{VK_GROUP_ID}"

# ─── VK API ───────────────────────────────────────────────────────────────────


# Ошибки авторизации VK: 5 - токен недействителен/протух, 27 - метод недоступен
# ключу сообщества (нужен пользовательский токен), 28 - протух ключ приложения
VK_AUTH_ERRORS = (5, 28)
VK_TOO_MANY_REQUESTS = 6
VK_GROUP_AUTH_DENIED = 27


class VkApiError(RuntimeError):
    """Ошибка VK API с кодом — по нему выбирается запасной способ загрузки."""

    def __init__(self, code: Optional[int], msg: str, hint: str = ""):
        self.code = code
        self.msg = msg
        super().__init__(f"VK API error {code}: {msg}{hint}")

# ВК разрешает пользовательскому токену 3 запроса в секунду. Пост с десятью
# фотографиями — это десяток загрузок подряд, и без паузы ВК отвечает ошибкой 6.
VK_RATE_LIMIT = float(os.environ.get("VK_RATE_LIMIT", "3"))
VK_MIN_INTERVAL = 1.0 / VK_RATE_LIMIT if VK_RATE_LIMIT > 0 else 0.0
VK_MAX_RETRIES = 3

_rate_lock = threading.Lock()
_last_call_at = 0.0


def _throttle() -> None:
    """Выдержать паузу между запросами к API.

    Блокировка общая на процесс: в режиме CROSSPOST_TARGET=all потоки ОК и ВК
    живут вместе, и считать интервал каждый сам по себе было бы неверно.
    """
    global _last_call_at
    if VK_MIN_INTERVAL <= 0:
        return
    with _rate_lock:
        wait = _last_call_at + VK_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def vk_call(method: str, params: dict) -> dict:
    """Выполнить запрос к VK API. Бросает RuntimeError на ошибку API.

    Токены VK ID живут около часа, поэтому на ошибке авторизации обновляем
    токен и повторяем запрос. Ошибка 6 (слишком часто) — не повод терять пост:
    ждём и пробуем снова.
    """
    refreshed = False
    for attempt in range(1, VK_MAX_RETRIES + 1):
        _throttle()
        resp = requests.post(
            f"{VK_API}/{method}",
            data={**params, "access_token": TOKENS.access_token, "v": VK_API_VERSION},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" not in data:
            log.debug("%s → %s", method, data.get("response"))
            return data["response"]

        err = data["error"]
        code = err.get("error_code")
        if code == VK_TOO_MANY_REQUESTS and attempt < VK_MAX_RETRIES:
            pause = VK_MIN_INTERVAL * 2 * attempt
            log.warning("ВК просит сбавить темп, ждём %.1f с и повторяем %s", pause, method)
            time.sleep(pause)
            continue
        if code in VK_AUTH_ERRORS and not refreshed and TOKENS.refresh():
            refreshed = True
            continue
        hint = ""
        if code in VK_AUTH_ERRORS and "another ip" in (err.get("error_msg") or ""):
            hint = (". Токен привязан к IP машины, где его получали — "
                    "используйте ключ сообщества, он к IP не привязан")
        raise VkApiError(code, err.get("error_msg"), hint)
    raise VkApiError(None, f"{method}: исчерпаны попытки из-за лимита запросов")


def _image_kind(img_data: bytes) -> tuple:
    """Имя файла и content-type по сигнатуре байтов.

    Расширение должно совпадать с содержимым: на .jpg с картинкой PNG внутри
    сервер загрузки отвечает пустым photo, и сохранение падает с ошибкой 100.
    """
    if img_data.startswith(b"\x89PNG"):
        return "photo.png", "image/png"
    if img_data.startswith(b"GIF8"):
        return "photo.gif", "image/gif"
    if img_data[8:12] == b"WEBP":
        return "photo.webp", "image/webp"
    return "photo.jpg", "image/jpeg"


def _upload_to_server(upload_url: str, img_data: bytes) -> dict:
    """Залить картинку на сервер загрузки и убедиться, что он принял её.

    Сервер отвечает 200 даже когда файл не принят — тогда в photo приходит
    пустая строка или "[]", и следом ВК отвечает "photo is undefined".
    """
    filename, content_type = _image_kind(img_data)
    resp = requests.post(
        upload_url, files={"photo": (filename, img_data, content_type)}, timeout=120
    )
    resp.raise_for_status()
    uploaded = resp.json()
    photo = uploaded.get("photo")
    if not photo or photo in ("[]", '""'):
        raise RuntimeError(f"сервер загрузки не принял файл ({len(img_data)} байт): {uploaded}")
    return uploaded


def _upload_photo_wall(img_data: bytes) -> list:
    """Штатный путь. Пользовательскому токену доступен, ключу сообщества — нет."""
    upload_url = vk_call("photos.getWallUploadServer", {"group_id": VK_GROUP_ID})["upload_url"]
    uploaded = _upload_to_server(upload_url, img_data)
    return vk_call("photos.saveWallPhoto", {
        "group_id": VK_GROUP_ID,
        "server": uploaded["server"],
        "photo": uploaded["photo"],
        "hash": uploaded["hash"],
    })


def _upload_photo_messages(img_data: bytes) -> list:
    """Обходной путь для ключа сообщества: сервер загрузки сообщений.

    photos.getWallUploadServer ключу сообщества запрещён (ошибка 27), а этот
    сервер доступен, и wall.post полученное вложение принимает.
    """
    upload_url = vk_call("photos.getMessagesUploadServer", {"peer_id": 0})["upload_url"]
    uploaded = _upload_to_server(upload_url, img_data)
    return vk_call("photos.saveMessagesPhoto", {
        "server": uploaded["server"],
        "photo": uploaded["photo"],
        "hash": uploaded["hash"],
    })


PHOTO_UPLOADERS = {"стену": _upload_photo_wall, "сообщения": _upload_photo_messages}

PHOTO_UPLOAD_ATTEMPTS = 3


def _upload_with_retries(uploader, img_data: bytes) -> list:
    """Повторить загрузку, если сервер не принял файл.

    Сервер загрузки периодически отвечает пустым photo — на посте из десяти
    фотографий так терялись три штуки. Каждая попытка берёт свежий upload_url:
    прежний к этому моменту мог уже протухнуть.
    """
    for attempt in range(1, PHOTO_UPLOAD_ATTEMPTS + 1):
        try:
            return uploader(img_data)
        except VkApiError:
            raise  # ошибки API разбирает вызывающий: там выбор способа загрузки
        except Exception as e:
            if attempt == PHOTO_UPLOAD_ATTEMPTS:
                raise
            log.warning("Попытка %d/%d не удалась (%s), повторяем",
                        attempt, PHOTO_UPLOAD_ATTEMPTS, e)
            time.sleep(attempt)
    return []

# Какой способ подошёл этому токену: определяется на первой фотографии,
# чтобы дальше не тратить лишний запрос на заведомо запрещённый метод
_photo_strategy: Optional[str] = None


def vk_upload_photo(image_url: str) -> Optional[str]:
    """
    Загрузить фото по URL и вернуть attachment вида photo<owner_id>_<id>.
    None — если скачать или залить не вышло.
    """
    try:
        img_data = requests.get(image_url, timeout=30).content
    except Exception as e:
        log.warning("Не удалось скачать фото %s: %s", image_url, e)
        return None

    global _photo_strategy
    names = [_photo_strategy] if _photo_strategy else list(PHOTO_UPLOADERS)
    for name in names:
        try:
            saved = _upload_with_retries(PHOTO_UPLOADERS[name], img_data)
        except VkApiError as e:
            if e.code == 27 and name != names[-1]:
                log.info("Загрузка через %s ключу сообщества запрещена, пробуем другой способ", name)
                continue
            log.warning("Ошибка загрузки фото в VK: %s", e)
            return None
        except Exception as e:
            log.warning("Ошибка загрузки фото в VK: %s", e)
            return None

        if not saved:
            log.warning("VK не вернул сохранённое фото для %s", image_url)
            return None
        if _photo_strategy != name:
            log.info("Фотографии заливаем через %s", name)
            _photo_strategy = name
        photo = saved[0]
        return f"photo{photo['owner_id']}_{photo['id']}"
    return None


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
    except VkApiError as e:
        if e.code in VK_AUTH_ERRORS or e.code == VK_GROUP_AUTH_DENIED:
            # Обхода, как для фотографий, у видео нет: video.save ключу
            # сообщества недоступен, а пользовательские токены ВК больше не выдаёт
            log.warning("Видео ключом сообщества загрузить нельзя (%s) — пост уйдёт без него", e)
        else:
            log.warning("Ошибка загрузки видео в VK: %s", e)
        return None
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


def check_token() -> None:
    """Проверить токен одним запросом до начала работы.

    Иначе неверный токен виден только как череда ошибок на каждой фотографии,
    и непонятно, дело в токене или в конкретном методе.
    """
    try:
        groups = vk_call("groups.getById", {"group_id": VK_GROUP_ID})
    except Exception as e:
        log.error(
            "Токен ВК не работает: %s. Если это токен из мини-приложения — он живёт "
            "около суток, откройте страницу выдачи токена заново. Если ключ сообщества — "
            "проверьте значение целиком, без кавычек и пробелов.", e
        )
        return
    # Ответ бывает списком (старые версии API) и объектом с groups
    items = groups.get("groups", groups) if isinstance(groups, dict) else groups
    name = items[0].get("name", "?") if items else "?"
    log.info("Токен ВК проверен: доступ к сообществу «%s» есть.", name)


def run_crosspost() -> None:
    check_token()
    source = MaxSource()
    state = State("posted_ids_vk.json", "marker_vk.txt")
    max_source.run_loop(source, state, vk_post_to_group, f"VK (сообщество {VK_GROUP_ID})")


if __name__ == "__main__":
    run_crosspost()
