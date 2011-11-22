'''
common.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from __future__ import division, print_function, absolute_import
from llfuse import ROOT_INODE
from .deltadump import INTEGER, BLOB, TIME, dump_table, load_table
import cPickle as pickle
import hashlib
import logging
import os
import stat
import sys
import tempfile
import time
import lzma

# Buffer size when writing objects
BUFSIZE = 256 * 1024

log = logging.getLogger('common')
 
 
# Has to be kept in sync with create_tables()!
DUMP_SPEC = [
             ('objects', 'id', (('id', INTEGER, 1),
                                ('size', INTEGER))),
             
             ('blocks', 'id', (('id', INTEGER, 1),
                             ('hash', BLOB, 32),
                             ('size', INTEGER),
                             ('obj_id', INTEGER, 1))),
             
             ('inodes', 'id', (('id', INTEGER, 1),
                               ('uid', INTEGER),
                               ('gid', INTEGER),
                               ('mode', INTEGER),
                               ('mtime', TIME),
                               ('atime', TIME),
                               ('ctime', TIME),
                               ('size', INTEGER),
                               ('rdev', INTEGER),
                               ('locked', INTEGER))),
             
             ('inode_blocks', 'inode, blockno', 
              (('inode', INTEGER),
               ('blockno', INTEGER, 1),
               ('block_id', INTEGER, 1))),
             
             ('symlink_targets', 'inode', (('inode', INTEGER, 1),
                                           ('target', BLOB))),
           
             ('names', 'id', (('id', INTEGER, 1),
                              ('name', BLOB))),
           
             ('contents', 'parent_inode, name_id',
              (('name_id', INTEGER, 1),
               ('inode', INTEGER, 1),
               ('parent_inode', INTEGER))),
             
             ('ext_attributes', 'inode', (('inode', INTEGER),
                                          ('name_id', INTEGER),
                                          ('value', BLOB))),      
] 
    
            
def setup_logging(options):        
    root_logger = logging.getLogger()
    if root_logger.handlers:
        log.debug("Logging already initialized.")
        return
        
    stdout_handler = add_stdout_logging(options.quiet)
    if hasattr(options, 'log') and options.log:
        root_logger.addHandler(options.log)
        debug_handler = options.log  
    else:
        debug_handler = stdout_handler
    setup_excepthook()
    
    if options.debug:
        root_logger.setLevel(logging.DEBUG)
        debug_handler.setLevel(logging.NOTSET)
        if 'all' not in options.debug:
            # Adding the filter to the root logger has no effect.
            debug_handler.addFilter(LoggerFilter(options.debug, logging.INFO))
        logging.disable(logging.NOTSET)
    else:
        root_logger.setLevel(logging.INFO)
        logging.disable(logging.DEBUG)
        
    return stdout_handler 
 
                        
class LoggerFilter(object):
    """
    For use with the logging module as a message filter.
    
    This filter accepts all messages which have at least the specified
    priority *or* come from a configured list of loggers.
    """

    def __init__(self, acceptnames, acceptlevel):
        """Initializes a Filter object"""
        
        self.acceptlevel = acceptlevel
        self.acceptnames = [ x.lower() for x in acceptnames ]

    def filter(self, record):
        '''Determine if the log message should be printed'''

        if record.levelno >= self.acceptlevel:
            return True

        if record.name.lower() in self.acceptnames:
            return True

        return False
    
def add_stdout_logging(quiet=False):
    '''Add stdout logging handler to root logger'''

    root_logger = logging.getLogger()
    formatter = logging.Formatter('%(message)s') 
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    if quiet:
        handler.setLevel(logging.WARN)
    else:
        handler.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    return handler

def get_seq_no(bucket):
    '''Get current metadata sequence number'''   
    from .backends.common import NoSuchObject
    
    seq_nos = list(bucket.list('s3ql_seq_no_')) 
    if not seq_nos:
        # Maybe list result is outdated
        seq_nos = [ 's3ql_seq_no_1' ]
    
    if (seq_nos[0].endswith('.meta') 
        or seq_nos[0].endswith('.dat')): 
        raise QuietError('Old file system revision, please run `s3qladm upgrade` first.')
         
    seq_nos = [ int(x[len('s3ql_seq_no_'):]) for x in seq_nos ]
    seq_no = max(seq_nos) 
    
    # Make sure that object really exists
    while ('s3ql_seq_no_%d' % seq_no) not in bucket:
        seq_no -= 1 
        if seq_no == 0:
            raise QuietError('No S3QL file system found in bucket.')
    while ('s3ql_seq_no_%d' % seq_no) in bucket:
        seq_no += 1 
    seq_no -= 1
    
    # Delete old seq nos
    for i in [ x for x in seq_nos if x < seq_no - 10 ]:
        try:
            del bucket['s3ql_seq_no_%d' % i]
        except NoSuchObject:
            pass # Key list may not be up to date
        
    return seq_no   

def cycle_metadata(bucket):
    from .backends.common import NoSuchObject
    
    log.info('Backing up old metadata...')
    for i in reversed(range(10)):
        try:
            bucket.copy("s3ql_metadata_bak_%d" % i, "s3ql_metadata_bak_%d" % (i + 1))
        except NoSuchObject:
            pass
                
    bucket.copy("s3ql_metadata", "s3ql_metadata_bak_0")             

def dump_metadata(ofh, db):
    
    log.info('Dumping metadata...')
    tmpfh = tempfile.TemporaryFile()
       
    for (table, order, columns) in DUMP_SPEC:
        log.info('..%s..', table)
        dump_table(table, order, columns, db=db, fh=tmpfh)
        
    # compress and send
    # FIXME: IIRC bzip2 was the best choice, need to benchmark
    # FIXME: Still need to document new build requirements
    log.info("Compressing and uploading metadata...")
    compr = lzma.LZMACompressor(options={ 'level': 7 })
    tmpfh.seek(0)
    while True:
        buf = tmpfh.read(BUFSIZE)
        if not buf:
            break
        buf = compr.compress(buf)
        if buf:
            ofh.write(buf)
    buf = compr.flush()
    if buf:
        ofh.write(buf)
    del compr # Free memory ASAP, LZMA level 7 needs 186 MB
    tmpfh.close()

def restore_metadata(ifh, conn):

    log.info('Downloading and decompressing metadata...')
    
    # decompress to temporary file, deltadump can't read from Python object
    tmp = tempfile.TemporaryFile()
    decompressor = lzma.LZMADecompressor()
    while True:
        buf = ifh.read(BUFSIZE)
        if not buf:
            break
        buf = decompressor.decompress(buf)
        if buf:
            tmp.write(buf)
    del decompressor
    tmp.seek(0) 

    log.info("Reading metadata...")
    create_tables(conn)
    for (table, _, columns) in DUMP_SPEC:
        log.info('..%s..', table)
        load_table(table, columns, db=conn, fh=tmp)
    tmp.close()
    
    log.info("Analyzing metadata...")
    conn.execute('UPDATE objects SET refcount='
                 '(SELECT COUNT(obj_id) FROM blocks WHERE obj_id = objects.id)')
    conn.execute('UPDATE blocks SET refcount='
                 '(SELECT COUNT(block_id) FROM inode_blocks WHERE block_id = blocks.id)')
    conn.execute('UPDATE inodes SET refcount='
                 '(SELECT COUNT(inode) FROM contents WHERE inode = inodes.id)')
    conn.execute('UPDATE names SET refcount='
                 '(SELECT COUNT(name_id) FROM contents WHERE name_id = names.id)'
                 '+ (SELECT COUNT(name_id) FROM ext_attributes WHERE name_id = names.id)')
    
    conn.execute('ANALYZE')
    
class QuietError(Exception):
    '''
    QuietError is the base class for exceptions that should not result
    in a stack trace being printed.
    
    It is typically used for exceptions that are the result of the user
    supplying invalid input data. The exception argument should be a
    string containing sufficient information about the problem.
    '''
    
    def __init__(self, msg=''):
        super(QuietError, self).__init__()
        self.msg = msg

    def __str__(self):
        return self.msg

def setup_excepthook():
    '''Modify sys.excepthook to log exceptions
    
    Also makes sure that exceptions derived from `QuietException`
    do not result in stacktraces.
    '''
    
    def excepthook(type_, val, tb):
        root_logger = logging.getLogger()
        if isinstance(val, QuietError):
            root_logger.error(val.msg)
        else:
            root_logger.error('Uncaught top-level exception', 
                              exc_info=(type_, val, tb))
            
    sys.excepthook = excepthook 
    
def inode_for_path(path, conn):
    """Return inode of directory entry at `path`
    
     Raises `KeyError` if the path does not exist.
    """
    from .database import NoSuchRowError
    
    if not isinstance(path, bytes):
        raise TypeError('path must be of type bytes')

    # Remove leading and trailing /
    path = path.lstrip(b"/").rstrip(b"/")

    # Traverse
    inode = ROOT_INODE
    for el in path.split(b'/'):
        try:
            inode = conn.get_val("SELECT inode FROM contents_v WHERE name=? AND parent_inode=?", 
                                 (el, inode))
        except NoSuchRowError:
            raise KeyError('Path %s does not exist' % path)

    return inode

def get_path(id_, conn, name=None):
    """Return a full path for inode `id_`.
    
    If `name` is specified, it is appended at the very end of the
    path (useful if looking up the path for file name with parent
    inode).
    """

    if name is None:
        path = list()
    else:
        if not isinstance(name, bytes):
            raise TypeError('name must be of type bytes')
        path = [ name ]

    maxdepth = 255
    while id_ != ROOT_INODE:
        # This can be ambiguous if directories are hardlinked
        (name2, id_) = conn.get_row("SELECT name, parent_inode FROM contents_v "
                                    "WHERE inode=? LIMIT 1", (id_,))
        path.append(name2)
        maxdepth -= 1
        if maxdepth == 0:
            raise RuntimeError('Failed to resolve name "%s" at inode %d to path',
                               name, id_)

    path.append(b'')
    path.reverse()

    return b'/'.join(path)


def _escape(s):
    '''Escape '/', '=' and '\0' in s'''

    s = s.replace('=', '=3D')
    s = s.replace('/', '=2F')
    s = s.replace('\0', '=00')

    return s

def get_bucket_cachedir(storage_url, cachedir):
    if not os.path.exists(cachedir):
        os.mkdir(cachedir, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return os.path.join(cachedir, _escape(storage_url))

# Name and inode of the special s3ql control file
CTRL_NAME = b'.__s3ql__ctrl__'
CTRL_INODE = 2

def sha256_fh(fh):
    fh.seek(0)
    
    # Bogus error about hashlib not having a sha256 member
    #pylint: disable=E1101
    sha = hashlib.sha256()

    while True:
        buf = fh.read(BUFSIZE)
        if not buf:
            break
        sha.update(buf)

    return sha.digest()

def init_tables(conn):
    # Insert root directory
    timestamp = time.time() - time.timezone
    conn.execute("INSERT INTO inodes (id,mode,uid,gid,mtime,atime,ctime,refcount) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                   (ROOT_INODE, stat.S_IFDIR | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
                   | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
                    os.getuid(), os.getgid(), timestamp, timestamp, timestamp, 1))

    # Insert control inode, the actual values don't matter that much 
    conn.execute("INSERT INTO inodes (id,mode,uid,gid,mtime,atime,ctime,refcount) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (CTRL_INODE, stat.S_IFIFO | stat.S_IRUSR | stat.S_IWUSR,
                  0, 0, timestamp, timestamp, timestamp, 42))

    # Insert lost+found directory
    inode = conn.rowid("INSERT INTO inodes (mode,uid,gid,mtime,atime,ctime,refcount) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (stat.S_IFDIR | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
                        os.getuid(), os.getgid(), timestamp, timestamp, timestamp, 1))
    name_id = conn.rowid('INSERT INTO names (name, refcount) VALUES(?,?)',
                         (b'lost+found', 1))
    conn.execute("INSERT INTO contents (name_id, inode, parent_inode) VALUES(?,?,?)",
                 (name_id, inode, ROOT_INODE))

def create_tables(conn): 
    # Table of storage objects
    # Refcount is included for performance reasons
    conn.execute("""
    CREATE TABLE objects (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        refcount  INT, 
        size      INT  
    )""")

    # Table of known data blocks
    # Refcount is included for performance reasons
    conn.execute("""
    CREATE TABLE blocks (
        id        INTEGER PRIMARY KEY,
        hash      BLOB(16) UNIQUE,
        refcount  INT,
        size      INT NOT NULL,    
        obj_id    INTEGER NOT NULL REFERENCES objects(id)
    )""")
                
    # Table with filesystem metadata
    # The number of links `refcount` to an inode can in theory
    # be determined from the `contents` table. However, managing
    # this separately should be significantly faster (the information
    # is required for every getattr!)
    conn.execute("""
    CREATE TABLE inodes (
        -- id has to specified *exactly* as follows to become
        -- an alias for the rowid.
        id        INTEGER PRIMARY KEY,
        uid       INT NOT NULL,
        gid       INT NOT NULL,
        mode      INT NOT NULL,
        mtime     REAL NOT NULL,
        atime     REAL NOT NULL,
        ctime     REAL NOT NULL,
        refcount  INT,
        size      INT NOT NULL DEFAULT 0,
        rdev      INT NOT NULL DEFAULT 0,
        locked    BOOLEAN NOT NULL DEFAULT 0
    )""")

    # Further Blocks used by inode (blockno >= 1)
    conn.execute("""
    CREATE TABLE inode_blocks (
        inode     INTEGER NOT NULL REFERENCES inodes(id),
        blockno   INT NOT NULL,
        block_id    INTEGER NOT NULL REFERENCES blocks(id),
        PRIMARY KEY (inode, blockno)
    )""")
    
    # Symlinks
    conn.execute("""
    CREATE TABLE symlink_targets (
        inode     INTEGER PRIMARY KEY REFERENCES inodes(id),
        target    BLOB NOT NULL
    )""")
    
    # Names of file system objects
    conn.execute("""
    CREATE TABLE names (
        id     INTEGER PRIMARY KEY,
        name   BLOB NOT NULL,
        refcount  INT,
        UNIQUE (name)
    )""")

    # Table of filesystem objects
    # rowid is used by readdir() to restart at the correct position
    conn.execute("""
    CREATE TABLE contents (
        rowid     INTEGER PRIMARY KEY AUTOINCREMENT,
        name_id   INT NOT NULL REFERENCES names(id),
        inode     INT NOT NULL REFERENCES inodes(id),
        parent_inode INT NOT NULL REFERENCES inodes(id),
        
        UNIQUE (parent_inode, name_id)
    )""")

    # Extended attributes
    conn.execute("""
    CREATE TABLE ext_attributes (
        inode     INTEGER NOT NULL REFERENCES inodes(id),
        name_id   INTEGER NOT NULL REFERENCES names(id),
        value     BLOB NOT NULL,
 
        PRIMARY KEY (inode, name_id)               
    )""")

    # Shortcuts
    conn.execute("""
    CREATE VIEW contents_v AS
    SELECT * FROM contents JOIN names ON names.id = name_id       
    """)    
    conn.execute("""
    CREATE VIEW ext_attributes_v AS
    SELECT * FROM ext_attributes JOIN names ON names.id = name_id
    """)        