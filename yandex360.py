"""
Модуль для работы с API Яндекс 360 (https://yandex.ru/dev/api360/doc/ru/)

Часть модуля синхронизации пользователей и подразделений ALD Pro <-> Яндекс 360.

Авторизация в API Яндекс 360 (согласно документации, раздел "Доступ к API"):
- Запросы отправляются на https://cloud-api.yandex.net/ (актуальный хост)
  или https://api360.yandex.net/ (устаревший хост).
- Приложения авторизуются с помощью OAuth-токенов Яндекс ID.
  Токен получается для OAuth-приложения (ClientID) по ссылке:
      https://oauth.yandex.ru/authorize?response_type=token&client_id=<ClientID>
  (нужны права directory:read_users / directory:write_users,
   directory:read_departments / directory:write_departments и т.д.)
- Токен передается в HTTP-заголовке каждого запроса:
      Authorization: OAuth <OAuth-токен>

Основные эндпоинты Directory API, используемые для синхронизации
(https://yandex.ru/dev/api360/doc/ru/ref/DepartmentService/ и UserService/):
- GET    /v1/directory/organizations/{org_id}/users         — список сотрудников
- POST   /v1/directory/organizations/{org_id}/users         — создать сотрудника
           (UserService_Create)
- PATCH  /v1/directory/organizations/{org_id}/users/{login} — изменить сотрудника
           (UserService_Update: departmentId, blocked и т.д.)
- GET    /v1/directory/organizations/{org_id}/departments   — список подразделений
- POST   /v1/directory/organizations/{org_id}/departments   — СОЗДАТЬ подразделение
           (DepartmentService_Create: name, parentDepartmentId, note)
- GET    /v1/directory/organizations/{org_id}/departments/{dep_id} — о подразделении
- PATCH  /v1/directory/organizations/{org_id}/departments/{dep_id} — изменить
           подразделение (DepartmentService_Update)

Для создания подразделений и сотрудников у OAuth-приложения должны быть
выданы права directory:write_departments и directory:write_users. Если при
POST возвращается HTTP 405 MethodNotAllowedError — как правило, это признак
устаревшего хоста api360.yandex.net (нужен cloud-api.yandex.net) либо
отсутствия прав записи у токена.
"""
import base64
import logging
from typing import Dict, Any, Optional

import httpx

logger = logging.getLogger('admin_helper')

# Хосты API Яндекс 360 (раздел "Доступ к API")
API_HOSTS = {
    'cloud-api.yandex.net': 'https://cloud-api.yandex.net',  # актуальный хост
    'api360.yandex.net': 'https://api360.yandex.net',        # устаревший хост
}

# Ссылка для получения OAuth-токена по ClientID
OAUTH_AUTHORIZE_URL_TEMPLATE = (
    "https://oauth.yandex.ru/authorize?response_type=token&client_id={client_id}"
)

# Права OAuth, необходимые для синхронизации пользователей и подразделений
REQUIRED_OAUTH_SCOPES = [
    'directory:read_users',
    'directory:read_departments',
    # Для записи (создания/изменения) при синхронизации потребуются:
    # 'directory:write_users',
    # 'directory:write_departments',
]

DEFAULT_TIMEOUT = 30.0

# Глобальное хранилище настроек авторизации в API Яндекс 360.
# Фактическое место хранения — БД (таблица module_settings, ключ
# 'y360_api_settings'), чтобы настройки не перезаписывались при слиянии
# веток git и не терялись после перезапуска сервиса. Здесь только кэш.
SETTINGS_KEY = 'y360_api_settings'

DEFAULT_SETTINGS = {
    'api_host': 'cloud-api.yandex.net',  # хост API (cloud-api.yandex.net / api360.yandex.net)
    'org_id': '',                        # идентификатор организации (org_id)
    'oauth_token': '',                   # OAuth-токен приложения Яндекс ID
    'client_id': '',                     # ClientID OAuth-приложения (для справки/получения токена)
}


def _load_from_db() -> Dict[str, Any]:
    """Прочитать настройки из БД (объединённые с дефолтом)."""
    result = dict(DEFAULT_SETTINGS)
    try:
        from database import get_module_settings
        stored = get_module_settings(SETTINGS_KEY)
        if isinstance(stored, dict):
            result.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
    except Exception as e:
        logger.error(f"Ошибка чтения настроек Яндекс 360 из БД: {e}")
    return result


_y360_settings = _load_from_db()


def _base_url() -> str:
    """Вернуть базовый URL API по сохраненному хосту."""
    host = _y360_settings.get('api_host', 'cloud-api.yandex.net')
    return API_HOSTS.get(host, API_HOSTS['cloud-api.yandex.net'])


def api_base_url() -> str:
    """Публичный доступ к базовому URL API (используется модулем синхронизации)."""
    return _base_url()


def make_async_client(base_url: str = None, headers: Dict[str, Any] = None) -> httpx.AsyncClient:
    """Создать httpx-клиент для запросов к API Яндекс 360 (общие настройки TLS/timeout)."""
    return httpx.AsyncClient(
        base_url=base_url or _base_url(),
        timeout=DEFAULT_TIMEOUT,
        verify=False,
        headers=headers or {},
    )


def get_settings() -> Dict[str, Any]:
    """Получить текущие настройки авторизации Яндекс 360."""
    return {
        'api_host': _y360_settings.get('api_host', ''),
        'org_id': _y360_settings.get('org_id', ''),
        'oauth_token': _y360_settings.get('oauth_token', ''),
        'client_id': _y360_settings.get('client_id', ''),
    }


def save_settings(api_host: str, org_id: str, oauth_token: str,
                  client_id: str = '') -> bool:
    """Сохранить настройки авторизации Яндекс 360 (в БД, таблицу module_settings)."""
    global _y360_settings
    if api_host not in API_HOSTS:
        api_host = 'cloud-api.yandex.net'
    _y360_settings['api_host'] = api_host
    _y360_settings['org_id'] = str(org_id).strip()
    _y360_settings['oauth_token'] = oauth_token.strip()
    _y360_settings['client_id'] = client_id.strip()
    try:
        from database import set_module_settings
        set_module_settings(SETTINGS_KEY, dict(_y360_settings))
    except Exception as e:
        logger.error(f"Не удалось сохранить настройки Яндекс 360 в БД: {e}")
        return False
    logger.info(
        f"Настройки Яндекс 360 сохранены в БД: host={api_host}, org_id={org_id}"
    )
    return True


def build_oauth_link(client_id: str) -> str:
    """Сформировать ссылку для получения OAuth-токена по ClientID."""
    return OAUTH_AUTHORIZE_URL_TEMPLATE.format(client_id=client_id.strip())


def _extract_org_id_from_token(token: str) -> Optional[int]:
    """
    Извлечь идентификатор организации из OAuth-токена.

    T2-токены Яндекс 360 имеют формат ``T.{base64_payload}.{signature}``,
    где в payload содержится id организации (pole org_id). Если токен обычного
    формата — вернет None.
    """
    try:
        parts = token.split('.')
        if len(parts) < 2 or not parts[1]:
            return None
        payload_b64 = parts[1]
        # добавляем недостающие символы padding для base64
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode(payload_b64)
        text = payload.decode('utf-8', errors='ignore')
        # ищем orgid/org_id в бинарном payload
        for marker in ('orgid', 'org_id'):
            idx = text.find(marker)
            if idx != -1:
                digits = ''
                for ch in text[idx + len(marker):]:
                    if ch.isdigit():
                        digits += ch
                    elif digits:
                        break
                if digits:
                    return int(digits)
        return None
    except Exception as e:
        logger.debug(f"Не удалось извлечь org_id из токена: {e}")
        return None


async def test_connection(api_host: str, org_id: str, oauth_token: str) -> Dict[str, Any]:
    """
    Проверить подключение к API Яндекс 360.

    Выполняет пробный запрос списка подразделений (GET
    /v1/directory/organizations/{org_id}/departments?limit=1) с заголовком
    ``Authorization: OAuth <токен>``.

    Returns:
        Dict {'success': bool, ...}
    """
    host = api_host if api_host in API_HOSTS else 'cloud-api.yandex.net'
    base_url = API_HOSTS[host]
    org_id = str(org_id).strip()
    oauth_token = oauth_token.strip()

    if not org_id or not org_id.isdigit():
        return {'success': False, 'detail': 'Укажите числовой идентификатор организации (org_id)'}
    if not oauth_token:
        return {'success': False, 'detail': 'Укажите OAuth-токен'}

    url = f"{base_url}/v1/directory/organizations/{org_id}/departments"
    headers = {
        'Authorization': f'OAuth {oauth_token}',
        'Accept': 'application/json',
    }

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, verify=False) as client:
            response = await client.get(url, headers=headers, params={'limit': 1})

        if response.status_code == 200:
            data = response.json()
            total = data.get('total', len(data.get('items', []) or []))
            logger.info(f"Успешное подключение к Яндекс 360: host={host}, org_id={org_id}")
            return {
                'success': True,
                'detail': f'Подключение успешно. Подразделений в организации: {total}',
                'total_departments': total,
            }
        elif response.status_code in (401, 403):
            logger.warning(f"Яндекс 360 отклонил токен ({response.status_code}): {response.text[:200]}")
            return {
                'success': False,
                'detail': f'Ошибка авторизации (HTTP {response.status_code}). '
                          f'Проверьте OAuth-токен и права приложения (directory:read_departments).',
            }
        elif response.status_code == 404:
            return {
                'success': False,
                'detail': 'Организация не найдена (HTTP 404). '
                          'Проверьте org_id и что токен выдан администратором этой организации.',
            }
        else:
            logger.warning(f"Яндекс 360 вернул статус {response.status_code}: {response.text[:200]}")
            return {'success': False, 'detail': f'HTTP {response.status_code}: {response.text[:150]}'}

    except httpx.ConnectError as e:
        logger.error(f"Ошибка подключения к Яндекс 360 ({base_url}): {e}")
        return {'success': False, 'detail': f'Ошибка подключения: {e}'}
    except Exception as e:
        logger.error(f"Ошибка при проверке подключения к Яндекс 360: {e}")
        return {'success': False, 'detail': f'Ошибка: {e}'}


# ============================================================================
# Ниже — заготовки клиентских методов Directory API для последующей
# синхронизации пользователей и подразделений ALD Pro <-> Яндекс 360.
# ============================================================================

def _authenticated_client() -> Optional[httpx.AsyncClient]:
    """Создать аутентифицированный клиент для запросов к API Яндекс 360."""
    if not _y360_settings.get('org_id') or not _y360_settings.get('oauth_token'):
        return None
    return httpx.AsyncClient(
        base_url=_base_url(),
        timeout=DEFAULT_TIMEOUT,
        verify=False,
        headers={
            'Authorization': f"OAuth {_y360_settings['oauth_token']}",
            'Accept': 'application/json',
        },
    )


async def get_departments(limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    """Получить список подразделений организации (для синхронизации с OU ALD Pro)."""
    client = _authenticated_client()
    if client is None:
        return {'success': False, 'detail': 'Яндекс 360 не настроен'}
    try:
        async with client:
            response = await client.get(
                f"/v1/directory/organizations/{_y360_settings['org_id']}/departments",
                params={'limit': limit, 'offset': offset},
            )
            if response.status_code != 200:
                return {'success': False, 'detail': f'HTTP {response.status_code}: {response.text[:150]}'}
            return {'success': True, **response.json()}
    except Exception as e:
        logger.error(f"Ошибка получения подразделений Яндекс 360: {e}")
        return {'success': False, 'detail': f'Ошибка: {e}'}


async def create_department(client: 'httpx.AsyncClient', org_id: str,
                            name: str, parent_department_id=None,
                            note: str = '') -> Dict[str, Any]:
    """Создать подразделение (DepartmentService_Create).

    POST /v1/directory/organizations/{org_id}/departments?org_id={org_id}
    Тело: {"name": str, "parentDepartmentId": int|null, "note": str}.
    Возвращает dict c 'success' и 'id' созданного подразделения.
    Требуется право OAuth directory:write_departments.
    """
    payload: Dict[str, Any] = {'name': (name or '').strip()[:150]}
    if parent_department_id not in (None, '', 0, '0'):
        try:
            payload['parentDepartmentId'] = int(parent_department_id)
        except (TypeError, ValueError):
            payload['parentDepartmentId'] = str(parent_department_id)
    else:
        payload['parentDepartmentId'] = None
    if note:
        payload['note'] = note[:200]
    url = f"/v1/directory/organizations/{org_id}/departments"
    resp = await client.post(url, params={'org_id': org_id}, json=payload)
    if resp.status_code not in (200, 201):
        return {'success': False,
                'status': resp.status_code,
                'detail': resp.text[:300]}
    body = resp.json() or {}
    dep = body.get('department') or body
    new_id = dep.get('id') or body.get('id')
    return {'success': True, 'id': str(new_id) if new_id is not None else '',
            'data': body}


async def get_users(limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    """Получить список сотрудников организации (для синхронизации с пользователями ALD Pro)."""
    client = _authenticated_client()
    if client is None:
        return {'success': False, 'detail': 'Яндекс 360 не настроен'}
    try:
        async with client:
            response = await client.get(
                f"/v1/directory/organizations/{_y360_settings['org_id']}/users",
                params={'limit': limit, 'offset': offset},
            )
            if response.status_code != 200:
                return {'success': False, 'detail': f'HTTP {response.status_code}: {response.text[:150]}'}
            return {'success': True, **response.json()}
    except Exception as e:
        logger.error(f"Ошибка получения сотрудников Яндекс 360: {e}")
        return {'success': False, 'detail': f'Ошибка: {e}'}
