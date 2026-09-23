"""
Модуль для работы с ALD Pro 3.2.0 API
"""
import httpx
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger('admin_helper')

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


async def get_organizational_units(root_dn: str = None) -> Dict[str, Any]:
    """
    Получить дерево организационных подразделений.
    
    Принцип работы:
    1. Сначала получаем список корневых подразделений через /api/ds/organizationalunits/catalogue/children
    2. Для каждого подразделения получаем его дочерние элементы через /api/ds/organizational-units/{dn}/organizational-units
    
    Args:
        root_dn: DN корневого подразделения (опционально, если не указан - получаем все корневые OU)
    
    Returns:
        Dict с древовидной структурой подразделений
    """
    client = await _get_authenticated_client()
    if not client:
        return {'success': False, 'detail': 'ALD Pro не настроен'}
    
    try:
        # Функция для рекурсивного получения подразделений
        async def fetch_ou_tree(ou_dn: str) -> list:
            """Рекурсивно получает подразделение и все его дочерние элементы."""
            # Получаем информацию о текущем подразделении
            ou_info_url = f"/api/ds/organizational-units/{ou_dn}"
            logger.info(f"Запрос к ALD Pro: GET {ou_info_url}")
            
            ou_info_response = await client.get(ou_info_url)
            if ou_info_response.status_code != 200:
                logger.warning(f"Не удалось получить информацию о подразделении {ou_dn}: {ou_info_response.status_code}")
                # Если не удалось получить информацию, создаем заглушку
                ou_info = {
                    'organizationunit_dn': ou_dn,
                    'organizationunit_parent_dn': '',
                    'organizationunit_display_name': ou_dn.split(',')[0].replace('ou=', ''),
                    'organizationunit_is_leaf': False,
                    'organizationunit_ou': ou_dn.split(',')[0].replace('ou=', '')
                }
            else:
                ou_info_data = ou_info_response.json()
                if ou_info_data.get('success'):
                    ou_info = ou_info_data.get('data', {})
                else:
                    logger.warning(f"ALD Pro вернул success=false для {ou_dn}: {ou_info_data}")
                    ou_info = {
                        'organizationunit_dn': ou_dn,
                        'organizationunit_parent_dn': '',
                        'organizationunit_display_name': ou_dn.split(',')[0].replace('ou=', ''),
                        'organizationunit_is_leaf': False,
                        'organizationunit_ou': ou_dn.split(',')[0].replace('ou=', '')
                    }
            
            # Получаем список дочерних подразделений
            children_url = f"/api/ds/organizational-units/{ou_dn}/organizational-units"
            logger.info(f"Запрос к ALD Pro: GET {children_url}")
            
            children_response = await client.get(children_url)
            if children_response.status_code != 200:
                logger.warning(f"Не удалось получить дочерние подразделения для {ou_dn}: {children_response.status_code}")
                children_list = []
            else:
                children_data = children_response.json()
                if children_data.get('success'):
                    children_list = children_data.get('data', [])
                else:
                    logger.warning(f"ALD Pro вернул success=false для детей {ou_dn}: {children_data}")
                    children_list = []
            
            # Преобразуем текущее подразделение в нужный формат
            current_unit = {
                'organizationunitlistitem_dn': ou_info.get('organizationunit_dn', ou_dn),
                'organizationunitlistitem_parent_dn': ou_info.get('organizationunit_parent_dn', ''),
                'organizationunitlistitem_display_name': ou_info.get('organizationunit_display_name', ''),
                'organizationunitlistitem_is_leaf': ou_info.get('organizationunit_is_leaf', len(children_list) == 0),
                'organizationunitlistitem_ou': ou_info.get('organizationunit_ou', ''),
                'children': []
            }
            
            # Рекурсивно обрабатываем дочерние подразделения
            for child_item in children_list:
                child_dn = child_item.get('organizationunitlistitem_dn', '')
                if child_dn:
                    # Рекурсивно получаем дерево для каждого дочернего элемента
                    child_tree = await fetch_ou_tree(child_dn)
                    if child_tree:
                        current_unit['children'].extend(child_tree)
            
            return [current_unit]
        
        # Если root_dn указан, получаем дерево начиная с него
        if root_dn:
            tree = await fetch_ou_tree(root_dn)
            logger.info(f"Построено дерево подразделений: {tree}")
            return {'success': True, 'data': tree}
        
        # Если root_dn не указан, получаем все корневые подразделения через catalogue/children
        logger.info("Запрос к ALD Pro: GET /api/ds/organizationalunits/catalogue/children")
        catalogue_url = "/api/ds/organizationalunits/catalogue/children"
        catalogue_response = await client.get(catalogue_url)
        
        if catalogue_response.status_code != 200:
            logger.warning(f"Не удалось получить каталог подразделений: {catalogue_response.status_code}")
            return {'success': False, 'detail': f'HTTP {catalogue_response.status_code}'}
        
        catalogue_data = catalogue_response.json()
        if not catalogue_data.get('success'):
            logger.warning(f"ALD Pro вернул success=false для catalogue/children: {catalogue_data}")
            return {'success': False, 'detail': 'ALD Pro вернул ошибку при получении каталога'}
        
        # Получаем список корневых подразделений
        root_units_list = catalogue_data.get('data', [])
        
        # Для каждого корневого подразделения строим дерево
        tree = []
        for root_item in root_units_list:
            root_dn = root_item.get('organizationunitlistitem_dn', '')
            if root_dn:
                root_tree = await fetch_ou_tree(root_dn)
                if root_tree:
                    tree.extend(root_tree)
        
        logger.info(f"Построено дерево подразделений: {tree}")
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
        # DN уже закодирован в URL, декодируем для использования в пути
        from urllib.parse import unquote
        decoded_dn = unquote(ou_dn)
        
        endpoint = f"/api/ds/organizational-units/{decoded_dn}/users-list"
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
