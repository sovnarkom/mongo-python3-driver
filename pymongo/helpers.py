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

"""Little bits and pieces used by the driver that don't really fit elsewhere."""

import sys
import struct
import warnings

from .son import SON
from .errors import OperationFailure, AutoReconnect
from . import bson
import pymongo

def _index_list(key_or_list, direction=None):
    """Helper to generate a list of (key, direction) pairs.

    Takes such a list, or a single key, or a single key and direction.
    """
    if direction is not None:
        return [(key_or_list, direction)]
    else:
        if isinstance(key_or_list, str):
            return [(key_or_list, pymongo.ASCENDING)]
        return key_or_list


def _index_document(index_list):
    """Helper to generate an index specifying document.

    Takes a list of (key, direction) pairs.
    """
    if not isinstance(index_list, list):
        raise TypeError("if no direction is specified, key_or_list must be an "
                        "instance of list")
    if not len(index_list):
        raise ValueError("key_or_list must not be the empty list")

    index = SON()
    for (key, value) in index_list:
        if not isinstance(key, str):
            raise TypeError("first item in each key pair must be a string")
        if not isinstance(value, int):
            raise TypeError("second item in each key pair must be ASCENDING or "
                            "DESCENDING")
        index[key] = value
    return index


def _reversed(l):
    """A version of the `reversed()` built-in for Python 2.3.
    """
    i = len(l)
    while i > 0:
        i -= 1
        yield l[i]
if sys.version_info[:3] >= (2, 4, 0):
    _reversed = reversed


def _unpack_response(response, cursor_id=None):
    """Unpack a response from the database.

    Check the response for errors and unpack, returning a dictionary
    containing the response data.

    :Parameters:
      - `response`: byte string as returned from the database
      - `cursor_id` (optional): cursor_id we sent to get this response -
        used for raising an informative exception when we get cursor id not
        valid at server response
    """
    response_flag = struct.unpack("<i", response[:4])[0]

    # Flag bit 0: CursorNotFound
    if response_flag & 1: # Check to see if bit 0 is set (2**0 == 1)
        # Shouldn't get this response if we aren't doing a getMore
        assert cursor_id is not None

        raise OperationFailure("cursor id '%s' not valid at server" %
                               cursor_id)
    
    # Flag bit 1: QueryFailure
    if response_flag & 2: # Check to see if bit 1 is set (2**1 == 2)
        error_object = bson.BSON(response[20:]).to_dict()
        if error_object["$err"] == "not master":
            raise AutoReconnect("master has changed")
        raise OperationFailure("database error: %s" %
                               error_object["$err"])

    # Flag bit 2: ShardConfigState
    # "Drivers should ignore this" (from Mongo Wire Protocol)

    
    # Flag bit 3: AwaitCapable
    # As of Mongod version 1.6, this is always set.
    
    # Flag bit 4-31
    # Bits 4-31 should be ignored (from Mongo Wire Protocol, Feb 2012)


    result = {}
    result["cursor_id"] = struct.unpack("<q", response[4:12])[0]
    result["starting_from"] = struct.unpack("<i", response[12:16])[0]
    result["number_returned"] = struct.unpack("<i", response[16:20])[0]
    result["data"] = bson._to_dicts(response[20:])
    assert len(result["data"]) == result["number_returned"]
    return result


# These two functions are some magic to get values we can use for deprecating
# method style access in favor of property style access while remaining
# backwards compatible.
def __prop_call(self, *args, **kwargs):
    warnings.warn("'%s()' has been deprecated and will be removed. "
                  "Please use '%s' instead." %
                  (self.__prop_name, self.__prop_name),
                  DeprecationWarning)
    return self

__class_cache = {}

def callable_value(value, prop_name):
    t = type(value)

    if "CallableVal" in str(t):
        return value

    if (t, prop_name) in __class_cache:
        cls = __class_cache[(t, prop_name)]
    else:
        cls = type.__new__(type, "CallableVal", (t,),
                           {"__call__": __prop_call,
                            "__prop_name": prop_name})
        __class_cache[(t, prop_name)] = cls

    try:
        # This works for regular classes
        value.__class__ = cls
        return value
    except:
        # This works for builtins
        return cls(value)
