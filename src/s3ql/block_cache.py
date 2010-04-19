'''
block_cache.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function

from contextlib import contextmanager
from s3ql.multi_lock import MultiLock
from s3ql.ordered_dict import OrderedDict
from s3ql.common import (ExceptionStoringThread, sha256_fh, TimeoutError)
import logging
import os
import threading
import time
import re

__all__ = [ "BlockCache" ]

# standard logger for this module
log = logging.getLogger("BlockCache")


# This is an additional limit on the cache, in addition to the cache size. It prevents that we
# run out of file descriptors, or simply eat up too much memory for cache elements if the users
# creates thousands of 10-byte files.
# Standard file descriptor limit per process is 1024
MAX_CACHE_ENTRIES = 768


class CacheEntry(file):
    """An element in the block cache
    
    If `obj_id` is `None`, then the object has not yet been
    uploaded to the backend. 
    """

    def __init__(self, inode, blockno, obj_id, filename, mode):
        super(CacheEntry, self).__init__(filename, mode)
        self.dirty = False
        self.obj_id = obj_id
        self.inode = inode
        self.blockno = blockno

    def truncate(self, *a, **kw):
        self.dirty = True
        return super(CacheEntry, self).truncate(*a, **kw)

    def write(self, *a, **kw):
        self.dirty = True
        return super(CacheEntry, self).write(*a, **kw)

    def writelines(self, *a, **kw):
        self.dirty = True
        return super(CacheEntry, self).writelines(*a, **kw)

    def __str__(self):
        return ('<CacheEntry, inode=%d, blockno=%d, dirty=%s, obj_id=%r>' %
                (self.inode, self.blockno, self.dirty, self.obj_id))

class BlockCache(object):
    """Provides access to file blocks
    
    This class manages access to file blocks. It takes care of creation,
    uploading, downloading and deduplication.
    
    In order for S3QL not to block entirely when objects need to be
    downloaded or uploaded, this class releases the global lock for
    network transactions. In these cases, a separate lock on inode and
    block number is used to prevent simultaneous access to the same block.
    """

    def __init__(self, bucket, cachedir, maxsize, dbcm):
        log.debug('Initializing')
        self.cache = OrderedDict()
        self.cachedir = cachedir
        self.maxsize = maxsize
        self.size = 0
        self.bucket = bucket
        self.mlock = MultiLock()
        self.dbcm = dbcm

        self.exp_thread = None
        self.need_expiry = threading.Event()
        self.ready_to_write = threading.Event()

    def start_expiration_thread(self):
        '''Start expiration thread'''

        log.debug('Starting background expiration thread')
        self.exp_thread = ExceptionStoringThread(self._expiry_loop, log, pass_self=True)
        self.exp_thread.run_flag = True
        self.exp_thread.start()

    def stop_expiration_thread(self):
        '''Stop background expiration thread'''

        log.debug('Waiting for background expiration thread')
        self.exp_thread.run_flag = False
        if self.exp_thread.is_alive():
            self.need_expiry.set()
        self.exp_thread.join_and_raise()

    def _expiry_loop(self, self_t):
        '''Run cache expiration loop'''

        try:
            while self_t.run_flag:
                log.debug('_expiry_loop: waiting for poke...')
                self.need_expiry.wait(5)
                log.debug('_expiry_loop: need_expiry has been set')
                while (self.size > self.maxsize or
                       len(self.cache) > MAX_CACHE_ENTRIES) and len(self.cache) > 0:
                    self._expire_parallel()
                self.need_expiry.clear()
                self.ready_to_write.set()
        except:
            # Prevent deadlocks
            self.ready_to_write.set()

            def fail():
                raise RuntimeError('Expiration thread quit unexpectedly')
            self.ready_to_write.wait = fail
            self.need_expiry.set = fail

            raise

        log.debug('_expiry_loop: exiting.')

    def get_bucket_size(self):
        '''Return total size of the underlying bucket'''

        return self.bucket.get_size()

    def __len__(self):
        '''Get number of objects in cache'''
        return len(self.cache)

    @contextmanager
    def get(self, inode, blockno, lock):
        """Get file handle for block `blockno` of `inode`
        
        This method releases `lock' for the managed context, so the caller must
        not hold any prior database locks and must not try to acquire any
        database locks in the managed context.
        """

        # Get object key    
        log.debug('Getting file handle for inode %i, block %i', inode, blockno)
        self.mlock.acquire(inode, blockno)
        lock.release()
        try:
            el = self._get(inode, blockno)
            oldsize = os.fstat(el.fileno()).st_size

            # Provide fh to caller
            try:
                yield el
            finally:
                # Update cachesize
                el.flush()
                newsize = os.fstat(el.fileno()).st_size
                self.size += newsize - oldsize

            # Wait for expiration if required
            if self.size > self.maxsize or len(self.cache) > MAX_CACHE_ENTRIES:
                log.debug('Cache size exceeded, waiting for expiration...')
                self.ready_to_write.clear()
                self.need_expiry.set()
                self.ready_to_write.wait()

        finally:
            self.mlock.release(inode, blockno)
            lock.acquire()


    def _get(self, inode, blockno):
        try:
            el = self.cache[(inode, blockno)]

        # Not in cache
        except KeyError:
            filename = os.path.join(self.cachedir,
                                    'inode_%d_block_%d' % (inode, blockno))
            try:
                obj_id = self.dbcm.get_val("SELECT obj_id FROM blocks WHERE inode=? AND blockno=?",
                                           (inode, blockno))

            # No corresponding object
            except KeyError:
                el = CacheEntry(inode, blockno, None, filename, "w+b")

            # Need to download corresponding object
            else:
                el = CacheEntry(inode, blockno, obj_id, filename, "w+b")
                retry_exc(300, [ KeyError ], self.bucket.fetch_fh,
                          's3ql_data_%d' % obj_id, el)
                self.size += os.fstat(el.fileno()).st_size

            self.cache[(inode, blockno)] = el

        # In Cache
        else:
            self.cache.to_head((inode, blockno))

        return el

    def recover(self):
        '''Register old files in cache directory'''

        if self.cache:
            raise RuntimeError('Cannot call recover() if there are already cache entries')

        for filename in os.listdir(self.cachedir):
            match = re.match('^inode_(\\d+)_block_(\\d+)$', filename)
            if match:
                log.debug('Recovering cache file %s', filename)
                (inode, blockno) = [ int(match.group(i)) for i in (1, 2) ]
            else:
                raise RuntimeError('Strange file in cache directory: %s' % filename)

            try:
                obj_id = self.dbcm.get_val('SELECT obj_id FROM blocks WHERE inode=? AND blockno=?',
                                          (inode, blockno))
            except KeyError:
                obj_id = None
                log.debug('Cache file does not belong to any object')
            else:
                log.debug('Cache file belongs to object %d', obj_id)

            el = CacheEntry(inode, blockno, obj_id,
                            os.path.join(self.cachedir, filename), "r+b")
            el.dirty = True
            self.size += os.fstat(el.fileno()).st_size
            self.cache[(inode, blockno)] = el

    def _prepare_upload(self, el):
        '''Prepare upload of specified cache entry
        
        Returns a function that does the required network transactions. Returns
        None if no network access is required.
        
        Caller has to take care of any necessary locking.
        '''

        log.debug('_prepare_upload(inode=%d, blockno=%d)', el.inode, el.blockno)

        if not el.dirty:
            return

        size = os.fstat(el.fileno()).st_size
        el.seek(0)
        hash_ = sha256_fh(el)

        old_obj_id = el.obj_id
        with self.dbcm.transaction() as conn:
            try:
                el.obj_id = conn.get_val('SELECT id FROM objects WHERE hash=?', (hash_,))

            except KeyError:
                need_upload = True
                el.obj_id = conn.rowid('INSERT INTO objects (refcount, hash, size) VALUES(?, ?, ?)',
                                      (1, hash_, size))
                log.debug('No matching hash, will upload to new object %s', el.obj_id)

            else:
                need_upload = False
                log.debug('Object %d has identical hash, relinking', el.obj_id)
                conn.execute('UPDATE objects SET refcount=refcount+1 WHERE id=?',
                             (el.obj_id,))

            if old_obj_id is None:
                log.debug('Not associated with any object previously.')
                conn.execute('INSERT INTO blocks (obj_id, inode, blockno) VALUES(?,?,?)',
                             (el.obj_id, el.inode, el.blockno))
                to_delete = False
            else:
                log.debug('Decreasing reference count for previous object %d', old_obj_id)
                conn.execute('UPDATE blocks SET obj_id=? WHERE inode=? AND blockno=?',
                             (el.obj_id, el.inode, el.blockno))
                refcount = conn.get_val('SELECT refcount FROM objects WHERE id=?',
                                        (old_obj_id,))
                if refcount > 1:
                    conn.execute('UPDATE objects SET refcount=refcount-1 WHERE id=?',
                                 (old_obj_id,))
                    to_delete = False
                else:
                    conn.execute('DELETE FROM objects WHERE id=?', (old_obj_id,))
                    to_delete = True


        if need_upload and to_delete:
            def doit():
                log.debug('Uploading..')
                self.bucket.store_fh('s3ql_data_%d' % el.obj_id, el)
                log.debug('No references to object %d left, deleting', old_obj_id)
                retry_exc(300, [ KeyError ], self.bucket.delete, 's3ql_data_%d' % old_obj_id)
        elif need_upload:
            def doit():
                log.debug('Uploading..')
                self.bucket.store_fh('s3ql_data_%d' % el.obj_id, el)
        elif to_delete:
            def doit():
                log.debug('No references to object %d left, deleting', old_obj_id)
                retry_exc(300, [ KeyError ], self.bucket.delete, 's3ql_data_%d' % old_obj_id)
        else:
            return
        return doit

    def _expire_parallel(self):
        """Remove oldest entries from the cache.
        
        Expires the oldest entries to free at least 1 MB. Expiration is
        done for all the keys at the same time using different threads.
        However, at most 25 threads are started.
        
        The 1 MB is based on the following calculation:
         - Uploading objects takes at least 0.15 seconds due to
           network latency
         - When uploading large objects, maximum throughput is about
           6 MB/sec.
         - Hence the minimum object size for maximum throughput is 
           6 MB/s * 0.15 s ~ 1 MB
         - If the object to be transferred is smaller than that, we have
           to upload several objects at the same time, so that the total
           amount of transferred data is 1 MB.
        """

        log.debug('_expire parallel started')

        threads = list()
        freed_size = 0
        while freed_size < 1024 * 1024 and len(threads) < 25 and len(self.cache) > 0:

            # If we pop the object before having locked it, another thread 
            # may download it - overwriting the existing file!
            try:
                el = self.cache.get_last()
            except IndexError:
                break

            log.debug('Least recently used object is %s, obtaining object lock..', el)
            self.mlock.acquire(el.inode, el.blockno)

            # Now that we have the lock, check that the object still exists
            if (el.inode, el.blockno) not in self.cache:
                self.mlock.release(el.inode, el.blockno)
                continue

            # Make sure the object is in the db before removing it from the cache
            fn = self._prepare_upload(el)
            log.debug('Removing object %s from cache..', el)
            del self.cache[(el.inode, el.blockno)]
            el.seek(0, os.SEEK_END)
            freed_size += el.tell()

            if fn is None:
                log.debug('expire_parallel: no network transaction required')
                el.close()
                os.unlink(el.name)
                self.mlock.release(el.inode, el.blockno)
            else:
                log.debug('expire_parallel: starting new thread for network transaction')
                # We have to be careful to include the *current*
                # el in the closure
                def do_upload(el=el, fn=fn):
                    fn()
                    el.close()
                    os.unlink(el.name)
                    self.mlock.release(el.inode, el.blockno)

                t = ExceptionStoringThread(do_upload, log)
                threads.append(t)
                t.start()

        self.size -= freed_size

        log.debug('Freed %d kb using %d expiry threads', freed_size / 1024, len(threads))
        log.debug('Waiting for expiry threads...')
        for t in threads:
            t.join_and_raise()
            #t.join()

        log.debug('_expire_parallel finished')


    def remove(self, inode, blockno=0):
        """Unlink blocks of given inode.
        
        If `blockno` is specified, unlinks only objects for blocks
        >= `blockno`. If no other blocks reference the objects,
        they are completely removed.
        
        As long as no objects need to be removed, blocks are processed
        sequentially. If an object needs to be removed, a new thread
        continues to process the remaining blocks in parallel.
        """

        log.debug('Removing blocks >= %d for inode %d', blockno, inode)

        # Remove elements from cache
        log.debug('Iterating through cache')
        for el in self.cache.itervalues():
            if el.inode != inode:
                continue
            if el.blockno < blockno:
                continue

            log.debug('Found block %d, removing', el.blockno)
            with self.mlock(el.inode, el.blockno):
                try:
                    self.cache.pop((el.inode, el.blockno))
                except KeyError:
                    log.debug('Object has already been expired.')
                    continue

            el.seek(0, 2)
            self.size -= el.tell()
            el.close()
            os.unlink(el.name)

        # Remove elements from db and backend
        log.debug('Deleting from database')
        threads = list()
        while True:
            with self.dbcm.transaction() as conn:
                try:
                    (obj_id, cur_block) = conn.get_row('SELECT obj_id, blockno FROM blocks '
                                                      'WHERE inode=? AND blockno >= ? LIMIT 1',
                                                      (inode, blockno))
                except KeyError:
                    break

                log.debug('Deleting block %d, object %d', cur_block, obj_id)
                conn.execute('DELETE FROM blocks WHERE inode=? AND blockno=?', (inode, cur_block))
                refcount = conn.get_val('SELECT refcount FROM objects WHERE id=?', (obj_id,))
                if refcount > 1:
                    log.debug('Decreasing refcount for object %d', obj_id)
                    conn.execute('UPDATE objects SET refcount=refcount-1 WHERE id=?', (obj_id,))
                    continue

                log.debug('Deleting object %d', obj_id)
                conn.execute('DELETE FROM objects WHERE id=?', (obj_id,))

            # Note that at this point we must make sure that any new objects 
            # don't reuse the key that we have just deleted from the DB. This
            # is ensured by using AUTOINCREMENT on the id column.

            # If there are more than 25 threads, we wait for the
            # first one to finish
            if len(threads) > 25:
                log.debug('More than 25 threads, waiting..')
                threads.pop(0).join_and_raise()

            # Start a removal thread              
            t = ExceptionStoringThread(retry_exc, log,
                                       args=(300, [ KeyError ], self.bucket.delete,
                                             's3ql_data_%d' % obj_id))
            threads.append(t)
            t.start()


        log.debug('Waiting for removal threads...')
        for t in threads:
            t.join_and_raise()


    def flush(self, inode):
        """Upload dirty data for `inode`"""

        # It is really unlikely that one inode will several small
        # blocks (the file would have to be terribly fragmented),
        # therefore there is no need to upload in parallel.

        log.debug('Flushing objects for inode %i', inode)
        for el in self.cache.itervalues():
            if el.inode != inode:
                continue
            if not el.dirty:
                continue

            log.debug('Flushing object %s', el)
            with self.mlock(el.inode, el.blockno):
                # Now that we have the lock, check that the object still exists
                if (el.inode, el.blockno) not in self.cache:
                    continue

                fn = self._prepare_upload(el)
                if fn:
                    fn()
                    el.dirty = False

        log.debug('Flushing for inode %d completed.', inode)

    def flush_all(self):
        """Upload all dirty data"""

        # It is really unlikely that one inode will several small
        # blocks (the file would have to be terribly fragmented),
        # therefore there is no need to upload in parallel.

        log.debug('Flushing all objects')
        for el in self.cache.itervalues():
            if not el.dirty:
                continue

            log.debug('Flushing object %s', el)
            with self.mlock(el.inode, el.blockno):
                # Now that we have the lock, check that the object still exists
                if (el.inode, el.blockno) not in self.cache:
                    continue

                fn = self._prepare_upload(el)
                if fn:
                    fn()
                    el.dirty = False
    def clear(self):
        """Upload all dirty data and clear cache"""

        log.debug('Clearing block cache')

        if self.exp_thread and self.exp_thread.is_alive():
            bak = self.maxsize
            self.maxsize = 0
            try:
                self.ready_to_write.clear()
                self.need_expiry.set()
                self.ready_to_write.wait()
            finally:
                self.maxsize = bak
        else:
            while len(self.cache) > 0:
                self._expire_parallel()


    def __del__(self):
        if len(self.cache) > 0:
            raise RuntimeError("BlockCache instance was destroyed without calling clear()!")


def retry_exc(timeout, exc_types, fn, *a, **kw):
    """Wait for fn(*a, **kw) to succeed
    
    If `fn(*a, **kw)` raises an exception in `exc_types`, the function is called again.
    If the timeout is reached, `TimeoutError` is raised.
    """

    step = 0.2
    waited = 0
    while waited < timeout:
        try:
            return fn(*a, **kw)
        except BaseException as exc:
            for exc_type in exc_types:
                if isinstance(exc, exc_type):
                    log.warn('Encountered %s error when calling %s, retrying...',
                             exc.__class__.__name__, fn.__name__)
                    break
            else:
                raise exc

        time.sleep(step)
        waited += step
        if step < timeout / 30:
            step *= 2

    raise TimeoutError()



