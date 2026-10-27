"""Remote libSQL transport preserving the repository's SQLite row/transaction contract."""
import os
import sqlite3


class Row(tuple):
    def __new__(cls, values, columns):
        row = super().__new__(cls, values)
        row.columns = columns
        return row

    def keys(self):
        return self.columns

    def __getitem__(self, key):
        if isinstance(key,str):
            # sqlite3.Row accepts case-insensitive column names. Remote libSQL may
            # report SQL-keyword identifiers such as `key` as uppercase `KEY`.
            try:key=next(i for i,name in enumerate(self.columns) if name.casefold()==key.casefold())
            except StopIteration:raise IndexError('No item with that key') from None
        return super().__getitem__(key)


def _call(fn, *args):
    import libsql
    try:
        return fn(*args)
    except (libsql.Error, ValueError) as exc:
        text = str(exc)
        # Never expose SQL parameters, credential-bearing URLs or provider diagnostics.
        if any(v in text.upper() for v in ('CONSTRAINT', 'SQLITE_CONSTRAINT')):
            raise sqlite3.IntegrityError('Remote database constraint failed') from None
        raise sqlite3.OperationalError('Remote database operation failed') from None


class Cursor:
    def __init__(self, raw):
        self.raw = raw

    @property
    def lastrowid(self):
        return self.raw.lastrowid

    @property
    def rowcount(self):
        return self.raw.rowcount

    @property
    def description(self):
        return self.raw.description

    def _row(self, value):
        return None if value is None else Row(value, tuple(c[0] for c in self.description))

    def fetchone(self):
        return self._row(_call(self.raw.fetchone))

    def fetchall(self):
        return [self._row(v) for v in _call(self.raw.fetchall)]

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row


class Connection:
    def __init__(self, raw):
        self.raw = raw
        self.closed = False

    def execute(self, sql, parameters=()):
        return Cursor(_call(self.raw.execute, sql, parameters))

    def executemany(self, sql, parameters):
        return Cursor(_call(self.raw.executemany, sql, parameters))

    def executescript(self, sql):
        return Cursor(_call(self.raw.executescript, sql))

    @property
    def in_transaction(self):
        return self.raw.in_transaction

    def commit(self):
        _call(self.raw.commit)

    def rollback(self):
        _call(self.raw.rollback)

    def close(self):
        if not self.closed:
            self.closed = True
            _call(self.raw.close)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        try:
            self.rollback() if exc_type else self.commit()
        finally:
            self.close()


def enabled():
    return os.environ.get('DATABASE_PROVIDER', 'sqlite') == 'turso'


def connect():
    import libsql
    url = os.environ.get('TURSO_DATABASE_URL', '')
    token = os.environ.get('TURSO_AUTH_TOKEN', '')
    if not url.startswith('libsql://') or not token:
        raise RuntimeError('Turso database configuration is missing')
    raw = _call(lambda: libsql.connect(url, auth_token=token))
    con = Connection(raw)
    con.execute('PRAGMA foreign_keys = ON')
    return con
