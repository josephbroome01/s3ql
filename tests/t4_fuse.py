'''
$Id$

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function

import shutil
import os
import time
import stat
from os.path  import basename 
from random   import randrange
from s3ql import libc
from s3ql.common import waitfor, ExceptionStoringThread
import s3ql.cli.mount_local
import s3ql.cli.umount
from _common import TestCase 
import filecmp
import tempfile
import posixpath
import unittest
import subprocess
import sys


class fuse_tests(TestCase): 

    @staticmethod
    def random_name():
        return "s3ql" + str(randrange(100, 999, 1))

    def setUp(self):
        self.base = tempfile.mkdtemp()

        # We need this to test multi block operations
        self.src = __file__
        if os.path.getsize(self.src) < 1048: 
            raise RuntimeError("test file %s should be bigger than 1 kb" % self.src)
        
        # Mount
        sys.argv = ['mount.s3ql_local', "--fg", "--blocksize", "1", '--fsck', "--quiet", self.base]
        #sys.argv = ['mount.s3ql_local', "--fg", '--single', "--blocksize", "1", '--fsck', 
        #            "--debug", 'frontend',  self.base]
        sys.argc = len(sys.argv)
        self.mount = ExceptionStoringThread(s3ql.cli.mount_local.main)
        self.mount.start()

        # Wait for mountpoint to come up
        try:
            self.assertTrue(waitfor(10, posixpath.ismount, self.base))
        except:
            self.mount.join_and_raise()
        
    def tearDown(self):
        try:
            self.umount()
        finally:
            # Umount if still mounted
            if posixpath.ismount(self.base):         
                subprocess.call(['fusermount', '-z', '-u', self.base])
                os.rmdir(self.base)   

        
    def umount(self):
        # Umount 
        time.sleep(0.5)
        self.assertTrue(waitfor(5, lambda : 
                                    subprocess.call(['fuser', '-m', '-s', self.base]) == 1))
        path = os.path.join(os.path.dirname(__file__), "..", "bin", "umount.s3ql")
        sys.argv = [path, "--quiet", self.base]
        #sys.argv = [path, "--debug", 'frontend', self.base]
        sys.argc = len(sys.argv)
        s3ql.cli.umount.DONTWAIT = True
        try:
            s3ql.cli.umount.main()
        except SystemExit as exc:
            if exc.code == 0:
                pass
            else:
                self.fail("Umount failed with error code %d" % exc.code)
        
        # Now wait for server process
        exc = self.mount.join_get_exc()
        self.assertTrue(isinstance(exc, SystemExit))
        self.assertEqual(exc.code, 0)
                        
        self.assertFalse(posixpath.ismount(self.base))
        os.rmdir(self.base)


    def test_mkdir(self):
        dirname = self.random_name()
        fullname = self.base + "/" + dirname
        os.mkdir(fullname)
        fstat = os.stat(fullname)
        self.assertTrue(stat.S_ISDIR(fstat.st_mode))
        self.assertEquals(libc.listdir(fullname), [])
        self.assertEquals(fstat.st_nlink, 2)
        self.assertTrue(dirname in libc.listdir(self.base))
        os.rmdir(fullname)
        self.assertRaises(OSError, os.stat, fullname)
        self.assertTrue(dirname not in libc.listdir(self.base))

    def test_symlink(self):
        linkname = self.random_name()
        fullname = self.base + "/" + linkname
        os.symlink("/imaginary/dest", fullname)
        fstat = os.lstat(fullname)
        self.assertTrue(stat.S_ISLNK(fstat.st_mode))
        self.assertEquals(os.readlink(fullname), "/imaginary/dest")
        self.assertEquals(fstat.st_nlink, 1)
        self.assertTrue(linkname in libc.listdir(self.base))
        os.unlink(fullname)
        self.assertRaises(OSError, os.lstat, fullname)
        self.assertTrue(linkname not in libc.listdir(self.base))

    def test_mknod(self):
        filename = self.base + "/" + self.random_name()
        src = self.src
        shutil.copyfile(src, filename)
        fstat = os.lstat(filename)
        self.assertTrue(stat.S_ISREG(fstat.st_mode))
        self.assertEquals(fstat.st_nlink, 1)
        self.assertTrue(basename(filename) in libc.listdir(self.base))
        self.assertTrue(filecmp.cmp(src, filename, False))
        os.unlink(filename)
        self.assertRaises(OSError, os.stat, filename)
        self.assertTrue(basename(filename) not in libc.listdir(self.base))

    def test_chown(self):
        filename = self.base + "/" + self.random_name()
        os.mkdir(filename)
        fstat = os.lstat(filename)
        uid = fstat.st_uid
        gid = fstat.st_gid
        
        uid_new = uid+1
        os.chown(filename, uid_new, -1)
        fstat = os.lstat(filename)      
        self.assertEquals(fstat.st_uid, uid_new)
        self.assertEquals(fstat.st_gid, gid)

        gid_new = gid+1
        os.chown(filename, -1, gid_new)
        fstat = os.lstat(filename)      
        self.assertEquals(fstat.st_uid, uid_new)
        self.assertEquals(fstat.st_gid, gid_new)

        os.rmdir(filename)
        self.assertRaises(OSError, os.stat, filename)
        self.assertTrue(basename(filename) not in libc.listdir(self.base))

    def test_write(self):
        name = self.base + "/" + self.random_name()
        src = self.src
        shutil.copyfile(src, name)
        self.assertTrue(filecmp.cmp(name, src, False))
        os.unlink(name)
        
    def test_statvfs(self):
        os.statvfs(self.base)
        
    def test_link(self):
        name1 = self.base + "/" + self.random_name()
        name2 = self.base + "/" + self.random_name()
        src = self.src
        shutil.copyfile(src, name1)
        self.assertTrue(filecmp.cmp(name1, src, False))
        os.link(name1, name2)

        fstat1 = os.lstat(name1)
        fstat2 = os.lstat(name2)

        self.assertEquals(fstat1, fstat2)
        self.assertEquals(fstat1.st_nlink, 2)

        self.assertTrue(basename(name2) in libc.listdir(self.base))
        self.assertTrue(filecmp.cmp(name1, name2, False))
        os.unlink(name2)
        fstat1 = os.lstat(name1)
        self.assertEquals(fstat1.st_nlink, 1)
        os.unlink(name1)

    def test_readdir(self):
        dir_ = self.base + "/" + self.random_name()
        file_ = dir_ + "/" + self.random_name()
        subdir = dir_ + "/" + self.random_name()
        subfile = subdir + "/" + self.random_name()
        src = self.src

        os.mkdir(dir_)
        shutil.copyfile(src, file_)
        os.mkdir(subdir)
        shutil.copyfile(src, subfile)

        listdir_is = libc.listdir(dir_)
        listdir_is.sort()
        listdir_should = [ basename(file_), basename(subdir) ]
        listdir_should.sort()
        self.assertEquals(listdir_is, listdir_should)

        os.unlink(file_)
        os.unlink(subfile)
        os.rmdir(subdir)
        os.rmdir(dir_)

    def test_truncate(self):
        filename = self.base + "/" + self.random_name()
        src = self.src
        shutil.copyfile(src, filename)
        self.assertTrue(filecmp.cmp(filename, src, False))
        fstat = os.stat(filename)
        size = fstat.st_size
        fd = os.open(filename, os.O_RDWR)
        
        os.ftruncate(fd, size + 1024) # add > 1 block
        self.assertEquals(os.stat(filename).st_size, size + 1024)

        os.ftruncate(fd, size - 1024) # Truncate > 1 block
        self.assertEquals(os.stat(filename).st_size, size - 1024)

        os.close(fd)
        os.unlink(filename)


# Somehow important according to pyunit documentation
def suite():
    return unittest.makeSuite(fuse_tests)


# Allow calling from command line
if __name__ == "__main__":
    unittest.main()
