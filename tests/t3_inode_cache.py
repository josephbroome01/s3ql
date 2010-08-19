'''
t2_inode_cache.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2010 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function


from s3ql import inode_cache
from s3ql import mkfs
from s3ql import database as dbcm
from _common import TestCase
import unittest2 as unittest
import time
import tempfile

class cache_tests(TestCase):

    def setUp(self):
        self.dbfile = tempfile.NamedTemporaryFile()
        dbcm.init(self.dbfile.name)
        mkfs.setup_tables(dbcm)
        mkfs.init_tables(dbcm)
        self.cache = inode_cache.InodeCache()

    def tearDown(self):
        self.cache.destroy()

    def test_create(self):
        attrs = {'mode': 784,
                 'refcount': 3,
                 'uid': 7,
                 'gid': 2,
                 'size': 34674,
                 'target': 'foobar',
                 'rdev': 11,
                 'atime': time.time(),
                 'ctime': time.time(),
                 'mtime': time.time() }

        inode = self.cache.create_inode(**attrs)

        for key in attrs.keys():
            self.assertEqual(attrs[key], getattr(inode, key))

        self.assertTrue(dbcm.has_val('SELECT 1 FROM inodes WHERE id=?',
                                          (inode.id,)))


    def test_del(self):
        attrs = {'mode': 784,
                'refcount': 3,
                'uid': 7,
                'target': 'foobar',
                'gid': 2,
                'size': 34674,
                'rdev': 11,
                'atime': time.time(),
                'ctime': time.time(),
                'mtime': time.time() }
        inode = self.cache.create_inode(**attrs)
        del self.cache[inode.id]
        self.assertFalse(dbcm.has_val('SELECT 1 FROM inodes WHERE id=?', (inode.id,)))
        self.assertRaises(KeyError, self.cache.__delitem__, inode.id)

    def test_get(self):
        attrs = {'mode': 784,
                'refcount': 3,
                'uid': 7,
                'gid': 2,
                'target': 'foobar',
                'size': 34674,
                'rdev': 11,
                'atime': time.time(),
                'ctime': time.time(),
                'mtime': time.time() }
        inode = self.cache.create_inode(**attrs)
        self.assertEqual(inode, self.cache[inode.id])

        dbcm.execute('DELETE FROM inodes WHERE id=?', (inode.id,))
        # Entry should still be in cache
        self.assertEqual(inode, self.cache[inode.id])

        # Now it should be out of the cache
        for _ in xrange(inode_cache.CACHE_SIZE + 1):
            dummy = self.cache[self.cache.create_inode(**attrs).id]

        self.assertRaises(KeyError, self.cache.__getitem__, inode.id)



def suite():
    return unittest.makeSuite(cache_tests)

if __name__ == "__main__":
    unittest.main()
