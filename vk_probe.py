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

Скрипт сначала показывает вложения последних записей сообщества, затем для
каждого способа заливает картинку 1x1, публикует тестовый пост и перечитывает
его через wall.getById: только так видно, осталось вложение на стене или ВК
принял его и молча выбросил. Тестовые посты остаются в группе — wall.delete
ключу сообщества запрещён, удалите их вручную.
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


def try_post_and_check(attachment: str) -> str:
    """Опубликовать пост и проверить, что вложение реально осталось на стене.

    wall.post отвечает успехом и на вложения, которые затем не показываются:
    ответ API — не доказательство, нужно перечитать пост через wall.getById.
    """
    posted = call("wall.post", {
        "owner_id": f"-{GROUP_ID}",
        "from_group": 1,
        "message": "Проверка вложения",
        "attachments": attachment,
    })
    if "error" in posted:
        return f"wall.post → {describe(posted)}"
    post_id = posted["response"]["post_id"]

    got = call("wall.getById", {"posts": f"-{GROUP_ID}_{post_id}"})
    if "error" in got:
        return f"опубликован id={post_id}, но проверить не вышло: {describe(got)}"
    items = got["response"].get("items", got["response"])
    attachments = items[0].get("attachments", []) if items else []
    kinds = [a.get("type") for a in attachments]
    verdict = f"ВЛОЖЕНИЕ НА СТЕНЕ: {kinds}" if kinds else "ВЛОЖЕНИЕ ПОТЕРЯНО (на стене пусто)"
    return f"{verdict}, пост id={post_id} — удалите вручную"


def try_docs_upload() -> str:
    """Обходной путь: залить картинку документом на стену сообщества."""
    got = call("docs.getWallUploadServer", {"group_id": GROUP_ID})
    if "error" in got:
        return f"docs.getWallUploadServer → {describe(got)}"
    resp = requests.post(
        got["response"]["upload_url"],
        files={"file": ("photo.jpg", TINY_JPEG, "image/jpeg")},
        timeout=60,
    )
    resp.raise_for_status()
    saved = call("docs.save", {"file": resp.json()["file"], "title": "photo"})
    if "error" in saved:
        return f"docs.save → {describe(saved)}"
    doc = saved["response"]
    doc = doc.get("doc", doc) if isinstance(doc, dict) else doc[0]
    return f"OK attachment=doc{doc['owner_id']}_{doc['id']}"


def show_recent_posts() -> None:
    """Показать типы вложений последних записей — видно, доехали ли фотографии."""
    got = call("wall.get", {"owner_id": f"-{GROUP_ID}", "count": 5})
    if "error" in got:
        print(f"wall.get: {describe(got)}")
        return
    print("Последние записи сообщества:")
    for item in got["response"].get("items", []):
        kinds = [a.get("type") for a in item.get("attachments", [])]
        text = (item.get("text") or "")[:40]
        print(f"   id={item['id']:>4}  вложения={kinds or 'нет'}  «{text}»")


def main() -> int:
    global TOKEN, GROUP_ID
    TOKEN, GROUP_ID = read_settings(sys.argv)
    if not TOKEN or not GROUP_ID:
        print("Нужны ключ сообщества и ID сообщества", file=sys.stderr)
        return 1

    print(f"Сообщество: {GROUP_ID}\nТокен: {TOKEN[:8]}...{TOKEN[-4:]}\n")

    whoami = call("groups.getById", {"group_id": GROUP_ID})
    print(f"groups.getById: {describe(whoami)}\n")
    show_recent_posts()

    for name, probe in (
        ("1. Штатная загрузка на стену", try_wall_upload),
        ("2. Через сервер сообщений", try_messages_upload),
        ("3. Через альбом сообщества", try_album_upload),
        ("4. Документом на стену", try_docs_upload),
    ):
        print(f"\n{name}")
        try:
            result = probe()
        except Exception as e:
            result = f"исключение: {e}"
        print(f"   {result}")
        if result.startswith("OK attachment="):
            attachment = result.split("=", 1)[1]
            print(f"   {try_post_and_check(attachment)}")

    print("\nГотово. Пришлите этот вывод целиком.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
