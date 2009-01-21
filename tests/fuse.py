#!/usr/bin/env python
#
#    Copyright (C) 2008  Nikolaus Rath <Nikolaus@rath.org>
#
#    This program can be distributed under the terms of the GNU LGPL.
#

import shutil
import os
import stat
import sys
from os.path  import basename
from random   import randrange
import filecmp
import time
from tests import TestCase, assert_true, assert_equals, assert_raises

class fuse(TestCase):
    """Perform checks on a mounted S3QL filesystem
    """

    def __init__(self, cb):
        self.base = "test_mp/"
        self.basefile = self.base + "README"
        self.cb = cb

        if not os.path.exists(self.basefile):
            raise Exception, "test directory %s does not exist" % self.base

        # We need this to test multi block operations
        self.src = sys.argv[0]
        fstat = os.stat(self.src)
        if fstat.st_size <= 1024: # 1 kb blocksize, see below
            raise Exception, "test file %s should be bigger than 1 kb" % self.src


    def test_mount(self):

        # Mount
        self.pid = os.spawnl(os.P_NOWAIT, "./s3qlfs_local", "s3qlfs_local",
                             "--fg", "--fsck", "--nonempty", "--blocksize", "1",
                             "--quiet", self.base)

        # This apparently takes quite some time
        time.sleep(5)
        assert_true(not os.path.exists(self.basefile))

        # Run Subtests
        try:
            self.t_mkdir()
            self.t_symlink()
            self.t_mknod()
            self.t_readdir()
            self.t_symlink()
            self.t_truncate()
        finally:
            # Umount
            time.sleep(3)
            assert_equals(os.spawnlp(os.P_WAIT, "fusermount",
                                    "fusermount", "-u", self.base), 0)
            (pid, status) = os.waitpid(self.pid, 0)

            assert_true(os.WIFEXITED(status))
            assert_equals(os.WEXITSTATUS(status), 0)
            assert_true(os.path.exists(self.basefile))

    def random_name(self):
        return "s3ql" + str(randrange(10,99,1))


    def t_mkdir(self):
        dirname = self.random_name()
        os.mkdir(self.base + dirname)
        fstat = os.stat(self.base + dirname)
        assert_true(stat.S_ISDIR(fstat.st_mode))
        assert_equals(os.listdir(self.base + dirname), [])
        assert_equals(fstat.st_nlink, 2)
        assert_true(dirname in os.listdir(self.base))
        os.rmdir(self.base + dirname)
        assert_raises(OSError, os.stat, self.base + dirname)
        assert_true(dirname not in os.listdir(self.base))
        self.cb()

    def t_symlink(self):
        linkname = self.random_name()
        os.symlink("/imaginary/dest", self.base + linkname)
        fstat = os.lstat(self.base + linkname)
        assert_true(stat.S_ISLNK(fstat.st_mode))
        assert_equals(os.readlink(self.base + linkname), "/imaginary/dest")
        assert_equals(fstat.st_nlink, 1)
        assert_true(linkname in os.listdir(self.base))
        os.unlink(self.base + linkname)
        assert_raises(OSError, os.lstat, self.base + linkname)
        assert_true(linkname not in os.listdir(self.base))
        self.cb()

    def t_mknod(self):
        filename = self.base + self.random_name()
        src = self.src
        shutil.copyfile(src, filename)
        fstat = os.lstat(filename)
        assert_true(stat.S_ISREG(fstat.st_mode))
        assert_equals(fstat.st_nlink, 1)
        assert_true(basename(filename) in os.listdir(self.base))
        assert_true(filecmp.cmp(src, filename, False))
        os.unlink(filename)
        assert_raises(OSError, os.stat, filename)
        assert_true(basename(filename) not in os.listdir(self.base))
        self.cb()


    def t_link(self):
        name1 = self.base + self.random_name()
        name2 = self.base + self.random_name()
        src = self.src
        shutil.copyfile(src, name1)
        os.link(name1, name2)

        fstat1 = os.lstat(name1)
        fstat2 = os.lstat(name2)

        assert_equals(fstat1, fstat2)
        assert_equals(fstat1.st_nlink, 2)

        assert_true(basename(name2) in os.listdir(self.base))
        assert_true(filecmp.cmp(name1, name2, False))
        os.unlink(name2)
        fstat1 = os.lstat(name1)
        assert_equals(fstat1.st_nlink, 1)
        os.unlink(name1)
        self.cb()

    def t_readdir(self):
        dir = self.base + self.random_name()
        file = dir + "/" + self.random_name()
        subdir = dir + "/" + self.random_name()
        subfile = subdir + "/" + self.random_name()
        src = self.src

        os.mkdir(dir)
        shutil.copyfile(src, file)
        os.mkdir(subdir)
        shutil.copyfile(src, subfile)

        listdir_is = os.listdir(dir)
        listdir_is.sort()
        listdir_should = [ basename(file), basename(subdir) ]
        listdir_should.sort()
        assert_equals(listdir_is, listdir_should)

        os.unlink(file)
        os.unlink(subfile)
        os.rmdir(subdir)
        os.rmdir(dir)
        self.cb()

    def t_truncate(self):
        filename = self.base + self.random_name()
        src = self.src
        shutil.copyfile(src, filename)
        fstat = os.stat(filename)
        size = fstat.st_size
        fd = os.open(filename, os.O_RDWR)

        os.ftruncate(fd, size + 1024) # add > 1 block
        assert_equals(os.stat(filename).st_size, size + 1024)

        os.ftruncate(fd, size - 1024) # Truncate > 1 block
        assert_equals(os.stat(filename).st_size, size - 1024)

        os.close(fd)
        os.unlink(filename)
        self.cb()
