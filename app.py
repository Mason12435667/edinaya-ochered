"""Запускает сервер. Служба и README запускают `python app.py` отсюда."""
import runpy
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent / "backend"
sys.path.insert(0, str(BACKEND))
runpy.run_path(str(BACKEND / "app.py"), run_name="__main__")
