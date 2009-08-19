"""
Common functions for S3QL

Copyright (C) 2008  Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL. 
"""

from __future__ import unicode_literals
import sys
import os
import resource
import stat
import traceback
import threading
import logging
import logging.handlers
import time
from time import sleep
from getpass import getpass

__all__ = [ "decrease_refcount",  "get_cachedir", "init_logging",
           "get_credentials", "get_dbfile", "get_inode", "get_path",
           "increase_refcount", "unused_name", "get_inodes",
           "update_atime", "update_mtime", "update_ctime", 
           "waitfor", "ROOT_INODE", "writefile", "ExceptionStoringThread",
           "EmbeddedException", 'CTRL_NAME', 'CTRL_INODE' ]

class Filter(object):
    """
    For use with the logging module as a message filter.
    
    This filter accepts all messages which have the specified priority 
    and come from a configured list of loggers.    
    """
   
    def __init__(self, acceptnames=None, acceptlevel=logging.DEBUG):
        """Initializes a Filter object.
        
        Passes through all messages with priority higher than
        `acceptlevel` or coming from a logger with name in `acceptnames`. 
        """
        if acceptnames is None:
            acceptnames = list()
            
        self.acceptlevel = acceptlevel
        self.acceptnames = acceptnames
        
    def filter(self, record):
        '''Determine if the log message should be printed
        '''
        if record.levelno != self.acceptlevel:
            return False
        
        name = record.name.lower()
        for accept in self.acceptnames:
            if name.startswith(accept.lower()):
                return True
        
        return False
            
def init_logging(fg, quiet=False, debug=None, debugfile=None):
    """Initializes logging system.
    
    If `fg` is set, logging messages are send to stdout. Otherwise logging
    is done via unix syslog.
    
    If `quiet` is set, only messages with priority larger than
    logging.WARN are printed. Otherwise the minimum priority is
    logging.INFO.
    
    `debug` can be set to a list of logger names from which debugging
    messages are to be printed. If debugfile is not none, it specifies
    a file into which the debugging messages are written.
    """            
    if debug is None:
        debug = list()
        
    root_logger = logging.getLogger()
    
    # Remove existing handlers. We have to copy the list
    # since it is going to change during iteration
    for hdlr in [ x for x in root_logger.handlers ]: 
        root_logger.removeHandler(hdlr)
    
    # Standard handler
    if fg: 
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('[%(name)s] %(message)s'))           
    else:
        handler = logging.handlers.SysLogHandler(b"/dev/log")
        handler.setFormatter(logging.Formatter('s3ql[%(process)d]: [%(name)s] %(message)s'))  
    
    if quiet:
        root_logger.setLevel(logging.WARN)
        handler.setLevel(logging.WARN)
    else:
        root_logger.setLevel(logging.INFO)
        handler.setLevel(logging.INFO)
        
    root_logger.addHandler(handler)


    # Debugging handler
    if debug:
        if debugfile is None:
            debug_handler = logging.StreamHandler()
        else:
            debug_handler = logging.handlers.WatchedFileHandler(debugfile)
            
        debug_handler.setFormatter(logging.Formatter('%(asctime)s,%(msecs)03d %(threadName)s: '
                                                     '[%(name)s] %(message)s',
                                                     datefmt="%H:%M:%S")) 
        debug_handler.addFilter(Filter(acceptnames=debug, acceptlevel=logging.DEBUG))
        root_logger.addHandler(debug_handler) 
        root_logger.setLevel(logging.DEBUG)
   

def update_atime(inode, conn):
    """Updates the atime of the specified object.

    The objects atime will be set to the current time.
    """
    conn.execute("UPDATE inodes SET atime=? WHERE id=?", 
                (time.time() - time.timezone, inode))

def update_ctime(inode, conn):
    """Updates the ctime of the specified object.

    The objects ctime will be set to the current time.
    """
    conn.execute("UPDATE inodes SET ctime=? WHERE id=?", 
                (time.time() - time.timezone, inode))


def update_mtime(inode, conn):
    """Updates the mtime of the specified object.

    The objects mtime will be set to the current time.
    """
    conn.execute("UPDATE inodes SET mtime=? WHERE id=?",
                (time.time() - time.timezone, inode))

def get_inode(path, conn):
    """Returns inode of object at `path`.
    
    Raises `KeyError` if the path does not exist.
    """
    return get_inodes(path, conn)[-1]
    
def get_inodes(path, conn):
    """Returns the inodes of the elements in `path`.
    
    The first inode of the resulting list will always be the inode
    of the root directory. Raises `KeyError` if the path
    does not exist.
    """
    
    if not isinstance(path, bytes):
        raise TypeError('path must be of type bytes')
    
    # Remove leading and trailing /
    path = path.lstrip(b"/").rstrip(b"/") 

    inode = ROOT_INODE
    
    # Root directory requested
    if not path:
        return [inode]
    
    # Traverse
    visited = [inode]
    for el in path.split(b'/'):
        try:
            inode = conn.get_val("SELECT inode FROM contents WHERE name=? AND parent_inode=?",
                                (el, inode))
        except StopIteration:
            raise KeyError('Path does not exist', path)
        
        visited.append(inode)

    return visited
    
def get_path(name, inode_p, conn):
    """Returns the full path of `name` with parent inode `inode_p`.
    """
    
    if not isinstance(name, bytes):
        raise TypeError('name must be of type bytes')
    
    path = [ name ] 
    
    maxdepth = 255
    while inode_p != ROOT_INODE:
        # This can be ambigious if directories are hardlinked
        (name2, inode_p) = conn.get_row("SELECT name, parent_inode FROM contents "
                                      "WHERE inode=? AND name != ? AND name != ?",
                                       (inode_p, b'.', b'..')) 
        path.append(name2)
        maxdepth -= 1
        if maxdepth == 0:
            raise RuntimeError('Failed to resolve name "%s" at inode %d to path',
                               name, inode_p)
        
    path.append(b'')
    path.reverse()
    
    return b'/'.join(path)
    
    
def decrease_refcount(inode, conn):
    """Decrease reference count for inode by 1.

    Also updates ctime.
    """
    conn.execute("UPDATE inodes SET refcount=refcount-1,ctime=? WHERE id=?",
             (time.time() - time.timezone, inode))

def increase_refcount(inode, conn):
    """Increase reference count for inode by 1.

    Also updates ctime.
    """
    conn.execute("UPDATE inodes SET refcount=refcount+1, ctime=? WHERE id=?",
             (time.time() - time.timezone, inode))

def get_cachedir(bucketname, path):
    """get directory to put cache files in.
    """

    if not os.path.exists(path):
        os.mkdir(path)

    return path + ("/%s-cache/" % bucketname)


def get_dbfile(bucketname, path):
    """get filename for metadata db.
    """

    if not os.path.exists(path):
        os.mkdir(path)

    return path + ("/%s.db" % bucketname)


def get_credentials(keyfile, key=None):
    """Get AWS credentials.

    If `key` has been specified, use this as access key and
    read the password from stdin. Otherwise, tries to read
    ~/.awssecret.
    """
    
    pw = None
    
    if key:
        if sys.stdin.isatty():
            pw = getpass("Enter AWS password: ")
        else:
            pw = sys.stdin.readline().rstrip()
            
    else:
        
        if os.path.isfile(keyfile):
            mode = os.stat(keyfile).st_mode
            kfile = open(keyfile, "r")
            key = kfile.readline().rstrip()

            if mode & (stat.S_IRGRP | stat.S_IROTH):
                sys.stderr.write("~/.awssecret has insecure permissions, "
                                 "reading password from terminal instead!\n")        
            else:    
                pw = kfile.readline().rstrip()
            kfile.close()    
            
        if not key:
            if sys.stdin.isatty():
                print "Enter AWS access key: ",
            key = sys.stdin.readline().rstrip()
           
        if not pw:
            if sys.stdin.isatty():
                pw = getpass("Enter AWS password: ")
            else:
                pw = sys.stdin.readline().rstrip()

    return (key, pw)

def waitfor(timeout, fn, *a, **kw):
    """Wait for fn(*a, **kw) to return True.
    
    Waits in increasing periods. Returns False if a timeout occurs, 
    True otherwise.
    """
    
    if fn(*a, **kw):
        return True
    
    step = 0.2
    while timeout > 0:    
        sleep(step)
        timeout -= step
        step *= 2
        if fn(*a, **kw):
            return True
        
    return False
    
def writefile(src, dest, server):
    """Copies the local file `src' into the fs as `dest`
    
    `dest` must not be opened yet by the server.
    """

    try:
        destfd = server.open(dest, None)
    except KeyError:
        mode = ( stat.S_IFREG | stat.S_IRUSR | stat.S_IWUSR )
        destfd = server.create(dest, mode)
        
    srcfh =  open(src, "rb")
    chunksize = resource.getpagesize()

    buf = srcfh.read(chunksize)
    off = 0
    while buf:
        server.write(buf, off, destfd)
        off += len(buf)
        buf = srcfh.read(chunksize)        
  
    srcfh.close()
    
    server.fsync(True, destfd)
    server.flush(destfd)
    server.release(destfd)
    

def unused_name(path, conn):
    '''Append suffix to path so that it does not exist
    '''
    
    if not isinstance(path, bytes):
        raise TypeError('path must be of type bytes')
    
    i = 0
    newpath = path
    path = path + b'-'
    try:
        while True:
            get_inode(newpath, conn)            
            i += 1
            newpath = path + bytes(i)
            
    except KeyError:
        pass
    
    return newpath
        

# Define inode of root directory
ROOT_INODE = 0

# Name and inode of the special s3ql control file
CTRL_NAME = '.__s3ql__ctrl__'
CTRL_INODE = 1

class ExceptionStoringThread(threading.Thread):
    '''Catch all exceptions and store them
    '''
    
    def __init__(self, target):
        super(ExceptionStoringThread, self).__init__()
        self.target = target
        self.exc = None
        self.tb = None
        self.joined = False
        
    def run(self):
        try:
            self.target()
        except BaseException as exc:
            self.exc = exc
            self.tb = sys.exc_info()[2] # This creates a circular reference chain
    
    def join_and_raise(self):
        '''Wait for the thread to finish, raise any occured exceptions
        '''
        self.joined = True
        self.join()
        if self.exc is not None:
            try:
                raise EmbeddedException(self.exc, self.tb, self.name)
            finally: 
                # Here we break the chain
                self.tb = None  
                
    def __del__(self):
        if not self.joined:
            raise RuntimeError("ExceptionStoringThread instance was destroyed "
                               "without calling join_and_raise()!")                

class EmbeddedException(Exception):
    '''Encapsulates an exception that happened in a different thread
    '''
    
    def __init__(self, exc, tb, threadname):
        super(EmbeddedException, self).__init__()
        self.exc = exc
        self.tb = tb
        self.threadname = threadname
        
    def __str__(self):
        return ''.join(['caused by an exception in thread %s.\n' % self.threadname,
                       'Original/inner traceback (most recent call last): \n' ] +
                       traceback.format_tb(self.tb) +
                       traceback.format_exception_only(type(self.exc), self.exc))