'''
mount.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from __future__ import division, print_function, absolute_import
from s3ql import fs, CURRENT_FS_REV, inode_cache
from s3ql.backends.common import get_bucket_factory, BucketPool
from s3ql.common import (setup_logging, get_bucket_cachedir, get_seq_no, 
    QuietError, ExceptionStoringThread, cycle_metadata, dump_metadata, 
    restore_metadata)
from s3ql.daemonize import daemonize
from s3ql.database import Connection
from s3ql.parse_args import ArgumentParser
from s3ql import upload_manager
import cPickle as pickle
import llfuse
import logging
import os
import shutil
import signal
import stat
import sys
import tempfile
import textwrap
import threading
import time

# We can't use relative imports because this file may
# be directly executed.

#import psyco
#psyco.profile()

log = logging.getLogger("mount")

def main(args=None):
    '''Mount S3QL file system'''

    if args is None:
        args = sys.argv[1:]

    options = parse_args(args)
    fuse_opts = get_fuse_opts(options)
    
    # Save handler so that we can remove it when daemonizing
    stdout_log_handler = setup_logging(options)
    
    if not os.path.exists(options.mountpoint):
        raise QuietError('Mountpoint does not exist.')
        
    if options.profile:
        import cProfile
        import pstats
        prof = cProfile.Profile()

    bucket_factory = get_bucket_factory(options)
    bucket_pool = BucketPool(bucket_factory)
    
    # Get paths
    cachepath = get_bucket_cachedir(options.storage_url, options.cachedir)
    
    # Retrieve metadata
    with bucket_pool() as bucket:
        (param, db) = get_metadata(bucket, cachepath)
            
    if options.nfs:
        log.info('Creating NFS indices...')
        # NFS may try to look up '..', so we have to speed up this kind of query
        db.execute('CREATE INDEX IF NOT EXISTS ix_contents_inode ON contents(inode)')
        
        # Since we do not support generation numbers, we have to keep the
        # likelihood of reusing a just-deleted inode low
        inode_cache.RANDOMIZE_INODES = True
    else:
        db.execute('DROP INDEX IF EXISTS ix_contents_inode')
                       
    metadata_upload_thread = MetadataUploadThread(bucket_pool, param, db,
                                                  options.metadata_upload_interval)
    operations = fs.Operations(bucket_pool, db, cachedir=cachepath + '-cache', 
                               blocksize=param['blocksize'],
                               cache_size=options.cachesize * 1024,
                               upload_event=metadata_upload_thread.event,
                               cache_entries=options.max_cache_entries)
    
    log.info('Mounting filesystem...')
    llfuse.init(operations, options.mountpoint, fuse_opts)
    try:
        if not options.fg:
            me = threading.current_thread()
            for t in threading.enumerate():
                if t is me:
                    continue
                log.error('Waiting for thread %s', t)
                t.join()
    
            if stdout_log_handler:
                logging.getLogger().removeHandler(stdout_log_handler)
            daemonize(options.cachedir)
        
        metadata_upload_thread.start()
        if options.upstart:
            os.kill(os.getpid(), signal.SIGSTOP)
        if options.profile:
            prof.runcall(llfuse.main, options.single)
        else:
            llfuse.main(options.single)
    
    finally:
        llfuse.close()
        metadata_upload_thread.stop()
            
    db_mtime = metadata_upload_thread.db_mtime
    
    if operations.encountered_errors:
        param['needs_fsck'] = True
    else:       
        param['needs_fsck'] = False
     
    # Do not update .params yet, dump_metadata() may
    # fail if the database is corrupted, in which case we
    # want to force an fsck.
       
    with bucket_pool() as bucket:   
        seq_no = get_seq_no(bucket)
        if db_mtime == os.stat(cachepath + '.db').st_mtime:
            log.info('File system unchanged, not uploading metadata.')
            del bucket['s3ql_seq_no_%d' % param['seq_no']]         
            param['seq_no'] -= 1
            pickle.dump(param, open(cachepath + '.params', 'wb'), 2)         
        elif seq_no == param['seq_no']:
            log.info('Uploading metadata...')     
            cycle_metadata(bucket)
            param['last-modified'] = time.time() - time.timezone
            with bucket.open_write('s3ql_metadata', param) as fh:
                dump_metadata(fh, db)
            pickle.dump(param, open(cachepath + '.params', 'wb'), 2)
        else:
            log.error('Remote metadata is newer than local (%d vs %d), '
                      'refusing to overwrite!', seq_no, param['seq_no'])
            log.error('The locally cached metadata will be *lost* the next time the file system '
                      'is mounted or checked and has therefore been backed up.')
            for name in (cachepath + '.params', cachepath + '.db'):
                for i in reversed(range(4)):
                    if os.path.exists(name + '.%d' % i):
                        os.rename(name + '.%d' % i, name + '.%d' % (i+1))     
                os.rename(name, name + '.0')
   
    db.execute('ANALYZE')
    db.execute('VACUUM')
    db.close() 

    if options.profile:
        tmp = tempfile.NamedTemporaryFile()
        prof.dump_stats(tmp.name)
        fh = open('s3ql_profile.txt', 'w')
        p = pstats.Stats(tmp.name, stream=fh)
        tmp.close()
        p.strip_dirs()
        p.sort_stats('cumulative')
        p.print_stats(50)
        p.sort_stats('time')
        p.print_stats(50)
        fh.close()

    if operations.encountered_errors:
        raise QuietError('Some errors were encountered while the file system was mounted,\n'
                         'you should run fsck.s3ql and examine ~/.s3ql/mount.log.')


def get_metadata(bucket, cachepath):
    '''Retrieve metadata
    
    Checks:
    - Revision
    - Unclean mounts
    
    Locally cached metadata is used if up-to-date.
    '''
           
    seq_no = get_seq_no(bucket)

    # Check for cached metadata
    db = None
    if os.path.exists(cachepath + '.params'):
        param = pickle.load(open(cachepath + '.params', 'rb'))
        if param['seq_no'] < seq_no:
            log.info('Ignoring locally cached metadata (outdated).')
            param = bucket.lookup('s3ql_metadata')
        else:
            log.info('Using cached metadata.')
            db = Connection(cachepath + '.db')
    else:
        param = bucket.lookup('s3ql_metadata')

    # Check for unclean shutdown
    if param['seq_no'] < seq_no:
        if (bucket.read_after_create_consistent() and
            bucket.read_after_delete_consistent()):
            raise QuietError(textwrap.fill(textwrap.dedent('''\
                It appears that the file system is still mounted somewhere else. If this is not
                the case, the file system may have not been unmounted cleanly and you should try
                to run fsck on the computer where the file system has been mounted most recently.
                ''')))
        else:                
            raise QuietError(textwrap.fill(textwrap.dedent('''\
                It appears that the file system is still mounted somewhere else. If this is not the
                case, the file system may have not been unmounted cleanly or the data from the 
                most-recent mount may have not yet propagated through the backend. In the later case,
                waiting for a while should fix the problem, in the former case you should try to run
                fsck on the computer where the file system has been mounted most recently.
                ''')))
       
    # Check revision
    if param['revision'] < CURRENT_FS_REV:
        raise QuietError('File system revision too old, please run `s3qladm upgrade` first.')
    elif param['revision'] > CURRENT_FS_REV:
        raise QuietError('File system revision too new, please update your '
                         'S3QL installation.')
        
    # Check that the fs itself is clean
    if param['needs_fsck']:
        raise QuietError("File system damaged or not unmounted cleanly, run fsck!")        
    if (time.time() - time.timezone) - param['last_fsck'] > 60 * 60 * 24 * 31:
        log.warn('Last file system check was more than 1 month ago, '
                 'running fsck.s3ql is recommended.')
    
    # Download metadata
    if not db:
        log.info("Downloading & uncompressing metadata...")
        os.close(os.open(cachepath + '.db.tmp', os.O_RDWR | os.O_CREAT | os.O_TRUNC,
                         stat.S_IRUSR | stat.S_IWUSR)) 
        db = Connection(cachepath + '.db.tmp', fast_mode=True)
        with bucket.open_read("s3ql_metadata") as fh:
            restore_metadata(fh, db)
        db.close()
        os.rename(cachepath + '.db.tmp', cachepath + '.db')
        db = Connection(cachepath + '.db')
 
    # Increase metadata sequence no 
    param['seq_no'] += 1
    param['needs_fsck'] = True
    bucket['s3ql_seq_no_%d' % param['seq_no']] = 'Empty'
    pickle.dump(param, open(cachepath + '.params', 'wb'), 2)
    
    return (param, db)


def get_fuse_opts(options):
    '''Return fuse options for given command line options'''

    fuse_opts = [ b"nonempty", b'fsname=%s' % options.storage_url,
                  'subtype=s3ql' ]

    if options.allow_other:
        fuse_opts.append(b'allow_other')
    if options.allow_root:
        fuse_opts.append(b'allow_root')
    if options.allow_other or options.allow_root:
        fuse_opts.append(b'default_permissions')

    return fuse_opts



def parse_args(args):
    '''Parse command line'''

    # Parse fstab-style -o options
    if '--' in args:
        max_idx = args.index('--')
    else:
        max_idx = len(args)
    if '-o' in args[:max_idx]:
        pos = args.index('-o')
        val = args[pos + 1]
        del args[pos]
        del args[pos]
        for opt in reversed(val.split(',')):
            if '=' in opt:
                (key, val) = opt.split('=')
                args.insert(pos, val)
                args.insert(pos, '--' + key)
            else:
                if opt in ('rw', 'defaults', 'auto', 'noauto', 'user', 'nouser', 'dev', 'nodev',
                           'suid', 'nosuid', 'atime', 'diratime', 'exec', 'noexec', 'group',
                           'mand', 'nomand', '_netdev', 'nofail', 'norelatime', 'strictatime',
                           'owner', 'users', 'nobootwait'):
                    continue
                elif opt == 'ro':
                    raise QuietError('Read-only mounting not supported.')
                args.insert(pos, '--' + opt)

    parser = ArgumentParser(
        description="Mount an S3QL file system.")

    parser.add_log('~/.s3ql/mount.log')
    parser.add_cachedir()
    parser.add_authfile()
    parser.add_debug_modules()
    parser.add_quiet()
    parser.add_version()
    parser.add_storage_url()
    
    parser.add_argument("mountpoint", metavar='<mountpoint>',
                        type=(lambda x: x.rstrip('/')),
                        help='Where to mount the file system')
        
    parser.add_argument("--cachesize", type=int, default=102400, metavar='<size>', 
                      help="Cache size in kb (default: 102400 (100 MB)). Should be at least 10 times "
                      "the blocksize of the filesystem, otherwise an object may be retrieved and "
                      "written several times during a single write() or read() operation.")
    parser.add_argument("--max-cache-entries", type=int, default=768, metavar='<num>',
                      help="Maximum number of entries in cache (default: %(default)d). "
                      'Each cache entry requires one file descriptor, so if you increase '
                      'this number you have to make sure that your process file descriptor '
                      'limit (as set with `ulimit -n`) is high enough (at least the number ' 
                      'of cache entries + 100).')
    parser.add_argument("--allow-other", action="store_true", default=False, help=
                      'Normally, only the user who called `mount.s3ql` can access the mount '
                      'point. This user then also has full access to it, independent of '
                      'individual file permissions. If the `--allow-other` option is '
                      'specified, other users can access the mount point as well and '
                      'individual file permissions are taken into account for all users.')
    parser.add_argument("--allow-root", action="store_true", default=False,
                      help='Like `--allow-other`, but restrict access to the mounting '
                           'user and the root user.')
    parser.add_argument("--fg", action="store_true", default=False,
                      help="Do not daemonize, stay in foreground")
    parser.add_argument("--single", action="store_true", default=False,
                      help="Run in single threaded mode. If you don't understand this, "
                           "then you don't need it.")
    parser.add_argument("--upstart", action="store_true", default=False,
                      help="Stay in foreground and raise SIGSTOP once mountpoint "
                           "is up.")
    parser.add_argument("--profile", action="store_true", default=False,
                      help="Create profiling information. If you don't understand this, "
                           "then you don't need it.")
    parser.add_argument("--compress", action="store", default='lzma', metavar='<name>',
                      choices=('lzma', 'bzip2', 'zlib', 'none'),
                      help="Compression algorithm to use when storing new data. Allowed "
                           "values: `lzma`, `bzip2`, `zlib`, none. (default: `%(default)s`)")
    parser.add_argument("--metadata-upload-interval", action="store", type=int,
                      default=24*60*60, metavar='<seconds>',
                      help='Interval in seconds between complete metadata uploads. '
                           'Set to 0 to disable. Default: 24h.')
    parser.add_argument("--threads", action="store", type=int,
                      default=1, metavar='<no>',
                      help='Number of parallel upload threads to use (default: %(default)s).')
    parser.add_argument("--nfs", action="store_true", default=False,
                      help='Support export of S3QL file systems over NFS ' 
                           '(default: %(default)s)')
        
    options = parser.parse_args(args)

    if options.allow_other and options.allow_root:
        parser.error("--allow-other and --allow-root are mutually exclusive.")

    if not options.log and not options.fg:
        parser.error("Please activate logging to a file or syslog, or use the --fg option.")
        
    if options.profile:
        options.single = True

    if options.upstart:
        options.fg = True
        
    if options.metadata_upload_interval == 0:
        options.metadata_upload_interval = None
        
    if options.compress == 'none':
        options.compress = None
            
    upload_manager.MAX_THREADS = options.threads
    
    return options

class MetadataUploadThread(ExceptionStoringThread):
    '''
    Periodically commit dirty inodes.
    '''    
    
    
    def __init__(self, bucket_pool, param, db, interval):
        super(MetadataUploadThread, self).__init__()
        self.bucket_pool = bucket_pool
        self.param = param
        self.db = db
        self.interval = interval
        self.daemon = True
        self.db_mtime = os.stat(db.file).st_mtime 
        self.event = threading.Event()
        self.quit = False
        self.name = 'Metadata-Upload-Thread'
           
    def run_protected(self):
        log.debug('MetadataUploadThread: start')
        
        while True:
            self.event.wait(self.interval)
            self.event.clear()
            
            if self.quit:
                break
            
            with llfuse.lock:
                new_mtime = os.stat(self.db.file).st_mtime 
                if self.db_mtime == new_mtime:
                    log.info('File system unchanged, not uploading metadata.')
                    continue
                
                # We dump to a file first, so that we don't hold the
                # lock for quite so long.
                log.info('Saving metadata...')
                fh = tempfile.TemporaryFile()
                dump_metadata(fh, self.db) 
              
            with self.bucket_pool() as bucket:
                seq_no = get_seq_no(bucket)
                if seq_no != self.param['seq_no']:
                    log.error('Remote metadata is newer than local (%d vs %d), '
                              'refusing to overwrite!', seq_no, self.param['seq_no'])
                    fh.close()
                    continue
                              
                log.info("Compressing & uploading metadata..")
                cycle_metadata(bucket)
                fh.seek(0)
                self.param['last-modified'] = time.time() - time.timezone
                
                # Temporarily decrease sequence no, this is not the final upload
                self.param['seq_no'] -= 1
                with bucket.open_write("s3ql_metadata", self.param) as obj_fh:
                    shutil.copyfileobj(fh, obj_fh)
                self.param['seq_no'] += 1
                
                fh.close()
                self.db_mtime = new_mtime    

        log.debug('MetadataUploadThread: end')    
        
    def stop(self):
        '''Wait for thread to finish, raise any occurred exceptions.
        
        This  method releases the global lock.
        '''
        
        self.quit = True
        self.event.set()
        self.join_and_raise()


if __name__ == '__main__':
    main(sys.argv[1:])
