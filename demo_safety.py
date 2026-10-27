"""Offline boundary used only by the opt-in, loopback showcase launcher."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import socket


def demo_environment(root: Path, data_dir: Path) -> dict[str, str]:
    """Build an allowlisted environment; never inherit provider or pilot settings."""
    allowed = ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'VIRTUAL_ENV',
               'LANG', 'LC_ALL', 'LD_LIBRARY_PATH', 'SSL_CERT_FILE')
    result = {key: os.environ[key] for key in allowed if key in os.environ}
    temp = data_dir / 'tmp'
    temp.mkdir(parents=True, exist_ok=True)
    result.update({
        'PYTHONPATH': str(root), 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'TMP': str(temp), 'TEMP': str(temp), 'TMPDIR': str(temp),
        'HOME': str(data_dir), 'USERPROFILE': str(data_dir),
        'APP_ENV': 'local', 'OPPORTUNITY_DEMO': '1',
        'APP_DATA_DIR': str(data_dir), 'OPPORTUNITY_DB': str(data_dir / 'jobs.db'),
        'DATABASE_PROVIDER': 'sqlite', 'STORAGE_PROVIDER': 'local',
        'AI_PROVIDER': 'disabled', 'LOCAL_PILOT_USERS': '0',
        'APP_NAME': 'Opportunity Engine', 'LOG_LEVEL': 'WARNING',
    })
    return result


def is_loopback(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode('ascii', errors='strict')
    if str(host).rstrip('.').lower() == 'localhost':
        return True
    try:
        address = ipaddress.ip_address(str(host).split('%')[0])
        return address.is_loopback or bool(
            getattr(address, 'ipv4_mapped', None) and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


def install_network_guard() -> None:
    """Reject external DNS and connections before any application code is imported."""
    if getattr(socket, '_opportunity_demo_guard', False):
        return
    original_dns = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_dns(host, *args, **kwargs):
        # Passive bind discovery does not create an outgoing connection.
        passive = (kwargs.get('flags', 0) | (args[4] if len(args) > 4 else 0)) & socket.AI_PASSIVE
        if host is not None and not is_loopback(host) and not passive:
            raise PermissionError('Offline showcase: external network access is disabled')
        return original_dns(host, *args, **kwargs)

    def check(address):
        if isinstance(address, tuple) and not is_loopback(address[0]):
            raise PermissionError('Offline showcase: external network access is disabled')

    def guarded_connect(sock, address):
        check(address)
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        check(address)
        return original_connect_ex(sock, address)

    socket.getaddrinfo = guarded_dns
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket._opportunity_demo_guard = True


def blocked_demo_write(path: str) -> bool:
    return (
        path.startswith(('/api/search/', '/search/', '/api/ai/', '/api/eval/'))
        or path in ('/api/discovery/run', '/api/work', '/api/schedules', '/settings')
        or (path.startswith('/api/profiles/') and path.endswith('/run'))
        or path.startswith('/api/admin/')
    )
