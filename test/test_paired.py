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

"""Test pairing support.

These tests are skipped by nose by default (since they depend on having a
paired setup. To run the tests just run this file manually.

Left and right nodes will be $db_ip:$db_port and $db_ip2:$db_port2 or
localhost:27017 and localhost:27018 by default.
"""

import unittest
import logging
import os

try:
    import pymongo
except ImportError:
    import sys
    sys.path.append("")

from pymongo.errors import ConnectionFailure, ConfigurationError
from pymongo.connection import Connection

skip_tests = True

class TestPaired(unittest.TestCase):
    def setUp(self):
#        logging.getLogger("pymongo.connection").setLevel(logging.DEBUG)
        left_host = os.environ.get("db_ip", "localhost")
        left_port = int(os.environ.get("db_port", 27017))
        self.left = (left_host, left_port)
        right_host = os.environ.get("db_ip2", "localhost")
        right_port = int(os.environ.get("db_port2", 27018))
        self.right = (right_host, right_port)
        self.bad = ("somedomainthatdoesntexist.org", 12345)

    def tearDown(self):
        pass
#        logging.getLogger("pymongo.connection").setLevel(logging.NOTSET)

    def skip(self):
        if skip_tests:
            from nose.plugins.skip import SkipTest
            raise SkipTest()

    def test_types(self):
        self.skip()
        self.assertRaises(TypeError, Connection.paired, 5)
        self.assertRaises(TypeError, Connection.paired, "localhost")
        self.assertRaises(TypeError, Connection.paired, None)
        self.assertRaises(TypeError, Connection.paired, 5, self.right)
        self.assertRaises(TypeError, Connection.paired, "localhost", self.right)
        self.assertRaises(TypeError, Connection.paired, None, self.right)
        self.assertRaises(TypeError, Connection.paired, self.left, 5)
        self.assertRaises(TypeError, Connection.paired, self.left, "localhost")
        self.assertRaises(TypeError, Connection.paired, self.left, "localhost")

    def test_connect(self):
        self.skip()
        self.assertRaises(ConnectionFailure, Connection.paired, self.bad, self.bad)

        connection = Connection.paired(self.left, self.right)
        self.assertTrue(connection)

        host = connection.host()
        port = connection.port()

        connection = Connection.paired(self.right, self.left)
        self.assertTrue(connection)
        self.assertEqual(host, connection.host())
        self.assertEqual(port, connection.port())

        slave = self.left == (host, port) and self.right or self.left

        self.assertRaises(ConfigurationError, Connection.paired, slave, self.bad)
        self.assertRaises(ConfigurationError, Connection.paired, self.bad, slave)

    # TODO test __repr__

if __name__ == "__main__":
    skip_tests = False
    unittest.main()