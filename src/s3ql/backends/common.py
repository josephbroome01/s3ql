'''
common.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright © 2008 Nikolaus Rath <Nikolaus.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from ..logging import logging # Ensure use of custom logger class
from abc import abstractmethod, ABCMeta
from functools import wraps
import time
import textwrap
import inspect

log = logging.getLogger(__name__)

RETRY_TIMEOUT = 60 * 60 * 24
def retry(method):
    '''Wrap *method* for retrying on some exceptions

    If *method* raises an exception for which the instance's
    `is_temp_failure(exc)` method is true, the *method* is called again
    at increasing intervals. If this persists for more than `RETRY_TIMEOUT`
    seconds, the most-recently caught exception is re-raised.
    '''

    if inspect.isgeneratorfunction(method):
        raise TypeError('Wrapping a generator function is pointless')

    @wraps(method)
    def wrapped(*a, **kw):
        self = a[0]
        interval = 1 / 50
        waited = 0
        retries = 0
        while True:
            try:
                return method(*a, **kw)
            except Exception as exc:
                # Access to protected member ok
                #pylint: disable=W0212
                if not self.is_temp_failure(exc):
                    raise
                if waited > RETRY_TIMEOUT:
                    log.error('%s.%s(*): Timeout exceeded, re-raising %r exception',
                            self.__class__.__name__, method.__name__, exc)
                    raise

                retries += 1
                if retries <= 2:
                    log_fn = log.debug
                elif retries <= 4:
                    log_fn = log.info
                else:
                    log_fn = log.warning

                log_fn('Encountered %s exception (%s), retrying call to %s.%s for the %d-th time...',
                       type(exc).__name__, exc, self.__class__.__name__, method.__name__, retries)

                if hasattr(exc, 'retry_after') and exc.retry_after:
                    log.debug('retry_after is %.2f seconds', exc.retry_after)
                    interval = exc.retry_after

            time.sleep(interval)
            waited += interval
            interval = min(5*60, 2*interval)

    extend_docstring(wrapped,
                     'This method has been wrapped and will automatically re-execute in '
                     'increasing intervals for up to `s3ql.backends.common.RETRY_TIMEOUT` '
                     'seconds if it raises an exception for which the instance\'s '
                     '`is_temp_failure` method returns True.')

    return wrapped

def extend_docstring(fun, s):
    '''Append *s* to *fun*'s docstring with proper wrapping and indentation'''

    if fun.__doc__ is None:
        fun.__doc__ = ''

    # Figure out proper indentation
    indent = 60
    for line in fun.__doc__.splitlines()[1:]:
        stripped = line.lstrip()
        if stripped:
            indent = min(indent, len(line) - len(stripped))

    indent_s = '\n' + ' ' * indent
    fun.__doc__ += ''.join(indent_s + line
                               for line in textwrap.wrap(s, width=80 - indent))
    fun.__doc__ += '\n'

class RetryIterator:
    '''
    A RetryIterator instance iterates over the elements produced by any
    generator function passed to its constructor, i.e. it wraps the iterator
    obtained by calling the generator function.  When retrieving elements from the
    wrapped iterator, exceptions may occur. Most such exceptions are
    propagated. However, exceptions for which the *is_temp_failure_fn* function
    returns True are caught. If that happens, the wrapped iterator is replaced
    by a new one obtained by calling the generator function again with the
    *start_after* parameter set to the last element that was retrieved before
    the exception occured.

    If attempts to retrieve the next element fail repeatedly, the iterator is
    replaced only after sleeping for increasing intervals. If no new element can
    be obtained after `RETRY_TIMEOUT` seconds, the last exception is no longer
    caught but propagated to the caller. This behavior is implemented by
    wrapping the __next__ method with the `retry` decorator.
    '''

    def __init__(self, generator, is_temp_failure_fn, args=(), kwargs=None):
        if not inspect.isgeneratorfunction(generator):
            raise TypeError('*generator* must be generator function')

        self.generator = generator
        self.iterator = None
        self.is_temp_failure = is_temp_failure_fn
        if kwargs is None:
            kwargs = {}
        self.kwargs = kwargs
        self.args = args

    def __iter__(self):
        return self

    @retry
    def __next__(self):
        if self.iterator is None:
            self.iterator = self.generator(*self.args, **self.kwargs)

        try:
            el = next(self.iterator)
        except Exception as exc:
            if self.is_temp_failure(exc):
                self.iterator = None
            raise

        self.kwargs['start_after'] = el
        return el

def retry_generator(method):
    '''Wrap *method* in a `RetryIterator`

    *method* must return a generator, and accept a keyword argument
    *start_with*. The RetryIterator's `is_temp_failure` attribute
    will be set to the `is_temp_failure` method of the instance
    to which *method* is bound.
    '''

    @wraps(method)
    def wrapped(*a, **kw):
        return RetryIterator(method, a[0].is_temp_failure, args=a, kwargs=kw)

    extend_docstring(wrapped,
                     'This generator method has been wrapped and will return a '
                     '`RetryIterator` instance.')

    return wrapped

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

    @property
    @abstractmethod
    def has_native_rename(self):
        '''True if the backend has a native, atomic rename operation'''
        pass

    def reset(self):
        '''Reset backend

        This resets the backend and ensures that it is ready to process
        requests. In most cases, this method does nothing. However, if e.g. a
        file handle returned by a previous call to `open_read` was not properly
        closed (e.g. because an exception happened during reading), the `reset`
        method will make sure that any underlying connection is properly closed.

        Obviously, this method must not be called while any file handles
        returned by the backend are still in use.
        '''

        pass

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

        `metadata` can be an additional (pickle-able) `dict` object to store with
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
    def copy(self, src, dest, metadata=None):
        """Copy data stored under key `src` to key `dest`

        If `dest` already exists, it will be overwritten. If *metadata* is
        `None` metadata will be copied from the source as well, otherwise
        *metadata* becomes the metadata for the new object and must be
        a pickle-able `dict` instance.

        Copying will be done on the remote side without retrieving object data.
        """

        pass

    @abstractmethod
    def update_meta(self, key, metadata):
        """Replace metadata of *key* with *metadata*

        Metadata must be `dict` instance and pickle-able.
        """

        pass

    def rename(self, src, dest, metadata=None):
        """Rename key `src` to `dest`

        If `dest` already exists, it will be overwritten. If *metadata* is
        `None` metadata will be preserved, otherwise *metadata* becomes the
        metadata for the renamed object and must be a pickle-able `dict`
        instance.

        Rename done remotely without retrieving object data.
        """

        self.copy(src, dest, metadata)
        self.delete(src)

    def close(self):
        '''Close any opened resources

        This method closes any resources allocated by the backend (e.g. network
        sockets). This method should be called explicitly before a backend
        object is garbage collected. The backend object may be re-used after
        `close` has been called, in this case the necessary resources are
        transparently allocated again.
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

class ChecksumError(Exception):
    """
    Raised if there is a checksum error in the data that we received.
    """

    def __init__(self, str_):
        super().__init__()
        self.str = str_

    def __str__(self):
        return self.str

