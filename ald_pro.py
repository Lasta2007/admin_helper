"""
Модуль для работы с ALD Pro 3.2.0 API
"""
import httpx
import logging
from typing import Dict, Any, Optional
from urllib.parse import quote, unquote

logger = logging.getLogger('admin_helper')


def _encode_dn(dn: str) -> str:
    """Кодирует DN для безопасного использования в URL-пути."""
    return quote(dn, safe='')

# Глобальное хранилище настроек и сессий. Фактическое место хранения
# настроек — БД (таблица module_settings, ключ 'aldpro_settings'), чтобы
# они не перезаписывались при слиянии веток git и не терялись после
# перезапуска сервиса. Здесь только кэш.
SETTINGS_KEY = 'aldpro_settings'

DEFAULT_SETTINGS = {
    'url': '',
    'login': '',
    'password': ''
}


def _load_from_db() -> Dict[str, Any]:
    """Прочитать настройки ALD Pro из БД (объединённые с дефолтом)."""
    result = dict(DEFAULT_SETTINGS)
    try:
        from database import get_module_settings
        stored = get_module_settings(SETTINGS_KEY)
        if isinstance(stored, dict):
            result.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
    except Exception as e:
        logger.error(f"Ошибка чтения настроек ALD Pro из БД: {e}")
    return result


_aldpro_settings = _load_from_db()
_aldpro_cookies = {}


def get_settings() -> Dict[str, Any]:
    """Получить текущие настройки ALD Pro."""
    return {
        'url': _aldpro_settings.get('url', ''),
        'login': _aldpro_settings.get('login', ''),
        'password': _aldpro_settings.get('password', '')
    }


def save_settings(url: str, login: str, password: str) -> bool:
    """Сохранить настройки ALD Pro (в БД, таблицу module_settings)."""
    global _aldpro_settings
    _aldpro_settings['url'] = url
    _aldpro_settings['login'] = login
    _aldpro_settings['password'] = password
    try:
        from database import set_module_settings
        set_module_settings(SETTINGS_KEY, dict(_aldpro_settings))
    except Exception as e:
        logger.error(f"Не удалось сохранить настройки ALD Pro в БД: {e}")
        return False
    logger.info(f"Настройки ALD Pro сохранены в БД: URL={url}, Login={login}")
    return True


async def test_connection(url: str, login: str, password: str) -> Dict[str, Any]:
    """
    Проверить подключение к ALD Pro API.
    
    Args:
        url: URL сервера ALD Pro (например, https://aldpro.example.ru/ad)
        login: Логин пользователя
        password: Пароль пользователя
    
    Returns:
        Dict с результатом проверки
    """
    global _aldpro_cookies
    
    base_url = url.rstrip('/')
    login_url = f"{base_url}/api/ds/login"
    
    try:
        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            # Выполняем вход
            response = await client.post(
                login_url,
                json={"data": {"login": login, "password": password}},
                headers={"Content-Type": "application/json", "Accept": "application/json"}
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('success'):
                    # Сохраняем cookies для последующих запросов
                    _aldpro_cookies = dict(client.cookies)
                    # Сохраняем настройки
                    save_settings(url, login, password)
                    logger.info(f"Успешное подключение к ALD Pro: {url}")
                    return {
                        'success': True,
                        'cookies': _aldpro_cookies
                    }
                else:
                    logger.warning(f"ALD Pro вернул success=false: {result}")
                    return {'success': False, 'detail': 'Ошибка аутентификации'}
            else:
                logger.warning(f"ALD Pro вернул статус {response.status_code}: {response.text[:200]}")
                return {'success': False, 'detail': f'HTTP {response.status_code}: {response.text[:100]}'}
                
    except httpx.ConnectError as e:
        logger.error(f"Ошибка подключения к ALD Pro {url}: {e}")
        return {'success': False, 'detail': f'Ошибка подключения: {str(e)}'}
    except Exception as e:
        logger.error(f"Ошибка при проверке подключения к ALD Pro: {e}")
        return {'success': False, 'detail': f'Ошибка: {str(e)}'}


async def _get_authenticated_client() -> Optional[httpx.AsyncClient]:
    """Создать аутентифицированный клиент для запросов к ALD Pro."""
    if not _aldpro_settings.get('url') or not _aldpro_settings.get('login'):
        return None
    
    base_url = _aldpro_settings['url'].rstrip('/')
    client = httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        cookies=_aldpro_cookies
    )
    client.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json"
    })
    return client


def _node_from_treenode(node: Dict[str, Any]) -> Dict[str, Any]:
    """Преобразовать tree_node (раздел 7.3 документации) в формат записи дерева."""
    dn = node.get('treenode_dn', '')
    name = node.get('treenode_display_name', '')
    return {
        'organizationunitlistitem_dn': dn,
        'organizationunitlistitem_parent_dn': node.get('treenode_parent_dn') or '',
        'organizationunitlistitem_display_name': name,
        'organizationunitlistitem_is_leaf': bool(node.get('treenode_is_leaf', False)),
        'organizationunitlistitem_ou': name,
        'children': [],
    }


def _make_stub_unit(ou_dn: str) -> Dict[str, Any]:
    """Создать запись подразделения по DN (заглушка, если API не вернул данные)."""
    name = ou_dn.split(',')[0]
    if name.lower().startswith('ou='):
        name = name[3:]
    return {
        'organizationunit_dn': ou_dn,
        'organizationunit_parent_dn': '',
        'organizationunit_display_name': name,
        'organizationunit_is_leaf': False,
        'organizationunit_ou': name,
    }


# ---------------------------------------------------------------------------
# Общий (переиспользуемый) клиент ALD Pro
#
# Нужен для обхода больших поддеревьев OU: авторизация выполняется один раз,
# а не на каждый запрос (раньше на каждое подразделение создавался новый
# клиент и выполнялся вход в ALD Pro, из-за чего обход был очень медленным).
# ---------------------------------------------------------------------------

_shared_client = None


def _client_alive(client) -> bool:
    """Проверить, что общий клиент ещё не закрыт."""
    try:
        return client is not None and not client.is_closed
    except Exception:
        return False


async def get_shared_client(force_new: bool = False):
    """Вернуть переиспользуемый авторизованный клиент ALD Pro (или None)."""
    global _shared_client
    if _client_alive(_shared_client) and not force_new:
        return _shared_client
    if _shared_client is not None:
        try:
            await _shared_client.aclose()
        except Exception:
            pass
        _shared_client = None
    client = await _get_authenticated_client()
    _shared_client = client
    return client


async def close_shared_client():
    """Закрыть переиспользуемый клиент ALD Pro."""
    global _shared_client
    if _shared_client is not None:
        try:
            await _shared_client.aclose()
        except Exception:
            pass
        _shared_client = None


async def _fetch_children(client: httpx.AsyncClient, ou_dn: str) -> list:
    """Получить список дочерних подразделений (раздел 7.9 документации ALD Pro).

    GET /api/ds/organizational-units/{dn}/organizational-units
    Элементы ответа содержат organizationunitlistitem_* поля.
    """
    children_url = f"/api/ds/organizational-units/{_encode_dn(ou_dn)}/organizational-units"
    logger.info(f"Запрос к ALD Pro: GET {children_url}")
    response = await client.get(children_url)
    if response.status_code != 200:
        logger.warning(f"Не удалось получить дочерние подразделения для {ou_dn}: {response.status_code}")
        return []
    data = response.json()
    if not data.get('success'):
        logger.warning(f"ALD Pro вернул success=false для детей {ou_dn}: {data}")
        return []
    return data.get('data', []) or []


async def fetch_child_units(ou_dn: str, client=None) -> list:
    """Дочерние OU подразделения в упрощённом виде [{'dn','name','parent'}].

    В отличие от get_organizational_units здесь НЕ используется флаг
    organizationunitlistitem_is_leaf: обходятся все узлы подряд, поэтому в
    поддерево попадают и те подразделения, у которых ALD Pro ошибочно выставил
    is_leaf=true (из-за чего часть OU раньше не получалась).

    Args:
        ou_dn: DN родительского подразделения.
        client: переиспользуемый клиент (см. get_shared_client); если не
                передан — создаётся временный.

    Returns:
        Список словарей {'dn', 'name', 'parent'}.
    """
    own_client = client is None
    if own_client:
        client = await _get_authenticated_client()
        if not client:
            logger.warning("ALD Pro не настроен — дочерние OU недоступны")
            return []
    try:
        items = await _fetch_children(client, unquote(ou_dn))
    finally:
        if own_client:
            await client.aclose()

    result = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        inner = item.get('organizationunitlistitem')
        src = inner if isinstance(inner, dict) else item
        dn = (src.get('organizationunitlistitem_dn')
              or src.get('organizationunit_dn') or src.get('dn') or '')
        if not dn:
            continue
        name = (src.get('organizationunitlistitem_display_name')
                or src.get('organizationunit_display_name')
                or src.get('organizationunitlistitem_ou')
                or src.get('organizationunit_ou') or '')
        parent = (src.get('organizationunitlistitem_parent_dn')
                  or src.get('organizationunit_parent_dn') or unquote(ou_dn))
        result.append({'dn': unquote(dn), 'name': name, 'parent': parent})
    return result


async def get_organizational_units(root_dn: str = None) -> Dict[str, Any]:
    """
    Получить дерево организационных подразделений.

    Источник данных (проверен на реальном сервере ALD Pro):
    1. GET /api/ds/organizational-units/catalogue/children — корневые объекты
       каталога. Ответ приходит в формате tree_node
       ({treenode_dn, treenode_display_name, treenode_parent_dn, ...}).
       ВАЖНО: поле tree_node_children у корневого объекта на реальной версии
       ALD Pro заполняется только для поддерева cn=accounts (техподдержка),
       поэтому для доменных OU (например, ou=nedra.net,cn=orgunits,...)
       дальше используется рекурсивный обход.
    2. Для каждого узла дети запрашиваются через
       GET /api/ds/organizational-units/{dn}/organizational-units (раздел 7.9) —
       этот эндпоинт возвращает organizationunitlistitem_* поля непосредственных
       дочерних OU. Обход идёт параллельно (asyncio.gather) и только для узлов
       с organizationunitlistitem_is_leaf=false, что резко сокращает число
       запросов к большим каталогам.

    Args:
        root_dn: DN узла, от которого строится дерево (опционально).
                 Если не указан — используются корневые объекты каталога.

    Returns:
        Dict {'success': True, 'data': [...]} — список корней дерева, где
        каждая запись содержит organizationunitlistitem_* поля и children.
    """
    import asyncio

    client = await _get_authenticated_client()
    if not client:
        return {'success': False, 'detail': 'ALD Pro не настроен'}

    try:
        async def fetch_ou_tree(ou_dn: str, info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            """Рекурсивно построить запись подразделения со всеми потомками.

            info — уже известные атрибуты узла (из ответа родителя или
            catalogue/children); если не переданы — запрашиваются отдельно.
            Дочерние узлы обходятся параллельно.
            """
            if info is None:
                ou_info_url = f"/api/ds/organizational-units/{_encode_dn(ou_dn)}"
                logger.info(f"Запрос к ALD Pro: GET {ou_info_url}")
                ou_info_response = await client.get(ou_info_url)
                ou_info_data = {}
                if ou_info_response.status_code == 200:
                    body = ou_info_response.json()
                    if body.get('success'):
                        ou_info_data = body.get('data', {}) or {}
                    else:
                        logger.warning(f"ALD Pro вернул success=false для {ou_dn}: {body}")
                else:
                    logger.warning(
                        f"Не удалось получить информацию о подразделении "
                        f"{ou_dn}: {ou_info_response.status_code}"
                    )
                info = ou_info_data or _make_stub_unit(ou_dn)

            children_list = await _fetch_children(client, ou_dn)

            current_unit = {
                'organizationunitlistitem_dn': info.get('organizationunit_dn', ou_dn),
                'organizationunitlistitem_parent_dn': info.get('organizationunit_parent_dn', ''),
                'organizationunitlistitem_display_name': info.get('organizationunit_display_name', ''),
                'organizationunitlistitem_is_leaf': (
                    info.get('organizationunit_is_leaf')
                    if info.get('organizationunit_is_leaf') is not None
                    else len(children_list) == 0
                ),
                'organizationunitlistitem_ou': info.get('organizationunit_ou', ''),
                'children': [],
            }

            # Рекурсивно обрабатываем дочерние подразделения.
            # Атрибуты ребёнка уже есть в ответе родителя — не запрашиваем их
            # повторно; листья (is_leaf=true) не обходим вовсе.
            tasks = []
            for child_item in children_list:
                child_dn = child_item.get('organizationunitlistitem_dn', '')
                if not child_dn:
                    continue
                child_is_leaf = bool(child_item.get('organizationunitlistitem_is_leaf'))
                child_info = {
                    'organizationunit_dn': child_dn,
                    'organizationunit_parent_dn': child_item.get('organizationunitlistitem_parent_dn', ''),
                    'organizationunit_display_name': child_item.get('organizationunitlistitem_display_name', ''),
                    'organizationunit_is_leaf': child_is_leaf,
                    'organizationunit_ou': child_item.get('organizationunitlistitem_ou', ''),
                }
                if child_is_leaf:
                    # У листа нет детей — добавляем сразу без сетевого запроса
                    current_unit['children'].append({**child_info,
                                                     'organizationunitlistitem_dn': child_dn,
                                                     'organizationunitlistitem_parent_dn': child_info['organizationunit_parent_dn'],
                                                     'organizationunitlistitem_display_name': child_info['organizationunit_display_name'],
                                                     'organizationunitlistitem_is_leaf': True,
                                                     'organizationunitlistitem_ou': child_info['organizationunit_ou'],
                                                     'children': []})
                    # Приводим ключи к формату listitem
                    last = current_unit['children'][-1]
                    for k in ('organizationunit_dn', 'organizationunit_parent_dn',
                              'organizationunit_display_name', 'organizationunit_is_leaf',
                              'organizationunit_ou'):
                        last.pop(k, None)
                else:
                    tasks.append(fetch_ou_tree(child_dn, info=child_info))

            if tasks:
                results = await asyncio.gather(*tasks)
                for res in results:
                    if res:
                        current_unit['children'].append(res)

            return current_unit

        def count_nodes(nodes):
            return sum(1 + count_nodes(n.get('children') or []) for n in nodes)

        # Если root_dn указан, строим дерево начиная с него
        if root_dn:
            tree = [await fetch_ou_tree(unquote(root_dn))]
            logger.info(f"Построено дерево подразделений: корней={len(tree)}, всего узлов={count_nodes(tree)}")
            return {'success': True, 'data': tree}

        # Иначе получаем корневые объекты каталога.
        # Согласно документации эндпоинт — /api/ds/organizational-units/catalogue/children;
        # ради совместимости пробуем также вариант написания без дефиса.
        catalogue_data = None
        last_status = None
        for catalogue_url in ("/api/ds/organizational-units/catalogue/children",
                              "/api/ds/organizationalunits/catalogue/children"):
            logger.info(f"Запрос к ALD Pro: GET {catalogue_url}")
            catalogue_response = await client.get(catalogue_url)
            last_status = catalogue_response.status_code
            if catalogue_response.status_code != 200:
                logger.warning(f"Каталог подразделений недоступен ({catalogue_url}): HTTP {last_status}")
                continue
            body = catalogue_response.json()
            if not body.get('success'):
                logger.warning(f"ALD Pro вернул success=false для {catalogue_url}: {body}")
                continue
            catalogue_data = body.get('data', []) or []
            break

        if catalogue_data is None:
            return {'success': False, 'detail': f'HTTP {last_status}'}

        def convert_catalogue_node(item: Dict[str, Any]) -> Dict[str, Any]:
            """Конвертировать tree_node из ответа каталога в формат записей дерева."""
            node = item.get('tree_node') or {}
            record = {
                'organizationunitlistitem_dn': node.get('treenode_dn', ''),
                'organizationunitlistitem_parent_dn': node.get('treenode_parent_dn') or '',
                'organizationunitlistitem_display_name': node.get('treenode_display_name', ''),
                'organizationunitlistitem_is_leaf': bool(node.get('treenode_is_leaf', False)),
                'organizationunitlistitem_ou': node.get('treenode_display_name', ''),
                'children': [convert_catalogue_node(c) for c in (item.get('tree_node_children') or [])
                             if c.get('tree_node')],
            }
            if record['children']:
                record['organizationunitlistitem_is_leaf'] = False
            return record

        # Формируем корни: если у корневого узла пришёл непустой
        # tree_node_children (как у домена nedra.net) — используем его как
        # готовое поддерево; иначе узел будет добран рекурсивным обходом ниже.
        roots = []           # [(dn, prebuilt_children | None)]
        for root_item in catalogue_data:
            node = root_item.get('tree_node') or {}
            dn = node.get('treenode_dn', '') or root_item.get('organizationunitlistitem_dn', '')
            if not dn:
                continue
            raw_children = root_item.get('tree_node_children') or []
            prebuilt = [convert_catalogue_node(c) for c in raw_children if c.get('tree_node')]
            roots.append((dn, prebuilt))

        async def build_root(dn: str, prebuilt: list) -> Dict[str, Any]:
            root_unit = await fetch_ou_tree(dn)
            if prebuilt and not root_unit['children']:
                root_unit['children'] = prebuilt
                root_unit['organizationunitlistitem_is_leaf'] = False
            elif prebuilt and root_unit['children']:
                # объединяем без дублей по DN
                have = {c['organizationunitlistitem_dn'] for c in root_unit['children']}
                for c in prebuilt:
                    if c['organizationunitlistitem_dn'] not in have:
                        root_unit['children'].append(c)
            return root_unit

        tree = await asyncio.gather(*(build_root(dn, kids) for dn, kids in roots))
        tree = [t for t in tree if t]

        logger.info(f"Построено дерево подразделений: корней={len(tree)}, всего узлов={count_nodes(tree)}")
        return {'success': True, 'data': tree}

    except Exception as e:
        logger.error(f"Ошибка при получении подразделений ALD Pro: {e}", exc_info=True)
        return {'success': False, 'detail': str(e)}
    finally:
        await client.aclose()


def build_ou_tree(units: list) -> list:
    """
    Построить иерархическое дерево подразделений из плоского списка.
    
    Args:
        units: Плоский список подразделений с полями:
               - organizationunitlistitem_dn (DN подразделения)
               - organizationunitlistitem_parent_dn (DN родительского подразделения)
               - organizationunitlistitem_display_name (Отображаемое имя)
               - organizationunitlistitem_is_leaf (Является ли конечным)
               - organizationunitlistitem_ou (Имя OU)
    
    Returns:
        Иерархический список с вложенными children
    """
    if not units:
        return []
    
    # Создаем словарь для быстрого доступа по DN
    unit_map = {}
    for unit in units:
        dn = unit.get('organizationunitlistitem_dn', '')
        if dn:  # Пропускаем записи без DN
            unit_map[dn] = {**unit, 'children': []}
    
    # Строим дерево
    root_units = []
    for dn, unit in unit_map.items():
        parent_dn = unit.get('organizationunitlistitem_parent_dn', '')
        
        # Если есть родитель и он существует в словаре
        if parent_dn and parent_dn in unit_map:
            unit_map[parent_dn]['children'].append(unit)
        else:
            # Корневое подразделение (нет родителя или родитель не найден)
            root_units.append(unit)
    
    # Сортируем корневые подразделения по имени
    root_units.sort(key=lambda x: x.get('organizationunitlistitem_display_name') or x.get('organizationunitlistitem_ou') or '')
    
    # Рекурсивно сортируем все дочерние подразделения
    def sort_children(node):
        if node.get('children'):
            node['children'].sort(key=lambda x: x.get('organizationunitlistitem_display_name') or x.get('organizationunitlistitem_ou') or '')
            for child in node['children']:
                sort_children(child)
    
    for root in root_units:
        sort_children(root)
    
    return root_units


async def get_organizational_unit_users(ou_dn: str, client=None) -> Dict[str, Any]:
    """
    Получить список пользователей подразделения.
    
    GET /api/ds/organizational-units/{organizationalUnitDistinguishedName}/users-list
    
    Args:
        ou_dn: DN организационного подразделения (URL-encoded)
        client: переиспользуемый авторизованный клиент (см.
                get_shared_client); если не передан — создаётся
                временный (с отдельной авторизацией).
    """
    own_client = client is None
    if own_client:
        client = await _get_authenticated_client()
    if not client:
        return {'success': False, 'detail': 'ALD Pro не настроен'}
    
    try:
        # DN может прийти как закоданным (из URL), так и в исходном виде — нормализуем
        decoded_dn = unquote(ou_dn)

        endpoint = f"/api/ds/organizational-units/{_encode_dn(decoded_dn)}/users-list"
        response = await client.get(endpoint)
        
        if response.status_code == 200:
            return response.json()
        else:
            logger.warning(f"ALD Pro вернул статус {response.status_code} при получении пользователей: {response.text[:200]}")
            return {'success': False, 'detail': f'HTTP {response.status_code}'}
    except Exception as e:
        logger.error(f"Ошибка при получении пользователей подразделения ALD Pro: {e}")
        return {'success': False, 'detail': str(e)}
    finally:
        if own_client:
            await client.aclose()
