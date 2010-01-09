# Copyright 2009 10gen, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tools for connecting to MongoDB.

To connect to a single instance of MongoDB use :class:`Connection`. To connect
to a replica pair use :meth:`~Connection.paired`.

.. seealso:: Module :mod:`~pymongo.master_slave_connection` for connecting to
   master-slave clusters.

To get a :class:`~pymongo.database.Database` instance from a
:class:`Connection` use either dictionary-style or attribute-style access:

.. doctest::

  >>> from pymongo import Connection
  >>> c = Connection()
  >>> c.test_database
  Database(Connection('localhost', 27017), u'test_database')
  >>> c['test-database']
  Database(Connection('localhost', 27017), u'test-database')
"""

import sys
import socket
import struct
import types
import threading
import random
import errno
import datetime
import warnings

from .errors import ConnectionFailure, ConfigurationError, AutoReconnect
from .errors import OperationFailure
from .database import Database
from .cursor_manager import CursorManager
from . import bson
from . import message
from . import helpers

_CONNECT_TIMEOUT = 20.0


class Pool(threading.local):
    """A simple connection pool.

    Uses thread-local socket per thread. By calling return_socket() a thread
    can return a socket to the pool.
    """

    # Non thread-locals
    __slots__ = ["sockets", "socket_factory"]
    sockets = []

    sock = None

    def __init__(self, socket_factory):
        self.socket_factory = socket_factory

    def socket(self):
        if self.sock is not None:
            return self.sock

        try:
            self.sock = self.sockets.pop()
        except IndexError:
            self.sock = self.socket_factory()
        return self.sock

    def return_socket(self):
        if self.sock is not None:
            self.sockets.append(self.sock)
        self.sock = None


class Connection(object): # TODO support auth for pooling
    """Connection to MongoDB.
    """

    HOST = "localhost"
    PORT = 27017

    def __init__(self, host=None, port=None, pool_size=None,
                 auto_start_request=None, timeout=None, slave_okay=False,
                 network_timeout=None, _connect=True):
        """Create a new connection to a single MongoDB instance at *host:port*.

        The resultant connection object has connection-pooling built in. It
        also performs auto-reconnection when necessary. If an operation fails
        because of a connection error,
        :class:`~pymongo.errors.ConnectionFailure` is raised. If
        auto-reconnection will be performed,
        :class:`~pymongo.errors.AutoReconnect` will be raised. Application code
        should handle this exception (recognizing that the operation failed)
        and then continue to execute.

        Raises :class:`TypeError` if host is not an instance of string or port
        is not an instance of ``int``. Raises
        :class:`~pymongo.errors.ConnectionFailure` if the connection cannot be
        made.

        :Parameters:
          - `host` (optional): hostname or IPv4 address of the instance to
            connect to
          - `port` (optional): port number on which to connect
          - `pool_size` (optional): DEPRECATED
          - `auto_start_request` (optional): DEPRECATED
          - `slave_okay` (optional): is it okay to connect directly to and
            perform queries on a slave instance
          - `timeout` (optional): DEPRECATED
          - `network_timeout` (optional): timeout (in seconds) to use for socket
            operations - default is no timeout

        .. seealso:: :meth:`end_request`
        .. versionchanged:: 1.3+
           DEPRECATED The `pool_size`, `auto_start_request`, and `timeout`
           parameters.
        .. versionadded:: 1.1
           The `network_timeout` parameter.
        """
        if host is None:
            host = self.HOST
        if port is None:
            port = self.PORT

        if pool_size is not None:
            warnings.warn("The pool_size parameter to Connection is "
                          "deprecated", DeprecationWarning)
        if auto_start_request is not None:
            warnings.warn("The auto_start_request parameter to Connection "
                          "is deprecated", DeprecationWarning)
        if timeout is not None:
            warnings.warn("The timeout parameter to Connection is deprecated",
                          DeprecationWarning)

        if not isinstance(host, str):
            raise TypeError("host must be an instance of (str, unicode)")
        if not isinstance(port, int):
            raise TypeError("port must be an instance of int")

        self.__host = None
        self.__port = None

        self.__nodes = [(host, port)]
        self.__slave_okay = slave_okay

        self.__cursor_manager = CursorManager(self)

        self.__pool = Pool(self.__connect)

        self.__network_timeout = network_timeout

        # cache of existing indexes used by ensure_index ops
        self.__index_cache = {}

        if _connect:
            self.__find_master()

    def __pair_with(self, host, port):
        """Pair this connection with a Mongo instance running on host:port.

        Raises TypeError if host is not an instance of string or port is not an
        instance of int. Raises ConnectionFailure if the connection cannot be
        made.

        :Parameters:
          - `host`: the hostname or IPv4 address of the instance to
            pair with
          - `port`: the port number on which to connect
        """
        if not isinstance(host, str):
            raise TypeError("host must be an instance of str")
        if not isinstance(port, int):
            raise TypeError("port must be an instance of int")
        self.__nodes.append((host, port))

        self.__find_master()

    def paired(cls, left, right=None,
               pool_size=None, auto_start_request=None):
        """Open a new paired connection to Mongo.

        Raises :class:`TypeError` if either `left` or `right` is not a tuple of
        the form ``(host, port)``. Raises :class:`~pymongo.ConnectionFailure`
        if the connection cannot be made.

        :Parameters:
          - `left`: ``(host, port)`` pair for the left MongoDB instance
          - `right` (optional): ``(host, port)`` pair for the right MongoDB
            instance
          - `pool_size` (optional): DEPRECATED
          - `auto_start_request` (optional): DEPRECATED
        """
        if right is None:
            right = (cls.HOST, cls.PORT)
        if pool_size is not None:
            warnings.warn("The pool_size parameter to Connection.paired is "
                          "deprecated", DeprecationWarning)
        if auto_start_request is not None:
            warnings.warn("The auto_start_request parameter to "
                          "Connection.paired is deprecated",
                          DeprecationWarning)

        connection = cls(left[0], left[1], _connect=False)
        connection.__pair_with(*right)
        return connection
    paired = classmethod(paired)

    def __master(self, sock):
        """Get the hostname and port of the master Mongo instance.

        Return a tuple (host, port).
        """
        result = self["admin"].command({"ismaster": 1}, _sock=sock)

        if result["ismaster"] == 1:
            return True
        else:
            if "remote" not in result:
                return False

            strings = result["remote"].split(":", 1)
            if len(strings) == 1:
                port = self.PORT
            else:
                port = int(strings[1])
            return (strings[0], port)

    def _cache_index(self, database, collection, index, ttl):
        """Add an index to the index cache for ensure_index operations.

        Return ``True`` if the index has been newly cached or if the index had
        expired and is being re-cached.

        Return ``False`` if the index exists and is valid.
        """
        now = datetime.datetime.utcnow()
        expire = datetime.timedelta(seconds=ttl) + now

        if database not in self.__index_cache:
            self.__index_cache[database] = {}
            self.__index_cache[database][collection] = {}
            self.__index_cache[database][collection][index] = expire
            return True

        if collection not in self.__index_cache[database]:
            self.__index_cache[database][collection] = {}
            self.__index_cache[database][collection][index] = expire
            return True

        if index in self.__index_cache[database][collection]:
            if now < self.__index_cache[database][collection][index]:
                return False

        self.__index_cache[database][collection][index] = expire
        return True

    def _purge_index(self, database_name,
                     collection_name=None, index_name=None):
        """Purge an index from the index cache.

        If `index_name` is None purge an entire collection.

        If `collection_name` is None purge an entire database.
        """
        if not database_name in self.__index_cache:
            return

        if collection_name is None:
            del self.__index_cache[database_name]
            return

        if not collection_name in self.__index_cache[database_name]:
            return

        if index_name is None:
            del self.__index_cache[database_name][collection_name]
            return

        if index_name in self.__index_cache[database_name][collection_name]:
            del self.__index_cache[database_name][collection_name][index_name]

    def host(self):
        """Current connected host.

        .. versionchanged:: 1.3
           ``host`` is now a property rather than a method. The ``host()``
           method is deprecated.
        """
        return helpers.callable_value(self.__host, "Connection.host")
    host = property(host)

    def port(self):
        """Current connected port.

        .. versionchanged:: 1.3
           ``port`` is now a property rather than a method. The ``port()``
           method is deprecated.
        """
        return helpers.callable_value(self.__port, "Connection.port")
    port = property(port)

    def slave_okay(self):
        """Is it okay for this connection to connect directly to a slave?
        """
        return self.__slave_okay
    slave_okay = property(slave_okay)

    def __find_master(self):
        """Create a new socket and use it to figure out who the master is.

        Sets __host and __port so that :attr:`host` and :attr:`port` will return the
        address of the master.
        """
        self.__host = None
        self.__port = None
        sock = None
        for (host, port) in self.__nodes:
            try:
                try:
                    sock = socket.socket()
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sock.settimeout(_CONNECT_TIMEOUT)
                    try:
                        sock.connect((host, port))
                    except OverflowError as e:
                        raise ConnectionFailure(e)
                    sock.settimeout(self.__network_timeout)
                    master = self.__master(sock)
                    if master is True:
                        self.__host = host
                        self.__port = port
                        return
                    if not master:
                        if self.__slave_okay:
                            self.__host = host
                            self.__port = port
                            return

                        raise ConfigurationError("trying to connect directly to"
                                                 " slave %s:%r - must specify "
                                                 "slave_okay to connect to "
                                                 "slaves" % (host, port))
                    if master not in self.__nodes:
                        raise ConfigurationError(
                            "%r claims master is %r, "
                            "but that's not configured" %
                            ((host, port), master))
                except socket.error as e:
                    continue
            finally:
                if sock is not None:
                    sock.close()
        raise AutoReconnect("could not find master")

    def __connect(self):
        """(Re-)connect to Mongo and return a new (connected) socket.

        Connect to the master if this is a paired connection.
        """
        if self.__host is None or self.__port is None:
            self.__find_master()

        try:
            sock = socket.socket()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(_CONNECT_TIMEOUT)
            sock.connect((self.__host, self.__port))
            sock.settimeout(self.__network_timeout)
            return sock
        except socket.error:
            raise AutoReconnect("could not connect to %r" % self.__nodes)

    def disconnect(self):
        """Disconnect from MongoDB.

        Disconnecting will close all underlying sockets in the
        connection pool. If the :class:`Connection` is used again it
        will be automatically re-opened. Care should be taken to make
        sure that :meth:`disconnect` is not called in the middle of a
        sequence of operations in which ordering is important. This
        could lead to unexpected results.

        .. seealso:: :meth:`end_request`
        .. versionadded:: 1.3
        """
        self.__pool = Pool(self.__connect)

    def _reset(self):
        """Reset everything and start connecting again.

        Closes all open sockets and resets them to None. Re-finds the master.

        This should be done in case of a connection failure or a "not master"
        error.
        """
        self.disconnect()
        self.__find_master()

    def set_cursor_manager(self, manager_class):
        """Set this connection's cursor manager.

        Raises :class:`TypeError` if `manager_class` is not a subclass of
        :class:`~pymongo.cursor_manager.CursorManager`. A cursor manager handles
        closing cursors. Different managers can implement different policies in
        terms of when to actually kill a cursor that has been closed.

        :Parameters:
          - `manager_class`: cursor manager to use
        """
        manager = manager_class(self)
        if not isinstance(manager, CursorManager):
            raise TypeError("manager_class must be a subclass of "
                            "CursorManager")

        self.__cursor_manager = manager

    def __check_response_to_last_error(self, response):
        """Check a response to a lastError message for errors.

        `response` is a byte string representing a response to the message.
        If it represents an error response we raise OperationFailure.
        """
        response = helpers._unpack_response(response)

        assert response["number_returned"] == 1
        error = response["data"][0]

        # TODO unify logic with database.error method
        if error.get("err", 0) is None:
            return
        if error["err"] == "not master":
            self._reset()
        raise OperationFailure(error["err"])

    def _send_message(self, message, with_last_error=False):
        """Say something to Mongo.

        Raises ConnectionFailure if the message cannot be sent. Raises
        OperationFailure if `with_last_error` is ``True`` and the response to
        the getLastError call returns an error.

        :Parameters:
          - `message`: message to send
          - `with_last_error`: check getLastError status after sending the
            message
        """
        sock = self.__pool.socket()
        try:
            (request_id, data) = message
            sock.sendall(data)
            # Safe mode. We pack the message together with a lastError
            # message and send both. We then get the response (to the
            # lastError) and raise OperationFailure if it is an error
            # response.
            if with_last_error:
                response = self.__receive_message_on_socket(1, request_id, sock)
                self.__check_response_to_last_error(response)
        except (ConnectionFailure, socket.error) as e:
            self._reset()
            raise AutoReconnect(str(e))

    def __receive_data_on_socket(self, length, sock):
        """Lowest level receive operation.

        Takes length to receive and repeatedly calls recv until able to
        return a buffer of that length, raising ConnectionFailure on error.
        """
        message = b""
        while len(message) < length:
            chunk = sock.recv(length - len(message))
            if chunk == b"":
                raise ConnectionFailure("connection closed")
            message += chunk
        return message

    def __receive_message_on_socket(self, operation, request_id, sock):
        """Receive a message in response to `request_id` on `sock`.

        Returns the response data with the header removed.
        """
        header = self.__receive_data_on_socket(16, sock)
        length = struct.unpack("<i", header[:4])[0]
        assert request_id == struct.unpack("<i", header[8:12])[0], \
            "ids don't match %r %r" % (request_id,
                                       struct.unpack("<i", header[8:12])[0])
        assert operation == struct.unpack("<i", header[12:])[0]

        return self.__receive_data_on_socket(length - 16, sock)

    def __send_and_receive(self, message, sock):
        """Send a message on the given socket and return the response data.
        """
        (request_id, data) = message
        sock.sendall(data)
        return self.__receive_message_on_socket(1, request_id, sock)

    # we just ignore _must_use_master here: it's only relavant for
    # MasterSlaveConnection instances.
    def _send_message_with_response(self, message,
                                    _sock=None, _must_use_master=False):
        """Send a message to Mongo and return the response.

        Sends the given message and returns the response.

        :Parameters:
          - `message`: (request_id, data) pair making up the message to send
        """
        # hack so we can do find_master on a specific socket...
        reset = False
        if _sock is None:
            reset = True
            _sock = self.__pool.socket()

        try:
            return self.__send_and_receive(message, _sock)
        except (ConnectionFailure, socket.error) as e:
            if reset:
                self._reset()
            raise AutoReconnect(str(e))

    def start_request(self):
        """DEPRECATED all operations will start a request.

        .. versionchanged:: 1.3+
           DEPRECATED
        """
        warnings.warn("the Connection.start_request method is deprecated",
                      DeprecationWarning)

    def end_request(self):
        """Allow this thread's connection to return to the pool.

        Calling :meth:`end_request` allows the :class:`~socket.socket`
        that has been reserved for this thread to be returned to the
        pool. Other threads will then be able to re-use that
        :class:`~socket.socket`. If your application uses many
        threads, or has long-running threads that infrequently perform
        MongoDB operations, then judicious use of this method can lead
        to performance gains. Care should be taken, however, to make
        sure that :meth:`end_request` is not called in the middle of a
        sequence of operations in which ordering is important. This
        could lead to unexpected results.

        One important case is when a thread is dying permanently. It
        is best to call :meth:`end_request` when you know a thread is
        finished, as otherwise its :class:`~socket.socket` will not be
        reclaimed.
        """
        self.__pool.return_socket()

    def __ne__(self, other):
        if isinstance(other, Connection):
            return (self.__host, self.__port) != (other.__host, other.__port)
        return NotImplemented

    def __eq__(self, other):
        return not self.__ne__(other)
    
    def __repr__(self):
        if len(self.__nodes) == 1:
            return "Connection(%r, %r)" % (self.__host, self.__port)
        elif len(self.__nodes) == 2:
            return ("Connection.paired((%r, %r), (%r, %r))" %
                    (self.__nodes[0][0],
                     self.__nodes[0][1],
                     self.__nodes[1][0],
                     self.__nodes[1][1]))

    def __getattr__(self, name):
        """Get a database by name.

        Raises InvalidName if an invalid database name is used.

        :Parameters:
          - `name`: the name of the database to get
        """
        return Database(self, name)

    def __getitem__(self, name):
        """Get a database by name.

        Raises InvalidName if an invalid database name is used.

        :Parameters:
          - `name`: the name of the database to get
        """
        return self.__getattr__(name)

    def close_cursor(self, cursor_id):
        """Close a single database cursor.

        Raises :class:`TypeError` if `cursor_id` is not an instance of ``(int, long)``. What closing the cursor actually means depends on this connection's cursor manager.

        :Parameters:
          - `cursor_id`: id of cursor to close

        .. seealso:: :meth:`set_cursor_manager` and
           the :mod:`~pymongo.cursor_manager` module
        """
        if not isinstance(cursor_id, int):
            raise TypeError("cursor_id must be an instance of (int, long)")

        self.__cursor_manager.close(cursor_id)

    def kill_cursors(self, cursor_ids):
        """Send a kill cursors message with the given ids.

        Raises :class:`TypeError` if `cursor_ids` is not an instance of
        ``list``.

        :Parameters:
          - `cursor_ids`: list of cursor ids to kill
        """
        if not isinstance(cursor_ids, list):
            raise TypeError("cursor_ids must be a list")
        return self._send_message(message.kill_cursors(cursor_ids))

    def __database_info(self):
        """Get a dictionary of (database_name: size_on_disk).
        """
        result = self["admin"].command({"listDatabases": 1})
        info = result["databases"]
        return dict([(db["name"], db["sizeOnDisk"]) for db in info])

    def server_info(self):
        """Get information about the MongoDB server we're connected to.
        """
        return self.admin.command({"buildinfo": 1})

    def database_names(self):
        """Get a list of the names of all databases on the connected server.
        """
        return list(self.__database_info().keys())

    def drop_database(self, name_or_database):
        """Drop a database.

        Raises :class:`TypeError` if `name_or_database` is not an instance of
        ``(str, unicode, Database)``

        :Parameters:
          - `name_or_database`: the name of a database to drop, or a
            :class:`~pymongo.database.Database` instance representing the
            database to drop
        """
        name = name_or_database
        if isinstance(name, Database):
            name = name.name

        if not isinstance(name, str):
            raise TypeError("name_or_database must be an instance of "
                            "(Database, str, unicode)")

        self._purge_index(name)
        self[name].command({"dropDatabase": 1})

    def __iter__(self):
        return self

    def __next__(self):
        raise TypeError("'Connection' object is not iterable")
