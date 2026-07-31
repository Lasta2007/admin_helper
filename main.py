from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from database import init_db
from api import router


# Создаем таблицы при запуске приложения
init_db()

app = FastAPI(
    title="Admin Helper",
    version="0.3.0"
)

# Пока разрешаем любые источники.
# Позже можно ограничить только локальной сетью.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключаем REST API
app.include_router(router)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": "Admin Helper",
        "version": "0.3.0"
    }


# Каталог со статикой
STATIC_DIR = Path(__file__).parent / "static"

if STATIC_DIR.exists():
    app.mount(
        "/",
        StaticFiles(
            directory=STATIC_DIR,
            html=True
        ),
        name="static"
    )
else:
    @app.get("/")
    def root():
        return {
            "message": "Static directory not found"
        }