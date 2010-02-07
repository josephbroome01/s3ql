'''
$Id$

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function

from time import sleep
from boto.s3.connection import S3Connection
from contextlib import contextmanager
import boto.exception as bex
import shutil
from cStringIO import StringIO
from s3ql.common import (retry, sha256, ExceptionStoringThread)
import tempfile
import hmac
import logging
import threading
import pycryptopp
import errno
import cPickle as pickle
import os
import time
import hashlib
import bz2
from base64 import b64decode, b64encode
import struct

__all__ = [ "Connection", "ConcurrencyError", "LocalConnection", 'ChecksumError' ]

log = logging.getLogger("s3")

# For testing 
# Don't change randomly, these values are fine tuned
# for the tests to work without too much time.
LOCAL_TX_DELAY = 0.02
LOCAL_PROP_DELAY = 0.09

class Connection(object):
    """Represents a connection to Amazon S3

    This class just dispatches everything to boto. Note separate boto connection 
    object for each thread.
    """

    def __init__(self, awskey, awspass):
        self.awskey = awskey
        self.awspass = awspass
        self.pool = list()
        self.conn_cnt = 0

    def _pop_conn(self):
        '''Get boto connection object from the pool'''

        try:
            conn = self.pool.pop()
        except IndexError:
            # Need to create a new connection
            log.debug("Creating new boto connection (active conns: %d)...",
                      self.conn_cnt)
            conn = S3Connection(self.awskey, self.awspass)
            self.conn_cnt += 1

        return conn

    def _push_conn(self, conn):
        '''Return boto connection object to pool'''
        self.pool.append(conn)

    def delete_bucket(self, name):
        """Delete bucket"""
        with self._get_boto() as boto:
            boto.delete_bucket(name)

    @contextmanager
    def _get_boto(self):
        """Provide boto connection object"""

        conn = self._pop_conn()
        try:
            yield conn
        finally:
            self._push_conn(conn)

    def create_bucket(self, name, passphrase=None):
        """Create and return an S3 bucket"""

        with self._get_boto() as boto:
            boto.create_bucket(name)
            retry(60, self.bucket_exists, name)

        return self.get_bucket(name, passphrase)

    def get_bucket(self, name, passphrase=None):
        """Return a bucket instance for the bucket `name`
        
        Raises `KeyError` if the bucket does not exist.
        """

        with self._get_boto() as boto:
            try:
                boto.get_bucket(name)
            except bex.S3ResponseError as e:
                if e.status == 404:
                    raise KeyError("Bucket %r does not exist." % name)
                else:
                    raise
        return Bucket(self, name, passphrase)

    def bucket_exists(self, name):
        """Check if the bucket `name` exists"""

        try:
            self.get_bucket(name)
        except KeyError:
            return False
        else:
            return True

class LocalConnection(Connection):
    """A connection that stores buckets on the local disk rather than
    on S3.
    """

    def __init__(self):
        super(LocalConnection, self).__init__('awskey', 'awspass')

    def delete_bucket(self, name):
        """Delete bucket"""

        if not os.path.exists(name):
            raise KeyError('Directory of local bucket does not exist')

        os.rmdir(name)


    def create_bucket(self, name, passphrase=None):
        """Create and return an S3 bucket"""

        if os.path.exists(name):
            raise RuntimeError('Bucket already exists')
        os.mkdir(name)

        return self.get_bucket(name, passphrase)

    def get_bucket(self, name, passphrase=None):
        """Return a bucket instance for the bucket `name`
        
        Raises `KeyError` if the bucket does not exist.
        """

        if not os.path.exists(name):
            raise KeyError('Local bucket directory %s does not exist' % name)
        return LocalBucket(self, name, passphrase)



class Bucket(object):
    """Represents a bucket stored in Amazon S3.

    This class should not be instantiated directly, but using
    `Connection.get_bucket()`.

    The class behaves more or less like a dict. It raises the
    same exceptions, can be iterated over and indexed into.
    """

    def clear(self):
        """Delete all objects
        
        This function starts multiple threads."""

        threads = list()
        for (no, s3key) in enumerate(self):
            if no != 0 and no % 1000 == 0:
                log.info('Deleted %d objects so far..', no)

            log.debug('Deleting key %s', s3key)
            t = ExceptionStoringThread(self.delete_key, args=(s3key,))
            t.start()
            threads.append(t)

            if len(threads) > 50:
                log.debug('50 threads reached, waiting..')
                threads.pop(0).join_and_raise()

        log.debug('Waiting for removal threads')
        for t in threads:
            t.join_and_raise()

    @contextmanager
    def _get_boto(self):
        '''Provide boto bucket object'''
        # Access to protected methods ok
        #pylint: disable-msg=W0212

        boto_conn = self.conn._pop_conn()
        try:
            yield boto_conn.get_bucket(self.name)
        finally:
            self.conn._push_conn(boto_conn)

    def __init__(self, conn, name, passphrase):
        self.conn = conn
        self.passphrase = passphrase
        self.name = name

    def __str__(self):
        return "<bucket: %s>" % self.name

    def __getitem__(self, key):
        return self.fetch(key)[0]

    def __setitem__(self, key, value):
        self.store(key, value)

    def __delitem__(self, key):
        self.delete_key(key)

    def __iter__(self):
        return self.keys()

    def  __contains__(self, key):
        return self.has_key(key)

    def has_key(self, key):
        with self._get_boto() as boto:
            bkey = boto.get_key(key)

        return bkey is not None

    def iteritems(self):
        for key in self.keys():
            yield (key, self[key])

    def lookup_key(self, key):
        """Return metadata for given key.

        If the key does not exist, KeyError is raised.
        """

        with self._get_boto() as boto:
            bkey = boto.get_key(key)

        if bkey is None:
            raise KeyError('Key does not exist: %s' % key)

        encrypted = bkey.metadata['encrypted'] == 'True'
        if encrypted and not self.passphrase:
            raise ChecksumError('Encrypted key and no passphrase supplied')

        if 'meta' in bkey.metadata:
            meta_raw = b64decode(bkey.metadata['meta'])
            if self.passphrase:
                meta_raw = decrypt(meta_raw, self.passphrase)
            metadata = pickle.loads(meta_raw)
        else:
            metadata = dict()

        return metadata

    def delete_key(self, key, force=False):
        """Deletes the specified key

        ``bucket.delete_key(key)`` can also be written as ``del bucket[key]``.
        If `force` is true, do not return an error if the key does not exist.

        """

        with self._get_boto() as boto:
            if not force and boto.get_key(key) is None:
                raise KeyError('Key does not exist: %s' % key)

            boto.delete_key(key)


    def keys(self):
        """List keys in bucket

        Returns an iterator over all keys in the bucket.
        """

        with self._get_boto() as boto:
            for bkey in boto.list():
                yield bkey.name

    def get_size(self):
        """Get total size of bucket"""

        with self._get_boto() as boto:
            size = 0
            for bkey in boto.list():
                size += bkey.size

        return size

    def fetch(self, key):
        """Return data stored under `key`.

        Returns a tuple with the data and metadata. If only the data
        itself is required, ``bucket[key]`` is a more concise notation
        for ``bucket.fetch(key)[0]``.
        """

        fh = StringIO()
        meta = self.fetch_fh(key, fh)

        return (fh.getvalue(), meta)

    def store(self, key, val, metadata=None):
        """Store data under `key`.

        `metadata` can be a dictionary of additional attributes to 
        store with the object. A key named ``last-modified`` with
        the current UTC timestamp is always added automatically.

        If no metadata is required, one can simply assign to the
        subscripted bucket instead of using this function:
        ``bucket[key] = val`` is equivalent to ``bucket.store(key,
        val)``.
        """
        if isinstance(val, unicode):
            val = val.encode('us-ascii')

        fh = StringIO(val)
        self.store_fh(key, fh, metadata)

    def store_wait(self, key, val, metadata=None):
        """Store data under `key` and wait for propagation

        Like `store`, but wait until the update has propagated in
        S3.
        """

        stamp = time.time() - time.timezone
        self.store(key, val, metadata)

        def check_key():
            try:
                meta = self.lookup_key(key)
            except KeyError:
                return False
            else:
                return meta['last-modified'] >= stamp

        retry(600, check_key)

    def fetch_fh(self, key, fh):
        """Fetch data for `key` and write to `fh`

        Return a dictionary with the metadata.
        """

        if self.passphrase:
            tmp = tempfile.TemporaryFile()
            (fh, tmp) = (tmp, fh)

        with self._get_boto() as boto:
            bkey = boto.get_key(key)
            if bkey is None:
                raise KeyError('Key does not exist: %s' % key)
            fh.seek(0)
            bkey.get_contents_to_file(fh)

        encrypted = bkey.metadata['encrypted'] == 'True'
        if encrypted and not self.passphrase:
            raise ChecksumError('Encrypted key and no passphrase supplied')

        if 'meta' in bkey.metadata:
            meta_raw = b64decode(bkey.metadata['meta'])
            if self.passphrase:
                meta_raw = decrypt(meta_raw, self.passphrase)
            metadata = pickle.loads(meta_raw)
        else:
            metadata = dict()

        if self.passphrase:
            (fh, tmp) = (tmp, fh)
            tmp.seek(0)
            fh.seek(0)
            decrypt_uncompress_fh(tmp, fh, self.passphrase)
            tmp.close()

        return metadata

    def store_fh(self, key, fh, metadata=None):
        """Store data in `fh` under `key`

        `metadata` can be a dictionary of additional attributes to 
        store with the object. A key named ``last-modified`` with
        the current UTC timestamp is always added automatically.
        """

        # TODO: We should get rid of the last-modified and only
        # store metadata if it's not empty.

        # TODO: Switch to compression with pyliblzma. To keep this backwards
        # compatible, we will introduce a new (unencryted) "compression-method"
        # metadata header that defaults to bz2 if absent.

        if metadata is None:
            metadata = dict()

        fh.seek(0)
        metadata['last-modified'] = time.time() - time.timezone
        meta_raw = pickle.dumps(metadata, 2)

        if self.passphrase:
            # We need to generate a temporary copy to determine the
            # size of the object (which needs to transmitted as Content-Length)
            nonce = struct.pack(b'<f', time.time() - time.timezone) + bytes(key)
            tmp = tempfile.TemporaryFile()
            compress_encrypt_fh(fh, tmp, self.passphrase, nonce)
            (fh, tmp) = (tmp, fh)
            meta_raw = encrypt(meta_raw, self.passphrase, nonce)
            fh.seek(0)

        done = False
        while not done:
            with self._get_boto() as boto:
                bkey = boto.new_key(key)
                bkey.set_metadata('meta', b64encode(meta_raw))
                bkey.set_metadata('encrypted', 'True' if self.passphrase else 'False')
                try:
                    bkey.set_contents_from_file(fh)
                    done = True
                except bex.S3ResponseError as exc:
                    if exc.status == 400 and exc.error_code == 'RequestTimeout':
                        log.warn('RequestTimeout when uploading to Amazon S3. Retrying..')
                    else:
                        raise

        if self.passphrase:
            (fh, tmp) = (tmp, fh)
            tmp.close()


    def copy(self, src, dest):
        """Copy data stored under `src` to `dest`"""

        with self._get_boto() as boto:
            boto.copy_key(dest, self.name, src)


class LocalBucket(Bucket):
    '''A bucket that is stored on the local harddisk'''

    def __init__(self, conn, name, passphrase):
        super(LocalBucket, self).__init__(conn, name, passphrase)
        self.bbucket = LocalBotoBucket(name)

    @contextmanager
    def _get_boto(self):
        '''Provide boto bucket object'''

        yield self.bbucket

class LocalBotoKey(dict):
    '''
    Pretends to be a boto S3 key.
    '''

    def __init__(self, bucket, name, meta):
        super(LocalBotoKey, self).__init__()
        self.bucket = bucket
        self.name = name
        self.metadata = meta

    def get_contents_to_file(self, fh):
        log.debug("LocalBotoKey: get_contents_to_file() for %s", self.name)

        if self.name in self.bucket.in_transmit:
            raise ConcurrencyError()

        self.bucket.in_transmit.add(self.name)
        sleep(LOCAL_TX_DELAY)
        self.bucket.in_transmit.remove(self.name)

        filename = os.path.join(self.bucket.name, b64encode(self.name))
        with open(filename + '.dat', 'rb') as src:
            fh.seek(0)
            fh.write(src.read())
        with open(filename + '.meta', 'rb') as src:
            self.metadata = pickle.load(src)

    def set_contents_from_file(self, fh):
        log.debug("LocalBotoKey: set_contents_from_file() for %s", self.name)

        if self.name in self.bucket.in_transmit:
            raise ConcurrencyError()

        self.bucket.in_transmit.add(self.name)
        sleep(LOCAL_TX_DELAY)
        self.bucket.in_transmit.remove(self.name)

        filename = os.path.join(self.bucket.name, b64encode(self.name))
        fh.seek(0)
        with open(filename + '.tmp', 'wb') as dest:
            dest.write(fh.read())
        with open(filename + '.mtmp', 'wb') as dest:
            pickle.dump(self.metadata, dest, 2)

        def set_():
            sleep(LOCAL_PROP_DELAY)
            log.debug("LocalBotoKey: Committing store for %s", self.name)
            try:
                os.rename(filename + '.tmp', filename + '.dat')
                os.rename(filename + '.mtmp', filename + '.meta')
            except OSError as e:
                # Quick successive calls of store may fail, because they
                # overwrite an existing .tmp file, which is already
                # renamed by an earlier thread when the current thread tries
                # to rename.
                if e.errno == errno.ENOENT:
                    pass
                else:
                    raise

        t = threading.Thread(target=set_)
        t.start()

    def set_metadata(self, key, val):
        self.metadata[key] = val

    def get_metadata(self, key):
        return self.metadata[key]


class LocalBotoBucket(object):
    """
    Represents a bucket stored on a local directory and
    emulates an artificial propagation delay and transmit time. 

    It tries to raise ConcurrencyError if several threads try to write or read
    the same object at a time (but it cannot guarantee to catch these cases).
    """

    def __init__(self, name):
        super(LocalBotoBucket, self).__init__()
        self.name = name
        self.in_transmit = set()

    def delete_key(self, key):
        log.debug("LocalBotoBucket: Handling delete_key(%s)", key)
        if key in self.in_transmit:
            raise ConcurrencyError
        self.in_transmit.add(key)
        sleep(LOCAL_TX_DELAY)
        self.in_transmit.remove(key)

        filename = os.path.join(self.name, b64encode(key))
        if not os.path.exists(filename + '.dat'):
            raise KeyError('Key does not exist in bucket')

        def set_():
            sleep(LOCAL_PROP_DELAY)
            log.debug("LocalBotoBucket: Committing delete_key(%s)", key)
            os.unlink(filename + '.dat')
            os.unlink(filename + '.meta')

        threading.Thread(target=set_).start()

    def list(self):
        # We add the size attribute outside init
        #pylint: disable-msg=W0201
        log.debug("LocalBotoBucket: Handling list()")
        for name in os.listdir(self.name):
            if not name.endswith('.dat'):
                continue
            key = b64decode(name[:-len('.dat')])
            el = LocalBotoKey(self, key, dict())
            el.size = os.path.getsize(os.path.join(self.name, name))
            yield el

    def get_key(self, key):
        log.debug("LocalBotoBucket: Handling get_key(%s)", key)
        if key in self.in_transmit:
            raise ConcurrencyError
        self.in_transmit.add(key)
        sleep(LOCAL_TX_DELAY)
        self.in_transmit.remove(key)
        filename = os.path.join(self.name, b64encode(key))
        if os.path.exists(filename + '.dat'):
            with open(filename + '.meta', 'rb') as src:
                metadata = pickle.load(src)

            return LocalBotoKey(self, key, metadata)
        else:
            return None

    def new_key(self, key):
        return LocalBotoKey(self, key, dict())

    def copy_key(self, dest, src_bucket, src):
        log.debug("LocalBotoBucket: Received copy from %s to %s", src, dest)

        if dest in self.in_transmit or src in self.in_transmit:
            raise ConcurrencyError
        self.in_transmit.add(src)
        self.in_transmit.add(dest)
        sleep(LOCAL_TX_DELAY)

        filename_src = os.path.join(src_bucket, b64encode(src))
        filename_dest = os.path.join(self.name, b64encode(dest))

        if not os.path.exists(filename_src + '.dat'):
            raise KeyError('source key does not exist')

        def set_():
            sleep(LOCAL_PROP_DELAY)
            log.debug("LocalBotoBucket: Committing copy from %s to %s", src, dest)
            shutil.copyfile(filename_src + '.dat', filename_dest + '.dat')
            shutil.copyfile(filename_src + '.meta', filename_dest + '.meta')

        threading.Thread(target=set_).start()
        self.in_transmit.remove(dest)
        self.in_transmit.remove(src)
        log.debug("LocalBotoBucket: Returning from copy %s to %s", src, dest)


class ConcurrencyError(Exception):
    """Raised if several threads try to access the same s3 object
    """
    pass

class ChecksumError(Exception):
    """
    Raised if there is a checksum error in the data that we received 
    from S3.
    """
    pass


def encrypt(buf, passphrase, nonce):
    '''Encrypt given string'''

    if isinstance(nonce, unicode):
        nonce = nonce.encode('utf-8')

    key = sha256(passphrase + nonce)
    cipher = pycryptopp.cipher.aes.AES(key)
    hmac_ = hmac.new(key, digestmod=hashlib.sha256)

    hmac_.update(buf)
    buf = cipher.process(buf)
    hash_ = cipher.process(hmac_.digest())

    return b''.join(
                    (struct.pack(b'<B', len(nonce)),
                    nonce, hash_, buf))

def decrypt(buf, passphrase):
    '''Decrypt given string'''

    fh = StringIO(buf)

    len_ = struct.unpack(b'<B', fh.read(struct.calcsize(b'<B')))[0]
    nonce = fh.read(len_)

    key = sha256(passphrase + nonce)
    cipher = pycryptopp.cipher.aes.AES(key)
    hmac_ = hmac.new(key, digestmod=hashlib.sha256)

    # Read (encrypted) hmac
    hash_ = fh.read(32) # Length of hash

    buf = fh.read()
    buf = cipher.process(buf)
    hmac_.update(buf)

    hash_ = cipher.process(hash_)

    if hash_ != hmac_.digest():
        raise ChecksumError('HMAC mismatch')

    return buf


def compress_encrypt_fh(ifh, ofh, passphrase, nonce):
    '''Read `ifh` and write compressed, encrypted data to `ofh`'''

    if isinstance(nonce, unicode):
        nonce = nonce.encode('utf-8')

    compr = bz2.BZ2Compressor(9)
    bs = 900 * 1024 # 900k blocksize
    key = sha256(passphrase + nonce)
    cipher = pycryptopp.cipher.aes.AES(key)
    hmac_ = hmac.new(key, digestmod=hashlib.sha256)

    # Write nonce
    ofh.write(struct.pack(b'<B', len(nonce)))
    ofh.write(nonce)
    off = ofh.tell()

    # Reserve space for hmac
    ofh.write(b'0' * 32)

    while True:
        buf = ifh.read(bs)
        if not buf:
            buf = compr.flush()
            buf = cipher.process(buf)
            ofh.write(buf)
            break

        hmac_.update(buf)
        buf = compr.compress(buf)
        if buf:
            buf = cipher.process(buf)
            ofh.write(buf)

    buf = hmac_.digest()
    buf = cipher.process(buf)
    ofh.seek(off)
    ofh.write(buf)


def decrypt_uncompress_fh(ifh, ofh, passphrase):
    '''Read `ofh` and write decrypted, uncompressed data to `ofh`'''

    decomp = bz2.BZ2Decompressor()
    bs = 900 * 1024 # 900k blocksize

    # Read nonce
    len_ = struct.unpack(b'<B', ifh.read(struct.calcsize(b'<B')))[0]
    nonce = ifh.read(len_)

    key = sha256(passphrase + nonce)
    cipher = pycryptopp.cipher.aes.AES(key)
    hmac_ = hmac.new(key, digestmod=hashlib.sha256)

    # Read (encrypted) hmac
    hash_ = ifh.read(32) # Length of hash

    while True:
        buf = ifh.read(bs)
        if not buf:
            break

        buf = cipher.process(buf)
        try:
            buf = decomp.decompress(buf)
        except IOError:
            raise ChecksumError('Invalid bz2 stream')

        if buf:
            hmac_.update(buf)
            ofh.write(buf)

    if decomp.unused_data:
        raise ChecksumError('Data after end of bz2 stream')

    # Decompress hmac
    hash_ = cipher.process(hash_)

    if hash_ != hmac_.digest():
        raise ChecksumError('HMAC mismatch')

