'''
$Id$

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import unicode_literals, division, print_function

from time import sleep
from datetime import datetime
from s3ql import isodate
from boto.s3.connection import S3Connection
from contextlib import contextmanager 
import boto.exception as bex
import copy
from cStringIO import StringIO
from base64 import b64decode
from s3ql.common import (waitfor) 
import tempfile
import hmac
import logging
import threading
import pycryptopp
import io
import time
import hashlib
import bz2
import struct

__all__ = [ "Connection", "ConcurrencyError", "Bucket", "LocalBucket", "Metadata" ]

log = logging.getLogger("s3")
 

class Connection(object):
    """Represents a connection to Amazon S3

    Currently, this just dispatches everything to boto. Note
    that boto is not threadsafe, so we need to create a
    separate boto connection object for each thread.
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

    def delete_bucket(self, name, recursive=False):
        """Delete bucket"""
        
        if recursive:
            bucket = self.get_bucket(name)
            bucket.clear()
            
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
            
            # S3 needs some time before we can fetch the bucket
            waitfor(10, self.bucket_exists, name)
            
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


class Bucket(object):
    """Represents a bucket stored in Amazon S3.

    This class should not be instantiated directly, but using
    `Connection.get_bucket()`.

    The class behaves more or less like a dict. It raises the
    same exceptions, can be iterated over and indexed into.
    """
    
    def clear(self):
        """Delete all objects"""
        
        for s3key in self:
            log.debug('Deleting key %s', s3key)
            del self[s3key]
              
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
        self.name = name
        self.conn = conn
        self.passphrase = passphrase
            
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
        try:
            self.lookup_key(key)
        except KeyError:
            return False
        else:
            return True

    def iteritems(self):
        for key in self.keys():
            yield (key, self[key])

    def keys(self):
        for pair in self.list_keys():
            yield pair[0]

    def lookup_key(self, key):
        """Return metadata for given key.

        If the key does not exist, KeyError is raised. Otherwise a
        `Metadata` instance is returned.
        """
 
        with self._get_boto() as boto:
            bkey = boto.get_key(key)

        if bkey is not None:
            return self.boto_key_to_metadata(bkey)
        else:
            raise KeyError('Key does not exist: %s' % key)
  
    def delete_key(self, key, force=False):
        """Deletes the specified key

        ``bucket.delete_key(key)`` can also be written as ``del bucket[key]``.
        If `force` is true, do not return an error if the key does not exist.

        """
 
        with self._get_boto() as boto:
            if not force and boto.get_key(key) is None:
                raise KeyError('Key does not exist: %s' % key)
            
            boto.delete_key(key)


    def list_keys(self):
        """List keys in bucket

        Returns an iterator over all keys in the bucket. The iterator
        generates tuples of the form (key, metadata), where metadata is
        of the form returned by `lookup_key`.

        This function is also used if the bucket itself is used in an
        iterative context: ``for key in bucket`` is the same as ``for
        (key,metadata) in bucket.list_keys()`` (except that the former
        doesn't define the `metadata` variable).
        """

        with self._get_boto() as boto:
            for key in boto.list():
                yield (unicode(key.name), self.boto_key_to_metadata(key))


    def fetch(self, key):
        """Return data stored under `key`.

        Returns a tuple with the data and metadata. If only the data
        itself is required, ``bucket[key]`` is a more concise notation
        for ``bucket.fetch(key)[0]``.
        """
 
        fh = StringIO()
        meta = self.fetch_fh(key, fh)
        
        return (fh.getvalue(), meta)

    def store(self, key, val):
        """Store data under `key`.

        Returns the etag of the data.

        If the metadata is not required, one can simply assign to the
        subscripted bucket instead of using this function:
        ``bucket[key] = val`` is equivalent to ``bucket.store(key,
        val)``.
        """
        if isinstance(val, unicode):
            val = val.encode('us-ascii')
            
        fh = StringIO(val)
        etag = self.store_fh(key, fh)
        
        return etag
            
    def fetch_fh(self, key, fh):
        """Fetch data for `key` and write to `fh`

        Returns the metadata in the format used by `lookup_key`.
        """
 
        if self.passphrase:
            fh = CEWriter(fh, self.passphrase)
            
        with self._get_boto() as boto:
            bkey = boto.get_key(key)
            if bkey is None:
                raise KeyError('Key does not exist: %s' % key)

            bkey.get_contents_to_file(fh)
           
            
        metadata = self.boto_key_to_metadata(bkey)         
        
        if self.passphrase:
            fh.verify()
            metadata.etag = fh.get_hash()   

        return metadata

    def store_fh(self, key, fh):
        """Store data in `fh` under `key`

        Returns the md5 sum of `fh`. 
        """

        if self.passphrase:
            # Generate session key
            nonce = struct.pack(b'<f', time.time() - time.timezone) + key.encode('utf-8')
            
            # Stupid boto insists on calculating the md5 sum
            # separately and then calling fh.seek(), so we
            # can't directly stream the data
            tmp = CEReader(fh, self.passphrase, nonce)
            fh = tempfile.TemporaryFile()
            copy_fh(tmp, fh)
            fh.seek(0)
                   
        with self._get_boto() as boto:
            bkey = boto.new_key(key)
            bkey.set_contents_from_file(fh)

        if self.passphrase:
            fh.close()
            return tmp.get_hash()
        else:
            return b64decode(bkey.etag.rstrip('"').lstrip('"').encode('us-ascii'))


    def copy(self, src, dest):
        """Copy data stored under `src` to `dest`"""

        with self._get_boto() as boto:
            boto.copy_key(dest, self.name, src)

    @staticmethod
    def boto_key_to_metadata(bkey):
        """Extracts metadata from boto key object.
        """
   
        # The format depends on how the data has been retrieved (fetch or list)
        try:
            last_modified = datetime.strptime(bkey.last_modified, "%a, %d %b %Y %H:%M:%S %Z")
        except ValueError:
            last_modified = isodate.parse_datetime(bkey.last_modified)
            
        # Convert to UTC if timezone aware
        if last_modified.utcoffset():
            last_modified = (last_modified - last_modified.utcoffset()).replace(tzinfo=None)
                                                
            
        return Metadata(usertags=bkey.metadata,
                        etag=b64decode(bkey.etag.rstrip('"').lstrip('"').encode('us-ascii')),
                        key=bkey.name,
                        last_modified=last_modified,
                        size=int(bkey.size))



class LocalBucket(Bucket):
    """Represents a bucket stored in Amazon S3.

    This class doesn't actually connect but holds all data in memory.
    It is meant only for testing purposes. It emulates an artificial
    propagation delay and transmit time.

    It tries to raise ConcurrencyError if several threads try to write or read
    the same object at a time (but it cannot guarantee to catch these cases).
    The reason for this is not that
    concurrent accesses to the same object were in itself harmful, but
    that they should not occur because the write(), sync() etc.
    systemcalls are synchronized. So if the s3 accesses occur
    concurrent, something went wrong with the syscall synchronization.
    """

    def __init__(self, name="local"):
        # We deliberately do not call the superclass constructor
        #pylint: disable-msg=W0231
        self.keystore = {}
        self.name = name
        self.in_transmit = set()
        self.tx_delay = 0
        self.prop_delay = 0


    def lookup_key(self, key):
        log.debug("LocalBucket: Received lookup for %s", key)
        if key in self.in_transmit:
            raise ConcurrencyError
        self.in_transmit.add(key)
        sleep(self.tx_delay)
        self.in_transmit.remove(key)
        if key not in self.keystore:
            raise KeyError('Key does not exist: %s' % key)
        else:
            return self.keystore[key][1]

    def list_keys(self):
        for key in self.keystore:
            yield (key, self.keystore[key][1])

    def delete_key(self, key, force=False):
        if key in self.in_transmit:
            raise ConcurrencyError
        log.debug("LocalBucket: Received delete for %s", key)
        self.in_transmit.add(key)
        sleep(self.tx_delay)
        self.in_transmit.remove(key)

        # Make sure the key exists, otherwise we get an error
        # in a different thread
        if not force and not self.keystore.has_key(key):
            raise KeyError

        def set_():
            sleep(self.prop_delay)
            log.debug("LocalBucket: Committing delete for %s", key)
            # Don't bother if some other thread already deleted it
            try:
                del self.keystore[key]
            except KeyError:
                pass

        threading.Thread(target=set_).start()

    def fetch(self, key):
        log.debug("LocalBucket: Received fetch for %s", key)
        if key in self.in_transmit:
            raise ConcurrencyError
        self.in_transmit.add(key)
        sleep(self.tx_delay)
        self.in_transmit.remove(key)
        return self.keystore[key]

    def store(self, key, val):
        log.debug("LocalBucket: Received store for %s", key)
        if key in self.in_transmit:
            raise ConcurrencyError
        self.in_transmit.add(key)
        sleep(self.tx_delay)
        metadata = Metadata(key = key,
                            size = len(val),
                            last_modified = datetime.utcnow(),
                            etag =  hashlib.md5(val).hexdigest())
        def set_():
            sleep(self.prop_delay)
            log.debug("LocalBucket: Committing store for %s, etag %s", key, metadata.etag)
            self.keystore[key] = (val, metadata)
        t = threading.Thread(target=set_)
        t.start()
        self.in_transmit.remove(key)
        log.debug("LocalBucket: Returning from store for %s" % key)
        return metadata.etag


    def fetch_fh(self, key, fh):
        (data, metadata) = self.fetch(key)
        fh.write(data)
        return metadata

    def store_fh(self, key, fh):
        return self.store(key, fh.read())

    def copy(self, src, dest):
        """Copies data stored under `src` to `dest`
        """
        log.debug("LocalBucket: Received copy from %s to %s", src, dest)
        if dest in self.in_transmit or src in self.in_transmit:
            raise ConcurrencyError
        self.in_transmit.add(src)
        self.in_transmit.add(dest)
        sleep(self.tx_delay)
        def set_():
            sleep(self.prop_delay)
            log.debug("LocalBucket: Committing copy from %s to %s", src, dest)
            self.keystore[dest] = copy.deepcopy(self.keystore[src])
        threading.Thread(target=set_).start()
        self.in_transmit.remove(dest)
        self.in_transmit.remove(src)
        log.debug("LocalBucket: Returning from copy %s to %s", src, dest)


class Metadata(dict):
    """Represents the metadata associated with an S3 object.

    The "hardcoded" meta-information etag, size and last-modified are
    accessible as instance attributes. For access to user defined
    metadata, the instance has to be subscripted.

    Note that the last-modified attribute is a datetime object.
    """
    
    __slots__ = [ 'etag', 'key', 'last_modified', 'size' ]
    
    def __init__(self, etag, key, last_modified, size, usertags=None):
        if usertags is not None:
            super(Metadata, self).__init__(usertags)
        else: 
            super(Metadata, self).__init__()
        self.etag = etag
        self.key = key
        self.last_modified = last_modified
        self.size = size
        
        

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


class CEReader(io.IOBase):
    '''
    Generate compressed and encrypted input stream from a plaintext
    file handle.
    '''   
    
    def __init__(self, fh, passphrase, nonce, blocksize=128*1024):
        super(CEReader, self).__init__()
        self.fh = fh
        self.compr = bz2.BZ2Compressor(9)
        self.blocksize = blocksize
        self.eof = False
        
        # Generate session key
        nonce = sha256(nonce)
        key = md5(passphrase + nonce)
        self.cipher = pycryptopp.cipher.aes.AES(key)
        self.hash = hmac.new(key, digestmod=hashlib.sha256)
        self.buf = nonce
        
    def read(self, len_):
        '''Read compress, decrypt and return data from underlying file handle'''
        
        buf = self.buf
        
        if not buf and self.eof:
            return ''
                
        if not buf:
            while not buf:
                buf = self.fh.read(self.blocksize)
                if not buf:
                    buf = self.compr.flush()
                    self.eof = True
                    break
                self.hash.update(buf)
                buf = self.compr.compress(buf)
                
            buf = self.cipher.process(buf)
            
        if self.eof:
            buf += self.cipher.process(self.hash.digest())
            
        tmp = buf[:len_]
        self.buf = buf[len_:]
        
        return tmp
        
    def readable(self):
        return True  
                   
    def get_hash(self):
        '''Return hash of retrieved plaintext data.
        
        If called before all data hasa been read, it may return an
        intermediate value that includes data that is still in the
        internal read buffer.
        '''

        return self.hash.digest() 
    
class CEWriter(io.IOBase): 
    '''
    Generate plaintext output stream from a compressed and 
    encrypted file handle
    '''   
    
    def __init__(self, fh, passphrase):
        super(CEWriter, self).__init__()
        self.fh = fh
        self.decomp = bz2.BZ2Decompressor()
        self.nonce = b''
        self.cipher = None
        self.hash = None
        self.passphrase = passphrase
        self.buf = b''
        
    def write(self, buf):
        '''Decrypt and decompress `buf`, then write to underlying fh'''
        
        len_ = len(buf)
        
        if not self.cipher:
            i = 32 - len(self.nonce) 
            self.nonce += buf[:i]
            buf = buf[i:]
            
            if not buf:
                return len_
            
            # Reconstruct session key
            key = md5(self.passphrase + self.nonce)
            self.cipher = pycryptopp.cipher.aes.AES(key)
            self.hash = hmac.new(key, digestmod=hashlib.sha256)
        
        buf = self.cipher.process(buf)
        try:
            buf = self.decomp.decompress(buf)
            
        except EOFError:
            # We are past the end of the compressed stream
            self.buf += self.cipher.process(buf)
            buf = b''
    
            if len(self.buf) > len(self.hash.digest()):
                raise ChecksumError('Received corrupted data')
            
        except IOError:
            raise ChecksumError('Received corrupted data')
        
        if buf:
            self.fh.write(buf)
            self.hash.update(buf)
        
        return len_

    def verify(self):
        '''Verify checksum'''
        
        digest = self.decomp.unused_data + self.buf  
        if digest != self.hash.digest():
            raise ChecksumError('Received corrupted data')
      
    def flush(self):  
        self.fh.flush()

    def get_hash(self):
        '''Return hash of written plaintext data.
        
        If called before all data has been written, it may return an
        intermediate value that excludes data that is still in the
        internal decompression buffer.
        '''

        return self.hash.digest()               
    
    @staticmethod
    def writeable():
        return True
    
def copy_fh(infh, outfh):
    '''Copy contents of `infh` to `outfh`'''
    
    while True:
        buf = infh.read(128*1024)
        if not buf:
            break
        outfh.write(buf)   
        
def md5(s):
    return hashlib.md5(s).digest()

def sha256(s):
    return hashlib.sha256(s).digest()