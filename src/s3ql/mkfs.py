'''
mkfs.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function

import stat
import os
import time

from s3ql.common import ROOT_INODE, CTRL_INODE

__all__ = [ "setup_tables", 'init_tables', 'create_indices' ]

def init_tables(conn):
    # Insert root directory
    timestamp = time.time() - time.timezone
    conn.execute("INSERT INTO inodes (id,mode,uid,gid,mtime,atime,ctime,refcount,nlink_off) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   (ROOT_INODE, stat.S_IFDIR | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
                   | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
                    os.getuid(), os.getgid(), timestamp, timestamp, timestamp, 1, 2))

    # Insert control inode, the actual values don't matter that much 
    conn.execute("INSERT INTO inodes (id,mode,uid,gid,mtime,atime,ctime,refcount) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (CTRL_INODE, stat.S_IFIFO | stat.S_IRUSR | stat.S_IWUSR,
                  0, 0, timestamp, timestamp, timestamp, 42))

    # Insert lost+found directory
    inode = conn.rowid("INSERT INTO inodes (mode,uid,gid,mtime,atime,ctime,refcount,nlink_off) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (stat.S_IFDIR | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
                        os.getuid(), os.getgid(), timestamp, timestamp, timestamp, 1, 1))
    conn.execute("INSERT INTO contents (name, inode, parent_inode) VALUES(?,?,?)",
                 (b"lost+found", inode, ROOT_INODE))

def setup_tables(conn):
    # Table with filesystem metadata
    # The number of links `refcount` to an inode can in theory
    # be determined from the `contents` table. However, managing
    # this separately should be significantly faster (the information
    # is required for every getattr!)
    conn.execute("""
    CREATE TABLE inodes (
        -- id has to specified *exactly* as follows to become
        -- an alias for the rowid.
        -- inode_t may be restricted to 32 bits, so we need to constrain the
        -- rowid. Also, as long as we don't store a separate generation no,
        -- we can't reuse old rowids. Therefore we will run out of inodes after
        -- 49 days if we insert 1000 rows per second. 
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        uid       INT NOT NULL,
        gid       INT NOT NULL,
        mode      INT NOT NULL,
        mtime     REAL NOT NULL,
        atime     REAL NOT NULL,
        ctime     REAL NOT NULL,
        refcount  INT NOT NULL,
        target    BLOB(256) ,
        size      INT NOT NULL DEFAULT 0,
        rdev      INT NOT NULL DEFAULT 0,
                                    
        -- Correction term to add to refcount to get st_nlink
        nlink_off INT NOT NULL DEFAULT 0
    )
    """)

    # Table of filesystem objects
    conn.execute("""
    CREATE TABLE contents (
        name      BLOB(256) NOT NULL,
        inode     INT NOT NULL REFERENCES inodes(id),
        parent_inode INT NOT NULL REFERENCES inodes(id),
        
        PRIMARY KEY (name, parent_inode)
    )""")
    
    # Extended attributes
    conn.execute("""
    CREATE TABLE ext_attributes (
        inode     INTEGER NOT NULL REFERENCES inodes(id),
        name      BLOB NOT NULL,
        value     BLOB NOT NULL,
 
        PRIMARY KEY (inode, name)               
    )""")

    # Refcount is included for performance reasons, for directories, the
    # refcount also includes the implicit '.' entry
    conn.execute("""
    CREATE TABLE objects (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        refcount  INT NOT NULL,
                  
        -- hash and size is only updated when the object is committed
        hash      BLOB(16) UNIQUE,
        size      INT NOT NULL                 
    )""")


    # Maps blocks to objects
    conn.execute("""
    CREATE TABLE blocks (
        inode     INTEGER NOT NULL REFERENCES inodes(id),
        blockno   INT NOT NULL,
        obj_id    INTEGER NOT NULL REFERENCES objects(id),
 
        PRIMARY KEY (inode, blockno)
    )""")

def create_indices(conn):
    conn.execute('CREATE INDEX ix_contents_parent_inode ON contents(parent_inode)')
    conn.execute('CREATE INDEX ix_contents_inode ON contents(inode)')
    conn.execute('CREATE INDEX ix_ext_attributes_inode ON ext_attributes(inode)')
    conn.execute('CREATE INDEX ix_objects_hash ON objects(hash)')
    conn.execute('CREATE INDEX ix_blocks_obj_id ON blocks(obj_id)')
    conn.execute('CREATE INDEX ix_blocks_inode ON blocks(inode)')             

