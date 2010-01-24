'''
$Id$

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function

import s3ql.libc_api as libc
import os 
import ctypes
import errno

__all__ = [ 'listdir', 'setxattr', 'getxattr' ]


# We are implementing our own listdir, because os.listdir
# does not release the GIL, which leads to deadlocks
# if it is called from the FUSE process
def listdir(path):
    '''Return directory entries in `path`'''
    
    dirp = libc.opendir(path)
    if not dirp:
        err = libc.get_errno()
        raise OSError(err, os.strerror(err), path)
    
    entries = list()
    try:
        libc.set_errno(0)
        while True:
            dirent = libc.readdir(dirp)
            if not dirent:
                err = libc.get_errno()
                if err == 0:
                    break
                else:
                    raise OSError(err, os.strerror(err), path)
            name = ctypes.string_at(dirent.contents.d_name)
            if name == '.' or name == '..':
                continue
            entries.append(name)
    finally:
        if libc.closedir(dirp) != 0:
            err = libc.get_errno()
            raise OSError(err, os.strerror(err), path)
    
    return entries

def setxattr(path, name, value):
    '''Set extended attribute'''
    
    if libc.setxattr(path, name, value, len(value), 0) != 0:
        err = libc.get_errno()
        raise OSError(err, os.strerror(err), path)
        
    
def getxattr(path, name, size_guess=42):
    '''Get extended attribute
    
    If the caller knows the approximate size of the attribute value,
    it should be supplied in `size_guess`. If the guess turns out
    to be wrong, the system call will be carried out twice (once
    to determine the size and once to actually get the value).
    '''
    
    # First we try with the guessed size
    bufsize = size_guess
    buf = ctypes.create_string_buffer(bufsize)
    ret = libc.getxattr(path, name, buf, bufsize)
    
    if ret < 0 and libc.get_errno() == errno.ERANGE:
        # We guessed the wrong size
        ret = libc.getxattr(path, name, buf, 0)
        
        if ret < 0:
            err = libc.get_errno()
            raise OSError(err, os.strerror(err), path)
        
        bufsize = ret
        buf = ctypes.create_string_buffer(bufsize)
        ret = libc.getxattr(path, name, buf, bufsize)
        
    # Other error
    if ret < 0:
        err = libc.get_errno()
        raise OSError(err, os.strerror(err), path)
        
    return ctypes.string_at(buf, ret)