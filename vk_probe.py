"""
Пробник: какими способами ключ сообщества может залить фото на стену.
=====================================================================
Токен VK ID (vk2.a.*) для методов API не годится вовсе — ошибки 1051 и 15.
Ключ сообщества методы вызывает, но photos.getWallUploadServer ему запрещён
(ошибка 27). Значит, нужно найти обходной путь загрузки, и этот скрипт
проверяет кандидатов по очереди на реальном сообществе.

Запуск (на любой машине, ключ сообщества к IP не привязан):

    python vk_probe.py                 # спросит ключ и ID сообщества
    python vk_probe.py vk1.a.... 239611995

Значения берутся также из переменных окружения VK_ACCESS_TOKEN и VK_GROUP_ID
или из файла .env рядом со скриптом — длинный ключ удобнее вставить в диалог
или положить в .env, чем задавать через set.

Скрипт заливает крошечную картинку, пробует опубликовать её тестовым постом
и сразу удаляет пост. В группе ничего не остаётся — но пост на несколько
секунд появится, так что запускайте, когда это не смущает.
"""

import base64
import os
import sys

import requests

try:  # .env читаем, если библиотека есть — она и так в зависимостях бота
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

VK_API = "https://api.vk.com/method"
TOKEN = ""
GROUP_ID = ""
API_VERSION = os.environ.get("VK_API_VERSION", "5.199")

# Картинка 1x1 пиксель — размер не важен, важна сама возможность залить
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


def read_settings(argv: list) -> tuple:
    """Ключ и ID сообщества: из аргументов, окружения, .env или диалога."""
    token = (argv[1] if len(argv) > 1 else os.environ.get("VK_ACCESS_TOKEN", "")).strip()
    group = (argv[2] if len(argv) > 2 else os.environ.get("VK_GROUP_ID", "")).strip()
    if not token:
        print("Ключ доступа сообщества (настройки группы → Работа с API → Ключи доступа).")
        token = input("Вставьте ключ: ").strip()
    if not group:
        group = input("ID сообщества (только цифры): ").strip()
    return token, group.lstrip("-")


def call(method: str, params: dict) -> dict:
    """Вызвать метод и вернуть {'response': ...} либо {'error': ...}."""
    resp = requests.post(
        f"{VK_API}/{method}",
        data={**params, "access_token": TOKEN, "v": API_VERSION},
        timeout=60,
    )
    return resp.json()


def describe(data: dict) -> str:
    if "error" in data:
        err = data["error"]
        return f"ОШИБКА {err.get('error_code')}: {err.get('error_msg')}"
    return "ок"


def upload(url: str, field: str, filename: str, content: bytes) -> dict:
    resp = requests.post(url, files={field: (filename, content, "image/jpeg")}, timeout=60)
    resp.raise_for_status()
    return resp.json()


def try_wall_upload() -> str:
    """Штатный путь: photos.getWallUploadServer + saveWallPhoto."""
    got = call("photos.getWallUploadServer", {"group_id": GROUP_ID})
    if "error" in got:
        return f"photos.getWallUploadServer → {describe(got)}"
    uploaded = upload(got["response"]["upload_url"], "photo", "p.jpg", TINY_JPEG)
    saved = call("photos.saveWallPhoto", {
        "group_id": GROUP_ID,
        "server": uploaded["server"],
        "photo": uploaded["photo"],
        "hash": uploaded["hash"],
    })
    if "error" in saved:
        return f"photos.saveWallPhoto → {describe(saved)}"
    photo = saved["response"][0]
    return f"OK attachment=photo{photo['owner_id']}_{photo['id']}"


def try_messages_upload() -> str:
    """Обходной путь: загрузка через сервер сообщений сообщества."""
    got = call("photos.getMessagesUploadServer", {"peer_id": 0})
    if "error" in got:
        return f"photos.getMessagesUploadServer → {describe(got)}"
    uploaded = upload(got["response"]["upload_url"], "photo", "p.jpg", TINY_JPEG)
    saved = call("photos.saveMessagesPhoto", {
        "server": uploaded["server"],
        "photo": uploaded["photo"],
        "hash": uploaded["hash"],
    })
    if "error" in saved:
        return f"photos.saveMessagesPhoto → {describe(saved)}"
    photo = saved["response"][0]
    return f"OK attachment=photo{photo['owner_id']}_{photo['id']}"


def try_album_upload() -> str:
    """Обходной путь: загрузка в альбом сообщества."""
    albums = call("photos.getAlbums", {"owner_id": f"-{GROUP_ID}"})
    if "error" in albums:
        return f"photos.getAlbums → {describe(albums)}"
    items = albums["response"].get("items") or []
    if not items:
        created = call("photos.createAlbum", {"title": "Кросспостинг", "group_id": GROUP_ID})
        if "error" in created:
            return f"photos.createAlbum → {describe(created)}"
        album_id = created["response"]["id"]
    else:
        album_id = items[0]["id"]

    got = call("photos.getUploadServer", {"album_id": album_id, "group_id": GROUP_ID})
    if "error" in got:
        return f"photos.getUploadServer → {describe(got)}"
    uploaded = upload(got["response"]["upload_url"], "file1", "p.jpg", TINY_JPEG)
    saved = call("photos.save", {
        "album_id": album_id,
        "group_id": GROUP_ID,
        "server": uploaded["server"],
        "photos_list": uploaded["photos_list"],
        "hash": uploaded["hash"],
    })
    if "error" in saved:
        return f"photos.save → {describe(saved)}"
    photo = saved["response"][0]
    return f"OK attachment=photo{photo['owner_id']}_{photo['id']}"


def try_post_and_delete(attachment: str) -> str:
    """Проверить, что ВК принимает такое вложение в пост, и убрать пост."""
    posted = call("wall.post", {
        "owner_id": f"-{GROUP_ID}",
        "from_group": 1,
        "message": "Проверка вложения, пост будет удалён автоматически",
        "attachments": attachment,
    })
    if "error" in posted:
        return f"wall.post → {describe(posted)}"
    post_id = posted["response"]["post_id"]
    deleted = call("wall.delete", {"owner_id": f"-{GROUP_ID}", "post_id": post_id})
    suffix = "" if "error" not in deleted else f" (удалить не вышло: {describe(deleted)}, id={post_id})"
    return f"wall.post принял вложение{suffix}"


def main() -> int:
    global TOKEN, GROUP_ID
    TOKEN, GROUP_ID = read_settings(sys.argv)
    if not TOKEN or not GROUP_ID:
        print("Нужны ключ сообщества и ID сообщества", file=sys.stderr)
        return 1

    print(f"Сообщество: {GROUP_ID}\nТокен: {TOKEN[:8]}...{TOKEN[-4:]}\n")

    whoami = call("groups.getById", {"group_id": GROUP_ID})
    print(f"groups.getById: {describe(whoami)}")

    for name, probe in (
        ("1. Штатная загрузка на стену", try_wall_upload),
        ("2. Через сервер сообщений", try_messages_upload),
        ("3. Через альбом сообщества", try_album_upload),
    ):
        print(f"\n{name}")
        try:
            result = probe()
        except Exception as e:
            result = f"исключение: {e}"
        print(f"   {result}")
        if result.startswith("OK attachment="):
            attachment = result.split("=", 1)[1]
            print(f"   {try_post_and_delete(attachment)}")

    print("\nГотово. Пришлите этот вывод целиком.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
