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

# Глобальное хранилище настроек и сессий
_aldpro_settings = {
    'url': '',
    'login': '',
    'password': ''
}
_aldpro_cookies = {}


def get_settings() -> Dict[str, Any]:
    """Получить текущие настройки ALD Pro."""
    return {
        'url': _aldpro_settings.get('url', ''),
        'login': _aldpro_settings.get('login', ''),
        'password': _aldpro_settings.get('password', '')
    }


def save_settings(url: str, login: str, password: str) -> bool:
    """Сохранить настройки ALD Pro."""
    global _aldpro_settings
    _aldpro_settings['url'] = url
    _aldpro_settings['login'] = login
    _aldpro_settings['password'] = password
    logger.info(f"Настройки ALD Pro сохранены: URL={url}, Login={login}")
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


async def get_organizational_units(root_dn: str = None) -> Dict[str, Any]:
    """
    Получить дерево организационных подразделений.

    Источник данных — единственный запрос (документация API ALD Pro 3.2.0,
    раздел 7.3):

        GET /api/ds/organizational-units/catalogue/children?nodeDistinguishedName={dn}

    Ответ содержит полное дерево узлов вида
    {"tree_node": {treenode_dn, treenode_display_name, treenode_parent_dn,
    treenode_is_leaf, treenode_type}, "tree_node_children": [...]} —
    т.е. вложенность приходит прямо из API, дополнительных запросов на каждый
    уровень не требуется.

    Примечание: раньше дерево строилось обходом
    /organizational-units/{dn}/organizational-units (раздел 7.9), но этот
    эндпоинт возвращает только непосредственных детей и не годится как источник
    полного дерева, а UI отображал лишь корневую запись.

    Args:
        root_dn: DN узла, от которого строится дерево (опционально).
                 Если не указан — используются корневые объекты каталога.

    Returns:
        Dict {'success': True, 'data': [...]} — список корней дерева, где
        каждая запись содержит organizationunitlistitem_* поля и children.
    """
    client = await _get_authenticated_client()
    if not client:
        return {'success': False, 'detail': 'ALD Pro не настроен'}

    try:
        params = {}
        if root_dn:
            params['nodeDistinguishedName'] = unquote(root_dn)

        # Согласно документации эндпоинт — /api/ds/organizational-units/catalogue/children;
        # ради совместимости пробуем также вариант написания без дефиса.
        last_status = None
        for catalogue_url in ("/api/ds/organizational-units/catalogue/children",
                              "/api/ds/organizationalunits/catalogue/children"):
            logger.info(f"Запрос к ALD Pro: GET {catalogue_url} {params or ''}")
            catalogue_response = await client.get(catalogue_url, params=params)
            last_status = catalogue_response.status_code
            if catalogue_response.status_code != 200:
                logger.warning(f"Каталог подразделений недоступен ({catalogue_url}): HTTP {last_status}")
                continue
            body = catalogue_response.json()
            if not body.get('success'):
                logger.warning(f"ALD Pro вернул success=false для {catalogue_url}: {body}")
                continue

            def convert(item: Dict[str, Any]) -> Dict[str, Any]:
                node = item.get('tree_node') or {}
                record = _node_from_treenode(node)
                children = item.get('tree_node_children') or []
                record['children'] = [convert(c) for c in children if c.get('tree_node')]
                if children:
                    record['organizationunitlistitem_is_leaf'] = False
                return record

            tree = [convert(item) for item in (body.get('data') or [])
                    if item.get('tree_node')]

            def count(nodes):
                return sum(1 + count(n['children']) for n in nodes)

            logger.info(f"Построено дерево подразделений: корней={len(tree)}, всего узлов={count(tree)}")
            return {'success': True, 'data': tree}

        return {'success': False, 'detail': f'HTTP {last_status}'}

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


async def get_organizational_unit_users(ou_dn: str) -> Dict[str, Any]:
    """
    Получить список пользователей подразделения.
    
    GET /api/ds/organizational-units/{organizationalUnitDistinguishedName}/users-list
    
    Args:
        ou_dn: DN организационного подразделения (URL-encoded)
    """
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
        await client.aclose()
