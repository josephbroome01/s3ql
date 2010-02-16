'''
$Id$

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function, absolute_import

import unittest
import t1_s3_local
from s3ql.backends import s3
from random import randrange
from _common import get_aws_credentials

# This tests usually fails due to propagation delays on S3.
@unittest.skip('disabled')
class s3_tests_remote(t1_s3_local.s3_tests_local):

    @staticmethod
    def random_name(prefix=""):
        return "s3ql-" + prefix + str(randrange(1000, 9999, 1))

    def setUp(self):
        (awskey, awspass) = get_aws_credentials()
        self.conn = s3.Connection(awskey, awspass)

        self.bucketname = self.random_name()
        tries = 10
        while self.conn.bucket_exists(self.bucketname) and tries > 10:
            self.bucketname = self.random_name()
            tries -= 1

        if tries == 0:
            raise RuntimeError("Failed to find an unused bucket name.")

        self.conn.create_bucket(self.bucketname)
        self.passphrase = 'flurp'

        self.bucket = self.conn.get_bucket(self.bucketname, self.passphrase)

        # This is the time in which we expect S3 changes to propagate. It may
        # be much longer for larger objects, but for tests this is usually enough.
        self.delay = 1

        self.name_cnt = 0

    def tearDown(self):
        self.conn.delete_bucket(self.bucketname, recursive=True)

    def runTest(self):
        # Run all tests in same environment, creating and deleting
        # the bucket every time just takes too long.

        self.tst_01_store_fetch_lookup_delete_key()
        self.tst_02_meta()
        self.tst_03_list_keys()
        self.tst_04_encryption()
        self.tst_06_copy()



# Somehow important according to pyunit documentation
def suite():
    return unittest.makeSuite(s3_tests_remote)


# Allow calling from command line
if __name__ == "__main__":
    unittest.main()
