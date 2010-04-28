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

    __slots__ = [ 'dirty', 'obj_id', 'inode', 'blockno', 'last_access', 'removed' ]

    def __init__(self, inode, blockno, obj_id, filename, mode):
        super(CacheEntry, self).__init__(filename, mode)
        self.dirty = False
        self.obj_id = obj_id
        self.inode = inode
        self.blockno = blockno
        self.last_access = 0

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
        self.removal_queue = RemovalQueue(self)
        self.io_thread = None
        self.expiry_lock = threading.Lock()

    def init(self):
        log.debug('init: start')
        self.io_thread = ExceptionStoringThread(self._io_loop, log, pass_self=True)
        self.io_thread.stop_event = threading.Event()
        self.io_thread.start()
        log.debug('init: end')

    def close(self):
        log.debug('close: start')
        if self.io_thread is not None:
            if self.io_thread.is_alive():
                self.io_thread.stop_event.set()
            self.io_thread.join_and_raise()
        self.removal_queue.wait()
        self.clear()
        log.debug('close: end')

    def _io_loop(self, self_t):
        '''Run IO loop'''

        log.debug('_io_loop: start')
        self_t.queue = UploadQueue(self)

        while not self_t.stop_event.is_set():
            self._do_io(self_t)
            log.debug('_io_loop: sleeping')
            self_t.stop_event.wait(5)

        self_t.queue.wait()
        log.debug('_io_loop: end')

    def _do_io(self, self_t):
        '''Flush all objects that have not been accessed in the last 10 seconds'''

        log.debug('_do_io: start')
        keep_running = True
        while keep_running:
            keep_running = False
            stamp = time.time()
            for el in self.cache.values_rev():
                if stamp - el.last_access < 10:
                    break
                if not el.dirty:
                    continue

                self_t.queue.add(el)
                keep_running = True

                if self_t.stop_event.is_set():
                    keep_running = False
                    break

        log.debug('_do_io: end')

    def _remove_entry(self, el):
        '''Try to remove `el' from cache
        
        The entry is only removed if it is not dirty.
        
        Both conditions are checked after the block has been locked and
        retrieved again from the cache. 
        '''

        with self.mlock(el.inode, el.blockno):
            try:
                el = self.cache[(el.inode, el.blockno)]
            except KeyError:
                log.debug('_remove_entry(%s): end (vanished)', el)
                return
            if el.dirty:
                log.debug('_remove_entry(%s): end (dirty)', el)
                return

            log.debug('_remove_entry(%s): removing from cache', el)
            del self.cache[(el.inode, el.blockno)]
            self.size -= os.fstat(el.fileno()).st_size
            el.close()
            os.unlink(el.name)

        log.debug('_remove_entry(%s): end', el)


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

        log.debug('get(inode=%d, block=%d): start', inode, blockno)

        lock.release()
        if self.size > self.maxsize or len(self.cache) > MAX_CACHE_ENTRIES:
            self._expire()
        self.mlock.acquire(inode, blockno)

        try:
            el = self._get(inode, blockno)
            oldsize = os.fstat(el.fileno()).st_size

            # Provide fh to caller
            try:
                log.debug('get(inode=%d, block=%d): yield', inode, blockno)
                yield el
            finally:
                # Update cachesize
                el.flush()
                newsize = os.fstat(el.fileno()).st_size
                self.size += newsize - oldsize

        finally:
            self.mlock.release(inode, blockno)
            lock.acquire()

        log.debug('get(inode=%d, block=%d): end', inode, blockno)


    def _get(self, inode, blockno):
        log.debug('_get(inode=%d, block=%d): start', inode, blockno)

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
                log.debug('get(inode=%d, block=%d): creating new block', inode, blockno)
                el = CacheEntry(inode, blockno, None, filename, "w+b")

            # Need to download corresponding object
            else:
                log.debug('get(inode=%d, block=%d): downloading block', inode, blockno)
                el = CacheEntry(inode, blockno, obj_id, filename, "w+b")
                retry_exc(300, [ KeyError ], self.bucket.fetch_fh,
                          's3ql_data_%d' % obj_id, el)
                self.size += os.fstat(el.fileno()).st_size

            self.cache[(inode, blockno)] = el

        # In Cache
        else:
            log.debug('get(inode=%d, block=%d): in cache', inode, blockno)
            self.cache.to_head((inode, blockno))

        el.last_access = time.time()

        log.debug('get(inode=%d, block=%d): end', inode, blockno)
        return el

    def _expire(self):
        """Perform cache expiry"""

        # Note that we have to make sure that the cache entry is written into
        # the database before we remove it from the cache!

        log.debug('_expire: start')

        queue = UploadQueue(self)

        with self.expiry_lock:
            while (len(self.cache) > MAX_CACHE_ENTRIES or
                   (len(self.cache) > 0  and self.size > self.maxsize)):

                # Try to expire entries that are not dirty
                for el in self.cache.values_rev():
                    if el.dirty:
                        log.debug('_expire: %s is dirty, trying to flush', el)
                        break
                    self._remove_entry(el)

                # If this did not work, then we try to flush just
                # enough entries
                need_size = self.size - self.maxsize
                need_entries = len(self.cache) - MAX_CACHE_ENTRIES
                for el in self.cache.values_rev():
                    if need_size < 0 and need_entries < 0:
                        break

                    log.debug('_expire: adding %s to queue', el)
                    if el.dirty:
                        need_size -= queue.add(el)
                    else:
                        need_size -= os.fstat(el.fileno()).st_size
                    need_entries -= 1

                log.debug('_expire: waiting for queue')
                queue.wait()

        log.debug('_expire: end')


    def remove(self, inode, lock, start_no, end_no=None):
        """Remove blocks for `inode`
        
        If `end_no` is not specified, remove just the `start_no` block.
        Otherwise removes all blocks from `start_no` to, but not including,
         `end_no`. 
        
        This method releases `lock' for the managed context, so the caller must
        not hold any prior database locks and must not try to acquire any
        database locks in the managed context.
        """

        log.debug('remove(inode=%d, start=%d, end=%s): start',
                  inode, start_no, end_no)

        lock.release()
        
        if end_no is None:
            end_no = start_no + 1
            
        try:
            for blockno in range(start_no, end_no):
                with self.mlock(inode, blockno):
                    if (inode, blockno) in self.cache:
                        # Type inference fails here
                        #pylint: disable-msg=E1103
                        el = self.cache.pop((inode, blockno))
    
                        self.size -= os.fstat(el.fileno()).st_size
                        el.close()
                        os.unlink(el.name)
    
                        if el.obj_id is None:
                            log.debug('remove(inode=%d, blockno=%d): end (block only in cache)',
                                      inode, blockno)
                            return
    
                        log.debug('remove(inode=%d, blockno=%d): block in cache and db', inode, blockno)
                        obj_id = el.obj_id
    
                    else:
                        try:
                            obj_id = self.dbcm.get_val('SELECT obj_id FROM blocks WHERE inode=? '
                                                       'AND blockno = ?', (inode, blockno))
                        except KeyError:
                            log.debug('remove(inode=%d, blockno=%d): end (block does not exist)',
                                      inode, blockno)
                            return
    
                        log.debug('remove(inode=%d, blockno=%d): block only in db ', inode, blockno)
    
                    self.dbcm.execute('DELETE FROM blocks WHERE inode=? AND blockno=?',
                                      (inode, blockno))
                self.removal_queue.add(obj_id)

        finally:
            lock.acquire()

        log.debug('remove(inode=%d, start=%d, end=%s): end',
                  inode, start_no, end_no)

    def flush(self, inode):
        """Upload dirty data for `inode`"""

        log.debug('flush(inode=%d): start', inode)
        queue = UploadQueue(self)

        for el in self.cache.itervalues():
            if el.inode != inode:
                continue
            if not el.dirty:
                continue

            queue.add(el)

        queue.wait()
        log.debug('flush(inode=%d): end', inode)

    def flush_all(self):
        """Upload all dirty data"""

        log.debug('flush_all: start')
        queue = UploadQueue(self)

        for el in self.cache.itervalues():
            if not el.dirty:
                continue

            queue.add(el)

        queue.wait()
        log.debug('flush_all: end')

    def clear(self):
        """Upload all dirty data and clear cache"""

        log.debug('clear: start')
        bak = self.maxsize
        # maxsize=0 is not sufficient, that would keep entries with 0 size
        self.maxsize = -1
        self._expire()
        self.maxsize = bak
        log.debug('clear: end')

    def __del__(self):
        if len(self.cache) > 0:
            raise RuntimeError("BlockCache instance was destroyed without calling close()!")


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


class UploadQueue(object):
    '''
    Schedules and executes object uploads to make optimum usage
    network bandwith and CPU time.
    '''

    def __init__(self, bcache):
        self.threads = list()
        self.bcache = bcache
        self.max_threads = 10
        self.transit_size = 0
        self.max_transit = 1024 * 1024

    def add(self, el):
        '''Upload cache entry `el`
        
        Returns size of cache entry. This function may block if the
        queue is already full, otherwise it returns immediately after
        compression while the upload proceeds in the background.
        '''

        log.debug('UploadQueue.add(%s): start', el)

        mlock = self.bcache.mlock
        mlock.acquire(el.inode, el.blockno)
        mlock_released = False
        try:
            # Now that we have the lock, check that the object still exists
            if (el.inode, el.blockno) not in self.bcache.cache:
                mlock.release(el.inode, el.blockno)
                mlock_released = True
                log.debug('UploadQueue.add(%s): end (entry has vanished)', el)
                return 0

            size = os.fstat(el.fileno()).st_size
            if not el.dirty:
                mlock.release(el.inode, el.blockno)
                mlock_released = True
                log.debug('UploadQueue.add(%s): end (entry not dirty)', el)
                return size

            log.debug('UploadQueue.add(%s): preparing upload', el)
            fn = self._prepare_upload(el)
            if fn:
                if (len(self.threads) > self.max_threads or
                    (self.transit_size > self.max_transit and len(self.threads) > 1)):
                    log.debug('UploadQueue.add(%s): waiting for upload thread', el)
                    self.wait_for_thread()

                log.debug('UploadQueue.add(%s): starting upload thread', el)
                def _do():
                    try:
                        log.debug('UploadQueue.add(%s): uploading...', el)
                        fn()
                        el.dirty = False
                        self.transit_size -= size
                        log.debug('UploadQueue.add(%s): upload complete.', el)
                    finally:
                        mlock.release(el.inode, el.blockno)
                self.transit_size += size
                t = ExceptionStoringThread(_do, log)
                self.threads.append(t)
                t.start()
                mlock_released = True
            else:
                log.debug('UploadQueue.add(%s): no upload required', el)
                el.dirty = False
                mlock.release(el.inode, el.blockno)
                mlock_released = True

        except:
            if not mlock_released:
                mlock.release(el.inode, el.blockno)
            raise

        log.debug('UploadQueue.add(%s): end', el)
        return size

    def wait_for_thread(self):
        while True:
            for t in self.threads:
                t.join(1)
                if not t.is_alive():
                    self.threads.remove(t)
                    t.join_and_raise()
                    return

    def wait(self):
        while len(self.threads) > 0:
            self.wait_for_thread()

    def _prepare_upload(self, el):
        '''Prepare upload of specified cache entry
        
        Returns a function that does the required network transactions. Returns
        None if no network access is required.
        
        Caller has to take care of any necessary locking.
        '''

        log.debug('UploadQueue._prepare_upload(inode=%d, blockno=%d): start',
                  el.inode, el.blockno)

        size = os.fstat(el.fileno()).st_size

        el.seek(0)
        hash_ = sha256_fh(el)
        old_obj_id = el.obj_id
        with self.bcache.dbcm.transaction() as conn:
            try:
                el.obj_id = conn.get_val('SELECT id FROM objects WHERE hash=?', (hash_,))

            except KeyError:
                need_upload = True
                el.obj_id = conn.rowid('INSERT INTO objects (refcount, hash, size) VALUES(?, ?, ?)',
                                      (1, hash_, size))
                log.debug('UploadQueue._prepare_upload(inode=%d, blockno=%d): created new object %d',
                          el.inode, el.blockno, el.obj_id)

            else:
                need_upload = False
                log.debug('UploadQueue._prepare_upload(inode=%d, blockno=%d): (re)linking to %d',
                          el.inode, el.blockno, el.obj_id)
                conn.execute('UPDATE objects SET refcount=refcount+1 WHERE id=?',
                             (el.obj_id,))

            if old_obj_id is None:
                log.debug('UploadQueue._prepare_upload(inode=%d, blockno=%d): no previous object',
                          el.inode, el.blockno)
                conn.execute('INSERT INTO blocks (obj_id, inode, blockno) VALUES(?,?,?)',
                             (el.obj_id, el.inode, el.blockno))
                to_delete = False
            else:
                conn.execute('UPDATE blocks SET obj_id=? WHERE inode=? AND blockno=?',
                             (el.obj_id, el.inode, el.blockno))
                refcount = conn.get_val('SELECT refcount FROM objects WHERE id=?',
                                        (old_obj_id,))
                if refcount > 1:
                    log.debug('_UploadQueue.prepare_upload(inode=%d, blockno=%d): '
                              'decreased refcount for prev. obj: %d',
                              el.inode, el.blockno, old_obj_id)
                    conn.execute('UPDATE objects SET refcount=refcount-1 WHERE id=?',
                                 (old_obj_id,))
                    to_delete = False
                else:
                    log.debug('UploadQueue._prepare_upload(inode=%d, blockno=%d): '
                              'prev. obj %d marked for removal',
                              el.inode, el.blockno, old_obj_id)
                    conn.execute('DELETE FROM objects WHERE id=?', (old_obj_id,))
                    to_delete = True


        if need_upload:
            fn = self.bcache.bucket.prep_store_fh('s3ql_data_%d' % el.obj_id, el)
            if to_delete:
                def doit():
                    fn()
                    retry_exc(300, [ KeyError ], self.bcache.bucket.delete,
                              's3ql_data_%d' % old_obj_id)
            else:
                doit = fn
        elif to_delete:
            doit = lambda : retry_exc(300, [ KeyError ], self.bcache.bucket.delete,
                                      's3ql_data_%d' % old_obj_id)
        else:
            doit = None

        log.debug('UploadQueue._prepare_upload(inode=%d, blockno=%d): end',
                  el.inode, el.blockno)
        return doit
    
    
class RemovalQueue(object):
    '''
    Schedules and executes object removals to make optimum usage network
    bandwith.
    '''

    def __init__(self, bcache):
        self.threads = list()
        self.bcache = bcache
        self.max_threads = 25

    def add(self, obj_id):
        '''Remove object obj_id
        
        This function may block if the queue is already full, otherwise it
        returns immediately while the removal proceeds in the background.
        '''

        log.debug('RemovalQueue.add(%s): start', obj_id)

 
        fn = self._prepare_removal(obj_id)
        
        if fn:
            if len(self.threads) > self.max_threads:
                log.debug('RemovalQueue.add(%s): waiting for removal thread', obj_id)
                self.wait_for_thread()

            log.debug('RemovalQueue.add(%s): starting removal thread', obj_id)
            t = ExceptionStoringThread(fn, log)
            self.threads.append(t)
            t.start()
        else:
            log.debug('RemovalQueue.add(%s): no retwork transaction required', obj_id)

        log.debug('RemovalQueue.add(%s): end', obj_id)

    def wait_for_thread(self):
        while True:
            for t in self.threads:
                t.join(1)
                if not t.is_alive():
                    self.threads.remove(t)
                    t.join_and_raise()
                    return

    def wait(self):
        while len(self.threads) > 0:
            self.wait_for_thread()

    def _prepare_removal(self, obj_id):
        '''Prepare removal of specified object
        
        Returns a function that does the required network transactions. Returns
        None if no network access is required.
        '''

        log.debug('RemovalQueue._prepare_remval(%d): start', obj_id)

        with self.bcache.dbcm.transaction() as conn:
            refcount = conn.get_val('SELECT refcount FROM objects WHERE id=?',
                                    (obj_id,))
            if refcount > 1:
                log.debug('RemovalQueue._prepare_removal(%d): decreased refcount', obj_id)
                conn.execute('UPDATE objects SET refcount=refcount-1 WHERE id=?',
                             (obj_id,))
                to_delete = False
            else:
                log.debug('RemovalQueue._prepare_remval(%d): refcount reached 0', obj_id)
                conn.execute('DELETE FROM objects WHERE id=?', (obj_id,))
                to_delete = True

        if to_delete:
            doit = lambda : retry_exc(300, [ KeyError ], self.bcache.bucket.delete,
                                      's3ql_data_%d' % obj_id)
        else:
            doit = None

        log.debug('RemovalQueue._prepare_remval(%d): end', obj_id)
        return doit    