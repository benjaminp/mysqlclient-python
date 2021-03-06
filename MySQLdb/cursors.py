"""MySQLdb Cursors

This module implements Cursors of various types for MySQLdb. By
default, MySQLdb uses the Cursor class.
"""
from __future__ import print_function, absolute_import
from functools import partial
import re
import sys

from .compat import unicode
from ._mysql_exceptions import (
    Warning, Error, InterfaceError, DataError,
    DatabaseError, OperationalError, IntegrityError, InternalError,
    NotSupportedError, ProgrammingError)


PY2 = sys.version_info[0] == 2
if PY2:
    text_type = unicode
else:
    text_type = str


#: Regular expression for :meth:`Cursor.executemany`.
#: executemany only supports simple bulk insert.
#: You can use it to load large dataset.
RE_INSERT_VALUES = re.compile(
    r"\s*((?:INSERT|REPLACE)\b.+\bVALUES?\s*)" +
    r"(\(\s*(?:%s|%\(.+\)s)\s*(?:,\s*(?:%s|%\(.+\)s)\s*)*\))" +
    r"(\s*(?:ON DUPLICATE.*)?);?\s*\Z",
    re.IGNORECASE | re.DOTALL)


class BaseCursor(object):
    """A base for Cursor classes. Useful attributes:

    description
        A tuple of DB API 7-tuples describing the columns in
        the last executed query; see PEP-249 for details.

    description_flags
        Tuple of column flags for last query, one entry per column
        in the result set. Values correspond to those in
        MySQLdb.constants.FLAG. See MySQL documentation (C API)
        for more information. Non-standard extension.

    arraysize
        default number of rows fetchmany() will fetch
    """

    #: Max stetement size which :meth:`executemany` generates.
    #:
    #: Max size of allowed statement is max_allowed_packet - packet_header_size.
    #: Default value of max_allowed_packet is 1048576.
    max_stmt_length = 64*1024

    from ._mysql_exceptions import (
        MySQLError, Warning, Error, InterfaceError,
        DatabaseError, DataError, OperationalError, IntegrityError,
        InternalError, ProgrammingError, NotSupportedError,
    )

    connection = None

    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.description_flags = None
        self.rowcount = -1
        self.arraysize = 1
        self._executed = None
        self.lastrowid = None
        self.messages = []
        self._result = None
        self._warnings = None
        self.rownumber = None
        self._rows = None

    def close(self):
        """Close the cursor. No further queries will be possible."""
        try:
            if self.connection is None:
                return
            while self.nextset():
                pass
        finally:
            self.connection = None
            self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        del exc_info
        self.close()

    def _ensure_bytes(self, x, encoding=None):
        if isinstance(x, text_type):
            x = x.encode(encoding)
        elif isinstance(x, (tuple, list)):
            x = type(x)(self._ensure_bytes(v, encoding=encoding) for v in x)
        return x

    def _escape_args(self, args, conn):
        ensure_bytes = partial(self._ensure_bytes, encoding=conn.encoding)

        if isinstance(args, (tuple, list)):
            if PY2:
                args = tuple(map(ensure_bytes, args))
            return tuple(conn.literal(arg) for arg in args)
        elif isinstance(args, dict):
            if PY2:
                args = dict((ensure_bytes(key), ensure_bytes(val)) for
                            (key, val) in args.items())
            return dict((key, conn.literal(val)) for (key, val) in args.items())
        else:
            # If it's not a dictionary let's try escaping it anyways.
            # Worst case it will throw a Value error
            if PY2:
                args = ensure_bytes(args)
            return conn.literal(args)

    def _check_executed(self):
        if not self._executed:
            raise ProgrammingError("execute() first")

    def nextset(self):
        """Advance to the next result set.

        Returns None if there are no more result sets.
        """
        if self._executed:
            self.fetchall()
        del self.messages[:]

        db = self._get_db()
        nr = db.next_result()
        if nr == -1:
            return None
        self._do_get_result(db)
        self._post_get_result()
        return 1

    def _do_get_result(self, db):
        self._result = result = self._get_result()
        if result is None:
            self.description = self.description_flags = None
        else:
            self.description = result.describe()
            self.description_flags = result.field_flags()

        self.rowcount = db.affected_rows()
        self.rownumber = 0
        self.lastrowid = db.insert_id()
        self._warnings = None

    def _post_get_result(self):
        pass

    def setinputsizes(self, *args):
        """Does nothing, required by DB API."""

    def setoutputsizes(self, *args):
        """Does nothing, required by DB API."""

    def _get_db(self):
        con = self.connection
        if con is None:
            raise ProgrammingError("cursor closed")
        return con

    def execute(self, query, args=None):
        """Execute a query.

        query -- string, query to execute on server
        args -- optional sequence or mapping, parameters to use with query.

        Note: If args is a sequence, then %s must be used as the
        parameter placeholder in the query. If a mapping is used,
        %(key)s must be used as the placeholder.

        Returns integer represents rows affected, if any
        """
        while self.nextset():
            pass
        db = self._get_db()

        # NOTE:
        # Python 2: query should be bytes when executing %.
        # All unicode in args should be encoded to bytes on Python 2.
        # Python 3: query should be str (unicode) when executing %.
        # All bytes in args should be decoded with ascii and surrogateescape on Python 3.
        # db.literal(obj) always returns str.

        if PY2 and isinstance(query, unicode):
            query = query.encode(db.encoding)

        if args is not None:
            if isinstance(args, dict):
                args = dict((key, db.literal(item)) for key, item in args.items())
            else:
                args = tuple(map(db.literal, args))
            if not PY2 and isinstance(query, (bytes, bytearray)):
                query = query.decode(db.encoding)
            try:
                query = query % args
            except TypeError as m:
                raise ProgrammingError(str(m))

        if isinstance(query, unicode):
            query = query.encode(db.encoding, 'surrogateescape')

        res = self._query(query)
        return res

    def executemany(self, query, args):
        # type: (str, list) -> int
        """Execute a multi-row query.

        :param query: query to execute on server
        :param args:  Sequence of sequences or mappings.  It is used as parameter.
        :return: Number of rows affected, if any.

        This method improves performance on multiple-row INSERT and
        REPLACE. Otherwise it is equivalent to looping over args with
        execute().
        """
        del self.messages[:]

        if not args:
            return

        m = RE_INSERT_VALUES.match(query)
        if m:
            q_prefix = m.group(1) % ()
            q_values = m.group(2).rstrip()
            q_postfix = m.group(3) or ''
            assert q_values[0] == '(' and q_values[-1] == ')'
            return self._do_execute_many(q_prefix, q_values, q_postfix, args,
                                         self.max_stmt_length,
                                         self._get_db().encoding)

        self.rowcount = sum(self.execute(query, arg) for arg in args)
        return self.rowcount

    def _do_execute_many(self, prefix, values, postfix, args, max_stmt_length, encoding):
        conn = self._get_db()
        escape = self._escape_args
        if isinstance(prefix, text_type):
            prefix = prefix.encode(encoding)
        if PY2 and isinstance(values, text_type):
            values = values.encode(encoding)
        if isinstance(postfix, text_type):
            postfix = postfix.encode(encoding)
        sql = bytearray(prefix)
        args = iter(args)
        v = values % escape(next(args), conn)
        if isinstance(v, text_type):
            if PY2:
                v = v.encode(encoding)
            else:
                v = v.encode(encoding, 'surrogateescape')
        sql += v
        rows = 0
        for arg in args:
            v = values % escape(arg, conn)
            if isinstance(v, text_type):
                if PY2:
                    v = v.encode(encoding)
                else:
                    v = v.encode(encoding, 'surrogateescape')
            if len(sql) + len(v) + len(postfix) + 1 > max_stmt_length:
                rows += self.execute(sql + postfix)
                sql = bytearray(prefix)
            else:
                sql += b','
            sql += v
        rows += self.execute(sql + postfix)
        self.rowcount = rows
        return rows

    def callproc(self, procname, args=()):
        """Execute stored procedure procname with args

        procname -- string, name of procedure to execute on server

        args -- Sequence of parameters to use with procedure

        Returns the original args.

        Compatibility warning: PEP-249 specifies that any modified
        parameters must be returned. This is currently impossible
        as they are only available by storing them in a server
        variable and then retrieved by a query. Since stored
        procedures return zero or more result sets, there is no
        reliable way to get at OUT or INOUT parameters via callproc.
        The server variables are named @_procname_n, where procname
        is the parameter above and n is the position of the parameter
        (from zero). Once all result sets generated by the procedure
        have been fetched, you can issue a SELECT @_procname_0, ...
        query using .execute() to get any OUT or INOUT values.

        Compatibility warning: The act of calling a stored procedure
        itself creates an empty result set. This appears after any
        result sets generated by the procedure. This is non-standard
        behavior with respect to the DB-API. Be sure to use nextset()
        to advance through all result sets; otherwise you may get
        disconnected.
        """

        db = self._get_db()
        if args:
            fmt = '@_{0}_%d=%s'.format(procname)
            q = 'SET %s' % ','.join(fmt % (index, db.literal(arg))
                                    for index, arg in enumerate(args))
            if isinstance(q, unicode):
                q = q.encode(db.encoding, 'surrogateescape')
            self._query(q)
            self.nextset()

        q = "CALL %s(%s)" % (procname,
                             ','.join(['@_%s_%d' % (procname, i)
                                       for i in range(len(args))]))
        if isinstance(q, unicode):
            q = q.encode(db.encoding, 'surrogateescape')
        self._query(q)
        return args

    def _query(self, q):
        db = self._get_db()
        self._result = None
        db.query(q)
        self._do_get_result(db)
        self._post_get_result()
        self._executed = q
        return self.rowcount

    def _fetch_row(self, size=1):
        if not self._result:
            return ()
        return self._result.fetch_row(size, self._fetch_type)

    def __iter__(self):
        return iter(self.fetchone, None)

    Warning = Warning
    Error = Error
    InterfaceError = InterfaceError
    DatabaseError = DatabaseError
    DataError = DataError
    OperationalError = OperationalError
    IntegrityError = IntegrityError
    InternalError = InternalError
    ProgrammingError = ProgrammingError
    NotSupportedError = NotSupportedError


class CursorStoreResultMixIn(object):
    """This is a MixIn class which causes the entire result set to be
    stored on the client side, i.e. it uses mysql_store_result(). If the
    result set can be very large, consider adding a LIMIT clause to your
    query, or using CursorUseResultMixIn instead."""

    def _get_result(self):
        return self._get_db().store_result()

    def _post_get_result(self):
        self._rows = self._fetch_row(0)
        self._result = None

    def fetchone(self):
        """Fetches a single row from the cursor. None indicates that
        no more rows are available."""
        self._check_executed()
        if self.rownumber >= len(self._rows):
            return None
        result = self._rows[self.rownumber]
        self.rownumber = self.rownumber + 1
        return result

    def fetchmany(self, size=None):
        """Fetch up to size rows from the cursor. Result set may be smaller
        than size. If size is not defined, cursor.arraysize is used."""
        self._check_executed()
        end = self.rownumber + (size or self.arraysize)
        result = self._rows[self.rownumber:end]
        self.rownumber = min(end, len(self._rows))
        return result

    def fetchall(self):
        """Fetchs all available rows from the cursor."""
        self._check_executed()
        if self.rownumber:
            result = self._rows[self.rownumber:]
        else:
            result = self._rows
        self.rownumber = len(self._rows)
        return result

    def scroll(self, value, mode='relative'):
        """Scroll the cursor in the result set to a new position according
        to mode.

        If mode is 'relative' (default), value is taken as offset to
        the current position in the result set, if set to 'absolute',
        value states an absolute target position."""
        self._check_executed()
        if mode == 'relative':
            r = self.rownumber + value
        elif mode == 'absolute':
            r = value
        else:
            raise ProgrammingError("unknown scroll mode %s" % repr(mode))
        if r < 0 or r >= len(self._rows):
            raise IndexError("out of range")
        self.rownumber = r

    def __iter__(self):
        self._check_executed()
        result = self.rownumber and self._rows[self.rownumber:] or self._rows
        return iter(result)


class CursorUseResultMixIn(object):

    """This is a MixIn class which causes the result set to be stored
    in the server and sent row-by-row to client side, i.e. it uses
    mysql_use_result(). You MUST retrieve the entire result set and
    close() the cursor before additional queries can be performed on
    the connection."""

    def _get_result(self):
        return self._get_db().use_result()

    def fetchone(self):
        """Fetches a single row from the cursor."""
        self._check_executed()
        r = self._fetch_row(1)
        if not r:
            return None
        self.rownumber = self.rownumber + 1
        return r[0]

    def fetchmany(self, size=None):
        """Fetch up to size rows from the cursor. Result set may be smaller
        than size. If size is not defined, cursor.arraysize is used."""
        self._check_executed()
        r = self._fetch_row(size or self.arraysize)
        self.rownumber = self.rownumber + len(r)
        return r

    def fetchall(self):
        """Fetchs all available rows from the cursor."""
        self._check_executed()
        r = self._fetch_row(0)
        self.rownumber = self.rownumber + len(r)
        return r

    def __iter__(self):
        return self

    def next(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    __next__ = next


class CursorTupleRowsMixIn(object):
    """This is a MixIn class that causes all rows to be returned as tuples,
    which is the standard form required by DB API."""

    _fetch_type = 0


class CursorDictRowsMixIn(object):
    """This is a MixIn class that causes all rows to be returned as
    dictionaries. This is a non-standard feature."""

    _fetch_type = 1


class Cursor(CursorStoreResultMixIn, CursorTupleRowsMixIn,
             BaseCursor):
    """This is the standard Cursor class that returns rows as tuples
    and stores the result set in the client."""


class DictCursor(CursorStoreResultMixIn, CursorDictRowsMixIn,
                 BaseCursor):
     """This is a Cursor class that returns rows as dictionaries and
    stores the result set in the client."""


class SSCursor(CursorUseResultMixIn, CursorTupleRowsMixIn,
               BaseCursor):
    """This is a Cursor class that returns rows as tuples and stores
    the result set in the server."""


class SSDictCursor(CursorUseResultMixIn, CursorDictRowsMixIn,
                   BaseCursor):
    """This is a Cursor class that returns rows as dictionaries and
    stores the result set in the server."""
