'''
mount.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function, absolute_import

# We can't use relative imports because this file may
# be directly executed.
import sys
from optparse import OptionParser
from s3ql import fs, mkfs
from s3ql.backends import s3
from s3ql.daemonize import daemonize
from s3ql.backends.common import ChecksumError, COMPRESS_BZIP2, COMPRESS_LZMA, COMPRESS_ZLIB
from s3ql.block_cache import BlockCache
from s3ql.common import (init_logging_from_options, get_backend, get_cachedir, get_dbfile,
                         QuietError, unlock_bucket, get_parameters, get_stdout_handler,
                         get_lockfile, cycle_metadata, dump_metadata, restore_metadata)
from s3ql.database import ConnectionManager
import llfuse
import tempfile
import textwrap
import os
import stat
import threading
import logging
import cPickle as pickle

__all__ = [ 'main' ]

log = logging.getLogger("mount")

def main(args=None):
    '''Mount S3QL file system'''

    if args is None:
        args = sys.argv[1:]

    options = parse_args(args)
    init_logging_from_options(options, 'mount.log')

    if not os.path.exists(options.mountpoint):
        raise QuietError('Mountpoint does not exist.')

    with get_backend(options) as (conn, bucketname):

        if not bucketname in conn:
            raise QuietError("Bucket does not exist.")
        bucket = conn.get_bucket(bucketname, compression=options.compression)

        # Check that the bucket is in the correct location
        if isinstance(bucket, s3.Bucket):
            with bucket._get_boto() as boto:
                if boto.get_location() not in ('EU', 'us-west-1'):
                    log.warn('Note: Your bucket is located in the US-Standard storage region.\n'
                             'Under very rare circumstances this can lead to problems, please\n'
                             'refer to the FAQ at http://code.google.com/p/s3ql/ for details.\n'
                             '(you can relocate your bucket with tune.s3ql --copy if desired)')
        try:
            unlock_bucket(bucket)
        except ChecksumError:
            raise QuietError('Checksum error - incorrect password?')

        dbfile = get_dbfile(options.storage_url, options.homedir)
        cachedir = get_cachedir(options.storage_url, options.homedir)
        lockfile = get_lockfile(options.storage_url, options.homedir)

        if os.path.exists(lockfile):
            with open(lockfile, 'r') as fh:
                pid = int(fh.read().strip())
            try:
                os.kill(pid, 0)
            except OSError:
                log.debug('Removing stale lock file.')
            else:
                raise QuietError('Bucket already mounted by PID %d' % pid)

        # Note that we have to update the PID after daemonizing
        with open(lockfile, 'w') as fh:
            fh.write('%d\n' % os.getpid())

        # Get most-recent s3ql_parameters object and check fs revision
        log.info('Getting file system parameters..')
        param = get_parameters(bucket)

        if os.path.exists(dbfile) or os.path.exists(cachedir):
            raise QuietError('Local cache files exist, file system has not been unmounted\n'
                             'cleanly. You need to run fsck.s3ql.')

        log.info("Downloading metadata...")
        fh = os.fdopen(os.open(dbfile, os.O_RDWR | os.O_CREAT,
                              stat.S_IRUSR | stat.S_IWUSR), 'w+b')
        fh.close()
        dbcm = ConnectionManager(dbfile)
        mkfs.setup_tables(dbcm)
        fh = tempfile.TemporaryFile()
        bucket.fetch_fh("s3ql_metadata", fh)
        fh.seek(0)
        restore_metadata(dbcm, fh)
        fh.close()

        mountcnt_db = dbcm.get_val('SELECT mountcnt FROM parameters')
        if mountcnt_db < param['mountcnt']:
            os.unlink(dbfile)
            if isinstance(bucket, s3.Bucket):
                raise QuietError(textwrap.fill(textwrap.dedent('''
                    It appears that the file system is still mounted somewhere else. If this is not
                    the case, the file system may have not been unmounted cleanly or the data from
                    the most-recent mount may have not yet propagated through S3. In the later case,
                    waiting for a while should fix the problem, in the former case you should try to
                    run fsck on the computer where the file system has been mounted most recently.
                    ''')))
            else:
                raise QuietError(textwrap.fill(textwrap.dedent('''
                    It appears that the file system is still mounted somewhere else. If this is not
                    the case, the file system may have not been unmounted cleanly and you should try
                    to run fsck on the computer where the file system has been mounted most recently.
                    ''')))
        elif mountcnt_db > param['mountcnt']:
            os.unlink(dbfile)
            raise RuntimeError('mountcnt_db > param[mountcnt], this should not happen.')

        os.mkdir(cachedir, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        cache = BlockCache(bucket, cachedir, int(options.cachesize * 1024), dbcm)

        # Check that the fs itself is clean
        if dbcm.get_val("SELECT needs_fsck FROM parameters"):
            raise QuietError("File system damaged, run fsck!")

        # Start server
        log.info('Analyzing metadata...')
        dbcm.execute('ANALYZE')

        param['mountcnt'] += 1
        dbcm.execute('UPDATE parameters SET mountcnt=mountcnt+1')
        bucket.store('s3ql_parameters_%d' % param['mountcnt'],
                     pickle.dumps(param, 2))

        try:
            try:
                operations = run_server(conn, bucket, cache, dbcm, options, lockfile)
            finally:
                log.info('Clearing cache...')
                cache.clear()
        finally:
            fh = tempfile.TemporaryFile()
            dump_metadata(dbcm, fh)
            fh.seek(0)
            log.info("Uploading database..")
            cycle_metadata(bucket)
            bucket.store_fh("s3ql_metadata", fh)
            fh.close()

    # Remove database
    log.debug("Cleaning up...")
    os.unlink(dbfile)
    os.rmdir(cachedir)
    os.unlink(lockfile)

    if operations.encountered_errors:
        raise QuietError('Some errors were encountered while the file system was mounted.\n'
                         'Please examine the log files for more information.')


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

def run_server(conn, bucket, cache, dbcm, options, lockfile):
    '''Start FUSE server and run main loop
    
    Returns the used `Operations` instance so that the `encountered_errors`
    attribute can be checked.
    '''

    if options.profile:
        import cProfile
        import pstats
        prof = cProfile.Profile()

    log.info('Mounting filesystem...')
    lock = threading.Lock()
    fuse_opts = get_fuse_opts(options)

    operations = fs.Operations(cache, dbcm, lock)
    llfuse.init(operations, options.mountpoint, fuse_opts, lock)
    try:
        if not options.fg:
            conn.prepare_fork()
            me = threading.current_thread()
            for t in threading.enumerate():
                if t is me:
                    continue
                log.warn('Waiting for thread %s', t)
                t.join()
            if get_stdout_handler() is not None:
                logging.getLogger().removeHandler(get_stdout_handler())
            daemonize(options.homedir)
            conn.finish_fork()

            # Update lock file
            with open(lockfile, 'w') as fh:
                fh.write('%d\n' % os.getpid())

        if options.profile:
            prof.runcall(llfuse.main, options.single)
        else:
            llfuse.main(options.single)

    finally:
        llfuse.close()

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

    return operations

def parse_args(args):
    '''Parse command line
    
    This function writes to stdout/stderr and may call `system.exit()` instead 
    of throwing an exception if it encounters errors.
    '''

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
                if opt == 'rw':
                    continue
                elif opt == 'ro':
                    raise QuietError('Read-only mounting not supported.')
                args.insert(pos, '--' + opt)

    if 'HOME' in os.environ:
        default_home = os.path.join(os.environ["HOME"], ".s3ql")
    else:
        default_home = None

    parser = OptionParser(
        usage="%prog  [options] <storage-url> <mountpoint>\n"
              "       %prog --help",
        description="Mount an S3QL file system.")

    parser.add_option("--homedir", type="string",
                      default=default_home,
                      help='Directory for log files, cache and authentication info. '
                      'Default: ~/.s3ql')
    parser.add_option("--cachesize", type="int", default=102400,
                      help="Cache size in kb (default: 102400 (100 MB)). Should be at least 10 times "
                      "the blocksize of the filesystem, otherwise an object may be retrieved and "
                      "written several times during a single write() or read() operation.")
    parser.add_option("--debug", action="append",
                      help="Activate debugging output from specified module. Use 'all' "
                           "to get debug messages from all modules. This option can be "
                           "specified multiple times.")
    parser.add_option("--quiet", action="store_true", default=False,
                      help="Be really quiet")
    parser.add_option("--allow-other", action="store_true", default=False, help=
                      'Normally, only the user who called `mount.s3ql` can access the mount '
                      'point. This user then also has full access to it, independent of '
                      'individual file permissions. If the `--allow-other` option is '
                      'specified, other users can access the mount point as well and '
                      'individual file permissions are taken into account for all users.')
    parser.add_option("--allow-root", action="store_true", default=False,
                      help='Like `--allow_other`, but also allow the root user '
                           'to access the mount point.')
    parser.add_option("--fg", action="store_true", default=False,
                      help="Do not daemonize, stay in foreground")
    parser.add_option("--single", action="store_true", default=False,
                      help="Run in single threaded mode. If you don't understand this, "
                           "then you don't need it.")
    parser.add_option("--profile", action="store_true", default=False,
                      help="Create profiling information. If you don't understand this, "
                           "then you don't need it.")
    parser.add_option("--bzip2", action="store_true", default=False,
                      help="Use bzip2 instead of LZMA algorithm for compressing new blocks.")
    parser.add_option("--zlib", action="store_true", default=False,
                      help="Use zlib instead of LZMA algorithm for compressing new blocks.")
    (options, pps) = parser.parse_args(args)

    #
    # Verify parameters
    #
    if len(pps) != 2:
        parser.error("Incorrect number of arguments.")
    options.storage_url = pps[0]
    options.mountpoint = pps[1]

    if options.zlib and options.bzip2:
        parser.error("--bzip2 and --zlib are mutually exclusive.")
    elif options.zlib:
        options.compression = COMPRESS_ZLIB
    elif options.bzip2:
        options.compression = COMPRESS_BZIP2
    else:
        options.compression = COMPRESS_LZMA

    if options.profile:
        options.single = True

    if options.homedir is None:
        raise QuietError('--homedir not specified and $HOME environment variable not set,\n'
                         'can not come up with a sensible default.')

    return options

if __name__ == '__main__':
    main(sys.argv[1:])
