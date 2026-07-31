"""
Токены VK ID: хранение на диске и автоматическое обновление.
===========================================================
Старый implicit flow (response_type=token) для новых приложений отключён,
поэтому пользовательский токен выдаётся через VK ID:
  - живёт около часа и обновляется по refresh_token;
  - refresh_token при каждом обновлении тоже меняется, старый перестаёт работать.

Отсюда требование: пара токенов хранится в файле VK_TOKEN_FILE внутри STATE_DIR,
а не только в переменных окружения — иначе после первого же обновления рестарт
бота остался бы со старым, уже недействительным refresh_token.

Переменные окружения:
  VK_ACCESS_TOKEN   - токен доступа (используется, если файла ещё нет)
  VK_REFRESH_TOKEN  - refresh-токен от VK ID (нужен для автообновления)
  VK_APP_ID         - ID Standalone-приложения (client_id)
  VK_DEVICE_ID      - device_id, выданный VK ID вместе с токеном
  VK_TOKEN_FILE     - имя файла с токенами внутри STATE_DIR (vk_token.json)
  VK_AUTH_CODE      - одноразовый код авторизации: бот сам обменяет его на
                      токены при первом старте (для хостинга без консоли)
  VK_CODE_VERIFIER  - PKCE-verifier к этому коду
  VK_REDIRECT_URI   - redirect_uri, с которым получали код
"""

import json
import logging
import os
import threading
import time
from typing import Optional

import requests

VK_ID_AUTH_URL = "https://id.vk.com/oauth2/auth"
DEFAULT_REDIRECT_URI = "https://oauth.vk.com/blank.html"

log = logging.getLogger(__name__)

_lock = threading.Lock()


class VkTokenStore:
    """Пара access/refresh токенов VK ID с обновлением по требованию."""

    def __init__(self, state_dir: str):
        self.path = os.path.join(state_dir, os.environ.get("VK_TOKEN_FILE", "vk_token.json"))
        self.app_id = os.environ.get("VK_APP_ID", "").strip()
        data = self._load()
        self.access_token: str = data.get("access_token") or os.environ.get("VK_ACCESS_TOKEN", "").strip()
        self.refresh_token: str = data.get("refresh_token") or os.environ.get("VK_REFRESH_TOKEN", "").strip()
        self.device_id: str = data.get("device_id") or os.environ.get("VK_DEVICE_ID", "").strip()
        if not data and os.environ.get("VK_AUTH_CODE", "").strip():
            self._bootstrap_from_code()
        if not self.access_token:
            raise SystemExit(
                "Не задан токен ВК: укажите VK_ACCESS_TOKEN или пару VK_AUTH_CODE + "
                "VK_CODE_VERIFIER (получить: python get_vk_token.py <APP_ID> --for-server)"
            )
        if self.can_refresh():
            log.info("Токен ВК: автообновление включено (VK ID).")
        else:
            log.warning(
                "Токен ВК: автообновление выключено (нет VK_REFRESH_TOKEN/VK_APP_ID/VK_DEVICE_ID). "
                "Токен VK ID живёт около часа — после протухания бот остановится на ошибке авторизации."
            )

    def _bootstrap_from_code(self) -> None:
        """Обменять код авторизации на токены при первом старте.

        ВК привязывает токен к IP той машины, которая делает этот обмен, поэтому
        на хостинге без консоли обмен выполняет сам бот: код и code_verifier
        приезжают в переменных окружения, а запрос уходит с адреса сервера.

        Код одноразовый и живёт считанные минуты — после успеха токены лежат в
        vk_token.json, и переменные VK_AUTH_CODE/VK_CODE_VERIFIER больше не нужны.
        """
        code = os.environ.get("VK_AUTH_CODE", "").strip()
        verifier = os.environ.get("VK_CODE_VERIFIER", "").strip()
        redirect_uri = os.environ.get("VK_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip()
        if not verifier or not self.app_id:
            log.error("Для обмена кода нужны VK_CODE_VERIFIER и VK_APP_ID — пропускаем.")
            return

        log.info("Меняем код авторизации на токены ВК...")
        try:
            resp = requests.post(VK_ID_AUTH_URL, data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": self.app_id,
                "device_id": self.device_id,
                "redirect_uri": redirect_uri,
                "state": os.urandom(16).hex(),
            }, timeout=30)
            data = resp.json()
        except Exception as e:
            log.error("Ошибка сети при обмене кода: %s", e)
            return

        if "access_token" not in data:
            log.error(
                "VK ID не принял код: %s. Код одноразовый и быстро протухает — "
                "получите новый (python get_vk_token.py <APP_ID> --for-server).", data
            )
            return

        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.device_id = data.get("device_id", self.device_id)
        self._save()
        log.info("Код обменян, токены сохранены в %s", self.path)

    def can_refresh(self) -> bool:
        return bool(self.refresh_token and self.app_id and self.device_id)

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            log.warning("Не удалось прочитать %s: %s", self.path, e)
            return {}

    def _save(self) -> None:
        payload = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "device_id": self.device_id,
            "updated_at": int(time.time()),
        }
        tmp = f"{self.path}.tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)  # атомарно: рестарт посреди записи не оставит битый файл
            os.chmod(self.path, 0o600)
        except Exception as e:
            log.error("Не удалось сохранить токены в %s: %s", self.path, e)

    def refresh(self) -> bool:
        """Обновить пару токенов. True — обновились, False — обновить нечем/не вышло."""
        with _lock:
            if not self.can_refresh():
                return False
            log.info("Обновляем токен ВК по refresh_token...")
            try:
                resp = requests.post(
                    VK_ID_AUTH_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self.refresh_token,
                        "client_id": self.app_id,
                        "device_id": self.device_id,
                        "state": os.urandom(16).hex(),
                    },
                    timeout=30,
                )
                data = resp.json()
            except Exception as e:
                log.error("Ошибка сети при обновлении токена ВК: %s", e)
                return False

            if "access_token" not in data:
                log.error("VK ID не вернул токен: %s", data)
                return False

            self.access_token = data["access_token"]
            # refresh_token одноразовый: если пришёл новый — старый уже недействителен
            self.refresh_token = data.get("refresh_token", self.refresh_token)
            self.device_id = data.get("device_id", self.device_id)
            self._save()
            log.info("Токен ВК обновлён (действует %s сек).", data.get("expires_in", "?"))
            return True


_store: Optional[VkTokenStore] = None


def get_store(state_dir: str) -> VkTokenStore:
    """Единое хранилище токенов на процесс (в режиме all потоки делят его)."""
    global _store
    if _store is None:
        _store = VkTokenStore(state_dir)
    return _store
