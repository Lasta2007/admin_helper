"""
Модуль WORK PC - парсинг файла log.txt с информацией о рабочих компьютерах.
Файл содержит данные разделенные знаком |.
Поля: Дата, Версия ОС, Версия ядра, Имя компьютера, Имя пользователя, 
Тип получения IP адреса, IP адрес, Mac адрес, Материнская плата, 
Свободное место HDD, SWAP, Процессор, Тип диска, Версия R7, Версия KAV, Версия CSP
"""

import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

from database import get_connection, get_setting, set_setting

logger = logging.getLogger('admin_helper')

# Поля файла log.txt
LOG_FIELDS = [
    'date',              # Дата
    'os_version',        # Версия ОС
    'kernel_version',    # Версия ядра
    'computer_name',     # Имя компьютера
    'username',          # Имя пользователя
    'ip_type',           # Тип получения IP адреса
    'ip_address',        # IP адрес
    'mac_address',       # Mac адрес
    'motherboard',       # Материнская плата
    'hdd_free',          # Свободное место HDD
    'swap',              # SWAP
    'cpu',               # Процессор
    'disk_type',         # Тип диска
    'r7_version',        # Версия R7
    'kav_version',       # Версия KAV
    'csp_version',       # Версия CSP
]


def parse_log_line(line: str) -> Optional[Dict[str, str]]:
    """
    Парсит одну строку из файла log.txt.
    Возвращает словарь с данными или None если строка некорректна.
    """
    line = line.strip()
    if not line:
        return None
    
    parts = line.split('|')
    
    # Ожидаем 15 или 16 полей (cprocsp может быть пустым в конце)
    if len(parts) < 15:
        logger.warning(f"[parse_log_line] Некорректная строка (ожидается минимум 15 полей, получено {len(parts)}): {line[:100]}")
        return None
    
    # Создаем словарь с данными
    data = {}
    for i, field in enumerate(LOG_FIELDS):
        if i < len(parts):
            data[field] = parts[i].strip()
        else:
            data[field] = ''
    
    return data


def parse_log_file(file_path: str) -> List[Dict[str, str]]:
    """
    Парсит весь файл log.txt.
    Возвращает список словарей с данными.
    """
    result = []
    
    try:
        path = Path(file_path)
        if not path.exists():
            logger.error(f"[parse_log_file] Файл не найден: {file_path}")
            return result
        
        with open(path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                data = parse_log_line(line)
                if data:
                    result.append(data)
        
        logger.info(f"[parse_log_file] Успешно распарсено {len(result)} записей из файла {file_path}")
        
    except Exception as e:
        logger.error(f"[parse_log_file] Ошибка при чтении файла {file_path}: {type(e).__name__}: {e}")
    
    return result


def init_work_pc_table():
    """
    Инициализирует таблицу work_pc в базе данных.
    Вызывается при миграции БД.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Проверяем существование таблицы
    existing_tables = cursor.execute("""
        SELECT name FROM sqlite_master WHERE type='table' AND name='work_pc'
    """).fetchall()
    
    if not existing_tables:
        cursor.execute("""
        CREATE TABLE work_pc(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT DEFAULT '',
            os_version TEXT DEFAULT '',
            kernel_version TEXT DEFAULT '',
            computer_name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            ip_type TEXT DEFAULT '',
            ip_address TEXT DEFAULT '',
            mac_address TEXT DEFAULT '',
            motherboard TEXT DEFAULT '',
            hdd_free TEXT DEFAULT '',
            swap TEXT DEFAULT '',
            cpu TEXT DEFAULT '',
            disk_type TEXT DEFAULT '',
            r7_version TEXT DEFAULT '',
            kav_version TEXT DEFAULT '',
            csp_version TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        )
        """)
        logger.info("[init_work_pc_table] Таблица 'work_pc' создана")
    
    conn.commit()
    conn.close()
    logger.info("[init_work_pc_table] Инициализация таблицы work_pc завершена")


def get_work_pc_data() -> List[Dict[str, Any]]:
    """
    Получает все данные из таблицы work_pc.
    """
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    
    rows = conn.execute("""
        SELECT *
        FROM work_pc
        ORDER BY date DESC, computer_name
    """).fetchall()
    
    conn.close()
    
    return [dict(r) for r in rows]


def get_work_pc_by_computer(computer_name: str) -> List[Dict[str, Any]]:
    """
    Получает данные по конкретному компьютеру.
    """
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    
    rows = conn.execute("""
        SELECT *
        FROM work_pc
        WHERE computer_name=?
        ORDER BY date DESC
    """, (computer_name,)).fetchall()
    
    conn.close()
    
    return [dict(r) for r in rows]


def save_work_pc_data(data: List[Dict[str, str]]) -> int:
    """
    Сохраняет данные в таблицу work_pc.
    Перед сохранением очищает таблицу.
    Возвращает количество сохраненных записей.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Очищаем таблицу перед новым импортом
    cursor.execute("DELETE FROM work_pc")
    logger.info("[save_work_pc_data] Таблица work_pc очищена")
    
    # Вставляем новые данные
    count = 0
    for record in data:
        try:
            cursor.execute("""
                INSERT INTO work_pc(
                    date, os_version, kernel_version, computer_name, username,
                    ip_type, ip_address, mac_address, motherboard, hdd_free,
                    swap, cpu, disk_type, r7_version, kav_version, csp_version,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.get('date', ''),
                record.get('os_version', ''),
                record.get('kernel_version', ''),
                record.get('computer_name', ''),
                record.get('username', ''),
                record.get('ip_type', ''),
                record.get('ip_address', ''),
                record.get('mac_address', ''),
                record.get('motherboard', ''),
                record.get('hdd_free', ''),
                record.get('swap', ''),
                record.get('cpu', ''),
                record.get('disk_type', ''),
                record.get('r7_version', ''),
                record.get('kav_version', ''),
                record.get('csp_version', ''),
                now
            ))
            count += 1
        except Exception as e:
            logger.error(f"[save_work_pc_data] Ошибка при сохранении записи: {e}")
    
    conn.commit()
    conn.close()
    
    logger.info(f"[save_work_pc_data] Сохранено {count} записей в таблицу work_pc")
    return count


def refresh_work_pc_data() -> Dict[str, Any]:
    """
    Обновляет данные в таблице work_pc из файла log.txt.
    Путь к файлу берется из настроек.
    """
    # Получаем путь к файлу из настроек
    log_path = get_setting('work_pc_log_path', '')
    
    if not log_path:
        logger.error("[refresh_work_pc_data] Путь к файлу log.txt не указан в настройках")
        return {
            "status": "error",
            "message": "Путь к файлу log.txt не указан в настройках"
        }
    
    # Проверяем существование файла
    if not Path(log_path).exists():
        logger.error(f"[refresh_work_pc_data] Файл не найден: {log_path}")
        return {
            "status": "error",
            "message": f"Файл не найден: {log_path}"
        }
    
    # Парсим файл
    data = parse_log_file(log_path)
    
    if not data:
        logger.warning(f"[refresh_work_pc_data] Нет данных для сохранения из файла {log_path}")
        return {
            "status": "warning",
            "message": "Файл пуст или содержит некорректные данные",
            "records_count": 0
        }
    
    # Сохраняем данные в БД
    count = save_work_pc_data(data)
    
    return {
        "status": "ok",
        "message": f"Успешно обновлено {count} записей",
        "records_count": count,
        "log_path": log_path
    }


def get_work_pc_settings() -> Dict[str, str]:
    """
    Получает настройки модуля WORK PC.
    """
    return {
        'log_path': get_setting('work_pc_log_path', ''),
        'update_interval': get_setting('work_pc_update_interval', '60')
    }


def set_work_pc_log_path(path: str) -> bool:
    """
    Устанавливает путь к файлу log.txt в настройках.
    """
    try:
        set_setting('work_pc_log_path', path)
        logger.info(f"[set_work_pc_log_path] Путь к файлу установлен: {path}")
        return True
    except Exception as e:
        logger.error(f"[set_work_pc_log_path] Ошибка при сохранении пути: {e}")
        return False


def set_work_pc_update_interval(interval: int) -> bool:
    """
    Устанавливает период обновления данных из файла log.txt (в минутах).
    """
    try:
        set_setting('work_pc_update_interval', str(interval))
        logger.info(f"[set_work_pc_update_interval] Период обновления установлен: {interval} мин")
        return True
    except Exception as e:
        logger.error(f"[set_work_pc_update_interval] Ошибка при сохранении периода: {e}")
        return False


# Импортируем sqlite3 для корректной работы row_factory
import sqlite3
