"""
Получение пользовательского токена ВК через VK ID (OAuth 2.1 + PKCE).
=====================================================================
Старый implicit flow (oauth.vk.com/authorize?response_type=token) ВК закрыл:
новым приложениям он отвечает invalid scope. Действующий путь — VK ID, и
пройти его руками в браузере нельзя: нужен code_verifier, который знает
только наша сторона.

По умолчанию используется redirect https://oauth.vk.com/blank.html — он
зарегистрирован у приложений сразу, настраивать в консоли ничего не нужно.
После согласия ВК перекинет на пустую страницу, а адрес из строки браузера
нужно вставить обратно в скрипт: код авторизации лежит в нём.

Если в настройках приложения удастся прописать доверенный redirect на
localhost, всё делается без копипаста — скрипт поднимет локальный сервер
и поймает редирект сам:

    set VK_REDIRECT_URI=http://localhost
    python get_vk_token.py 54697320

Запуск (на своей машине, не на сервере — нужен браузер):

    python get_vk_token.py 54697320     # ID приложения аргументом
    python get_vk_token.py              # спросит ID при запуске

Дальше откроется браузер, вы жмёте «Разрешить» — и скрипт печатает готовые
значения переменных. Флаги --no-scope и --scope "wall,photos" помогают понять,
на что ругается VK ID, если вместо формы согласия показывает "Error loading".

На выходе — access_token и refresh_token. Первый кладём в VK_ACCESS_TOKEN,
второй в VK_REFRESH_TOKEN, плюс VK_APP_ID и VK_DEVICE_ID: с этой четвёркой
бот обновляет протухший токен сам.

Переменные окружения:
  VK_APP_ID       - ID приложения (client_id), можно передать аргументом
  VK_REDIRECT_URI - redirect_uri ровно как в настройках приложения
                    (по умолчанию https://oauth.vk.com/blank.html)
  VK_PORT         - порт локального сервера-ловушки, если redirect на localhost
  VK_SCOPE        - права через запятую (по умолчанию wall,photos,video,groups)
"""

import base64
import hashlib
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

VK_ID_AUTHORIZE_URL = "https://id.vk.com/authorize"
VK_ID_AUTH_URL = "https://id.vk.com/oauth2/auth"

APP_ID = os.environ.get("VK_APP_ID", "").strip()
# Стандартный redirect зарегистрирован у приложений по умолчанию — с ним не нужно
# ничего настраивать в консоли. Ловушка на localhost включается через VK_REDIRECT_URI.
REDIRECT_URI = os.environ.get("VK_REDIRECT_URI", "https://oauth.vk.com/blank.html").strip()
# Через запятую, а не через пробел: на пробелы VK ID отвечает страницей "Error loading"
SCOPE = os.environ.get("VK_SCOPE", "wall,photos,video,groups").strip()
PORT = int(os.environ.get("VK_PORT", "80"))

PAGE_OK = "<h2>Готово. Токен получен, окно можно закрыть.</h2>"
PAGE_FAIL = "<h2>В адресе нет кода авторизации. Вернитесь в консоль.</h2>"


def make_pkce() -> tuple:
    """code_verifier и его S256-хеш: подтверждают, что код меняем мы, а не перехватчик."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def build_authorize_url(app_id: str, challenge: str, state: str, scope: str) -> str:
    params = {
        "response_type": "code",
        "client_id": app_id,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scope:  # пустой scope — диагностика: так VK ID не может ругаться на права
        params["scope"] = scope
    return f"{VK_ID_AUTHORIZE_URL}?" + urlencode(params)


def parse_args(argv: list) -> tuple:
    """Разобрать аргументы: ID приложения, scope и режим диагностики.

    Полезно, когда VK ID показывает 'Error loading' без объяснений: запуск
    с --no-scope отвечает на вопрос, в правах дело или в настройках приложения.
    """
    app_id, scope = "", SCOPE
    args = argv[1:]
    while args:
        arg = args.pop(0)
        if arg == "--no-scope":
            scope = ""
        elif arg == "--scope":
            scope = args.pop(0) if args else ""
        elif not app_id:
            app_id = arg.strip()
    return app_id, scope


class CatchHandler(BaseHTTPRequestHandler):
    """Принимает единственный запрос — редирект VK ID с ?code=... в адресе."""

    query: dict = {}

    def do_GET(self):
        CatchHandler.query = parse_qs(urlparse(self.path).query)
        body = (PAGE_OK if CatchHandler.query.get("code") else PAGE_FAIL).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # без служебного шума в консоли
        pass


def catch_redirect(url: str, timeout: int = 300) -> dict:
    """Поднять локальный сервер, открыть браузер и дождаться редиректа.

    Возвращает разобранный query редиректа либо {} — тогда вызывающий код
    откатывается на ручной ввод адреса.
    """
    try:
        server = HTTPServer(("127.0.0.1", PORT), CatchHandler)
    except OSError as e:
        print(f"\nНе удалось занять порт {PORT}: {e}", file=sys.stderr)
        print("Порт занят или нужен запуск от администратора. "
              "Можно указать другой: VK_PORT (и такой же redirect в настройках ВК).",
              file=sys.stderr)
        return {}

    CatchHandler.query = {}
    # Один запрос и выходим: больше редиректов не будет, висеть сервером незачем
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("\nОткрываю браузер. Войдите под аккаунтом администратора сообщества "
          "и разрешите доступ.")
    print("Если браузер не открылся, откройте ссылку вручную:\n")
    print(url + "\n")
    webbrowser.open(url)

    thread.join(timeout)
    server.server_close()
    if not CatchHandler.query:
        print("Не дождались редиректа.", file=sys.stderr)
    return CatchHandler.query


def show_link(url: str) -> dict:
    """Просто открыть ссылку: код придётся забрать из адресной строки вручную."""
    print("\nОткрываю браузер. Войдите под аккаунтом администратора сообщества "
          "и разрешите доступ.")
    print("Если браузер не открылся, откройте ссылку вручную:\n")
    print(url + "\n")
    webbrowser.open(url)
    return {}


def ask_redirect_manually() -> dict:
    """Запасной путь: пользователь вставляет адрес из браузера сам."""
    print("\nВставьте адрес, на который вас перекинуло (целиком, с ?code=...):")
    return parse_qs(urlparse(input("Адрес после редиректа: ").strip()).query)


def main() -> int:
    app_id, scope = parse_args(sys.argv)
    app_id = app_id or APP_ID
    if not app_id:
        app_id = input("ID приложения (client_id из dev.vk.ru): ").strip()
    if not app_id.isdigit():
        print(f"ID приложения должен быть числом, получено: {app_id!r}", file=sys.stderr)
        return 1

    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(app_id, challenge, state, scope)
    print(f"\nredirect_uri: {REDIRECT_URI}\nscope: {scope or '(не запрашиваем)'}")

    # Ловушка имеет смысл только для localhost: на blank.html редирект уходит
    # к ВК, и адрес с кодом остаётся лишь в адресной строке браузера
    local = urlparse(REDIRECT_URI).hostname in ("localhost", "127.0.0.1")
    query = (catch_redirect(url) if local else show_link(url)) or ask_redirect_manually()

    code = (query.get("code") or [""])[0]
    device_id = (query.get("device_id") or [""])[0]
    got_state = (query.get("state") or [""])[0]

    if not code:
        print("\nКод авторизации не получен. Проверьте, что в настройках приложения "
              f"доверенный redirect URL — ровно {REDIRECT_URI}", file=sys.stderr)
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
