#!/usr/bin/env python3
'''
t1_backends.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright © 2008 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

if __name__ == '__main__':
    import pytest
    import sys
    sys.exit(pytest.main([__file__] + sys.argv[1:]))

from s3ql.logging import logging
import mock_server
from dugong import ConnectionClosed
from s3ql import backends, BUFSIZE
from s3ql.backends.local import Backend as LocalBackend
from s3ql.backends import s3c
from s3ql.backends.gs import Backend as GSBackend
from s3ql.backends.common import (NoSuchObject, AuthenticationError, AuthorizationError,
                                  DanglingStorageURLError, CorruptedObjectError)
from s3ql.backends.comprenc import ComprencBackend, ObjectNotEncrypted
from s3ql.backends.s3c import BadDigestError, OperationAbortedError, HTTPError, S3Error
from contextlib import ExitStack
from common import get_remote_test_info, NoTestSection, catch_logmsg, CLOCK_GRANULARITY
import s3ql.backends.common
import tempfile
import re
import functools
import time
import pytest
from pytest import raises as assert_raises
import shutil
import struct
import threading

log = logging.getLogger(__name__)
empty_set = set()

class BackendWrapper:

    def __init__(self, name, retry_time=0):
        self.name = name
        self.retry_time = retry_time
        self.backend = self._init()

        self.orig_prefix = self.backend.prefix
        self.prefix_counter = 0

    def _init(self):
        '''Return backend instance'''
        pass

    def cleanup(self):
        '''Cleanup backend'''
        self.backend.close()

    def reset(self):
        '''Prepare backend for reuse'''

        # "clear" the backend by selecting a different prefix for every
        # test (actually deleting all objects would mean that we have to
        # wait for propagation delays)
        self.backend.prefix = '%s%3d/' % (self.orig_prefix,
                                          self.prefix_counter)
        self.prefix_counter += 1

    def __str__(self):
        return self.name

class LocalBackendWrapper(BackendWrapper):

    def __init__(self):
        super().__init__('local')

    def _init(self):
        self.backend_dir = tempfile.mkdtemp(prefix='s3ql-backend-')
        return backends.local.Backend('local://' + self.backend_dir, None, None)

    def cleanup(self):
        super().cleanup()
        shutil.rmtree(self.backend_dir)

class MockBackendWrapper(BackendWrapper):
    def __init__(self, request_handler, storage_url):
        backend_name = re.match(r'^([a-zA-Z0-9]+)://', storage_url).group(1)
        self.backend_class = backends.prefix_map[backend_name]
        self.request_handler = request_handler
        self.may_temp_fail = False
        self.storage_url = storage_url
        super().__init__('mock_' + backend_name)

    def _init(self):
        self.server = mock_server.StorageServer(self.request_handler, ('localhost', 0))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        storage_url = self.storage_url % { 'host': self.server.server_address[0],
                                           'port': self.server.server_address[1] }
        backend = self.backend_class(storage_url, 'joe', 'swordfish', { 'no-ssl': True })

        # Enable OAuth when using Google Backend
        if isinstance(backend, GSBackend):
            backend.use_oauth2 = True
            backend.hdr_prefix = 'x-goog-' # Normally set in __init__
            backend.access_token[backend.password] = 'foobar'

        # Mock server should not have temporary failures by default
        is_temp_failure = backend.is_temp_failure
        @functools.wraps(backend.is_temp_failure)
        def wrap(exc):
            if self.may_temp_fail:
                return is_temp_failure(exc)
            else:
                return False
        backend.is_temp_failure = wrap
        return backend

    def cleanup(self):
        super().cleanup()
        self.server.server_close()
        self.server.shutdown()


class RemoteBackendWrapper(BackendWrapper):

    def __init__(self, backend_name, backend_class):
        self.class_ = backend_class
        super().__init__(backend_name, retry_time=600)

    def _init(self):
        # May raise NoTestSection
        (login, password, storage_url) = get_remote_test_info(self.name + '-test')

        backend = self.class_(storage_url, login, password, {})
        try:
            backend.fetch('empty_object')
        except DanglingStorageURLError:
            raise SystemExit('%s does not exist' % storage_url)
        except AuthorizationError:
            raise SystemExit('No permission to access %s' % storage_url)
        except AuthenticationError:
            raise SystemExit('Unable to access %s, invalid credentials' % storage_url)
        except NoSuchObject:
            pass
        else:
            raise SystemExit('%s not empty' % storage_url)
        return backend

    def cleanup(self):
        self.backend.clear()
        self.backend.close()

_backend_wrappers = []
# It'd be nice if we could use the setup_module hook instead, but
# unfortunately that gets executed *after* pytest_generate_tests.
def _init_wrappers():
    '''Get list of *BackendWrapper* instances for all available backends'''

    # Local backend
    _backend_wrappers.append(LocalBackendWrapper())

    # Backends talking to local mock servers
    for (request_handler, storage_url) in mock_server.handler_list:
        _backend_wrappers.append(MockBackendWrapper(request_handler, storage_url))

    # Backends talking to actual remote servers (if available)
    for (backend_name, backend_class) in backends.prefix_map.items():
         if backend_name == 'local': # local backend has already been handled
             continue

         try:
             bw = RemoteBackendWrapper(backend_name, backend_class)
         except NoTestSection as exc:
             log.info('Not doing remote tests for %s backend: %s',
                      backend_name, exc.reason)
             continue
         _backend_wrappers.append(bw)

def teardown_module(self):
    '''Clean-up all backend wrappers'''

    # Use ExitStack to ensure that all wrappers are cleaned up,
    # even if a cleanup raises an exception
    with ExitStack() as stack:
        for w in _backend_wrappers:
            stack.callback(w.cleanup)

@pytest.fixture()
def backend_wrapper(request):
    bw = request.param
    request.addfinalizer(bw.reset)
    return request.param

@pytest.fixture()
def retry_time(backend_wrapper):
    return backend_wrapper.retry_time

def pytest_generate_tests(metafunc):
    if not _backend_wrappers:
        _init_wrappers()

    if 'backend' in metafunc.fixturenames:
        assert 'compenc_kind' in metafunc.fixturenames
        assert 'backend_wrapper' in metafunc.fixturenames

        if getattr(metafunc.function, 'require_encryption', False):
            compenc_kind = ('aes+lzma', 'aes')
        elif getattr(metafunc.function, 'require_compenc', False):
            compenc_kind = ('aes', 'aes+lzma', 'lzma', 'zlib', 'bzip2')
        else:
            compenc_kind = ('plain', 'aes', 'aes+lzma', 'lzma', 'zlib', 'bzip2')
        metafunc.parametrize("compenc_kind", compenc_kind)

        if hasattr(metafunc.function, 'wrapper_filter'):
            wrappers = [ x for x in _backend_wrappers
                         if metafunc.function.wrapper_filter(x) ]
        else:
            wrappers = _backend_wrappers

        # Needs to be function scope, otherwise we cannot assign
        # different parametrizations to different test functions
        # (cf. https://bitbucket.org/hpk42/pytest/issue/531/)
        metafunc.parametrize("backend_wrapper", wrappers, indirect=True,
                             ids=[ str(w) for w in wrappers ])

@pytest.fixture()
def backend(compenc_kind, backend_wrapper):
    plain_backend = backend_wrapper.backend
    if compenc_kind == 'plain':
        return plain_backend
    elif compenc_kind == 'aes+lzma':
        return ComprencBackend(b'schlurz', ('lzma', 6), plain_backend)
    elif compenc_kind == 'aes':
        return ComprencBackend(b'schlurz', (None, 6), plain_backend)
    else:
        return ComprencBackend(None, (compenc_kind, 6), plain_backend)

def require_plain_backend(class_):
    '''Require plain backend of type *class*_

    Returns a decorator that marks the test function for being
    called only with plain backends of the specific class.
    '''

    def decorator(test_fn):
        assert not hasattr(test_fn, 'wrapper_filter')
        test_fn.wrapper_filter = lambda x: isinstance(x.backend, class_)
        return test_fn
    return decorator

def require_backend_wrapper(class_):
    '''Require backend wrapper of type *class*_

    Returns a decorator that marks the test function for being
    called only with backends whose wrappers are instances
    of *class_*.
    '''

    def decorator(test_fn):
        assert not hasattr(test_fn, 'wrapper_filter')
        test_fn.wrapper_filter = lambda x: isinstance(x, class_)
        return test_fn
    return decorator

def require_wrapper(filter_fn):
    def decorator(test_fn):
        assert not hasattr(test_fn, 'wrapper_filter')
        test_fn.wrapper_filter = filter_fn
        return test_fn
    return decorator

def require_immediate_consistency(test_fn):
    '''Require immediate consistency

    Decorator. Marks the function to be called only with backends
    offering immediate consistency.
    '''

    assert not hasattr(test_fn, 'wrapper_filter')
    test_fn.wrapper_filter = (lambda x: x.retry_time == 0)
    return test_fn

def require_compression_or_encryption(test_fn):
    '''Require compressing or encrypting backend

    Decorator. Marks the function to be called only with backends
    that encrypt or compress (or both) their contents.
    '''

    test_fn.require_compenc = True
    return test_fn

def require_encryption(test_fn):
    '''Require encrypting backend

    Decorator. Marks the function to be called only with backends
    that encrypt their contents.
    '''

    test_fn.require_encryption = True
    return test_fn

def newname(name_counter=[0]):
    '''Return random, unique string'''
    name_counter[0] += 1
    return "s3ql/<tag=%d>/!sp ace_'quote\":_&end\\" % name_counter[0]

def newvalue():
    return newname().encode()

def fetch_object(backend, key, retry_time, sleep_time=1):
    '''Read data and metadata for *key* from *backend*

    If `NoSuchObject` exception is encountered, retry for
    up to *retry_time* seconds.
    '''
    waited=0
    while True:
        try:
            return backend.fetch(key)
        except NoSuchObject:
            if waited >= retry_time:
                raise
        time.sleep(sleep_time)
        waited += sleep_time

def lookup_object(backend, key, retry_time, sleep_time=1):
    '''Read metadata for *key* from *backend*

    If `NoSuchObject` exception is encountered, retry for
    up to *retry_time* seconds.
    '''
    waited=0
    while True:
        try:
            return backend.lookup(key)
        except NoSuchObject:
            if waited >= retry_time:
                raise
        time.sleep(sleep_time)
        waited += sleep_time

def assert_in_index(backend, keys, retry_time, sleep_time=1):
    '''Assert that *keys* will appear in index

    Raises assertion error if *keys* do not show up within
    *retry_time* seconds.
    '''
    waited=0
    keys = set(keys) # copy
    while True:
        index = set(backend.list())
        if not keys - index:
            return
        elif waited >= retry_time:
            assert keys - index == empty_set
        time.sleep(sleep_time)
        waited += sleep_time

def assert_not_in_index(backend, keys, retry_time, sleep_time=1):
    '''Assert that *keys* will disappear from index

    Raises assertion error if *keys* do not disappear within
    *retry_time* seconds.
    '''
    waited=0
    keys = set(keys) # copy
    while True:
        index = set(backend.list())
        if keys - index == keys:
            return
        elif waited >= retry_time:
            assert keys - index == keys
        time.sleep(sleep_time)
        waited += sleep_time

def assert_not_readable(backend, key, retry_time, sleep_time=1):
    '''Assert that *key* does not exist in *backend*

    Asserts that a `NoSuchObject` exception will be raised when trying to read
    the object after at most *retry_time* seconds.
    '''
    waited=0
    while True:
        try:
            backend.fetch(key)
        except NoSuchObject:
            return
        if waited >= retry_time:
            pytest.fail('object %s still present in backend' % key)
        time.sleep(sleep_time)
        waited += sleep_time

def test_read_write(backend, retry_time):
    key = newname()
    value = newvalue()
    metadata = { 'jimmy': 'jups@42' }

    assert key not in backend
    assert_raises(NoSuchObject, backend.lookup, key)
    assert_raises(NoSuchObject, backend.fetch, key)

    def do_write(fh):
        fh.write(value)
    backend.perform_write(do_write, key, metadata)

    assert_in_index(backend, [key], retry_time)
    (value2, metadata2) = fetch_object(backend, key, retry_time)

    assert value == value2
    assert metadata == metadata2
    assert lookup_object(backend, key, retry_time) == metadata

def test_complex_meta(backend, retry_time):
    key = newname()
    value = newvalue()

    metadata = { 'com\nplex: key': 42,
                 'farp_': False, 'non-farp': True,
                 'blu%rz': 23.283475,
                 'görp': b'heelo',
                 'sch.al': 'gorroobalp\nfurrö!',
                 'lo-ng': 'foobarz' * 80 }

    assert key not in backend
    backend.store(key, value, metadata)
    (value2, metadata2) = fetch_object(backend, key, retry_time)

    assert value == value2
    assert metadata == metadata2
    assert lookup_object(backend, key, retry_time) == metadata

def test_list(backend, retry_time):
    keys = ([ 'prefixa' + newname() for dummy in range(6) ]
            + [ 'prefixb' + newname() for dummy in range(6) ])
    values = [ newvalue() for dummy in range(12) ]

    assert set(backend.list()) == empty_set
    for i in range(12):
        backend[keys[i]] = values[i]
    assert_in_index(backend, keys, retry_time)

    assert set(backend.list('prefixa')) == set(keys[:6])
    assert set(backend.list('prefixb')) == set(keys[6:])
    assert set(backend.list('prefixc')) == empty_set

@require_immediate_consistency
def test_readslowly(backend):
    key = newname()
    value = newvalue()
    metadata = { 'jimmy': 'jups@42' }

    backend.store(key, value, metadata)

    s3ql.backends.common.BUFSIZE = 1
    try:
        with backend.open_read(key) as fh:
            # Force slow reading from underlying layer
            if hasattr(fh, 'fh'):
                def read_slowly(size, *, real_read=fh.fh.read):
                    return real_read(1)
                fh.fh.read = read_slowly

            buf = []
            while True:
                buf.append(fh.read(1))
                if not buf[-1]:
                    break
            value2 = b''.join(buf)
            metadata2 =  fh.metadata
    finally:
        s3ql.backends.common.BUFSIZE = BUFSIZE

    assert value == value2
    assert metadata == metadata2

def test_delete(backend, retry_time):
    key = newname()
    value = newvalue()

    backend[key] = value

    # Wait for object to become visible
    assert_in_index(backend, [key], retry_time)
    fetch_object(backend, key, retry_time)

    # Delete it
    del backend[key]

    # Make sure that it's truly gone
    assert_not_in_index(backend, [key], retry_time)
    assert_not_readable(backend, key, retry_time)

def test_delete_multi(backend, retry_time):
    keys = [ newname() for _ in range(30) ]
    value = newvalue()

    # Create objects
    for key in keys:
        backend[key] = value

    # Wait for them
    assert_in_index(backend, keys, retry_time)
    for key in keys:
        fetch_object(backend, key, retry_time)

    # Delete half of them
    # We don't use force=True but catch the exemption to increase the
    # chance that some existing objects are not deleted because of the
    # error.
    to_delete = keys[::2]
    to_delete.insert(7, 'not_existing')
    try:
        backend.delete_multi(to_delete)
    except NoSuchObject:
        pass

    # Without full consistency, deleting an non-existing object
    # may not give an error
    assert retry_time or len(to_delete) > 0

    deleted = set(keys[::2]) - set(to_delete)
    assert len(deleted) > 0
    remaining = set(keys) - deleted

    assert_not_in_index(backend, deleted, retry_time)
    for key in deleted:
        assert_not_readable(backend, key, retry_time)

    assert_in_index(backend, remaining, retry_time)
    for key in remaining:
        fetch_object(backend, key, retry_time)

def test_clear(backend, retry_time):
    keys = [ newname() for _ in range(5) ]
    value = newvalue()

    # Create objects
    for key in keys:
        backend[key] = value

    # Wait for them
    assert_in_index(backend, keys, retry_time)
    for key in keys:
        fetch_object(backend, key, retry_time)

    # Delete everything
    backend.clear()

    assert_not_in_index(backend, keys, retry_time)
    for key in keys:
        assert_not_readable(backend, key, retry_time)

def test_copy(backend, retry_time):
    key1 = newname()
    key2 = newname()
    value = newvalue()
    metadata = { 'jimmy': 'jups@42' }

    backend.store(key1, value, metadata)

    # Wait for object to become visible
    assert_in_index(backend, [key1], retry_time)
    fetch_object(backend, key1, retry_time)

    assert_not_in_index(backend, [key2], retry_time)
    assert_not_readable(backend, key2, retry_time)

    backend.copy(key1, key2)

    assert_in_index(backend, [key2], retry_time)
    (value2, metadata2) = fetch_object(backend, key2, retry_time)

    assert value == value2
    assert metadata == metadata2

def test_copy_newmeta(backend, retry_time):
    key1 = newname()
    key2 = newname()
    value = newvalue()
    meta1 = { 'jimmy': 'jups@42' }
    meta2 = { 'jiy': 'jfobauske42' }

    backend.store(key1, value, meta1)

    # Wait for object to become visible
    assert_in_index(backend, [key1], retry_time)
    fetch_object(backend, key1, retry_time)

    assert_not_in_index(backend, [key2], retry_time)
    assert_not_readable(backend, key2, retry_time)

    backend.copy(key1, key2, meta2)

    assert_in_index(backend, [key2], retry_time)
    (value2, meta) = fetch_object(backend, key2, retry_time)

    assert value == value2
    assert meta == meta2

def test_rename(backend, retry_time):
    key1 = newname()
    key2 = newname()
    value = newvalue()
    metadata = { 'jimmy': 'jups@42' }

    backend.store(key1, value, metadata)

    # Wait for object to become visible
    assert_in_index(backend, [key1], retry_time)
    fetch_object(backend, key1, retry_time)

    assert_not_in_index(backend, [key2], retry_time)
    assert_not_readable(backend, key2, retry_time)

    backend.rename(key1, key2)

    assert_in_index(backend, [key2], retry_time)
    (value2, metadata2) = fetch_object(backend, key2, retry_time)

    assert value == value2
    assert metadata == metadata2

    assert_not_in_index(backend, [key1], retry_time)
    assert_not_readable(backend, key1, retry_time)

def test_rename_newmeta(backend, retry_time):
    key1 = newname()
    key2 = newname()
    value = newvalue()
    meta1 = { 'jimmy': 'jups@42' }
    meta2 = { 'apple': 'potatoes' }

    backend.store(key1, value, meta1)

    # Wait for object to become visible
    assert_in_index(backend, [key1], retry_time)
    fetch_object(backend, key1, retry_time)

    assert_not_in_index(backend, [key2], retry_time)
    assert_not_readable(backend, key2, retry_time)

    backend.rename(key1, key2, meta2)

    assert_in_index(backend, [key2], retry_time)
    (value2, meta) = fetch_object(backend, key2, retry_time)

    assert value == value2
    assert meta == meta2

def test_update_meta(backend, retry_time):
    key = 'simple'
    value = b'not too hard'
    meta1 = { 'jimmy': 'jups@42' }
    meta2 = { 'apple': 'potatoes' }

    backend.store(key, value, meta1)

    # Wait for object to become visible
    assert_in_index(backend, [key], retry_time)
    fetch_object(backend, key, retry_time)

    backend.update_meta(key, meta2)

    # Wait for updated metadata
    waited=0
    sleep_time = 1
    while True:
        (value2, meta) = fetch_object(backend, key, retry_time)
        if meta != meta1:
            break
        elif waited >= retry_time:
            pytest.fail('metadata for %s not updated after %d seconds'
                        % (key, waited))
        time.sleep(sleep_time)
        waited += sleep_time

    assert value == value2
    assert meta == meta2

# Google storage does not return errors after 200 ok
@require_wrapper(lambda x: isinstance(x, MockBackendWrapper)
                 and isinstance(x.backend, s3c.Backend)
                 and not isinstance(x.backend, GSBackend))
def test_copy_error(backend, backend_wrapper, monkeypatch):
    value = b'hello there, let us see whats going on'
    key1 = 'object-key1'
    key2 = 'object-key2'
    backend[key1] = value

    # Monkeypatch request handler to produce error
    handler_class = backend_wrapper.server.RequestHandlerClass
    def do_PUT(self, real_PUT=handler_class.do_PUT, count=[0]):
        count[0] += 1
        if count[0] > 3:
            return real_PUT(self)
        else:
            self.send_error(200, code='OperationAborted')
    monkeypatch.setattr(handler_class, 'do_PUT', do_PUT)
    assert_raises(OperationAbortedError, backend.copy, key1, key2)

    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    backend.copy(key1, key2)

@require_compression_or_encryption
@require_immediate_consistency
def test_corruption(backend, retry_time):
    plain_backend = backend.backend

    # Create compressed object
    key = newname()
    value = newvalue()
    backend[key] = value

    # Retrieve compressed data
    (compr_value, meta) = fetch_object(plain_backend, key, retry_time)
    compr_value = bytearray(compr_value)

    # Overwrite with corrupted data
    # (this needs immediate consistency)
    compr_value[-3:] = b'000'
    plain_backend.store(key, compr_value, meta)

    with pytest.raises(CorruptedObjectError) as exc:
        fetch_object(backend, key, retry_time)

    if backend.passphrase is None: # compression only
        assert exc.value.str == 'Invalid compressed stream'
    else:
        assert exc.value.str == 'HMAC mismatch'

@require_compression_or_encryption
@require_immediate_consistency
def test_extra_data(backend, retry_time):
    plain_backend = backend.backend

    # Create compressed object
    key = newname()
    value = newvalue()
    backend[key] = value

    # Retrieve compressed data
    (compr_value, meta) = fetch_object(plain_backend, key, retry_time)
    compr_value = bytearray(compr_value)

    # Overwrite with extended data
    # (this needs immediate consistency)
    compr_value += b'000'
    plain_backend.store(key, compr_value, meta)

    with pytest.raises(CorruptedObjectError) as exc:
        fetch_object(backend, key, retry_time)

    if backend.passphrase is None: # compression only
        assert exc.value.str == 'Data after end of compressed stream'
    else:
        assert exc.value.str == 'Extraneous data at end of object'

def test_multi_packet(backend):
    '''Write and read packet extending over multiple chunks'''
    key = newname()

    def do_write(fh):
        for i in range(5):
            fh.write(b'\xFF' * BUFSIZE)
    backend.perform_write(do_write, key)

    def do_read(fh):
        buf = bytearray()
        while True:
            tmp = fh.read(BUFSIZE//2)
            if not tmp:
                break
            buf += tmp
        return buf
    res = backend.perform_read(do_read, key)
    assert res == b'\xFF' * (5*BUFSIZE)

# No short reads
@require_plain_backend(LocalBackend)
@require_compression_or_encryption
def test_issue431(backend):
    key = newname()
    hdr_len = struct.calcsize(b'<I')

    def do_write(fh):
        fh.write(b'\xFF' * 50)
        fh.write(b'\xFF' * 50)
    backend.perform_write(do_write, key)

    def do_read(fh):
        fh.read(50 + 2*hdr_len)
        fh.read(50)
        assert fh.read(50) == b''
    backend.perform_read(do_read, key)

@require_immediate_consistency
@require_encryption
def test_encryption(backend):
    plain_backend = backend.backend

    plain_backend['plain'] = b'foobar452'
    backend.store('encrypted', b'testdata', { 'tag': True })

    assert plain_backend['encrypted'] != b'testdata'
    assert_raises(CorruptedObjectError, backend.fetch, 'plain')
    assert_raises(CorruptedObjectError, backend.lookup, 'plain')

    backend.passphrase = None
    backend.store('not-encrypted', b'testdata2395', { 'tag': False })
    assert_raises(CorruptedObjectError, backend.fetch, 'encrypted')
    assert_raises(CorruptedObjectError, backend.lookup, 'encrypted')

    backend.passphrase = b'jobzrul'
    assert_raises(CorruptedObjectError, backend.fetch, 'encrypted')
    assert_raises(CorruptedObjectError, backend.lookup, 'encrypted')
    assert_raises(ObjectNotEncrypted, backend.fetch, 'not-encrypted')
    assert_raises(ObjectNotEncrypted, backend.lookup, 'not-encrypted')

@require_encryption
def test_replay(backend, retry_time):
    plain_backend = backend.backend

    # Create encrypted object
    key1 = newname()
    key2 = newname()
    value = newvalue()
    backend[key1] = value

    # Retrieve compressed data
    (compr_value, meta) = fetch_object(plain_backend, key1, retry_time)
    compr_value = bytearray(compr_value)

    # Copy into new object
    plain_backend.store(key2, compr_value, meta)

    assert_raises(CorruptedObjectError, fetch_object, backend, key2, retry_time)

@require_backend_wrapper(MockBackendWrapper)
def test_corrupted_get(backend, backend_wrapper, monkeypatch):
    key = 'brafasel'
    value = b'hello there, let us see whats going on'
    backend[key] = value

    # Monkeypatch request handler to produce invalid etag
    handler_class = backend_wrapper.server.RequestHandlerClass
    def send_header(self, keyword ,value, count=[0],
                    send_header_real=handler_class.send_header):
        if keyword == 'ETag':
            count[0] += 1
            if count[0] <= 3:
                value = value[::-1]
        return send_header_real(self, keyword, value)
    monkeypatch.setattr(handler_class, 'send_header', send_header)

    with catch_logmsg('^MD5 mismatch for', count=1, level=logging.WARNING):
        assert_raises(BadDigestError, backend.fetch, key)

    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    with catch_logmsg('^MD5 mismatch for', count=2, level=logging.WARNING):
        assert backend[key] == value

@require_backend_wrapper(MockBackendWrapper)
def test_corrupted_meta(backend, backend_wrapper, monkeypatch):
    key = 'brafasel'
    value = b'hello there, let us see whats going on'
    backend[key] = value

    # Monkeypatch request handler to mess up metadata
    handler_class = backend_wrapper.server.RequestHandlerClass
    def send_header(self, keyword ,value, count=[0],
                    send_header_real=handler_class.send_header):
        if keyword == self.hdr_prefix + 'Meta-md5':
            count[0] += 1
            if count[0] <= 3:
                value = value[::-1]
        return send_header_real(self, keyword, value)
    monkeypatch.setattr(handler_class, 'send_header', send_header)

    with catch_logmsg('^MD5 mismatch in metadata for', count=1, level=logging.WARNING):
        assert_raises(BadDigestError, backend.fetch, key)

    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    with catch_logmsg('^MD5 mismatch in metadata for', count=2, level=logging.WARNING):
        assert backend[key] == value

@require_backend_wrapper(MockBackendWrapper)
def test_corrupted_put(backend, backend_wrapper, monkeypatch):
    key = 'brafasel'
    value = b'hello there, let us see whats going on'

    # Monkeypatch request handler to produce invalid etag
    handler_class = backend_wrapper.server.RequestHandlerClass
    def send_header(self, keyword ,value, count=[0],
                    send_header_real=handler_class.send_header):
        if keyword == 'ETag':
            count[0] += 1
            if count[0] < 3:
                value = value[::-1]
        return send_header_real(self, keyword, value)
    monkeypatch.setattr(handler_class, 'send_header', send_header)

    fh = backend.open_write(key)
    fh.write(value)
    assert_raises(BadDigestError, fh.close)

    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    fh.close()

    assert backend[key] == value

@require_backend_wrapper(MockBackendWrapper)
def test_get_s3error(backend, backend_wrapper, monkeypatch):
    value = b'hello there, let us see whats going on'
    key = 'quote'
    backend[key] = value

    # Monkeypatch request handler to produce 3 errors
    handler_class = backend_wrapper.server.RequestHandlerClass
    def do_GET(self, real_GET=handler_class.do_GET, count=[0]):
        count[0] += 1
        if count[0] > 3:
            return real_GET(self)
        else:
            self.send_error(503, code='OperationAborted')
    monkeypatch.setattr(handler_class, 'do_GET', do_GET)
    assert_raises(OperationAbortedError, backend.fetch, value)

    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    assert backend[key] == value

@require_backend_wrapper(MockBackendWrapper)
def test_head_s3error(backend, backend_wrapper, monkeypatch):
    value = b'hello there, let us see whats going on'
    key = 'quote'
    meta = {'bar': 42, 'foo': 42**2}
    backend.store(key, value, metadata=meta)

    # Monkeypatch request handler to produce 3 errors
    handler_class = backend_wrapper.server.RequestHandlerClass
    def do_HEAD(self, real_HEAD=handler_class.do_HEAD, count=[0]):
        count[0] += 1
        if count[0] > 3:
            return real_HEAD(self)
        else:
            self.send_error(503, code='OperationAborted')
    monkeypatch.setattr(handler_class, 'do_HEAD', do_HEAD)
    with pytest.raises(HTTPError) as exc:
        backend.lookup(key)
    assert exc.value.status == 503

    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    assert backend.lookup(key) == meta

@require_backend_wrapper(MockBackendWrapper)
def test_delete_s3error(backend, backend_wrapper, monkeypatch):
    value = b'hello there, let us see whats going on'
    key = 'quote'
    backend[key] = value

    # Monkeypatch request handler to produce 3 errors
    handler_class = backend_wrapper.server.RequestHandlerClass
    def do_DELETE(self, real_DELETE=handler_class.do_DELETE, count=[0]):
        count[0] += 1
        if count[0] > 3:
            return real_DELETE(self)
        else:
            self.send_error(503, code='OperationAborted')
    monkeypatch.setattr(handler_class, 'do_DELETE', do_DELETE)
    assert_raises(OperationAbortedError, backend.delete, key)

    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    backend.delete(key)

@require_backend_wrapper(MockBackendWrapper)
def test_backoff(backend, backend_wrapper, monkeypatch):
    value = b'hello there, let us see whats going on'
    key = 'quote'
    backend[key] = value

    # Monkeypatch request handler
    handler_class = backend_wrapper.server.RequestHandlerClass
    timestamps = []
    def do_DELETE(self, real_DELETE=handler_class.do_DELETE):
        timestamps.append(time.time())
        if len(timestamps) < 3:
            self.send_error(503, code='SlowDown',
                            extra_headers={'Retry-After': '1'})
        else:
            return real_DELETE(self)

    monkeypatch.setattr(handler_class, 'do_DELETE', do_DELETE)
    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    backend.delete(key)

    assert timestamps[1] - timestamps[0] > 1 - CLOCK_GRANULARITY
    assert timestamps[2] - timestamps[1] > 1 - CLOCK_GRANULARITY
    assert timestamps[2] - timestamps[0] < 10

@require_backend_wrapper(MockBackendWrapper)
def test_httperror(backend, backend_wrapper, monkeypatch):
    value = b'hello there, let us see whats going on'
    key = 'quote'
    backend[key] = value

    # Monkeypatch request handler to produce a HTTP Error
    handler_class = backend_wrapper.server.RequestHandlerClass
    def do_DELETE(self, real_DELETE=handler_class.do_DELETE, count=[0]):
        count[0] += 1
        if count[0] >= 3:
            return real_DELETE(self)
        content = "I'm a proxy, and I messed up!".encode('utf-8')
        self.send_response(502, "Bad Gateway")
        self.send_header("Content-Type", 'text/plain; charset="utf-8"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(content)

    monkeypatch.setattr(handler_class, 'do_DELETE', do_DELETE)
    assert_raises(HTTPError, backend.delete, key)

    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    backend.delete(key)

# Require mock backend with expect100 support
@require_wrapper(lambda x: isinstance(x, MockBackendWrapper)
                 and 'disable-expect100' in x.backend.known_options)
def test_put_s3error_early(backend, backend_wrapper, monkeypatch):
    '''Fail after expect-100'''

    data = b'hello there, let us see whats going on'
    key = 'borg'

    # Monkeypatch request handler to produce 3 errors
    handler_class = backend_wrapper.server.RequestHandlerClass
    def handle_expect_100(self, real=handler_class.handle_expect_100, count=[0]):
        count[0] += 1
        if count[0] > 3:
            return real(self)
        else:
            self.send_error(503, code='OperationAborted')
            return False
    monkeypatch.setattr(handler_class, 'handle_expect_100', handle_expect_100)
    fh = backend.open_write(key)
    fh.write(data)
    assert_raises(OperationAbortedError, fh.close)

    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    fh.close()

@require_backend_wrapper(MockBackendWrapper)
def test_put_s3error_med(backend, backend_wrapper, monkeypatch):
    '''Fail as soon as data is received'''
    data = b'hello there, let us see whats going on'
    key = 'borg'

    # Monkeypatch request handler to produce 3 errors
    handler_class = backend_wrapper.server.RequestHandlerClass
    def do_PUT(self, real_PUT=handler_class.do_PUT, count=[0]):
        count[0] += 1
        # Note: every time we return an error, the request will be retried
        # *twice*: once because of the error, and a second time because the
        # connection has been closed by the server.
        if count[0] > 2:
            return real_PUT(self)
        else:
            self.send_error(503, code='OperationAborted')

            # Since we don't read all the data, we have to close
            # the connection
            self.close_connection = True

    monkeypatch.setattr(handler_class, 'do_PUT', do_PUT)
    fh = backend.open_write(key)
    fh.write(data)
    assert_raises(OperationAbortedError, fh.close)

    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    fh.close()

@require_backend_wrapper(MockBackendWrapper)
def test_put_s3error_late(backend, backend_wrapper, monkeypatch):
    '''Fail after reading all data'''
    data = b'hello there, let us see whats going on'
    key = 'borg'

    # Monkeypatch request handler to produce 3 errors
    handler_class = backend_wrapper.server.RequestHandlerClass
    def do_PUT(self, real_PUT=handler_class.do_PUT, count=[0]):
        count[0] += 1
        if count[0] > 3:
            return real_PUT(self)
        else:
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_error(503, code='OperationAborted')

    monkeypatch.setattr(handler_class, 'do_PUT', do_PUT)
    fh = backend.open_write(key)
    fh.write(data)
    assert_raises(OperationAbortedError, fh.close)

    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    fh.close()

@require_backend_wrapper(MockBackendWrapper)
def test_issue58(backend, backend_wrapper, monkeypatch):
    '''Send error while client is sending data'''

    # Monkeypatch request handler
    handler_class = backend_wrapper.server.RequestHandlerClass
    def do_PUT(self, real=handler_class.do_PUT, count=[0]):
        count[0] += 1
        if count[0] > 1:
            return real(self)

        # Read half the data
        self.rfile.read(min(BUFSIZE, int(self.headers['Content-Length'])//2))

        # Then generate an error and close the connection
        self.send_error(401, code='MalformedXML')
        self.close_connection = True

    monkeypatch.setattr(handler_class, 'do_PUT', do_PUT)

    # Write a big object. We need to write random data, or
    # compression while make the payload too small
    with pytest.raises(S3Error) as exc_info:
        with backend.open_write('borg') as fh, \
                open('/dev/urandom', 'rb') as rnd:
            for _ in range(5):
                fh.write(rnd.read(BUFSIZE))
    assert exc_info.value.code == 'MalformedXML'

    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    fh.close()

@require_backend_wrapper(MockBackendWrapper)
def test_issue58_b(backend, backend_wrapper, monkeypatch):
    '''Close connection while client is sending data'''

    # Monkeypatch request handler
    handler_class = backend_wrapper.server.RequestHandlerClass
    def do_PUT(self, real=handler_class.do_PUT, count=[0]):
        count[0] += 1
        if count[0] > 1:
            return real(self)

        # Read half the data
        self.rfile.read(min(BUFSIZE, int(self.headers['Content-Length'])//2))

        # Then close the connection silently
        self.close_connection = True
    monkeypatch.setattr(handler_class, 'do_PUT', do_PUT)

    # Write a big object. We need to write random data, or
    # compression while make the payload too small
    with pytest.raises(ConnectionClosed):
        with backend.open_write('borg') as fh, \
                open('/dev/urandom', 'rb') as rnd:
            for _ in range(5):
                fh.write(rnd.read(BUFSIZE))

    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    fh.close()

# Require mock server *and* google storage backend
@require_wrapper(lambda x: isinstance(x, MockBackendWrapper)
                 and isinstance(x.backend, GSBackend))
def test_expired_token_get(backend, backend_wrapper, monkeypatch):
    '''Test handling of expired OAuth token'''


    key = 'borg'
    data = b'hello there, let us see whats going on'

    # Monkeypatch backend class to check if token is refreshed
    token_refreshed = False
    def _get_access_token(self):
        nonlocal token_refreshed
        token_refreshed = True
        self.access_token[self.password] = 'foobar'
    monkeypatch.setattr(GSBackend, '_get_access_token',
                        _get_access_token)

    # Store some data
    backend[key] = data

    # Monkeypatch request handler to produce error
    handler_class = backend_wrapper.server.RequestHandlerClass
    def do_GET(self, real=handler_class.do_GET, count=[0]):
        count[0] += 1
        if count[0] > 1:
            return real(self)
        else:
            self.send_error(401, code='AuthenticationRequired')
    monkeypatch.setattr(handler_class, 'do_GET', do_GET)

    token_refreshed = False
    assert backend[key] == data
    assert token_refreshed

# Require mock server *and* google storage backend
@require_wrapper(lambda x: isinstance(x, MockBackendWrapper)
                 and isinstance(x.backend, GSBackend))
def test_expired_token_put(backend, backend_wrapper, monkeypatch):
    '''Test handling of expired OAuth token'''

    key = 'borg'
    data = b'hello there, let us see whats going on'

    # Monkeypatch backend class to check if token is refreshed
    token_refreshed = False
    def _get_access_token(self):
        nonlocal token_refreshed
        token_refreshed = True
        self.access_token[self.password] = 'foobar'
    monkeypatch.setattr(GSBackend, '_get_access_token',
                        _get_access_token)

    # Monkeypatch request handler to produce error
    handler_class = backend_wrapper.server.RequestHandlerClass
    def do_PUT(self, real=handler_class.do_PUT, count=[0]):
        count[0] += 1
        if count[0] > 1:
            return real(self)
        else:
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_error(401, code='AuthenticationRequired')
    monkeypatch.setattr(handler_class, 'do_PUT', do_PUT)

    token_refreshed = False
    backend[key] = data
    assert token_refreshed

@require_backend_wrapper(MockBackendWrapper)
def test_conn_abort(backend, backend_wrapper, monkeypatch):
    '''Close connection while sending data'''

    data = b'hello there, let us see whats going on'
    key = 'borg'
    backend[key] = data

    # Monkeypatch request handler
    handler_class = backend_wrapper.server.RequestHandlerClass
    def send_data(self, data, count=[0]):
        count[0] += 1
        if count[0] >= 3:
            self.wfile.write(data)
        else:
            self.wfile.write(data[:len(data)//2])
            self.close_connection = True
    monkeypatch.setattr(handler_class, 'send_data', send_data)

    with pytest.raises(ConnectionClosed):
        with catch_logmsg("^Object closed prematurely, can't check MD5",
                          count=1, level=logging.WARNING):
            backend.fetch(key)

    monkeypatch.setattr(backend_wrapper, 'may_temp_fail', True)
    assert backend[key] == data
