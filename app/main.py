"""
Точка входа для платформы relaxdev (она запускает `python app/main.py`).
Реальная логика — в max_to_ok_crosspost.py / max_to_vk_crosspost.py в корне.
Площадка выбирается переменной CROSSPOST_TARGET (ok | vk), по умолчанию ok.
"""
import os
import sys

# Корень репозитория (на уровень выше каталога app/), где лежат модули бота
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TARGET = os.environ.get("CROSSPOST_TARGET", "ok").lower()

if TARGET == "vk":
    from max_to_vk_crosspost import run_crosspost
elif TARGET == "ok":
    from max_to_ok_crosspost import run_crosspost
else:
    raise SystemExit(f"Неизвестный CROSSPOST_TARGET={TARGET!r}, ожидается 'ok' или 'vk'")

if __name__ == "__main__":
    run_crosspost()
