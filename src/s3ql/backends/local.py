'''
local.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function, absolute_import

from .common import AbstractConnection, AbstractBucket
import shutil
import logging
import cPickle as pickle
import os
import errno
import threading

log = logging.getLogger("backend.local")

class Connection(AbstractConnection):
    """
    A connection that stores buckets on the local disk
    
    This class is threadsafe. All methods (except for internal methods
    starting with underscore) may be called concurrently by different
    threads.    
    """
    
    def __init__(self):
        super(Connection, self).__init__()
        self.lock = threading.RLock()

    def delete_bucket(self, name, recursive=False):
        """Delete bucket"""

        with self.lock:
            if not os.path.exists(name):
                raise KeyError('Directory of local bucket does not exist')
    
            if recursive:
                shutil.rmtree(name)
            else:
                os.rmdir(name)

    def create_bucket(self, name, passphrase=None, compression='bzip2'):
        """Create and return a bucket"""

        with self.lock:
            if os.path.exists(name):
                raise RuntimeError('Bucket already exists')
            os.mkdir(name)
    
            return self.get_bucket(name, passphrase, compression)

    def get_bucket(self, name, passphrase=None, compression='bzip2'):
        """Return a bucket instance for the bucket `name`
        
        Raises `KeyError` if the bucket does not exist.
        """
        
        with self.lock:
            if not os.path.exists(name):
                raise KeyError('Local bucket directory %s does not exist' % name)
            return Bucket(name, self.lock, passphrase, compression)


class Bucket(AbstractBucket):
    '''
    A bucket that is stored on the local hard disk
    
    This class is threadsafe. All methods (except for internal methods
    starting with underscore) may be called concurrently by different
    threads.    
    '''

    def __init__(self, name, lock, passphrase, compression):
        super(Bucket, self).__init__(passphrase, compression)
        self.name = name
        self.lock = lock

    def __str__(self):
        return '<local bucket, name=%r>' % self.name

    def clear(self):
        """Delete all objects in bucket"""
        with self.lock:
            for name in os.listdir(self.name):
                path = os.path.join(self.name, name)
                if os.path.isdir(path):
                    shutil.rmtree(path)
                    os.rmdir(path)
                else:
                    os.unlink(path)

    def contains(self, key):
        with self.lock:
            path = self._key_to_path(key) + '.dat'
            try:
                os.lstat(path)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return False
                raise
            return True


    def raw_lookup(self, key):
        with self.lock:
            path = self._key_to_path(key)
            try:
                with open(path + '.meta', 'rb') as src:
                    return pickle.load(src)
            except IOError as exc:
                if exc.errno == errno.ENOENT:
                    raise KeyError('Key %r not in bucket' % key)
                else:
                    raise

    def delete(self, key, force=False):
        with self.lock:
            path = self._key_to_path(key)
            try:
                os.unlink(path + '.dat')
                os.unlink(path + '.meta')
            except IOError as exc:
                if exc.errno == errno.ENOENT:
                    if force:
                        pass
                    else:
                        raise KeyError('Key %r not in bucket' % key)
                else:
                    raise


    def list(self, prefix=None):
        with self.lock:
            if prefix:
                base = os.path.dirname(self._key_to_path(prefix))
            else:
                base = self.name
                
            for (_, _, names) in os.walk(base):
                for name in names:
                    if not name.endswith('.dat'):
                        continue
                    key = unescape(name[:-4])
                    
                    if not prefix or key.startswith(prefix):
                        yield key
            

    def get_size(self):
        with self.lock:
            size = 0
            for (path, _, names) in os.walk(self.name):
                for name in names:
                    if not name.endswith('.dat'):
                        continue
                    size += os.path.getsize(os.path.join(path, name))
    
            return size

    def raw_fetch(self, key, fh):
        with self.lock:
            path = self._key_to_path(key)
            try:
                with open(path + '.dat', 'rb') as src:
                    fh.seek(0)
                    shutil.copyfileobj(src, fh)
                with open(path + '.meta', 'rb') as src:
                    metadata = pickle.load(src)
            except IOError as exc:
                if exc.errno == errno.ENOENT:
                    raise KeyError('Key %r not in bucket' % key)
                else:
                    raise
    
            return metadata

    def raw_store(self, key, fh, metadata):
        with self.lock:
            path = self._key_to_path(key)
            fh.seek(0)
            try:
                dest = open(path + '.dat', 'wb')
            except IOError as exc:
                if exc.errno != errno.ENOENT:
                    raise
                os.makedirs(os.path.dirname(path))
                dest = open(path + '.dat', 'wb')
            
            shutil.copyfileobj(fh, dest)
            dest.close()
                    
            with open(path + '.meta', 'wb') as dest:
                pickle.dump(metadata, dest, 2)

    def copy(self, src, dest):
        with self.lock:
            if not isinstance(src, str):
                raise TypeError('key must be of type str')
    
            if not isinstance(dest, str):
                raise TypeError('key must be of type str')
    
            path_src = self._key_to_path(src)
            path_dest = self._key_to_path(dest)
    
            # Can't use shutil.copyfile() here, need to make
            # sure destination path exists
            try:
                dest = open(path_dest + '.dat', 'wb')
            except IOError as exc:
                if exc.errno != errno.ENOENT:
                    raise
                os.makedirs(os.path.dirname(path_dest))
                dest = open(path_dest + '.dat', 'wb')
            
            try:
                with open(path_src + '.dat', 'rb') as src:
                    shutil.copyfileobj(src, dest)
            except IOError as exc:
                if exc.errno == errno.ENOENT:
                    raise KeyError('Key %r not in bucket' % src)
                else:
                    raise
            finally:
                dest.close()
            
            shutil.copyfile(path_src + '.meta', path_dest + '.meta')

    def rename(self, src, dest):
        with self.lock:
            src_path = self._key_to_path(src)
            dest_path = self._key_to_path(dest)
            if not os.path.exists(src_path + '.dat'):
                raise KeyError('Key %r not in bucket' % src)
               
            try: 
                os.rename(src_path, dest_path)
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise
                os.makedirs(os.path.dirname(dest_path))
                os.rename(src_path, dest_path)       
            
        
    def _key_to_path(self, key):
        '''Return path for given key'''
        
        key = escape(key)
        
        if not key.startswith('s3ql_data_'):
            return os.path.join(self.name, key)
        
        no = key[10:]
        path = [ self.name, 's3ql_data']
        for i in range(0, len(no), 3):
            path.append(no[:i])
        path.append(key)
        
        return os.path.join(*path)

def escape(s):
    '''Escape '/', '=' and '\0' in s'''

    s = s.replace('=', '=3D')
    s = s.replace('/', '=2F')
    s = s.replace('\0', '=00')

    return s

def unescape(s):
    '''Un-Escape '/', '=' and '\0' in s'''

    s = s.replace('=2F', '/')
    s = s.replace('=00', '\0')
    s = s.replace('=3D', '=')

    return s


