"""
Получение пользовательского токена ВК через VK ID (OAuth 2.1 + PKCE).
=====================================================================
Старый implicit flow (oauth.vk.com/authorize?response_type=token) для новых
приложений отдаёт invalid_request/invalid scope — ВК перевёл авторизацию на
VK ID. Здесь тот же путь, только руками в браузере его не пройти: нужен
code_verifier, который знает лишь наша сторона.

Запуск (локально, не на сервере — нужен браузер):

    python get_vk_token.py 54697320     # ID приложения аргументом
    python get_vk_token.py              # спросит ID при запуске

ID можно задать и переменной VK_APP_ID, но в PowerShell/cmd это отдельная
команда, поэтому аргумент проще.

Скрипт напечатает ссылку, вы открываете её в браузере под аккаунтом
администратора сообщества, разрешаете доступ, и вставляете сюда адрес
страницы, на которую вас перекинуло (целиком, вместе с ?code=...&device_id=...).

На выходе — access_token и refresh_token. Первый кладём в VK_ACCESS_TOKEN,
второй в VK_REFRESH_TOKEN, плюс VK_APP_ID и VK_DEVICE_ID: с этой четвёркой
бот обновляет протухший токен сам.

Переменные окружения:
  VK_APP_ID       - ID Standalone-приложения (client_id), обязателен
  VK_REDIRECT_URI - redirect_uri, ровно как в настройках приложения
                    (по умолчанию https://oauth.vk.ru/blank.html)
  VK_SCOPE        - права через пробел (по умолчанию wall photos video groups)
"""

import base64
import hashlib
import os
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import requests

VK_ID_AUTHORIZE_URL = "https://id.vk.com/authorize"
VK_ID_AUTH_URL = "https://id.vk.com/oauth2/auth"

APP_ID = os.environ.get("VK_APP_ID", "").strip()
REDIRECT_URI = os.environ.get("VK_REDIRECT_URI", "https://oauth.vk.ru/blank.html").strip()
SCOPE = os.environ.get("VK_SCOPE", "wall photos video groups").strip()


def make_pkce() -> tuple:
    """code_verifier и его S256-хеш: подтверждают, что код меняем мы, а не перехватчик."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def main() -> int:
    app_id = (sys.argv[1] if len(sys.argv) > 1 else APP_ID).strip()
    if not app_id:
        app_id = input("ID Standalone-приложения (client_id из dev.vk.ru): ").strip()
    if not app_id.isdigit():
        print(f"ID приложения должен быть числом, получено: {app_id!r}", file=sys.stderr)
        return 1

    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(16)

    url = f"{VK_ID_AUTHORIZE_URL}?" + urlencode({
        "response_type": "code",
        "client_id": app_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

    print("\n1) Откройте ссылку в браузере под аккаунтом администратора сообщества:\n")
    print(url)
    print("\n2) Разрешите доступ. Вас перекинет на страницу с ?code=... в адресе.")
    print("   Скопируйте адрес целиком и вставьте сюда.\n")

    redirected = input("Адрес после редиректа: ").strip()
    query = parse_qs(urlparse(redirected).query)
    code = (query.get("code") or [""])[0]
    device_id = (query.get("device_id") or [""])[0]
    got_state = (query.get("state") or [""])[0]

    if not code:
        print("В адресе нет параметра code. Скопируйте строку browser'а целиком.", file=sys.stderr)
        return 1
    if got_state and got_state != state:
        print("state не совпал — авторизация не та, начните заново.", file=sys.stderr)
        return 1

    resp = requests.post(VK_ID_AUTH_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": app_id,
        "device_id": device_id,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }, timeout=30)

    data = resp.json()
    if "access_token" not in data:
        print(f"\nVK ID вернул ошибку: {data}", file=sys.stderr)
        return 1

    print("\nГотово. Пропишите в переменные окружения сервера:\n")
    print(f"VK_ACCESS_TOKEN={data['access_token']}")
    print(f"VK_REFRESH_TOKEN={data.get('refresh_token', '')}")
    print(f"VK_DEVICE_ID={data.get('device_id', device_id)}")
    print(f"VK_APP_ID={app_id}")
    print(f"\nТокен действует {data.get('expires_in', '?')} сек, дальше бот обновит его сам.")
    print("Значения секретные: не публикуйте их и не коммитьте в репозиторий.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
