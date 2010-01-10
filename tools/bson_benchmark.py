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

"""Benchmarking for the `pymongo.bson` module.

Depends on `simplejson`. Which is really just the Python 2.6 `json` module, so
this should be updated to use that if it exists an `simplejson` otherwise.
"""

import datetime
import cProfile as profile
import sys
sys.path[0:0] = [""]

trials = 100000

try:
    from pymongo import _cybson as bson
    print('with cython extension', trials, 'trials')
except:
    from pymongo import bson
    print('without cython extension', trials, 'trials')

def run(case, function):
    start = datetime.datetime.now()
    for _ in range(trials):
        result = function(case)
    print("took: %s" % (datetime.datetime.now() - start))
    return result

def main():
    test_cases = [{},
                  {"hello": "world"},
                  {"hello": "world",
                   "mike": "something",
                   "here's": "an\u8744other"},
                  {"int": 200,
                   "bool": True,
                   "an int": 20,
                   "a bool": False},
                  {"this": 5,
                   "is": {"a": True},
                   "big": [True, 5.5],
                   "object": None}]

    for case in test_cases:
        print("case: %r" % case)
        print("enc bson", end=' ')
        enc_bson = run(case, bson.BSON.from_dict)
        print("dec bson", end=' ')
        assert case == run(enc_bson, bson._to_dict)

if __name__ == "__main__":
    profile.run("main()")
