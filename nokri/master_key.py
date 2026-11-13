"""Resolve an optional MASTER_KEY without using a shared hard-coded secret."""

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def master_key_path(database_path):
    """Keep the generated key on the same persistent volume as the database."""
    return Path(database_path).parent / '.master_key'


def resolve_master_key(db, configured=None):
    """Use an explicit key or atomically create and reuse a private local one.

    Never mint a replacement for an existing encrypted database: doing so would
    silently strand its stored AnyModel credentials.
    """
    path = master_key_path(db.path)
    if configured:
        key = configured.encode() if isinstance(configured, str) else configured
    else:
        if path.is_symlink():
            raise RuntimeError(f'Файл ключа {path} не должен быть символической ссылкой')
        if path.exists():
            if not path.is_file():
                raise RuntimeError(f'Файл ключа {path} не является обычным файлом')
            if os.name == 'posix' and path.stat().st_mode & 0o077:
                raise RuntimeError(f'Слишком широкие права на {path}. Выполните chmod 600 {path}')
            key = path.read_bytes().strip()
        else:
            if db.rows('SELECT 1 FROM secrets LIMIT 1'):
                raise RuntimeError('В БД есть зашифрованные API-ключи, но MASTER_KEY и .master_key отсутствуют. Восстановите прежний ключ')
            candidate = Fernet.generate_key()
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if path.is_symlink():
                    raise RuntimeError(f'Файл ключа {path} не должен быть символической ссылкой') from None
                if os.name == 'posix' and path.stat().st_mode & 0o077:
                    raise RuntimeError(f'Слишком широкие права на {path}. Выполните chmod 600 {path}') from None
                key = path.read_bytes().strip()
            else:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(candidate + b'\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                key = candidate
    try:
        fernet = Fernet(key)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError('MASTER_KEY или .master_key содержит некорректный ключ Fernet') from exc
    encrypted = db.rows('SELECT cipher FROM secrets LIMIT 1')
    if encrypted:
        try:
            fernet.decrypt(encrypted[0]['cipher'].encode())
        except InvalidToken as exc:
            raise RuntimeError('MASTER_KEY не соответствует зашифрованному API-ключу в базе. Восстановите прежний ключ') from exc
    return key
