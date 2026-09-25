"""
Модуль для работы с API Яндекс 360 (https://yandex.ru/dev/api360/doc/ru/)

Часть модуля синхронизации пользователей и подразделений ALD Pro <-> Яндекс 360.

ВАЖНО: у API Яндекс 360 ДВА ХОСТА С РАЗНЫМИ НАБОРАМИ МЕТОДОВ
(см. https://yandex.ru/dev/api360/doc/ru/concepts/about):
- https://api360.yandex.net  — основной хост Directory API для организаций.
  Только здесь поддерживается создание подразделений:
      POST https://api360.yandex.net/directory/v1/org/{orgId}/departments
  (DepartmentService_Create). На других хостах этот метод возвращает
  HTTP 405 Method Not Allowed.
- https://cloud-api.yandex.net — общий облачный хост Yandex Cloud API.
  Часть методов каталога доступна здесь, но НЕ методы создания
  подразделений. Используется как дополнительный (fallback) хост.

Поэтому все операции СОЗДАНИЯ ПОДРАЗДЕЛЕНИЙ всегда выполняются ТОЛЬКО
через api360.yandex.net, независимо от выбранного в настройках хоста.
Для остальных операций используется хост из настроек, а при 404/405 с
автоматическим повтором на другом хосте (см. request()).

Формат путей Directory API (согласно актуальной документации):
- GET    /directory/v1/org/{orgId}/users          — список сотрудников
           (UserService_List)
- POST   /directory/v1/org/{orgId}/users          — создать сотрудника
           (UserService_Create)
- PATCH  /directory/v1/org/{orgId}/users/{id}     — изменить сотрудника
           (UserService_Update: departmentId, blocked и т.д.)
- GET    /directory/v1/org/{orgId}/departments    — список подразделений
           (DepartmentService_List)
- POST   /directory/v1/org/{orgId}/departments    — СОЗДАТЬ подразделение
           (DepartmentService_Create: name, parentDepartmentId, note)
           *** только на https://api360.yandex.net ***
- GET    /directory/v1/org/{orgId}/departments/{id} — о подразделении
           (DepartmentService_Get)
- PATCH  /directory/v1/org/{orgId}/departments/{id} — изменить
           (DepartmentService_Update)

Авторизация (раздел "Доступ к API"):
- Приложения авторизуются с помощью OAuth-токенов Яндекс ID.
  Токен получается для OAuth-приложения (ClientID) по ссылке:
      https://oauth.yandex.ru/authorize?response_type=token&client_id=<ClientID>
  (нужны права directory:read_users / directory:write_users,
   directory:read_departments / directory:write_departments и т.д.)
- Токен передается в HTTP-заголовке каждого запроса:
      Authorization: OAuth <OAuth-токен>

Если при создании подразделения возвращается HTTP 405 MethodNotAllowedError —
это признак того, что запрос ушёл не на api360.yandex.net, либо у токена нет
права directory:write_departments.
"""
import base64
import logging
from typing import Dict, Any, Optional

import httpx

logger = logging.getLogger('admin_helper')

# Хосты API Яндекс 360 (раздел "Доступ к API").
# У каждого хоста свой набор поддерживаемых методов:
# - api360.yandex.net  — основной хост Directory API; ТОЛЬКО здесь доступен
#   DepartmentService_Create (POST /directory/v1/org/{orgId}/departments);
# - cloud-api.yandex.net — дополнительный облачный хост (часть методов).
API_HOSTS = {
    'api360.yandex.net': 'https://api360.yandex.net',        # основной хост API 360
    'cloud-api.yandex.net': 'https://cloud-api.yandex.net',  # дополнительный хост
}

# Основной хост Directory API Яндекс 360. Создание подразделений
# (DepartmentService_Create) поддерживается ТОЛЬКО на нём.
PRIMARY_API_HOST = 'api360.yandex.net'
PRIMARY_BASE_URL = API_HOSTS[PRIMARY_API_HOST]

# Методы, доступные только на основном хосте (api360.yandex.net):
# (HTTP-метод, путь должен начинаться с /directory/v1/org/{orgId}/departments)
WRITE_ONLY_ON_PRIMARY = ('POST',)


def other_host(host: str) -> str:
    """Вернуть «другой» хост API (для повторных попыток при 404/405)."""
    return ('cloud-api.yandex.net' if host == PRIMARY_API_HOST
            else PRIMARY_API_HOST)

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
    # Основной хост API Яндекс 360 (здесь доступны ВСЕ методы, включая
    # создание подразделений DepartmentService_Create):
    'api_host': 'api360.yandex.net',
    # Дополнительный хост (cloud-api.yandex.net). Пусто — не использовать.
    # Используется автоматически при недоступности метода на основном хосте.
    'api_host_alt': 'cloud-api.yandex.net',
    'org_id': '',                        # идентификатор организации (orgId)
    'oauth_token': '',                   # OAuth-токен приложения Яндекс ID (чтение каталога)
    'oauth_token_write': '',             # токен для операций записи (создание подразделений/сотрудников); если пусто — используется oauth_token
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
    # совместимость со старыми настройками: раньше по умолчанию выбирался
    # cloud-api.yandex.net, на котором недоступно создание подразделений
    if result.get('api_host') not in API_HOSTS:
        result['api_host'] = DEFAULT_SETTINGS['api_host']
    return result


_y360_settings = _load_from_db()


def normalize_host(host: str) -> str:
    """Привести значение хоста к известному ключу API_HOSTS."""
    host = (host or '').strip().lower()
    host = host.removeprefix('https://').removeprefix('http://').rstrip('/')
    if host in API_HOSTS:
        return host
    return ''


def primary_base_url() -> str:
    """Базовый URL основного хоста api360.yandex.net.

    Создание подразделений (DepartmentService_Create) поддерживается только
    на этом хосте — сюда всегда отправляются POST /departments.
    """
    return PRIMARY_BASE_URL


def _base_url() -> str:
    """Вернуть базовый URL API по сохраненному основному хосту настроек."""
    host = _y360_settings.get('api_host', DEFAULT_SETTINGS['api_host'])
    return API_HOSTS.get(normalize_host(host), API_HOSTS[DEFAULT_SETTINGS['api_host']])


def alt_base_url() -> Optional[str]:
    """Базовый URL дополнительного хоста (или None, если не задан/совпадает)."""
    host = normalize_host(_y360_settings.get('api_host_alt', ''))
    if not host or API_HOSTS[host] == _base_url():
        return None
    return API_HOSTS[host]


def api_base_url() -> str:
    """Публичный доступ к базовому URL API (используется модулем синхронизации)."""
    return _base_url()


def org_path(org_id: str, suffix: str = '') -> str:
    """Путь Directory API: /directory/v1/org/{orgId}[/suffix]."""
    return f"/directory/v1/org/{org_id}{('/' + suffix.lstrip('/')) if suffix else ''}"


async def request(method: str, path: str, *, base_url: str = None,
                  fallback: bool = True, **kwargs) -> httpx.Response:
    """Выполнить асинхронный запрос к API Яндекс 360 с учетом двух хостов.

    У хостов api360.yandex.net и cloud-api.yandex.net разные наборы
    поддерживаемых методов, поэтому:
      - создающие запросы (method == 'POST') всегда идут на основной хост
        api360.yandex.net (только там доступен DepartmentService_Create);
      - остальные запросы идут на хост из настроек; если метод/путь там
        недоступны (HTTP 404/405) и задан дополнительный хост — запрос
        автоматически повторяется на нём (fallback=True).

    Возвращает httpx.Response. Вызывать внутри уже запущенного event loop.
    """
    method = method.upper()
    explicit_base = base_url
    if method in WRITE_ONLY_ON_PRIMARY:
        # создание подразделений/сотрудников — только на api360.yandex.net
        first = PRIMARY_BASE_URL
        retry = None
    else:
        first = explicit_base or _base_url()
        retry = alt_base_url() if (fallback and explicit_base is None) else None
        if retry == first:
            retry = None

    resp = await make_async_client(base_url=first).request(method, path, **kwargs)
    if retry and resp.status_code in (404, 405):
        logger.warning(
            "Метод %s %s вернул HTTP %s на %s — повторяю на %s",
            method, path, resp.status_code, first, retry)
        resp = await make_async_client(base_url=retry).request(
            method, path, **kwargs)
    return resp


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
        'api_host_alt': _y360_settings.get('api_host_alt', ''),
        'org_id': _y360_settings.get('org_id', ''),
        'oauth_token': _y360_settings.get('oauth_token', ''),
        'oauth_token_write': _y360_settings.get('oauth_token_write', ''),
        'client_id': _y360_settings.get('client_id', ''),
    }


def save_settings(api_host: str, org_id: str, oauth_token: str,
                  client_id: str = '',
                  oauth_token_write: Optional[str] = None,
                  api_host_alt: Optional[str] = None) -> bool:
    """Сохранить настройки авторизации Яндекс 360 (в БД, таблицу module_settings)."""
    global _y360_settings
    host = normalize_host(api_host)
    if not host:
        host = DEFAULT_SETTINGS['api_host']
    _y360_settings['api_host'] = host
    if api_host_alt is not None:
        alt = normalize_host(api_host_alt)
        # дополнительный хост не должен совпадать с основным
        _y360_settings['api_host_alt'] = '' if alt == host else alt
    _y360_settings['org_id'] = str(org_id).strip()
    _y360_settings['oauth_token'] = oauth_token.strip()
    _y360_settings['client_id'] = client_id.strip()
    if oauth_token_write is not None:
        _y360_settings['oauth_token_write'] = oauth_token_write.strip()
    try:
        from database import set_module_settings
        set_module_settings(SETTINGS_KEY, dict(_y360_settings))
    except Exception as e:
        logger.error(f"Не удалось сохранить настройки Яндекс 360 в БД: {e}")
        return False
    logger.info(
        f"Настройки Яндекс 360 сохранены в БД: host={host}, "
        f"alt_host={_y360_settings.get('api_host_alt') or '—'}, org_id={org_id}"
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
    /directory/v1/org/{orgId}/departments?limit=1) с заголовком
    ``Authorization: OAuth <токен>`` на выбранном хосте; при HTTP 404/405
    автоматически повторяет запрос на другом хосте (у хостов разные наборы
    методов).

    Returns:
        Dict {'success': bool, ...}
    """
    host = normalize_host(api_host) or DEFAULT_SETTINGS['api_host']
    base_url = API_HOSTS[host]
    org_id = str(org_id).strip()
    oauth_token = oauth_token.strip()

    if not org_id or not org_id.isdigit():
        return {'success': False, 'detail': 'Укажите числовой идентификатор организации (org_id)'}
    if not oauth_token:
        return {'success': False, 'detail': 'Укажите OAuth-токен'}

    path = org_path(org_id, 'departments')
    headers = {
        'Authorization': f'OAuth {oauth_token}',
        'Accept': 'application/json',
    }

    async def _try(url_base: str) -> httpx.Response:
        async with httpx.AsyncClient(base_url=url_base, timeout=DEFAULT_TIMEOUT,
                                     verify=False) as client:
            return await client.get(path, headers=headers, params={'limit': 1})

    try:
        response = await _try(base_url)
        used_host = host
        # у api360.yandex.net и cloud-api.yandex.net разные наборы методов —
        # при недоступности пути/метода пробуем второй хост
        if response.status_code in (404, 405):
            alt = other_host(host)
            logger.warning(
                "GET %s%s вернул HTTP %s на %s — повторяю на %s",
                base_url, path, response.status_code, host, alt)
            response = await _try(API_HOSTS[alt])
            used_host = alt

        if response.status_code == 200:
            data = response.json()
            total = data.get('total', len(data.get('items', []) or []))
            logger.info(f"Успешное подключение к Яндекс 360: host={used_host}, org_id={org_id}")
            return {
                'success': True,
                'host_used': used_host,
                'detail': f'Подключение успешно ({API_HOSTS[used_host]}). '
                          f'Подразделений в организации: {total}',
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
# Клиентские методы Directory API для синхронизации пользователей
# и подразделений ALD Pro <-> Яндекс 360.
#
# ВАЖНО про два хоста: создание подразделений (DepartmentService_Create)
# поддерживается ТОЛЬКО на https://api360.yandex.net, поэтому все POST-запросы
# выполняются через request(), который принудительно использует этот хост.
# ============================================================================

def auth_headers(token: Optional[str] = None) -> Dict[str, str]:
    """Заголовки авторизации: основной токен (чтение) или переданный токен."""
    tok = (token or _y360_settings.get('oauth_token') or '').strip()
    return {
        'Authorization': f'OAuth {tok}',
        'Accept': 'application/json',
    }


async def get_departments(limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    """Получить список подразделений организации (для синхронизации с OU ALD Pro)."""
    if not _y360_settings.get('org_id') or not _y360_settings.get('oauth_token'):
        return {'success': False, 'detail': 'Яндекс 360 не настроен'}
    try:
        response = await request(
            'GET', org_path(_y360_settings['org_id'], 'departments'),
            params={'limit': limit, 'offset': offset},
            headers=auth_headers())
        if response.status_code != 200:
            return {'success': False, 'detail': f'HTTP {response.status_code}: {response.text[:150]}'}
        return {'success': True, **response.json()}
    except Exception as e:
        logger.error(f"Ошибка получения подразделений Яндекс 360: {e}")
        return {'success': False, 'detail': f'Ошибка: {e}'}


def get_write_token() -> Optional[str]:
    """Вернуть отдельный токен для операций ЗАПИСИ (создание/изменение
    подразделений и сотрудников), если он задан; иначе — основной токен.

    Примечание: методы создания (POST /directory/v1/org/{orgId}/departments,
    POST .../users) доступны только на api360.yandex.net и только при
    использовании токена, выданного корпоративному приложению Яндекс 360
    с правом directory:write_*. Обычный OAuth-токен «личного» приложения
    часто дает 405/403.
    """
    token = (_y360_settings.get('oauth_token_write') or '').strip()
    if token:
        return token
    return (_y360_settings.get('oauth_token') or '').strip() or None


async def create_department(org_id: str, name: str, parent_department_id=None,
                            note: str = '',
                            token: Optional[str] = None) -> Dict[str, Any]:
    """Создать подразделение (DepartmentService_Create).

    POST https://api360.yandex.net/directory/v1/org/{orgId}/departments
    Тело: {"name": str, "parentDepartmentId": int|null, "note": str}.
    Возвращает dict c 'success' и 'id' созданного подразделения.
    Требуется право OAuth directory:write_departments.

    ВАЖНО: метод DepartmentService_Create поддерживается только на основном
    хосте api360.yandex.net — запрос всегда отправляется туда, независимо от
    хоста, выбранного в настройках (на cloud-api.yandex.net он возвращает
    HTTP 405 MethodNotAllowedError). Перенаправление выполняет request().
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
    resp = await request('POST', org_path(org_id, 'departments'),
                         params={'org_id': org_id}, json=payload,
                         headers=auth_headers(token or get_write_token()))
    if resp.status_code not in (200, 201):
        return {'success': False,
                'status': resp.status_code,
                'detail': resp.text[:300],
                'url': str(resp.request.url)}
    body = resp.json() or {}
    dep = body.get('department') or body
    new_id = dep.get('id') or body.get('id')
    return {'success': True, 'id': str(new_id) if new_id is not None else '',
            'data': body}


async def update_department(org_id: str, dept_id: str, payload: Dict[str, Any],
                            token: Optional[str] = None) -> Dict[str, Any]:
    """Изменить подразделение (DepartmentService_Update).

    PATCH /directory/v1/org/{orgId}/departments/{id}
    """
    resp = await request('PATCH', org_path(org_id, f'departments/{dept_id}'),
                         params={'org_id': org_id}, json=payload,
                         headers=auth_headers(token))
    if resp.status_code != 200:
        return {'success': False, 'status': resp.status_code,
                'detail': resp.text[:300]}
    return {'success': True, 'data': resp.json() or {}}


async def patch_user(org_id: str, user_id: str, payload: Dict[str, Any],
                     token: Optional[str] = None) -> Dict[str, Any]:
    """Изменить сотрудника (UserService_Update).

    PATCH /directory/v1/org/{orgId}/users/{id}
    """
    resp = await request('PATCH', org_path(org_id, f'users/{user_id}'),
                         params={'org_id': org_id}, json=payload,
                         headers=auth_headers(token))
    if resp.status_code != 200:
        return {'success': False, 'status': resp.status_code,
                'detail': resp.text[:300]}
    return {'success': True, 'data': resp.json() or {}}


async def create_employee(org_id: str, payload: Dict[str, Any],
                          token: Optional[str] = None) -> Dict[str, Any]:
    """Создать сотрудника (UserService_Create).

    POST /directory/v1/org/{orgId}/users — выполняется через request(),
    который для POST всегда использует основной хост api360.yandex.net.
    """
    resp = await request('POST', org_path(org_id, 'users'),
                         params={'org_id': org_id}, json=payload,
                         headers=auth_headers(token or get_write_token()))
    if resp.status_code not in (200, 201):
        return {'success': False, 'status': resp.status_code,
                'detail': resp.text[:300], 'url': str(resp.request.url)}
    body = resp.json() or {}
    emp = body.get('employee') or body.get('user') or body
    return {'success': True,
            'login': str(emp.get('login') or payload.get('login') or ''),
            'id': str(emp.get('id') or ''),
            'data': body}


async def get_users(limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    """Получить список сотрудников организации (для синхронизации с пользователями ALD Pro)."""
    if not _y360_settings.get('org_id') or not _y360_settings.get('oauth_token'):
        return {'success': False, 'detail': 'Яндекс 360 не настроен'}
    try:
        response = await request(
            'GET', org_path(_y360_settings['org_id'], 'users'),
            params={'limit': limit, 'offset': offset},
            headers=auth_headers())
        if response.status_code != 200:
            return {'success': False, 'detail': f'HTTP {response.status_code}: {response.text[:150]}'}
        return {'success': True, **response.json()}
    except Exception as e:
        logger.error(f"Ошибка получения сотрудников Яндекс 360: {e}")
        return {'success': False, 'detail': f'Ошибка: {e}'}
