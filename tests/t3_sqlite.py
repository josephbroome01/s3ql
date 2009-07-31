#!/usr/bin/env python
#
#    Copyright (C) 2008  Nikolaus Rath <Nikolaus@rath.org>
#
#    This program can be distributed under the terms of the GNU LGPL.
#
       
import tempfile
import unittest
from time import time
from s3ql import mkfs, s3
from s3ql.common import ROOT_INODE
from s3ql.cursor_manager import CursorManager
import apsw
import stat
import os


class sqlite_tests(unittest.TestCase): 

    def setUp(self):
        self.bucket = s3.LocalBucket()
        self.bucket.tx_delay = 0
        self.bucket.prop_delay = 0

        self.dbfile = tempfile.NamedTemporaryFile()
        self.cachedir = tempfile.mkdtemp() + "/"
        self.blocksize = 1024

        self.cur = CursorManager(self.dbfile.name)
        mkfs.setup_db(self.cur, self.blocksize)
        

    def tearDown(self):
        self.dbfile.close()
        os.rmdir(self.cachedir)
        
    def test_contents_parent_inode(self):
        """Check that parent_inode can only be a directory
        """
        cur = self.cur
        
        # Create a file
        cur.execute("INSERT INTO inodes (mode,uid,gid,mtime,atime,ctime,refcount,size) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (stat.S_IFREG, os.getuid(), os.getgid(), time(), time(), time(), 1, 0))
        inode = cur.last_rowid()
        cur.execute("INSERT INTO contents (name, inode, parent_inode) VALUES(?,?,?)",
                   ("testfile", inode, ROOT_INODE))
                           
        # Try to create a file with a file as parent
        cur.execute("INSERT INTO inodes (mode,uid,gid,mtime,atime,ctime,refcount,size) "
                    "VALUES (?,?,?,?,?,?,?,0)",
                    (stat.S_IFREG, os.getuid(), os.getgid(), time(), time(), time(), 1))     
        inode2 = cur.last_rowid()             
        self.assertRaises(apsw.ConstraintError, cur.execute, 
                          "INSERT INTO contents (name, inode, parent_inode) VALUES(?,?,?)",
                          ("testfile2", inode, inode2))
        
                   
    def test_inodes_mode(self):
        """Check that inodes can only have one type.
        
        Note that not all combinations are forbidden, for example
        ``S_IFREG|S_IFDIR == S_IFSOCK``.
        """
        cur = self.cur
        
        # Create a file
        self.assertRaises(apsw.ConstraintError,
                          cur.execute,
                          "INSERT INTO inodes (mode,uid,gid,mtime,atime,ctime,refcount) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (stat.S_IFCHR | stat.S_IFIFO, 
                           os.getuid(), os.getgid(), time(), time(), time(), 1))
       
              
# Somehow important according to pyunit documentation
def suite():
    return unittest.makeSuite(sqlite_tests)

# Allow calling from command line
if __name__ == "__main__":
    unittest.main()