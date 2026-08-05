"""
Модуль WORK PC - парсинг файла log.txt с информацией о рабочих компьютерах.
Файл содержит данные разделенные знаком |.
Первая строка - заголовки полей.
Модуль проверяет в заданный интервал изменение времени модификации файла,
и если оно изменилось - парсит файл. Данные не сохраняются в базу данных.
"""

import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
import os

from database import get_setting, set_setting

logger = logging.getLogger('admin_helper')

# Глобальная переменная для хранения последнего времени модификации файла
_last_mtime: Optional[float] = None
_last_parsed_data: List[Dict[str, str]] = []
_last_headers: List[str] = []


def parse_log_line(line: str, headers: List[str]) -> Optional[Dict[str, str]]:
    """
    Парсит одну строку из файла log.txt.
    Возвращает словарь с данными или None если строка некорректна.
    Поддерживает гибкое количество полей - минимум 15, максимум len(headers).
    Если полей меньше чем заголовков, недостающие поля заполняются пустыми строками.
    Очищает значения от префиксов: swap:, kasp:, cprocsp:, r7office:
    """
    line = line.strip()
    if not line:
        return None
    
    parts = line.split('|')
    
    # Минимальное количество полей для корректной строки
    min_fields = 15
    
    if len(parts) < min_fields:
        logger.warning(f"[parse_log_line] Некорректная строка (меньше {min_fields} полей, получено {len(parts)}): {line[:100]}")
        return None
    
    # Если полей больше чем заголовков, обрезаем до количества заголовков
    # Если полей меньше чем заголовков, дополняем пустыми значениями
    if len(parts) > len(headers):
        logger.debug(f"[parse_log_line] Строка содержит {len(parts)} полей, ожидаемо {len(headers)}. Обрезаем.")
        parts = parts[:len(headers)]
    elif len(parts) < len(headers):
        logger.debug(f"[parse_log_line] Строка содержит {len(parts)} полей, ожидаемо {len(headers)}. Дополняем пустыми.")
        parts.extend([''] * (len(headers) - len(parts)))
    
    # Функция для очистки значений от префиксов
    def clean_value(value: str) -> str:
        value = value.strip()
        # Удаляем префиксы swap:, kasp:, cprocsp:, r7office: (с пробелом после двоеточия или без)
        prefixes = ['swap:', 'kasp:', 'cprocsp:', 'r7office:']
        for prefix in prefixes:
            if value.startswith(prefix):
                value = value[len(prefix):]
                # Удаляем возможный пробел в начале
                value = value.lstrip()
                break
        return value
    
    # Создаем словарь с данными используя заголовки из первой строки
    data = {}
    for i, header in enumerate(headers):
        data[header.strip()] = clean_value(parts[i])
    
    return data


def parse_log_file(file_path: str) -> tuple[List[Dict[str, str]], List[str]]:
    """
    Парсит весь файл log.txt.
    Возвращает кортеж: (список словарей с данными, список заголовков).
    Первая строка файла считается заголовком, если она не похожа на данные.
    Если первая строка похожа на данные (начинается с даты), используются стандартные заголовки.
    """
    result = []
    headers = []
    
    # Стандартные заголовки для файлов без заголовков (на русском языке)
    DEFAULT_HEADERS = [
        'Дата авторизации', 'Версия ОС', 'Версия ядра', 'Имя устройства', 'Имя пользователя',
        'Тип сети', 'IP', 'Mac', 'Название мат. платы', 'Свободно места на основном разделе',
        'Размер swap и насколько он занят', 'Процессор', 'Тип диска', 'Р7 офис', 'Касперский', 'Криптопро'
    ]
    
    try:
        path = Path(file_path)
        if not path.exists():
            logger.error(f"[parse_log_file] Файл не найден: {file_path}")
            return result, headers
        
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
            if not lines:
                logger.warning(f"[parse_log_file] Файл пуст: {file_path}")
                return result, headers
            
            # Проверяем, является ли первая строка заголовком или данными
            first_line = lines[0].strip()
            first_parts = first_line.split('|')
            
            # Если первая строка начинается с даты (формат YYYY-MM-DD) и имеет 16 полей,
            # считаем что это данные, а не заголовки
            is_header = True
            if len(first_parts) >= 15:
                # Проверяем, похоже ли первое поле на дату
                import re
                if re.match(r'^\d{4}-\d{2}-\d{2}', first_parts[0]):
                    is_header = False
                    headers = DEFAULT_HEADERS
                    logger.info(f"[parse_log_file] Первая строка похожа на данные, используем стандартные заголовки: {headers}")
            
            if is_header:
                # Первая строка - заголовки
                headers = [h.strip() for h in first_line.split('|')]
                logger.info(f"[parse_log_file] Заголовки из файла: {headers}")
                data_lines = lines[1:]
            else:
                # Первая строка - данные, используем стандартные заголовки
                data_lines = lines
            
            # Парсим строки данных
            for line_num, line in enumerate(data_lines, 1):
                data = parse_log_line(line, headers)
                if data:
                    result.append(data)
        
        # Сортируем данные по дате авторизации (по убыванию - новые записи сверху)
        if result and 'Дата авторизации' in headers:
            try:
                result.sort(key=lambda x: x.get('Дата авторизации', ''), reverse=True)
                logger.info(f"[parse_log_file] Данные отсортированы по дате авторизации (по убыванию)")
            except Exception as e:
                logger.warning(f"[parse_log_file] Ошибка при сортировке данных: {e}")
        
        logger.info(f"[parse_log_file] Успешно распарсено {len(result)} записей из файла {file_path}")
        
    except Exception as e:
        logger.error(f"[parse_log_file] Ошибка при чтении файла {file_path}: {type(e).__name__}: {e}")
    
    return result, headers


def get_file_mtime(file_path: str) -> Optional[float]:
    """
    Получает время последней модификации файла.
    Возвращает timestamp или None если файл не найден.
    """
    try:
        path = Path(file_path)
        if not path.exists():
            return None
        return os.path.getmtime(file_path)
    except Exception as e:
        logger.error(f"[get_file_mtime] Ошибка при получении времени модификации файла {file_path}: {e}")
        return None


def check_and_parse_log() -> Dict[str, Any]:
    """
    Проверяет изменение времени модификации файла log.txt и парсит его при необходимости.
    Путь к файлу и интервал проверки берутся из настроек.
    Возвращает данные из файла (без сохранения в БД).
    """
    global _last_mtime, _last_parsed_data, _last_headers
    
    # Получаем путь к файлу из настроек
    log_path = get_setting('work_pc_log_path', '')
    
    if not log_path:
        logger.error("[check_and_parse_log] Путь к файлу log.txt не указан в настройках")
        return {
            "status": "error",
            "message": "Путь к файлу log.txt не указан в настройках",
            "data": [],
            "headers": []
        }
    
    # Проверяем существование файла
    if not Path(log_path).exists():
        logger.error(f"[check_and_parse_log] Файл не найден: {log_path}")
        return {
            "status": "error",
            "message": f"Файл не найден: {log_path}",
            "data": [],
            "headers": []
        }
    
    # Получаем текущее время модификации файла
    current_mtime = get_file_mtime(log_path)
    
    if current_mtime is None:
        return {
            "status": "error",
            "message": "Не удалось получить время модификации файла",
            "data": [],
            "headers": []
        }
    
    # Проверяем изменился ли файл
    if _last_mtime is not None and current_mtime == _last_mtime:
        logger.debug(f"[check_and_parse_log] Файл не изменился, возвращаем закэшированные данные")
        return {
            "status": "ok",
            "message": "Файл не изменился",
            "data": _last_parsed_data,
            "headers": _last_headers,
            "file_changed": False
        }
    
    # Файл изменился или это первый запуск - парсим его
    logger.info(f"[check_and_parse_log] Файл изменился (старое mtime: {_last_mtime}, новое mtime: {current_mtime})")
    
    data, headers = parse_log_file(log_path)
    
    # Обновляем глобальные переменные
    _last_mtime = current_mtime
    _last_parsed_data = data
    _last_headers = headers
    
    if not data:
        logger.warning(f"[check_and_parse_log] Нет данных для возврата из файла {log_path}")
        return {
            "status": "warning",
            "message": "Файл пуст или содержит некорректные данные",
            "data": [],
            "headers": headers,
            "file_changed": True
        }
    
    return {
        "status": "ok",
        "message": f"Успешно распарсено {len(data)} записей",
        "data": data,
        "headers": headers,
        "file_changed": True,
        "records_count": len(data),
        "log_path": log_path
    }


def get_work_pc_data() -> Dict[str, Any]:
    """
    Получает все данные из последнего распарсенного файла.
    Возвращает данные и заголовки.
    Если данные еще не загружены, выполняет проверку и парсинг файла.
    """
    global _last_parsed_data, _last_headers
    
    # Если данные еще не загружены (пустой список И пустые заголовки), выполняем проверку и парсинг
    if not _last_parsed_data and not _last_headers:
        logger.info("[get_work_pc_data] Данные еще не загружены, выполняем check_and_parse_log()")
        result = check_and_parse_log()
        if result.get("status") in ["ok", "warning"]:
            return {
                "status": "ok",
                "data": result.get("data", []),
                "headers": result.get("headers", [])
            }
        else:
            return {
                "status": result.get("status", "error"),
                "message": result.get("message", ""),
                "data": [],
                "headers": []
            }
    
    return {
        "status": "ok",
        "data": _last_parsed_data,
        "headers": _last_headers
    }


def get_work_pc_by_computer(computer_name: str) -> List[Dict[str, Any]]:
    """
    Получает данные по конкретному компьютеру из распарсенных данных.
    Имя поля компьютера берется из заголовков (обычно 'Имя устройства' или 'computer_name').
    """
    global _last_parsed_data, _last_headers
    
    # Определяем имя поля для имени компьютера
    computer_field = None
    for header in _last_headers:
        if header.lower() in ['имя устройства', 'computer_name', 'имя компьютера', 'hostname', 'компьютер']:
            computer_field = header
            break
    
    if computer_field is None:
        logger.warning(f"[get_work_pc_by_computer] Не найдено поле для имени компьютера в заголовках: {_last_headers}")
        return []
    
    result = [record for record in _last_parsed_data if record.get(computer_field) == computer_name]
    return result


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
    global _last_mtime
    
    try:
        set_setting('work_pc_log_path', path)
        # Сбрасываем время модификации чтобы при следующем запросе файл был распарсен заново
        _last_mtime = None
        logger.info(f"[set_work_pc_log_path] Путь к файлу установлен: {path}")
        return True
    except Exception as e:
        logger.error(f"[set_work_pc_log_path] Ошибка при сохранении пути: {e}")
        return False


def set_work_pc_update_interval(interval: int) -> bool:
    """
    Устанавливает период проверки изменения файла log.txt (в минутах).
    """
    try:
        set_setting('work_pc_update_interval', str(interval))
        logger.info(f"[set_work_pc_update_interval] Период проверки установлен: {interval} мин")
        return True
    except Exception as e:
        logger.error(f"[set_work_pc_update_interval] Ошибка при сохранении периода: {e}")
        return False


def reset_cache():
    """
    Сбрасывает кэш времени модификации и данных.
    Используется при изменении пути к файлу.
    """
    global _last_mtime, _last_parsed_data, _last_headers
    _last_mtime = None
    _last_parsed_data = []
    _last_headers = []
    logger.info("[reset_cache] Кэш work_pc сброшен")
