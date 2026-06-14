"""
Точка входа для платформы relaxdev (она запускает `python app/main.py`).
Реальная логика — в max_to_ok_crosspost.py в корне репозитория.
Этот файл лишь добавляет корень в sys.path и стартует основной цикл.
"""
import os
import sys

# Корень репозитория (на уровень выше каталога app/), где лежит модуль бота
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from max_to_ok_crosspost import run_crosspost

if __name__ == "__main__":
    run_crosspost()
