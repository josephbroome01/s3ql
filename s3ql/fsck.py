#!/usr/bin/env python
#
#    Copyright (C) 2008  Nikolaus Rath <Nikolaus@rath.org>
#
#    This program can be distributed under the terms of the GNU LGPL.
#

import os
import types
import stat
from time import time
import tempfile
from s3ql.common import (addfile, get_path, my_cursor, unused_name)
import fs
import logging

__all__ = [" a_check_parameters", "b_check_cache", "c_check_contents", "d_check_inodes",
           "e_check_s3", "f_check_keylist" ]

log = logging.getLogger("fsck")

def b_check_cache(conn, cachedir, bucket, checkonly):
    """Verifies that the s3 table agrees with the cache.

    Checks that:
    - For each file in the cache, there is an entry in the table
    - For each entry in the table, there is a cache file

    If `checkonly` is disabled, it also:
    - Commits all cache entries to S3 and deletes them

    Returns `False` if any errors have been found.

    The prefix of the method name indicates the order in which
    the fsck routines should be called.
    """

    c1 = conn.cursor()
    c2 = conn.cursor()
    found_errors = False

    # Go through all cache files according to DB
    res = c1.execute("SELECT s3key,cachefile,dirty FROM s3_objects "
                     "WHERE cachefile IS NOT NULL")

    for (s3key, cachefile, dirty) in res:
        found_errors = True
        if not os.path.exists(cachedir + cachefile):
            if dirty:
                log.warn("Dropped changes to %s (no longer in cache)" % s3key)
            else:
                log.warn("Removed cache flag for %s" % s3key)

        else:
            if dirty:
                log.warn("Committing cached changes for %s")
                if not checkonly:
                    etag = bucket.store_from_file(s3key, cachedir + cachefile)
                    c2.execute("UPDATE s3_objects SET etag=? WHERE s3key=?",
                               (etag, s3key))
                    os.unlink(cachedir + cachefile)

        if not checkonly:
            c2.execute("UPDATE s3_objects SET cachefile=?,fd=?,dirty=? "
                       "WHERE s3key=?", (None, None, False, s3key))


    # Check if any cache files are left
    log.info("Checking objects in cache...")
    for cachefile in os.listdir(cachedir):
        found_errors = True

        log.warn("Removing unassociated cache file %s" % cachefile)
        if not checkonly:
            os.unlink(cachedir + cachefile)

    return not found_errors

def a_check_parameters(conn, checkonly):
    """Check that filesystem parameters are set

    Returns `False` if any errors have been found.

    The prefix of the method name indicates the order in which
    the fsck routines should be called.
    """
    found_errors = False
    cursor = conn.cursor()

    res = list(cursor.execute("SELECT label FROM parameters"))
    if len(res) != 1:
        found_errors = True
        log.warn("No unique filesystem label - please report this as a bug")
    if type(res[0][0]) not in types.StringTypes:
        found_errors = True
        log.warn("Filesystem label has wrong type - please report this as a bug")

    res = list(cursor.execute("SELECT blocksize FROM parameters"))
    if len(res) != 1:
        found_errors = True
        log.warn("No unique blocksize - please report this as a bug")
    if type(res[0][0]) not in [types.LongType, types.IntType]:
        found_errors = True
        log.warn("Filesystem blocksize has wrong type - please report this as a bug")

    return not found_errors


def c_check_contents(conn, checkonly):
    """Check contents table
    
    Returns `False` if any errors have been found.

    The prefix of the method name indicates the order in which
    the fsck routines should be called.
    """
    c1 = my_cursor(conn.cursor())                 
    c2 = my_cursor(conn.cursor())
    found_errors = False

    #
    # root directory
    #
    res = c1.get_row("SELECT inode, parent_inode, mode FROM contents_ext "
                     "WHERE inode = parent_inode")

    if not res:
        found_errors = True
        log.warn("Recreating missing root directory")
        if not checkonly:
            c1.execute("INSERT INTO inodes (mode,uid,gid,mtime,atime,ctime,refcount) "
                       "VALUES (?,?,?,?,?,?,?)",
                        (stat.S_IFDIR | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
                         | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
                         os.getuid(), os.getgid(), time(), time(), time(), 3))
            inode_r = conn.last_insert_rowid()
            c1.execute("INSERT INTO contents (name, inode, parent_inode) VALUES(?,?,?)",
                       (buffer("/"), inode_r, inode_r))
        else:
            # Otherwise the other tests throw exceptions
            inode_r = 0
    else:    
        inode_r = res[0]

    #
    # /lost+found
    #
    res = c1.get_row("SELECT inode, mode FROM contents_ext WHERE name=? AND parent_inode=?", 
                     (buffer("lost+found"), inode_r))

    if not res:
        found_errors = True
        log.warn("Recreating missing lost+found directory")
        if not checkonly:
            c1.execute("INSERT INTO inodes (mode,uid,gid,mtime,atime,ctime,refcount) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (stat.S_IFDIR | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
                        os.getuid(), os.getgid(), time(), time(), time(), 2))
            inode = conn.last_insert_rowid()
            c1.execute("INSERT INTO contents (name, inode, parent_inode) VALUES(?,?,?)",
                       (buffer("lost+found"), inode, inode_r))

    else:
        (inode_l, mode) = res

    if not stat.S_ISDIR(mode):
        found_errors = True
        log.warn("lost+found has wrong mode, fixing..")
        if not checkonly:
            c2.execute("UPDATE inodes SET mode=? WHERE inode=?",
                       (stat.S_IFDIR | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
                        inode_l))

    #
    # remaining filesystem
    #
    res = c1.execute("SELECT name, inode, parent_inode FROM contents")

    for (name, inode, inode_p) in res:
        name = str(name)
        
        mode = c2.get_val("SELECT mode FROM inodes WHERE id=?", (inode_p,))

        # Parent is directory
        if not stat.S_ISDIR(mode):
            found_errors = True
            path = get_path(name, inode_p, c2)
            log.warn("Parent of %s is not a directory, moving to lost+found" % path)
            if not checkonly:
                newname = unused_name(c2, path[1:].replace("/", ":"), inode_l)
                c2.execute("UPDATE contents SET name=?, parent_inode=? WHERE name=? AND parent_inode=?",
                           (buffer(newname), inode_l, buffer(name), inode_p))

    return not found_errors




def d_check_inodes(conn, checkonly):
    """Check inode table

    Checks that:
    - refcounts are correct
    - each inode has a content entry

    Returns `False` if any errors have been found.

    The prefix of the method name indicates the order in which
    the fsck routines should be called.
    """

    c1 = my_cursor(conn.cursor())
    c2 = my_cursor(conn.cursor())
    found_errors = False

    # Find lost+found inode
    # If we are in checkonly, it may not be present and we will
    # not need it
    if not checkonly:
        inode_r = c1.get_val("SELECT inode FROM contents WHERE inode = parent_inode")
        inode_l = c1.get_val("SELECT inode FROM contents WHERE name=? AND parent_inode=?",
                             (buffer("lost+found"), inode_r))


    res = c1.execute("SELECT id,refcount,mode FROM inodes")
    for (inode,refcount,mode) in res:

        # Ensure inode is referenced
        res2 = c2.get_list("SELECT name FROM contents WHERE inode=?", (inode,))
        if not res2:
            found_errors = True
            log.warn("Inode %s not referenced, adding to lost+found")
            if not checkonly:
                name =  unused_name(c2, "inode-" + str(inode), inode_l)
                c2.execute("INSERT INTO contents (name, inode, parent_inode) "
                           "VALUES (?,?,?)", (buffer(name), inode, inode_l))
                c2.execute("UPDATE inodes SET refcount=? WHERE id=?",
                           (1, inode))
        else:
            name = str(res2[0][0])


        # Directory
        if stat.S_ISDIR(mode):

            if len(res2) > 1:
                ### FIXME: The following is not working.
                found_errors = True
                log.warn("Replacing directory hardlink %s with symlink" % name)
                if not checkonly:
                    (uid, gid) = c2.execute("SELECT uid,gid FROM inodes "
                                            "WHERE id=?", (inode,)).next()
                    c2.execute("INSERT INTO inodes (mode,uid,gid,target,mtime,atime,ctime,refcount) "
                               "VALUES(?, ?, ?, ?, ?, ?, ?, 1)",
                               (stat.S_IFLNK, uid, gid, buffer(res2[1][0]),
                                time(), time(), time()))
                    c2.execute("UPDATE contents SET inode=? WHERE name=?",
                               (conn.last_insert_rowid(), buffer(name)))


            # Check reference count
            res2 = c2.execute("SELECT mode FROM contents_ext WHERE parent_inode=? AND inode != parent_inode", (inode,))

            no = 2
            for (mode2,) in res2:
                if stat.S_ISDIR(mode2):
                    no += 1

            if no != refcount:
                found_errors = True
                log.warn("Fixing reference count of directory %s from %d to %d"
                     % (name, refcount, no))
                if not checkonly:
                    c2.execute("UPDATE inodes SET refcount=? WHERE id=?",
                               (no, inode))

        # File
        else:

            # Check reference count
            if refcount != len(res2):
                found_errors = True
                log.warn("Fixing reference count of file %s from %d to %d",
                     (name, refcount, len(res2)))
                if not checkonly:
                    c2.execute("UPDATE inodes SET refcount=? WHERE id=?",
                               (len(res2), inode))


    return not found_errors

def e_check_s3(conn, bucket, checkonly):
    """Checks s3_objects table.

    Checks that:
    - offsets are blocksize apart
    - s3key corresponds to inode and offset

    Returns `False` if any errors have been found.

    The prefix of the method name indicates the order in which
    the fsck routines should be called.
    """
    c1 = conn.cursor()
    c2 = conn.cursor()
    found_errors = False

    # Find blocksize
    blocksize = c1.execute("SELECT blocksize FROM parameters").next()[0]

    res = c1.execute("SELECT s3key,inode,offset FROM s3_objects")

    for (s3key, inode, offset) in res:

        # Check blocksize
        if offset % blocksize != 0:
            found_errors = True

            # Try to shift upward or downward
            offset_d = blocksize * int(offset/blocksize)
            offset_u = blocksize * (int(offset/blocksize)+1)
            if not list(c2.execute("SELECT s3key FROM s3_objects WHERE inode=? AND offset=?",
                                   (inode, offset_d))):
                log.warn("Object %s does not start at blocksize boundary, moving downwards"
                     % s3key)
                if not checkonly:
                    c2.execute("UPDATE s3_objects SET offset=? WHERE s3key=?",
                               (offset_d, s3key))

            elif not list(c2.execute("SELECT s3key FROM s3_objects WHERE inode=? AND offset=?",
                                     (inode, offset_u))):
                log.warn("Object %s does not start at blocksize boundary, moving upwards"
                     % s3key)
                if not checkonly:
                    c2.execute("UPDATE s3_objects SET offset=? WHERE s3key=?",
                               (offset_u, s3key))

            else:
                log.warn("Object %s does not start at blocksize boundary, deleting"
                     % s3key)
                if not checkonly:
                    c2.execute("DELETE FROM s3_objects WHERE s3key=?", (s3key,))


        s3key2 = fs.io2s3key(inode, offset)
        if s3key2 != s3key:
            found_errors = True
            log.warn("Object %s has invalid s3key, replacing with %s"
                 % (s3key, s3key2))
            if not checkonly:
                c2.execute("UPDATE s3_objects SET s3key=? WHERE s3key=?",
                           (s3key2, s3key))
                bucket.copy(s3key, s3key2)
                bucket.delete_key(s3key)


    return not found_errors


def f_check_keylist(conn, bucket, checkonly):
    """Checks the list of S3 objects.

    Checks that:
    - no s3 object is larger than the blocksize
    - all s3 objects are referred in the s3 table
    - all objects in the s3 table exist
    - etags match (update metadata in case of conflict)


    Returns `False` if any errors have been found.

    The prefix of the method name indicates the order in which
    the fsck routines should be called.
    """
    c1 = my_cursor(conn.cursor())
    c2 = my_cursor(conn.cursor())
    found_errors = False

    # Find lost+found inode
    # If we are in checkonly, it may not be present and we will
    # not need it
    if not checkonly:
        inode_r = c1.get_val("SELECT inode FROM contents WHERE inode = parent_inode")
        inode_l = c1.get_val("SELECT inode FROM contents WHERE name=? AND parent_inode=?",
                             (buffer("lost+found"), inode_r))

    # Find blocksize
    blocksize = c1.execute("SELECT blocksize FROM parameters").next()[0]

    # We use this table to keep track of the s3keys that we have
    # seen
    c1.execute("CREATE TEMP TABLE s3keys AS SELECT s3key FROM s3_objects")


    for (s3key, meta) in bucket.list_keys():

        # We only bother with our own objects
        if not s3key.startswith("s3ql_data_"):
            continue

        c1.execute("DELETE FROM s3keys WHERE s3key=?", (s3key,))

        # Size
        if meta.size > blocksize:
            found_errors = True
            log.warn("object %s is larger than blocksize (%d > %d), truncating (original object in lost+found)"
                 % (s3key, meta.size, blocksize))
            if not checkonly:
                tmp = tempfile.NamedTemporaryFile()
                bucket.fetch_to_file(s3key, tmp.name)

                # Save full object in lost+found
                addfile(unused_name(c1, s3key, inode_l), tmp, inode_l, c1, bucket)

                # Truncate and readd
                tmp.seek(blocksize)
                tmp.truncate()
                etag_new = bucket.store_from_file(s3key, tmp.name)
                tmp.close()
                c1.execute("UPDATE s3_objects SET etag=? WHERE s3key=?",
                           (etag_new, s3key))

        # Is it referenced in object table?
        res = list(c1.execute("SELECT etag,size FROM s3_objects WHERE s3key=?",
                   (s3key,)))

        # Object is not listed in object table
        if not res:
            found_errors = True
            log.warn("object %s not in referenced in table, adding to lost+found" % s3key)
            if not checkonly:
                lfname = unused_name(c1, s3key, inode_l)
                c1.execute("INSERT INTO inodes (mode,uid,gid,mtime,atime,ctime,refcount) "
                           "VALUES (?,?,?,?,?,?,?)",
                           (stat.S_IFREG | stat.S_IRUSR | stat.S_IWUSR,
                            os.getuid(), os.getgid(), time(), time(), time(), 1))
                inode = conn.last_insert_rowid()
                c1.execute("INSERT INTO contents (name, inode, parent_inode) VALUES(?,?,?)",
                           (buffer(lfname), inode, inode_l))

                # Now we need to assign the s3 object to this inode, but this
                # unfortunately means that we have to change the s3key.
                s3key_new = fs.io2s3key(inode,0)
                bucket.copy(s3key, s3key_new)
                del bucket[s3key]

                c1.execute("INSERT INTO s3_objects (inode,offset,s3key,size,etag) "
                           "VALUES (?,?,?,?)", (inode, 0, buffer(s3key_new),
                                                os.stat(tmp).st_size, meta.etag))

        # Object is in object table, check metadata
        else:
            (etag,size) = res[0]

            if not size == meta.size:
                found_errors = True
                log.warn("object %s has incorrect size in metadata, adjusting" % s3key)

                if not checkonly:
                    c1.execute("UPDATE s3_objects SET size=? WHERE s3key=?",
                               (meta.size, s3key))

            if not etag == meta.etag:
                found_errors = True
                log.warn("object %s has incorrect etag in metadata, adjusting" % s3key)

                if not checkonly:
                    c1.execute("UPDATE s3_objects SET etag=? WHERE s3key=?",
                               (meta.etag, s3key))


    # Now handle objects that only exist in s3_objects
    res = c2.execute("SELECT s3key FROM s3keys")
    for (s3key,) in res:
        found_errors = True
        log.warn("object %s only exists in table but not on s3, deleting" % s3key)
        if not checkonly:
            c1.execute("DELETE FROM s3_objects WHERE s3key=?", (buffer(s3key),))

    return not found_errors


