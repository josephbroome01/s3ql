'''
t4_fuse.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function
from _common import TestCase
from cStringIO import StringIO
from os.path import basename
from s3ql import libc, common
from s3ql.common import retry, ExceptionStoringThread
import filecmp
import os
import posixpath
import s3ql.cli.fsck
import s3ql.cli.mkfs
import s3ql.cli.mount
import s3ql.cli.umount
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

class fuse_tests(TestCase):

    def setUp(self):
        # We need this to test multi block operations
        self.src = __file__
        if os.path.getsize(self.src) < 1048:
            raise RuntimeError("test file %s should be bigger than 1 kb" % self.src)

        self.mnt_dir = tempfile.mkdtemp()
        self.cache_dir = tempfile.mkdtemp()
        self.bucket_dir = tempfile.mkdtemp()

        self.bucketname = 'local://' + os.path.join(self.bucket_dir, 'mybucket')
        self.passphrase = 'oeut3d'

        # Make sure that the mount thread does not mess with the
        # logging settings
        common.init_logging = lambda * a, **kw: None

        self.mount_thread = None
        self.name_cnt = 0

    def tearDown(self):
        # Umount if still mounted
        if posixpath.ismount(self.mnt_dir):
            subprocess.call(['fusermount', '-z', '-u', self.mnt_dir])

        shutil.rmtree(self.mnt_dir)
        shutil.rmtree(self.cache_dir)
        shutil.rmtree(self.bucket_dir)

    def mount(self):
        sys.stdin = StringIO('%s\n%s\n' % (self.passphrase, self.passphrase))
        try:
            s3ql.cli.mkfs.main(['-L', 'test fs', '--blocksize', '10',
                                '--encrypt', '--homedir', self.cache_dir, self.bucketname ])
        except SystemExit as exc:
            self.fail("mkfs.s3ql failed: %s" % exc)

        sys.stdin = StringIO('%s\n' % self.passphrase)
        self.mount_thread = ExceptionStoringThread(s3ql.cli.mount.main, logger=None,
                                       args=(["--fg", '--homedir', self.cache_dir, self.bucketname,
                                              self.mnt_dir],))
        self.mount_thread.start()

        # Wait for mountpoint to come up
        try:
            retry(3, posixpath.ismount, self.mnt_dir)
        except:
            self.mount_thread.join_and_raise()

    def umount(self):
        time.sleep(0.5)
        retry(5, lambda: subprocess.call(['fuser', '-m', '-s', self.mnt_dir]) == 1)
        s3ql.cli.umount.DONTWAIT = True
        try:
            s3ql.cli.umount.main([self.mnt_dir])
        except SystemExit as exc:
            self.fail("Umount failed: %s" % exc)

        # Now wait for server process
        exc = self.mount_thread.join_get_exc()
        self.assertIsNone(exc)
        self.assertFalse(posixpath.ismount(self.mnt_dir))

        # Now run an fsck
        sys.stdin = StringIO('%s\n' % self.passphrase)
        try:
            s3ql.cli.fsck.main(['--homedir', self.cache_dir, self.bucketname])
        except SystemExit as exc:
            self.fail("fsck failed: %s" % exc)

    def runTest(self):
        # Run all tests in same environment, mounting and umounting
        # just takes too long otherwise

        self.mount()
        self.tst_chown()
        self.tst_link()
        self.tst_mkdir()
        self.tst_mknod()
        self.tst_readdir()
        self.tst_statvfs()
        self.tst_symlink()
        self.tst_truncate()
        self.tst_write()
        self.umount()

    def newname(self):
        self.name_cnt += 1
        return "s3ql_%d" % self.name_cnt

    def tst_mkdir(self):
        dirname = self.newname()
        fullname = self.mnt_dir + "/" + dirname
        os.mkdir(fullname)
        fstat = os.stat(fullname)
        self.assertTrue(stat.S_ISDIR(fstat.st_mode))
        self.assertEquals(libc.listdir(fullname), [])
        self.assertEquals(fstat.st_nlink, 2)
        self.assertTrue(dirname in libc.listdir(self.mnt_dir))
        os.rmdir(fullname)
        self.assertRaises(OSError, os.stat, fullname)
        self.assertTrue(dirname not in libc.listdir(self.mnt_dir))

    def tst_symlink(self):
        linkname = self.newname()
        fullname = self.mnt_dir + "/" + linkname
        os.symlink("/imaginary/dest", fullname)
        fstat = os.lstat(fullname)
        self.assertTrue(stat.S_ISLNK(fstat.st_mode))
        self.assertEquals(os.readlink(fullname), "/imaginary/dest")
        self.assertEquals(fstat.st_nlink, 1)
        self.assertTrue(linkname in libc.listdir(self.mnt_dir))
        os.unlink(fullname)
        self.assertRaises(OSError, os.lstat, fullname)
        self.assertTrue(linkname not in libc.listdir(self.mnt_dir))

    def tst_mknod(self):
        filename = os.path.join(self.mnt_dir, self.newname())
        src = self.src
        shutil.copyfile(src, filename)
        fstat = os.lstat(filename)
        self.assertTrue(stat.S_ISREG(fstat.st_mode))
        self.assertEquals(fstat.st_nlink, 1)
        self.assertTrue(basename(filename) in libc.listdir(self.mnt_dir))
        self.assertTrue(filecmp.cmp(src, filename, False))
        os.unlink(filename)
        self.assertRaises(OSError, os.stat, filename)
        self.assertTrue(basename(filename) not in libc.listdir(self.mnt_dir))

    def tst_chown(self):
        filename = os.path.join(self.mnt_dir, self.newname())
        os.mkdir(filename)
        fstat = os.lstat(filename)
        uid = fstat.st_uid
        gid = fstat.st_gid

        uid_new = uid + 1
        os.chown(filename, uid_new, -1)
        fstat = os.lstat(filename)
        self.assertEquals(fstat.st_uid, uid_new)
        self.assertEquals(fstat.st_gid, gid)

        gid_new = gid + 1
        os.chown(filename, -1, gid_new)
        fstat = os.lstat(filename)
        self.assertEquals(fstat.st_uid, uid_new)
        self.assertEquals(fstat.st_gid, gid_new)

        os.rmdir(filename)
        self.assertRaises(OSError, os.stat, filename)
        self.assertTrue(basename(filename) not in libc.listdir(self.mnt_dir))


    def tst_write(self):
        name = os.path.join(self.mnt_dir, self.newname())
        src = self.src
        shutil.copyfile(src, name)
        self.assertTrue(filecmp.cmp(name, src, False))

        # Don't unlink file, we want to see if cache flushing
        # works

    def tst_statvfs(self):
        os.statvfs(self.mnt_dir)

    def tst_link(self):
        name1 = os.path.join(self.mnt_dir, self.newname())
        name2 = os.path.join(self.mnt_dir, self.newname())
        src = self.src
        shutil.copyfile(src, name1)
        self.assertTrue(filecmp.cmp(name1, src, False))
        os.link(name1, name2)

        fstat1 = os.lstat(name1)
        fstat2 = os.lstat(name2)

        self.assertEquals(fstat1, fstat2)
        self.assertEquals(fstat1.st_nlink, 2)

        self.assertTrue(basename(name2) in libc.listdir(self.mnt_dir))
        self.assertTrue(filecmp.cmp(name1, name2, False))
        os.unlink(name2)
        fstat1 = os.lstat(name1)
        self.assertEquals(fstat1.st_nlink, 1)
        os.unlink(name1)

    def tst_readdir(self):
        dir_ = os.path.join(self.mnt_dir, self.newname())
        file_ = dir_ + "/" + self.newname()
        subdir = dir_ + "/" + self.newname()
        subfile = subdir + "/" + self.newname()
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

    def tst_truncate(self):
        filename = os.path.join(self.mnt_dir, self.newname())
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

    # TODO: test_stat.s3ql

    # TODO: test cp.s3ql



# Somehow important according to pyunit documentation
def suite():
    return unittest.makeSuite(fuse_tests)


# Allow calling from command line
if __name__ == "__main__":
    unittest.main()
