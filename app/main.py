"""
Точка входа для платформы relaxdev (она запускает `python app/main.py`).
Реальная логика — в max_to_ok_crosspost.py / max_to_vk_crosspost.py в корне.

Площадка выбирается переменной CROSSPOST_TARGET:
  ok   - только Одноклассники (значение по умолчанию)
  vk   - только ВКонтакте
  all  - обе сразу, каждая в своём потоке одного процесса
"""
import logging
import os
import sys
import threading

# Корень репозитория (на уровень выше каталога app/), где лежат модули бота
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

RAW_TARGET = os.environ.get("CROSSPOST_TARGET", "")
TARGET = RAW_TARGET.strip().lower() or "ok"

log = logging.getLogger(__name__)


def run_all() -> None:
    """Поднять ОК и ВК в одном процессе, каждый в своём потоке.

    Состояние площадок лежит в разных файлах (posted_ids.json против
    posted_ids_vk.json), поэтому общий STATE_DIR им не мешает.

    Если один из потоков падает, процесс завершается с ненулевым кодом целиком:
    так супервизор (docker restart / платформа) перезапустит обе площадки,
    а не оставит висеть половину бота без единой строчки в логах.
    """
    from max_to_ok_crosspost import run_crosspost as run_ok
    from max_to_vk_crosspost import run_crosspost as run_vk

    died = threading.Event()

    def guard(name: str, func):
        def wrapper():
            try:
                func()
            except Exception:
                log.exception("Поток %s аварийно завершился", name)
            else:
                log.error("Поток %s неожиданно завершился без ошибки", name)
            finally:
                # finally, а не просто вызов: иначе KeyboardInterrupt/SystemExit
                # внутри потока оставит процесс висеть навсегда
                died.set()
        return wrapper

    for name, func in (("ok", run_ok), ("vk", run_vk)):
        threading.Thread(target=guard(name, func), name=name, daemon=True).start()
        log.info("Поток %s запущен", name)

    died.wait()
    log.error("Одна из площадок остановилась — завершаем процесс для перезапуска.")
    sys.exit(1)


if TARGET == "all":
    run_crosspost = run_all
elif TARGET == "vk":
    from max_to_vk_crosspost import run_crosspost
elif TARGET == "ok":
    from max_to_ok_crosspost import run_crosspost
else:
    raise SystemExit(
        f"Неизвестный CROSSPOST_TARGET={RAW_TARGET!r}, ожидается 'ok', 'vk' или 'all'"
    )

if __name__ == "__main__":
    if not RAW_TARGET.strip():
        # Тихий дефолт однажды уже увёл бэкфилл не в ту соцсеть — теперь это видно в логах
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
        log.warning("CROSSPOST_TARGET не задан, используется 'ok'. Укажите 'vk' или 'all' явно.")
    run_crosspost()
