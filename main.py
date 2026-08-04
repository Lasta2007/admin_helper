from pathlib import Path
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from database import init_db, get_networks, get_setting
from api import router, ping_all_hosts_parallel, logger


# Глобальная переменная для управления фоновой задачей
background_task_running = False
background_task = None


async def background_ping_task():
    """Фоновая задача для периодического пинга всех подсетей."""
    global background_task_running
    
    logger.info("[background_ping_task] Фоновая задача пинга запущена")
    
    while background_task_running:
        try:
            # Получаем интервал из настроек (в минутах)
            interval_minutes = int(get_setting("ping_interval", "60"))
            timeout = int(get_setting("ping_timeout", "3"))
            
            # Конвертируем минуты в секунды
            interval_seconds = interval_minutes * 60
            
            logger.info(f"[background_ping_task] Начало фонового пинга всех подсетей (интервал: {interval_minutes} мин)")
            
            # Получаем все подсети
            networks = get_networks()
            
            for network in networks:
                try:
                    logger.info(f"[background_ping_task] Пинг подсети {network['cidr']} (ID={network['id']})")
                    
                    # Генерируем все хосты из подсети
                    import ipaddress
                    net = ipaddress.ip_network(network["cidr"], strict=False)
                    hosts_to_ping = [{"ip": str(ip)} for ip in net.hosts()]
                    
                    # Выполняем пинг
                    await ping_all_hosts_parallel(hosts_to_ping, network["id"], timeout)
                    
                except Exception as e:
                    logger.error(f"[background_ping_task] Ошибка при пинге подсети {network['cidr']}: {e}")
            
            logger.info(f"[background_ping_task] Фоновый пинг завершен. Следующий через {interval_minutes} мин")
            
            # Ждем следующий интервал (в секундах)
            await asyncio.sleep(interval_seconds)
            
        except asyncio.CancelledError:
            logger.info("[background_ping_task] Задача отменена")
            break
        except Exception as e:
            logger.error(f"[background_ping_task] Критическая ошибка: {e}")
            await asyncio.sleep(10)  # Пауза перед повторной попыткой


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan контекст для запуска/остановки фоновых задач."""
    global background_task_running, background_task
    
    # Запуск при старте приложения
    logger.info("[lifespan] Запуск приложения, старт фоновой задачи пинга")
    background_task_running = True
    background_task = asyncio.create_task(background_ping_task())
    
    yield
    
    # Остановка при завершении приложения
    logger.info("[lifespan] Остановка приложения, остановка фоновой задачи пинга")
    background_task_running = False
    
    if background_task:
        background_task.cancel()
        try:
            await background_task
        except asyncio.CancelledError:
            pass
    
    logger.info("[lifespan] Фоновая задача пинга остановлена")


# Создаем таблицы при запуске приложения
init_db()

app = FastAPI(
    title="Admin Helper",
    version="0.3.0",
    lifespan=lifespan
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