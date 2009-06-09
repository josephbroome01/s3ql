#!/usr/bin/env python
#
#    Copyright (C) 2008  Nikolaus Rath <Nikolaus@rath.org>
#
#    This program can be distributed under the terms of the GNU LGPL.
#

import tempfile
import unittest
from s3ql import mkfs, s3, fs, fsck
from s3ql.common import *
import apsw
import stat
import os
import time
import fuse
import resource
from random import randrange

class fs_api_tests(unittest.TestCase):

    # FIXME: Whenever we test that an entry exist (or does not exist),
    # we have to use both readdir() and getattr(), not only one!
 
    def setUp(self):
        self.bucket = s3.LocalBucket()
        self.bucket.tx_delay = 0
        self.bucket.prop_delay = 0

        self.dbfile = tempfile.NamedTemporaryFile()
        self.cachedir = tempfile.mkdtemp() + "/"
        self.blocksize = 1024

        # Only warnings and errors
        logger.log_level = 0

        mkfs.setup_db(self.dbfile.name, self.blocksize)
        mkfs.setup_bucket(self.bucket, self.dbfile.name)

        self.server = fs.server(self.bucket, self.dbfile.name, self.cachedir)


    def tearDown(self):
        # May not have been called if a test failed
        if hasattr(self, "server"):
            self.server.close()
        self.dbfile.close()
        os.rmdir(self.cachedir)


    def fsck(self):
        self.server.close()
        del self.server
        conn = apsw.Connection(self.dbfile.name)
        self.assertTrue(fsck.a_check_parameters(conn, checkonly=True))
        self.assertTrue(fsck.b_check_cache(conn, self.cachedir, self.bucket, checkonly=True))
        self.assertTrue(fsck.c_check_contents(conn, checkonly=True))
        self.assertTrue(fsck.d_check_inodes(conn, checkonly=True))
        self.assertTrue(fsck.e_check_s3(conn, self.bucket, checkonly=True))
        self.assertTrue(fsck.f_check_keylist(conn, self.bucket, checkonly=True))

    def random_name(self, prefix=""):
        return "s3ql" + prefix + str(randrange(100,999,1))

    def test_01_getattr_root(self):
        fstat = self.server.getattr("/")
        self.assertTrue(stat.S_ISDIR(fstat["st_mode"]))
        self.fsck()

    def test_02_utimens(self):
        # We work on the root directory
        path="/"
        fstat_old = self.server.getattr(path)
        atime_new = fstat_old["st_atime"] - 72
        mtime_new = fstat_old["st_mtime"] - 72
        self.server.utimens(path, (atime_new, mtime_new))
        fstat_new = self.server.getattr(path)

        self.assertEquals(fstat_new["st_mtime"], mtime_new)
        self.assertEquals(fstat_new["st_atime"], atime_new)
        self.assertTrue(fstat_new["st_ctime"] > fstat_old["st_ctime"])

        self.fsck()

    def test_03_mkdir_rmdir(self):
        linkcnt = self.server.getattr("/")["st_nlink"]

        name = os.path.join("/",  self.random_name())
        mtime_old = self.server.getattr("/")["st_mtime"]
        self.assertRaises(fs.FUSEError, self.server.getattr, name)
        self.server.mkdir(name, stat.S_IRUSR | stat.S_IXUSR)
        self.assertTrue(self.server.getattr("/")["st_mtime"] > mtime_old)
        fstat = self.server.getattr(name)

        self.assertEquals(self.server.getattr("/")["st_nlink"], linkcnt+1)
        self.assertTrue(stat.S_ISDIR(fstat["st_mode"]))
        self.assertEquals(fstat["st_nlink"], 2)

        sub = os.path.join(name, self.random_name())
        self.assertRaises(fs.FUSEError, self.server.getattr, sub)
        self.server.mkdir(sub, stat.S_IRUSR | stat.S_IXUSR)

        fstat = self.server.getattr(name)
        fstat2 = self.server.getattr(sub)

        self.assertTrue(stat.S_ISDIR(fstat2["st_mode"]))
        self.assertEquals(fstat["st_nlink"], 3)
        self.assertEquals(fstat2["st_nlink"], 2)
        self.assertTrue(self.server.getattr("/")["st_nlink"] == linkcnt+1)

        self.assertRaises(fs.FUSEError, self.server.rmdir, name)
        self.server.rmdir(sub)
        self.assertRaises(fs.FUSEError, self.server.getattr, sub)
        self.assertEquals(self.server.getattr(name)["st_nlink"], 2)

        mtime_old = self.server.getattr("/")["st_mtime"]
        self.server.rmdir(name)
        self.assertTrue(self.server.getattr("/")["st_mtime"] > mtime_old)
        self.assertRaises(fs.FUSEError, self.server.getattr, name)
        self.assertTrue(self.server.getattr("/")["st_nlink"] == linkcnt)

        self.fsck()

    def test_04_symlink(self):
        name = os.path.join("/",  self.random_name())
        target = "../../wherever/this/is"
        self.assertRaises(fs.FUSEError, self.server.getattr, name)
        mtime_old = self.server.getattr("/")["st_mtime"]
        self.server.symlink(name, target)
        self.assertTrue(self.server.getattr("/")["st_mtime"] > mtime_old)
        fstat = self.server.getattr(name)

        self.assertTrue(stat.S_ISLNK(fstat["st_mode"]))
        self.assertEquals(fstat["st_nlink"], 1)

        self.assertEquals(self.server.readlink(name), target)

        mtime_old = self.server.getattr("/")["st_mtime"]
        self.server.unlink(name)
        self.assertTrue(self.server.getattr("/")["st_mtime"] > mtime_old)
        self.assertRaises(fs.FUSEError, self.server.getattr, name)

        self.fsck()

    def test_05_create_unlink(self):
        name = os.path.join("/",  self.random_name())
        mode = ( stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP )

        self.assertRaises(fs.FUSEError, self.server.getattr, name)
        mtime_old = self.server.getattr("/")["st_mtime"]
        fh = self.server.create(name, mode)
        self.server.release(name, fh)
        self.server.flush(name, fh)

        self.assertEquals(self.server.getattr(name)["st_mode"], mode | stat.S_IFREG)
        self.assertEquals(self.server.getattr(name)["st_nlink"], 1)
        self.assertTrue(self.server.getattr("/")["st_mtime"] > mtime_old)

        mtime_old = self.server.getattr("/")["st_mtime"]
        self.server.unlink(name)
        self.assertTrue(self.server.getattr("/")["st_mtime"] > mtime_old)
        self.assertRaises(fs.FUSEError, self.server.getattr, name)

        self.fsck()


    def test_06_chmod_chown(self):
        # Create file
        name = os.path.join("/",  self.random_name())
        mode = ( stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP )
        fh = self.server.create(name, mode)
        self.server.release(name, fh)
        self.server.flush(name, fh)

        mode_new = ( stat.S_IFREG |
                     stat.S_IROTH | stat.S_IWOTH | stat.S_IXGRP | stat.S_IRGRP )
        ctime_old = self.server.getattr(name)["st_ctime"]
        self.server.chmod(name, mode_new)
        self.assertEquals(self.server.getattr(name)["st_mode"], mode_new | stat.S_IFREG)
        self.assertTrue(self.server.getattr(name)["st_ctime"] > ctime_old)

        uid_new = 1231
        gid_new = 3213
        ctime_old = self.server.getattr(name)["st_ctime"]
        self.server.chown(name, uid_new, gid_new)
        self.assertEquals(self.server.getattr(name)["st_uid"], uid_new)
        self.assertEquals(self.server.getattr(name)["st_gid"], gid_new)
        self.assertTrue(self.server.getattr(name)["st_ctime"] > ctime_old)

        self.server.unlink(name)
        self.fsck()

    def test_07_open_write_read(self):
        # Create file
        name = os.path.join("/",  self.random_name())
        mode = ( stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP )
        fh = self.server.create(name, mode)
        self.server.release(name, fh)
        self.server.flush(name, fh)

        # Write testfile
        destfh = self.server.open(name, os.O_RDWR)
        bufsize = resource.getpagesize()

        srcfh = open(__file__, "rb")

        buf = srcfh.read(bufsize)
        off = 0
        while len(buf) != 0:
            self.assertEquals(self.server.write(name, buf, off, destfh), len(buf))
            off += len(buf)
            buf = srcfh.read(bufsize)

        # Read testfile
        srcfh.seek(0)
        buf = srcfh.read(bufsize)
        off = 0
        while len(buf) != 0:
            self.assertTrue(buf == self.server.read(name, bufsize, off, destfh))
            off += len(buf)
            buf = srcfh.read(bufsize)
        self.server.release(name, fh)
        self.server.flush(name, fh)

        srcfh.close()
        self.fsck()


    def test_08_link(self):
        # Create file
        target = os.path.join("/",  self.random_name("target"))
        mode = ( stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP )
        fh = self.server.create(target, mode)
        self.server.release(target, fh)
        self.server.flush(target, fh)


        name = os.path.join("/",  self.random_name())
        self.assertRaises(fs.FUSEError, self.server.getattr, name)
        mtime_old = self.server.getattr("/")["st_mtime"]
        self.server.link(name, target)
        self.assertTrue(self.server.getattr("/")["st_mtime"] > mtime_old)
        fstat = self.server.getattr(name)

        self.assertEquals(fstat, self.server.getattr(target))
        self.assertEquals(fstat["st_nlink"], 2)

        self.server.unlink(name)
        self.assertEquals(self.server.getattr(target)["st_nlink"], 1)
        self.assertRaises(fs.FUSEError, self.server.getattr, name)

        self.server.unlink(target)
        self.assertRaises(fs.FUSEError, self.server.getattr, target)

        self.fsck()

    @staticmethod   
    def random_data(len):
        fd = open("/dev/urandom", "rb")
        return fd.read(len)
        
    def test_09_write_read_cmplx(self):
        # Create file with holes
        name = os.path.join("/",  self.random_name())
        mode = ( stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP )
        off = int(5.9 * self.blocksize)
        datalen = int(0.2 * self.blocksize)
        data = self.random_data(datalen)
        fh = self.server.create(name, mode)
        self.server.write(name, data, off, fh)
        filelen = datalen + off
        self.assertEquals(self.server.getattr(name)["st_size"], filelen)

        off2 = int(0.5 * self.blocksize)
        self.assertEquals(self.server.read(name, len(data)+off2, off, fh), data)
        self.assertEquals(self.server.read(name, len(data)+off2, off-off2, fh), 
                          "\0" * off2 + data)
        self.assertEquals(self.server.read(name, 182, off+len(data), fh), "")

        # Write at another position
        off = int(1.9 * self.blocksize)
        self.server.write(name, data, off, fh)
        self.assertEquals(self.server.getattr(name)["st_size"], filelen)
        self.assertEquals(self.server.read(name, len(data)+off2, off, fh), data + "\0" * off2)

        self.server.release(name, fh)
        self.server.flush(name, fh)

        self.fsck()
        
    def test_11_truncate_within(self):
        # Create file with holes
        name = os.path.join("/",  self.random_name())
        mode = ( stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP )
        off = int(5.5 * self.blocksize)
        datalen = int(0.3 * self.blocksize)
        data = self.random_data(datalen)
    
        fh = self.server.create(name, mode)
        self.server.write(name, data, off, fh)
        filelen = datalen + off
        self.assertEquals(self.server.getattr(name)["st_size"], filelen)

        # Extend within same block
        ext = int(0.15 * self.blocksize)
        self.server.ftruncate(name, filelen+ext, fh)
        self.assertEquals(self.server.getattr(name)["st_size"], filelen+ext)
        self.assertEquals(self.server.read(name, len(data)+2*ext, off, fh),
                          data + "\0" * ext)
        self.assertEquals(self.server.read(name, 2*ext, off+len(data), fh),
                          "\0" * ext)
        
        # Truncate it
        self.server.ftruncate(name, filelen-ext, fh)
        self.assertEquals(self.server.getattr(name)["st_size"], filelen-ext)
        self.assertEquals(self.server.read(name, len(data)+2 * ext, off, fh), 
                          data[0:-ext])
        
        # And back to original size, data should have been lost
        self.server.ftruncate(name, filelen, fh)
        self.assertEquals(self.server. getattr(name)["st_size"], filelen)
        self.assertEquals(self.server.read(name, len(data)+2 * ext, off, fh),
                          data[0:-ext] + "\0" * ext)

        self.server.release(name, fh)
        self.server.flush(name, fh)
        self.fsck()
        
    def test_12_truncate_across(self):
        # Create file with holes
        name = os.path.join("/",  self.random_name())
        mode = ( stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP )
        off = int(5.5 * self.blocksize)
        datalen = int(0.3 * self.blocksize)
        data = self.random_data(datalen)
    
        fh = self.server.create(name, mode)
        self.server.write(name, data, off, fh)
        filelen = datalen + off
        self.assertEquals(self.server.getattr(name)["st_size"], filelen)

        # Extend within same block
        ext = int(0.5 * self.blocksize)
        self.server.ftruncate(name, filelen+ext, fh)
        self.assertEquals(self.server.getattr(name)["st_size"], filelen+ext)
        self.assertEquals(self.server.read(name, len(data)+2*ext, off, fh),
                          data + "\0" * ext)
        self.assertEquals(self.server.read(name, 2*ext, off+len(data), fh),
                          "\0" * ext)
        
        # Truncate it
        ext = int(0.1 * self.blocksize)
        self.server.ftruncate(name, filelen-ext, fh)
        self.assertEquals(self.server.getattr(name)["st_size"], filelen-ext)
        self.assertEquals(self.server.read(name, len(data)+2 * ext, off, fh), 
                          data[0:-ext])
        
        # And back to original size, data should have been lost
        self.server.ftruncate(name, filelen, fh)
        self.assertEquals(self.server. getattr(name)["st_size"], filelen)
        self.assertTrue(self.server.read(name, len(data)+2 * ext, off, fh) ==
                          data[0:-ext] + "\0" * ext)
        
        self.server.release(name, fh)
        self.server.flush(name, fh)
        self.fsck()
                
    def test_10_rename(self):
        dirname_old = os.path.join("/", self.random_name("olddir"))
        dirname_new = os.path.join("/", self.random_name("newdir"))
        filename_old = os.path.join(dirname_old, self.random_name("oldfile"))
        filename_new = self.random_name("newfile")
        filename_new1 = os.path.join(dirname_old, filename_new)
        filename_new2 = os.path.join(dirname_new, filename_new)
        
        # Create directory with file
        mode = ( stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IFDIR )
        self.server.mkdir(dirname_old, mode)
        mode = ( stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR )
        fh = self.server.create(filename_old, mode)
        self.server.write(dirname_old, "Some random contents", 0, fh)
        self.server.release(filename_old, fh)
        self.server.flush(filename_old, fh)
        
        # Rename file
        fstat = self.server.getattr(filename_old)
        mtime_old = self.server.getattr(dirname_old)["st_mtime"]
        self.server.rename(filename_old, filename_new1)
        self.assertRaises(fs.FUSEError, self.server.getattr, filename_old)
        self.assertEquals(fstat, self.server.getattr(filename_new1))
        self.assertTrue(self.server.getattr(dirname_old)["st_mtime"] > mtime_old)
        
        # Rename directory
        fstat2 = self.server.getattr(filename_new1)
        fstat = self.server.getattr(dirname_old)
        mtime_old = self.server.getattr("/")["st_mtime"]
        self.server.rename(dirname_old, dirname_new)
        self.assertRaises(fs.FUSEError, self.server.getattr, dirname_old)
        self.assertRaises(fs.FUSEError, self.server.getattr, filename_new1)
        self.assertEquals(fstat, self.server.getattr(dirname_new))
        self.assertEquals(fstat2, self.server.getattr(filename_new2))
        self.assertTrue(self.server.getattr("/")["st_mtime"] > mtime_old)
        
    # Also check the addfile function from fsck.py here

    # Check that s3 object locking works when retrieving

    # Check that s3 object locking works when creating

    # Check that s3 objects are committed after fsync


# Somehow important according to pyunit documentation
def suite():
    return unittest.makeSuite(fs_api_tests)


# Allow calling from command line
if __name__ == "__main__":
    unittest.main()
