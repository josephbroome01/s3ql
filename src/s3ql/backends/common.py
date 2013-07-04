'''
common.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from ..logging import logging # Ensure use of custom logger class
from ..common import QuietError, BUFSIZE, PICKLE_PROTOCOL, ChecksumError
from ..inherit_docstrings import (copy_ancestor_docstring, prepend_ancestor_docstring,
                                  ABCDocstMeta)
from Crypto.Cipher import AES
from Crypto.Util import Counter
from abc import abstractmethod, ABCMeta
from base64 import b64decode, b64encode
from contextlib import contextmanager
from functools import wraps
from getpass import getpass
from io import BytesIO
import bz2
import ssl
import configparser
import hashlib
import hmac
import http.client
import lzma
import os
import pickle
import re
import socket
import stat
import struct
import sys
import threading
import time
import zlib
import textwrap

log = logging.getLogger(__name__)

HMAC_SIZE = 32

RETRY_TIMEOUT = 60 * 60 * 24
def retry(fn):
    '''Decorator for retrying a method on some exceptions
    
    If the decorated method raises an exception for which the instance's
    `is_temp_failure(exc)` method is true, the decorated method is called again
    at increasing intervals. If this persists for more than *timeout* seconds,
    the most-recently caught exception is re-raised.
    '''

    @wraps(fn)
    def wrapped(self, *a, **kw):
        interval = 1 / 50
        waited = 0
        while True:
            try:
                return fn(self, *a, **kw)
            except Exception as exc:
                # Access to protected member ok
                #pylint: disable=W0212
                if not self.is_temp_failure(exc):
                    raise
                if waited > RETRY_TIMEOUT:
                    log.error('%s.%s(*): Timeout exceeded, re-raising %r exception',
                            self.__class__.__name__, fn.__name__, exc)
                    raise

                log.info('Encountered %s exception (%s), retrying call to %s.%s...',
                          type(exc).__name__, exc, self.__class__.__name__, fn.__name__)

                if hasattr(exc, 'retry_after') and exc.retry_after:
                    interval = exc.retry_after
                
            time.sleep(interval)
            waited += interval
            interval = min(5*60, 2*interval)

    # False positive
    #pylint: disable=E1101
    s = ('This method has been decorated and will automatically recall itself in '
         'increasing intervals for up to s3ql.backends.common.RETRY_TIMEOUT '
         'seconds if it raises an exception for which the instance\'s '
         '`is_temp_failure` method returns True.')
    if wrapped.__doc__ is None:
        wrapped.__doc__ = ''
        
    # Figure out proper indentation
    indent = 60
    for line in wrapped.__doc__.splitlines()[1:]:
        stripped = line.lstrip()
        if stripped:
            indent = min(indent, len(line) - len(stripped))

    indent_s = '\n' + ' ' * indent
    wrapped.__doc__ += ''.join(indent_s + line
                               for line in textwrap.wrap(s, width=80 - indent))
    wrapped.__doc__ += '\n'

    return wrapped

def is_temp_network_error(exc):
    '''Return true if *exc* represents a potentially temporary network problem'''

    if isinstance(exc, (http.client.IncompleteRead, socket.timeout,
                        ConnectionError, TimeoutError, InterruptedError)):
        return True
     
    # Server closed connection
    elif (isinstance(exc, http.client.BadStatusLine)
          and (not exc.line or exc.line == "''")):
        return True

    # Formally this is a permanent error. However, it may also indicate
    # that there is currently no network connection to the DNS server
    elif (isinstance(exc, socket.gaierror) 
          and exc.errno in (socket.EAI_AGAIN, socket.EAI_NONAME)):
        return True 
              
    return False
    
    
def http_connection(hostname, port, ssl_context=None):
    '''Return http connection to *hostname*:*port*
    
    This method honors the http_proxy and https_proxy environment
    variables.
    '''
    
    log.debug('Connecting to %s...', hostname)
    
    if 'https_proxy' in os.environ:
        proxy = os.environ['https_proxy']
        hit = re.match(r'^(https?://)?([a-zA-Z0-9.-]+)(:[0-9]+)?/?$', proxy)
        if not hit:
            log.warning('Unable to parse proxy setting %s', proxy)
        
        if hit.group(1) == 'https://':
            log.warning('HTTPS connection to proxy is probably pointless and not supported, '
                        'will use standard HTTP')
        
        if hit.group(3):
            proxy_port = int(hit.group(3)[1:])
        else:
            proxy_port = 80
            
        proxy_host = hit.group(2)
        log.info('Using proxy %s:%d', proxy_host, proxy_port)
        
        if ssl_context:
            conn = http.client.HTTPSConnection(proxy_host, proxy_port,
                                               context=ssl_context)
        else:
            conn = http.client.HTTPConnection(proxy_host, proxy_port)
        conn.set_tunnel(hostname, port)
        return conn
    
    elif ssl_context:
        return http.client.HTTPSConnection(hostname, port, context=ssl_context)
    else:
        return http.client.HTTPConnection(hostname, port)

    
def get_ssl_context(options):
    '''Construct SSLContext object from *options*

    If SSL is disabled, return None.
    '''

    if options.no_ssl:
        return None
    
    # Best practice according to http://docs.python.org/3/library/ssl.html#protocol-versions
    context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
    context.options |= ssl.OP_NO_SSLv2
    context.verify_mode = ssl.CERT_REQUIRED

    path = options.ssl_ca_path
    if path is None:
        log.debug('Reading default CA certificates.')
        context.set_default_verify_paths()
    elif os.path.isfile(path):
        log.debug('Reading CA certificates from file %s', path)
        context.load_verify_locations(cafile=path)
    else:
        log.debug('Reading CA certificates from directory %s', path)
        context.load_verify_locations(capath=path)

    return context


def sha256(s):
    return hashlib.sha256(s).digest()

class BackendPool(object):
    '''A pool of backends

    This class is threadsafe. All methods (except for internal methods
    starting with underscore) may be called concurrently by different
    threads.    
    '''

    def __init__(self, factory):
        '''Init pool
        
        *factory* should be a callable that provides new
        connections.
        '''

        self.factory = factory
        self.pool = []
        self.lock = threading.Lock()

    def pop_conn(self):
        '''Pop connection from pool'''

        with self.lock:
            if self.pool:
                return self.pool.pop()
            else:
                return self.factory()

    def push_conn(self, conn):
        '''Push connection back into pool'''

        with self.lock:
            self.pool.append(conn)

    @contextmanager
    def __call__(self):
        '''Provide connection from pool (context manager)'''

        conn = self.pop_conn()
        try:
            yield conn
        finally:
            self.push_conn(conn)


class AbstractBackend(object, metaclass=ABCMeta):
    '''Functionality shared between all backends.
    
    Instances behave similarly to dicts. They can be iterated over and
    indexed into, but raise a separate set of exceptions.
    
    The backend guarantees get after create consistency, i.e. a newly created
    object will be immediately retrievable. Additional consistency guarantees
    may or may not be available and can be queried for with instance methods.
    '''

    needs_login = True

    def __init__(self):
        super().__init__()

    def __getitem__(self, key):
        return self.fetch(key)[0]

    def __setitem__(self, key, value):
        self.store(key, value)

    def __delitem__(self, key):
        self.delete(key)

    def __iter__(self):
        return self.list()

    def  __contains__(self, key):
        return self.contains(key)

    def iteritems(self):
        for key in self.list():
            yield (key, self[key])

    @retry
    def perform_read(self, fn, key):
        '''Read object data using *fn*, retry on temporary failure
        
        Open object for reading, call `fn(fh)` and close object. If a temporary
        error (as defined by `is_temp_failure`) occurs during opening, closing
        or execution of *fn*, the operation is retried.
        '''
        with self.open_read(key) as fh:
            return fn(fh)

    @retry
    def perform_write(self, fn, key, metadata=None, is_compressed=False):
        '''Read object data using *fn*, retry on temporary failure
        
        Open object for writing, call `fn(fh)` and close object. If a temporary
        error (as defined by `is_temp_failure`) occurs during opening, closing
        or execution of *fn*, the operation is retried.
        '''

        with self.open_write(key, metadata, is_compressed) as fh:
            return fn(fh)

    def fetch(self, key):
        """Return data stored under `key`.

        Returns a tuple with the data and metadata. If only the data itself is
        required, ``backend[key]`` is a more concise notation for
        ``backend.fetch(key)[0]``.
        """

        def do_read(fh):
            data = fh.read()
            return (data, fh.metadata)

        return self.perform_read(do_read, key)

    def store(self, key, val, metadata=None):
        """Store data under `key`.

        `metadata` can be a dict of additional attributes to store with the
        object.

        If no metadata is required, one can simply assign to the subscripted
        backend instead of using this function: ``backend[key] = val`` is
        equivalent to ``backend.store(key, val)``.
        """

        self.perform_write(lambda fh: fh.write(val), key, metadata)

    @abstractmethod
    def is_temp_failure(self, exc):
        '''Return true if exc indicates a temporary error
    
        Return true if the given exception indicates a temporary problem. Most
        instance methods automatically retry the request in this case, so the
        caller does not need to worry about temporary failures.
        
        However, in same cases (e.g. when reading or writing an object), the
        request cannot automatically be retried. In these case this method can
        be used to check for temporary problems and so that the request can be
        manually restarted if applicable.
        '''

        pass

    @abstractmethod
    def lookup(self, key):
        """Return metadata for given key.

        If the key does not exist, `NoSuchObject` is raised.
        """

        pass

    @abstractmethod
    def get_size(self, key):
        '''Return size of object stored under *key*'''
        pass

    @abstractmethod
    def open_read(self, key):
        """Open object for reading

        Return a file-like object. Data can be read using the `read`
        method. metadata is stored in its *metadata* attribute and can be
        modified by the caller at will. The object must be closed explicitly.
        """

        pass

    @abstractmethod
    def open_write(self, key, metadata=None, is_compressed=False):
        """Open object for writing

        `metadata` can an additional (pickle-able) python object to store with
        the data. Returns a file- like object. The object must be closed closed
        explicitly. After closing, the *get_obj_size* may be used to retrieve
        the size of the stored object (which may differ from the size of the
        written data).

        The *is_compressed* parameter indicates that the caller is going to
        write compressed data, and may be used to avoid recompression by the
        backend.
        """

        pass

    @abstractmethod
    def clear(self):
        """Delete all objects in backend"""
        pass

    def contains(self, key):
        '''Check if `key` is in backend'''

        try:
            self.lookup(key)
        except NoSuchObject:
            return False
        else:
            return True

    @abstractmethod
    def delete(self, key, force=False):
        """Delete object stored under `key`

        ``backend.delete(key)`` can also be written as ``del backend[key]``.  If
        `force` is true, do not return an error if the key does not exist. Note,
        however, that even if *force* is False, it is not guaranteed that an
        attempt to delete a non-existing object will raise an error.
        """
        pass

    def delete_multi(self, keys, force=False):
        """Delete objects stored under `keys`

        Deleted objects are removed from the *keys* list, so that the caller can
        determine which objects have not yet been processed if an exception is
        occurs.
        
        If *force* is True, attempts to delete non-existing objects will
        succeed. Note, however, that even if *force* is False, it is not
        guaranteed that an attempt to delete a non-existing object will raise an
        error.
        """

        if not isinstance(keys, list):
            raise TypeError('*keys* parameter must be a list')

        for (i, key) in enumerate(keys):
            try:
                self.delete(key, force=force)
            except:
                del keys[:i]
                raise

        del keys[:]
    
    @abstractmethod
    def list(self, prefix=''):
        '''List keys in backend

        Returns an iterator over all keys in the backend.
        '''
        pass

    @abstractmethod
    def copy(self, src, dest):
        """Copy data stored under key `src` to key `dest`
        
        If `dest` already exists, it will be overwritten. The copying
        is done on the remote side. 
        """

        pass

    def rename(self, src, dest):
        """Rename key `src` to `dest`
        
        If `dest` already exists, it will be overwritten. The rename
        is done on the remote side.
        """

        self.copy(src, dest)
        self.delete(src)

class BetterBackend(AbstractBackend, metaclass=ABCDocstMeta):
    '''
    This class adds encryption, compression and integrity protection to a plain
    backend.
    '''

    def __init__(self, passphrase, compression, backend):
        super().__init__()

        assert passphrase is None or isinstance(passphrase, (bytes, bytearray, memoryview))
        
        self.passphrase = passphrase
        self.compression = compression
        self.backend = backend

        if compression not in ('bzip2', 'lzma', 'zlib', None):
            raise ValueError('Unsupported compression: %s' % compression)

    @copy_ancestor_docstring
    def lookup(self, key):
        metadata = self.backend.lookup(key)
        convert_legacy_metadata(metadata)
        return self._unwrap_meta(metadata)

    @prepend_ancestor_docstring
    def get_size(self, key):
        '''
        This method returns the compressed size, i.e. the storage space
        that's actually occupied by the object.
        '''

        return self.backend.get_size(key)

    @copy_ancestor_docstring
    def is_temp_failure(self, exc):
        return self.backend.is_temp_failure(exc)

    def _unwrap_meta(self, metadata):
        '''Unwrap metadata
        
        If the backend has a password set but the object is not encrypted,
        `ObjectNotEncrypted` is raised.
        '''

        if (not isinstance(metadata, dict)
            or 'encryption' not in metadata
            or 'compression' not in metadata):
            raise MalformedObjectError()

        encr_alg = metadata['encryption']
        encrypted = (encr_alg != 'None')

        if encrypted and not self.passphrase:
            raise ChecksumError('Encrypted object and no passphrase supplied')

        elif not encrypted and self.passphrase:
            raise ObjectNotEncrypted()

        # Pre 2.x buckets
        if any(k.startswith('meta') for k in metadata):
            parts = [ metadata[k] for k in sorted(metadata.keys())
                      if k.startswith('meta') ]
            buf = b64decode(''.join(parts))
        else:
            try:
                buf = metadata['data']
            except KeyError:
                raise MalformedObjectError() from None
            buf = b64decode(buf)

        if encrypted:
            buf = decrypt(buf, self.passphrase)

        try:
            metadata = pickle.loads(buf)
        except pickle.UnpicklingError as exc:
            if (isinstance(exc.args[0], str)
                and exc.args[0].startswith('invalid load key')):
                raise ChecksumError('Invalid metadata') from None
            raise

        return metadata

    @prepend_ancestor_docstring
    def open_read(self, key):
        """
        If the backend has a password set but the object is not encrypted,
        `ObjectNotEncrypted` is raised.
        """

        fh = self.backend.open_read(key)
        try:
            convert_legacy_metadata(fh.metadata)

            # Also checks if this is a BetterBucket storage object
            metadata = self._unwrap_meta(fh.metadata)

            compr_alg = fh.metadata['compression']
            encr_alg = fh.metadata['encryption']
    
            if compr_alg == 'BZIP2':
                decompressor = bz2.BZ2Decompressor()
            elif compr_alg == 'LZMA':
                decompressor = lzma.LZMADecompressor()
            elif compr_alg == 'ZLIB':
                decompressor = zlib.decompressobj()
            elif compr_alg == 'None':
                decompressor = None
            else:
                raise RuntimeError('Unsupported compression: %s' % compr_alg)
    
            if encr_alg == 'AES':
                fh = LegacyDecryptDecompressFilter(fh, self.passphrase, decompressor)
            else:
                if encr_alg == 'AES_v2':
                    fh = DecryptFilter(fh, self.passphrase)
                elif encr_alg != 'None':
                    raise RuntimeError('Unsupported encryption: %s' % encr_alg)
    
                if decompressor:
                    fh = DecompressFilter(fh, decompressor)
    
            fh.metadata = metadata
        except:
            fh.close()
            raise
        
        return fh

    @copy_ancestor_docstring
    def open_write(self, key, metadata=None, is_compressed=False):

        # We always store metadata (even if it's just None), so that we can
        # verify that the object has been created by us when we call lookup().
        meta_buf = pickle.dumps(metadata, PICKLE_PROTOCOL)

        meta_raw = dict()

        if self.passphrase:
            meta_raw['encryption'] = 'AES_v2'
            nonce = struct.pack('<f', time.time()) + key.encode('utf-8')
            meta_raw['data'] = b64encode(encrypt(meta_buf, self.passphrase, nonce))
        else:
            meta_raw['encryption'] = 'None'
            meta_raw['data'] = b64encode(meta_buf)
            nonce = None

        if is_compressed or not self.compression:
            compr = None
            meta_raw['compression'] = 'None'
        elif self.compression == 'zlib':
            compr = zlib.compressobj(9)
            meta_raw['compression'] = 'ZLIB'
        elif self.compression == 'bzip2':
            compr = bz2.BZ2Compressor(9)
            meta_raw['compression'] = 'BZIP2'
        elif self.compression == 'lzma':
            compr = lzma.LZMACompressor(preset=7)
            meta_raw['compression'] = 'LZMA'

        fh = self.backend.open_write(key, meta_raw)

        if nonce:
            fh = EncryptFilter(fh, self.passphrase, nonce)
        if compr:
            fh = CompressFilter(fh, compr)

        return fh


    @copy_ancestor_docstring
    def clear(self):
        return self.backend.clear()

    @copy_ancestor_docstring
    def contains(self, key):
        return self.backend.contains(key)

    @copy_ancestor_docstring
    def delete(self, key, force=False):
        return self.backend.delete(key, force)

    @copy_ancestor_docstring
    def delete_multi(self, keys, force=False):
        return self.backend.delete_multi(keys, force=force)
    
    @copy_ancestor_docstring
    def list(self, prefix=''):
        return self.backend.list(prefix)

    @copy_ancestor_docstring
    def copy(self, src, dest):
        return self.backend.copy(src, dest)

    @copy_ancestor_docstring
    def rename(self, src, dest):
        return self.backend.rename(src, dest)


class AbstractInputFilter(object, metaclass=ABCMeta):
    '''Process data while reading'''

    def __init__(self):
        super().__init__()
        self.buffer = b''

    def read(self, size=None):
        '''Try to read *size* bytes
        
        If *None*, read until EOF.
        '''

        if size is None:
            remaining = 1 << 31
        else:
            remaining = size - len(self.buffer)

        while remaining > 0:
            buf = self._read(BUFSIZE)
            if not buf:
                break
            remaining -= len(buf)
            self.buffer += buf

        if size is None:
            buf = self.buffer
            self.buffer = b''
        else:
            buf = self.buffer[:size]
            self.buffer = self.buffer[size:]

        return buf

    @abstractmethod
    def _read(self, size):
        '''Read roughly *size* bytes'''
        pass

class CompressFilter(object):
    '''Compress data while writing'''

    def __init__(self, fh, compr):
        '''Initialize
        
        *fh* should be a file-like object. *decomp* should be a fresh compressor
        instance with a *compress* method.
        '''
        super().__init__()

        self.fh = fh
        self.compr = compr
        self.obj_size = 0
        self.closed = False

    def write(self, data):
        '''Write *data*'''

        buf = self.compr.compress(data)
        if buf:
            self.fh.write(buf)
            self.obj_size += len(buf)

    def close(self):
        buf = self.compr.flush()
        self.fh.write(buf)
        self.obj_size += len(buf)
        self.fh.close()
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False

    def get_obj_size(self):
        if not self.closed:
            raise RuntimeError('Object must be closed first.')
        return self.obj_size


class DecompressFilter(AbstractInputFilter):
    '''Decompress data while reading'''

    def __init__(self, fh, decomp, metadata=None):
        '''Initialize
        
        *fh* should be a file-like object. *decomp* should be a
        fresh decompressor instance with a *decompress* method.
        '''
        super().__init__()

        self.fh = fh
        self.decomp = decomp
        self.metadata = metadata

    def _read(self, size):
        '''Read roughly *size* bytes'''

        buf = b''
        while not buf:
            buf = self.fh.read(size)
            if not buf:
                if self.decomp.unused_data:
                    raise ChecksumError('Data after end of compressed stream')
                return b''

            try:
                buf = self.decomp.decompress(buf)
            except IOError as exc:
                if exc.args == ('invalid data stream',):
                    raise ChecksumError('Invalid compressed stream') from None
                raise
            except lzma.LZMAError as exc:
                if exc.args == ('Corrupt input data',):
                    raise ChecksumError('Invalid compressed stream') from None
                raise
            except zlib.error as exc:
                if exc.args[0].startswith('Error -3 while decompressing:'):
                    log.warning('LegacyDecryptDecompressFilter._read(): %s',
                             exc.args[0])
                    raise ChecksumError('Invalid compressed stream') from None
                raise

        return buf

    def close(self):
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False

class EncryptFilter(object):
    '''Encrypt data while writing'''

    def __init__(self, fh, passphrase, nonce):
        '''Initialize
        
        *fh* should be a file-like object.
        '''
        super().__init__()

        self.fh = fh
        self.obj_size = 0
        self.closed = False

        self.key = sha256(passphrase + nonce)
        self.cipher = aes_cipher(self.key)
        self.hmac = hmac.new(self.key, digestmod=hashlib.sha256)

        self.fh.write(struct.pack(b'<B', len(nonce)))
        self.fh.write(nonce)

        self.obj_size += len(nonce) + 1

    def write(self, data):
        '''Write *data*
        
        len(data) must be < 2**32.
    
        Every invocation of `write` generates a packet that contains both the
        length of the data and the data, so the passed data should have
        reasonable size (if the data is written in e.g. 4 byte chunks, it is
        blown up by 100%)
        '''

        if len(data) == 0:
            return

        buf = struct.pack(b'<I', len(data)) + data
        self.hmac.update(buf)
        buf = self.cipher.encrypt(buf)
        if buf:
            self.fh.write(buf)
            self.obj_size += len(buf)

    def close(self):
        # Packet length of 0 indicates end of stream, only HMAC follows
        buf = struct.pack(b'<I', 0)
        self.hmac.update(buf)
        buf = self.cipher.encrypt(buf + self.hmac.digest())
        self.fh.write(buf)
        self.obj_size += len(buf)
        self.fh.close()
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False

    def get_obj_size(self):
        if not self.closed:
            raise RuntimeError('Object must be closed first.')
        return self.obj_size


class DecryptFilter(AbstractInputFilter):
    '''Decrypt data while reading
    
    Reader has to read the entire stream in order for HMAC
    checking to work.
    '''

    def __init__(self, fh, passphrase, metadata=None):
        '''Initialize
        
        *fh* should be a file-like object.
        '''
        super().__init__()

        self.fh = fh
        self.off_size = struct.calcsize(b'<I')
        self.remaining = 0 # Remaining length of current packet
        self.metadata = metadata
        self.hmac_checked = False

        # Read nonce
        len_ = struct.unpack(b'<B', fh.read(struct.calcsize(b'<B')))[0]
        nonce = fh.read(len_)

        key = sha256(passphrase + nonce)
        self.cipher = aes_cipher(key)
        self.hmac = hmac.new(key, digestmod=hashlib.sha256)

    def _read(self, size):
        '''Read roughly *size* bytes'''

        buf = self.fh.read(size)
        if not buf:
            if not self.hmac_checked:
                raise ChecksumError('HMAC mismatch')
            return b''

        inbuf = self.cipher.decrypt(buf)
        outbuf = b''
        while True:

            if len(inbuf) <= self.remaining:
                self.remaining -= len(inbuf)
                self.hmac.update(inbuf)
                outbuf += inbuf
                break
            elif len(inbuf) <= self.remaining + self.off_size:
                inbuf += self.fh.read(self.off_size)
                continue
                
            outbuf += inbuf[:self.remaining]
            self.hmac.update(inbuf[:self.remaining + self.off_size])
            paket_size = struct.unpack(b'<I', inbuf[self.remaining
                                                    :self.remaining + self.off_size])[0]
            inbuf = inbuf[self.remaining + self.off_size:]
            self.remaining = paket_size

            # End of file, read and check HMAC
            if paket_size == 0:
                if len(inbuf) != HMAC_SIZE:
                    inbuf += self.cipher.decrypt(self.fh.read(HMAC_SIZE - len(inbuf)))
                if not hmac.compare_digest(inbuf, self.hmac.digest()):
                    raise ChecksumError('HMAC mismatch')
                self.hmac_checked = True
                break

        return outbuf

    def close(self):
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False

class LegacyDecryptDecompressFilter(AbstractInputFilter):
    '''Decrypt and Decompress data while reading
    
    Reader has to read the entire stream in order for HMAC
    checking to work.
    '''

    def __init__(self, fh, passphrase, decomp, metadata=None):
        '''Initialize
        
        *fh* should be a file-like object.
        '''
        super().__init__()

        self.fh = fh
        self.metadata = metadata
        self.decomp = decomp
        self.hmac_checked = False

        # Read nonce
        len_ = struct.unpack(b'<B', fh.read(struct.calcsize(b'<B')))[0]
        nonce = fh.read(len_)
        self.hash = fh.read(HMAC_SIZE)

        key = sha256(passphrase + nonce)
        self.cipher = aes_cipher(key)
        self.hmac = hmac.new(key, digestmod=hashlib.sha256)

    def _read(self, size):
        '''Read roughly *size* bytes'''

        buf = None
        while not buf:
            buf = self.fh.read(size)
            if not buf and not self.hmac_checked:
                if not hmac.compare_digest(self.cipher.decrypt(self.hash), 
                                           self.hmac.digest()):
                    raise ChecksumError('HMAC mismatch')
                elif self.decomp and self.decomp.unused_data:
                    raise ChecksumError('Data after end of compressed stream')
                else:
                    self.hmac_checked = True
                    return b''
            elif not buf:
                return b''

            buf = self.cipher.decrypt(buf)
            if not self.decomp:
                break

            try:
                buf = self.decomp.decompress(buf)
            except IOError as exc:
                if exc.args == ('invalid data stream',):
                    raise ChecksumError('Invalid compressed stream') from None
                raise
            except lzma.LZMAError as exc:
                if exc.args == ('Corrupt input data',):
                    raise ChecksumError('Invalid compressed stream') from None
                raise
            except zlib.error as exc:
                if exc.args[0].startswith('Error -3 while decompressing:'):
                    log.warning('LegacyDecryptDecompressFilter._read(): %s',
                             exc.args[0])
                    raise ChecksumError('Invalid compressed stream') from None
                raise

        self.hmac.update(buf)
        return buf

    def close(self):
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False
    
def encrypt(buf, passphrase, nonce):
    '''Encrypt *buf*'''

    key = sha256(passphrase + nonce)
    cipher = aes_cipher(key) 
    hmac_ = hmac.new(key, digestmod=hashlib.sha256)

    hmac_.update(buf)
    buf = cipher.encrypt(buf)
    hash_ = cipher.encrypt(hmac_.digest())

    return b''.join(
                    (struct.pack(b'<B', len(nonce)),
                    nonce, hash_, buf))

def decrypt(buf, passphrase):
    '''Decrypt *buf'''

    fh = BytesIO(buf)

    len_ = struct.unpack(b'<B', fh.read(struct.calcsize(b'<B')))[0]
    nonce = fh.read(len_)

    key = sha256(passphrase + nonce)
    cipher = aes_cipher(key) 
    hmac_ = hmac.new(key, digestmod=hashlib.sha256)

    # Read (encrypted) hmac
    hash_ = fh.read(HMAC_SIZE)

    buf = fh.read()
    buf = cipher.decrypt(buf)
    hmac_.update(buf)

    hash_ = cipher.decrypt(hash_)

    if not hmac.compare_digest(hash_, hmac_.digest()):
        raise ChecksumError('HMAC mismatch')

    return buf

class ObjectNotEncrypted(Exception):
    '''
    Raised by the backend if an object was requested from an encrypted
    backend, but the object was stored without encryption.
    
    We do not want to simply return the uncrypted object, because the
    caller may rely on the objects integrity being cryptographically
    verified.
    '''

    pass


class MalformedObjectError(Exception):
    '''
    Raised by BetterBackend when trying to access an object that
    wasn't stored by BetterBackend, i.e. has no information about
    encryption or compression.
    '''

    pass

class NoSuchObject(Exception):
    '''Raised if the requested object does not exist in the backend'''

    def __init__(self, key):
        super().__init__()
        self.key = key

    def __str__(self):
        return 'Backend does not have anything stored under key %r' % self.key

class DanglingStorageURLError(Exception):
    '''Raised if the backend can't store data at the given location'''

    def __init__(self, loc):
        super().__init__()
        self.loc = loc

    def __str__(self):
        return '%r does not exist' % self.loc


class AuthorizationError(Exception):
    '''Raised if the credentials don't give access to the requested backend'''

    def __init__(self, msg):
        super().__init__()
        self.msg = msg

    def __str__(self):
        return 'Access denied. Server said: %s' % self.msg

class AuthenticationError(Exception):
    '''Raised if the credentials are invalid'''

    def __init__(self, msg):
        super().__init__()
        self.msg = msg

    def __str__(self):
        return 'Access denied. Server said: %s' % self.msg
   
def aes_cipher(key):
    '''Return AES cipher in CTR mode for *key*'''
    
    return AES.new(key, AES.MODE_CTR, 
                   counter=Counter.new(128, initial_value=0)) 
        
def convert_legacy_metadata(meta):

    # For legacy format, meta is always a dict
    if not isinstance(meta, dict):
        return
    
    if ('encryption' in meta and
        'compression' in meta):
        return

    if 'encrypted' not in meta:
        meta['encryption'] = 'None'
        meta['compression'] = 'None'
        return

    s = meta.pop('encrypted')

    if s == 'True':
        meta['encryption'] = 'AES'
        meta['compression'] = 'BZIP2'

    elif s == 'False':
        meta['encryption'] = 'None'
        meta['compression'] = 'None'

    elif s.startswith('AES/'):
        meta['encryption'] = 'AES'
        meta['compression'] = s[4:]

    elif s.startswith('PLAIN/'):
        meta['encryption'] = 'None'
        meta['compression'] = s[6:]
    else:
        raise RuntimeError('Unsupported encryption')

    if meta['compression'] == 'BZ2':
        meta['compression'] = 'BZIP2'

    if meta['compression'] == 'NONE':
        meta['compression'] = 'None'


def get_backend(options, plain=False):
    '''Return backend for given storage-url
    
    If *plain* is true, don't attempt to unlock and don't wrap into
    BetterBackend.
    '''

    return get_backend_factory(options, plain)()

def get_backend_factory(options, plain=False):
    '''Return factory producing backend objects for given storage-url
    
    If *plain* is true, don't attempt to unlock and don't wrap into
    BetterBackend.    
    '''

    hit = re.match(r'^([a-zA-Z0-9]+)://', options.storage_url)
    if not hit:
        raise QuietError('Unknown storage url: %s' % options.storage_url)

    backend_name = 's3ql.backends.%s' % hit.group(1)
    try:
        __import__(backend_name)
    except ImportError:
        raise QuietError('No such backend: %s' % hit.group(1)) from None

    backend_class = getattr(sys.modules[backend_name], 'Backend')

    # Read authfile
    config = configparser.ConfigParser()
    if os.path.isfile(options.authfile):
        mode = os.stat(options.authfile).st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            raise QuietError("%s has insecure permissions, aborting." % options.authfile)
        config.read(options.authfile)

    backend_login = None
    backend_pw = None
    backend_passphrase = None
    for section in config.sections():
        def getopt(name):
            try:
                return config.get(section, name)
            except configparser.NoOptionError:
                return None

        pattern = getopt('storage-url')

        if not pattern or not options.storage_url.startswith(pattern):
            continue

        backend_login = backend_login or getopt('backend-login')
        backend_pw = backend_pw or getopt('backend-password')
        backend_passphrase = backend_passphrase or getopt('fs-passphrase')
        if backend_passphrase is None and getopt('bucket-passphrase') is not None:
            backend_passphrase = getopt('bucket-passphrase')
            log.warning("Warning: the 'bucket-passphrase' configuration option has been "
                     "renamed to 'fs-passphrase'! Please update your authinfo file.")

    if not backend_login and backend_class.needs_login:
        if sys.stdin.isatty():
            backend_login = getpass("Enter backend login: ")
        else:
            backend_login = sys.stdin.readline().rstrip()

    if not backend_pw and backend_class.needs_login:
        if sys.stdin.isatty():
            backend_pw = getpass("Enter backend password: ")
        else:
            backend_pw = sys.stdin.readline().rstrip()

    ssl_context = get_ssl_context(options)
    try:
        backend = backend_class(options.storage_url, backend_login, backend_pw,
                                ssl_context)
        
        # Do not use backend.lookup(), this would use a HEAD request and
        # not provide any useful error messages if something goes wrong
        # (e.g. wrong credentials)
        backend.fetch('s3ql_passphrase')
        
    except DanglingStorageURLError as exc:
        raise QuietError(str(exc)) from None
    
    except AuthorizationError:
        raise QuietError('No permission to access backend.') from None

    except AuthenticationError:
        raise QuietError('Invalid credentials or skewed system clock.') from None
        
    except NoSuchObject:
        encrypted = False
        
    else:
        encrypted = True

    if plain:
        return lambda: backend_class(options.storage_url, backend_login, backend_pw,
                                     ssl_context)
            
    if encrypted and not backend_passphrase:
        if sys.stdin.isatty():
            backend_passphrase = getpass("Enter file system encryption passphrase: ")
        else:
            backend_passphrase = sys.stdin.readline().rstrip()
        backend_passphrase = backend_passphrase.encode('utf-8')
    elif not encrypted:
        backend_passphrase = None

    if hasattr(options, 'compress'):
        compress = options.compress
    else:
        compress = None

    if not encrypted:
        return lambda: BetterBackend(None, compress,
                                    backend_class(options.storage_url, backend_login, backend_pw,
                                                  ssl_context))

    tmp_backend = BetterBackend(backend_passphrase, compress, backend)

    try:
        data_pw = tmp_backend['s3ql_passphrase']
    except ChecksumError:
        raise QuietError('Wrong backend passphrase') from None

    return lambda: BetterBackend(data_pw, compress,
                                 backend_class(options.storage_url, backend_login, backend_pw,
                                               ssl_context))
