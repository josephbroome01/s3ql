'''
$Id$

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function 

from random import randrange
from s3ql.common import waitfor, ExceptionStoringThread
from s3ql import s3, common
import sys
from cStringIO import StringIO
import posixpath
import unittest
import tempfile
import time
import subprocess
from _common import TestCase
import s3ql.cli.mkfs
import s3ql.cli.mount
import s3ql.cli.fsck
import s3ql.cli.umount
import shutil
import logging

class RemoteCmdTests(TestCase): 

    @staticmethod
    def random_name():
        return "s3ql" + str(randrange(10000, 99999, 1))

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.cache = tempfile.mkdtemp()
        
        # Fake connection
        s3.Connection = s3.LocalConnection
        
    def tearDown(self):
        shutil.rmtree(self.base)
        shutil.rmtree(self.cache)
            
    def test_mount(self):
        bucketname = 'test_bucket'
        passphrase = 'foobar'
        
        # Init logging, make sure that further changes do nothing
        common.init_logging(logging.WARN)
        common.init_logging = lambda : None
        
        # Create filesystem
        sys.stdin = StringIO('bla\n%s\n%s\n' % (passphrase, passphrase))
        try:
            s3ql.cli.mkfs.main(['--awskey', 'foo', '-L', 'test fs', '--blocksize', '10',
                                '--encrypt', '--quiet', '--cachedir', self.cache, bucketname ])
        except SystemExit as exc:
            self.fail("mkfs.s3ql failed: %s" % exc)   

        # Mount filesystem
        sys.stdin = StringIO('foo\n%s\n' % passphrase)   
        mount = ExceptionStoringThread(s3ql.cli.mount.main, 
                                       args=(["--fg", "--quiet", '--awskey', 'foo',
                                              '--cachedir', self.cache, bucketname, self.base],))
        mount.start()

        # Wait for mountpoint to come up
        self.assertTrue(waitfor(10, posixpath.ismount, self.base))

        # Umount as soon as mountpoint is no longer in use
        time.sleep(0.5)
        self.assertTrue(waitfor(5, lambda: subprocess.call(['fuser', '-m', '-s', self.base]) == 1))
        s3ql.cli.umount.DONTWAIT = True
        try:
            s3ql.cli.umount.main(["--quiet", self.base])
        except SystemExit as exc:
            self.fail("Umount failed: %s" % exc)
        
        # Now wait for server process
        exc = mount.join_get_exc()
        self.assertIsNone(exc)
        self.assertFalse(posixpath.ismount(self.base))
        
        # Now run an fsck
        sys.stdin = StringIO('foo\n%s\n' % passphrase)
        try:
            s3ql.cli.fsck.main(['--awskey', 'foo', '--quiet', '--cachedir', self.cache, bucketname])
        except SystemExit as exc:
            self.fail("fsck failed: %s" % exc)


# Somehow important according to pyunit documentation
def suite():
    return unittest.makeSuite(RemoteCmdTests)


# Allow calling from command line
if __name__ == "__main__":
    unittest.main()
