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
from s3ql import fs, CURRENT_FS_REV
from s3ql.backends import s3
from s3ql.daemonize import daemonize
from s3ql.backends.common import (ChecksumError)
from s3ql.common import (init_logging_from_options, get_backend, get_bucket_home,
                         QuietError, unlock_bucket, get_stdout_handler,
                         cycle_metadata, dump_metadata, restore_metadata)
import s3ql.database as dbcm
import llfuse
import tempfile
import textwrap
import os
import stat
import time
import threading
import logging
import cPickle as pickle

__all__ = [ 'main' ]

log = logging.getLogger("mount")

def main(args=None):
    '''Mount S3QL file system'''

    try:
        import psyco
        psyco.profile()
    except ImportError:
        pass

    if args is None:
        args = sys.argv[1:]

    options = parse_args(args)
    init_logging_from_options(options, 'mount.log')

    if not os.path.exists(options.mountpoint):
        raise QuietError('Mountpoint does not exist.')

    if options.profile:
        import cProfile
        import pstats
        prof = cProfile.Profile()

    with get_backend(options) as (conn, bucketname):

        if not bucketname in conn:
            raise QuietError("Bucket does not exist.")
        bucket = conn.get_bucket(bucketname, compression=options.compress)

        # Unlock bucket
        try:
            unlock_bucket(options, bucket)
        except ChecksumError:
            raise QuietError('Checksum error - incorrect password?')

        # Get paths
        home = get_bucket_home(options.storage_url, options.homedir)

        # Check for unclean shutdown on this computer
        if (os.path.exists(home + '.db')
            or os.path.exists(home + '.params')
            or os.path.exists(home + '-cache')):
            raise QuietError('Local cache files exist, file system has not been unmounted\n'
                             'cleanly. You need to run fsck.s3ql.')

        lock = threading.Lock()
        fuse_opts = get_fuse_opts(options)

        # Get file system parameters
        log.info('Getting file system parameters..')
        seq_nos = [ int(x[len('s3ql_seq_no_'):]) for x in bucket.list('s3ql_seq_no_') ]
        if not seq_nos:
            raise QuietError('Old file system revision, please run tune.s3ql --upgrade first.')
        seq_no = max(seq_nos)
        param = bucket.lookup('s3ql_metadata')

        # Check revision
        if param['revision'] < CURRENT_FS_REV:
            raise QuietError('File system revision too old, please run tune.s3ql --upgrade first.')
        elif param['revision'] > CURRENT_FS_REV:
            raise QuietError('File system revision too new, please update your '
                             'S3QL installation.')

        # Check that the fs itself is clean
        if param['needs_fsck']:
            raise QuietError("File system damaged, run fsck!")

        # Check for unclean shutdown on other computer
        if param['seq_no'] < seq_no:
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
        elif param['seq_no'] > seq_no:
            raise RuntimeError('param[seq_no] > seq_no, this should not happen.')

        if (time.time() - time.timezone) - param['last_fsck'] > 60 * 60 * 24 * 31:
            log.warn('Last file system check was more than 1 month ago, '
                     'running fsck.s3ql is recommended.')

        # Download metadata
        log.info("Downloading & uncompressing metadata...")
        fh = os.fdopen(os.open(home + '.db', os.O_RDWR | os.O_CREAT,
                               stat.S_IRUSR | stat.S_IWUSR), 'w+b')
        if param['DB-Format'] == 'dump':
            fh.close()
            dbcm.init(home + '.db')
            fh = tempfile.TemporaryFile()
            bucket.fetch_fh("s3ql_metadata", fh)
            fh.seek(0)
            log.info('Reading metadata...')
            restore_metadata(fh)
            fh.close()
        elif param['DB-Format'] == 'sqlite':
            bucket.fetch_fh("s3ql_metadata", fh)
            fh.close()
            dbcm.init(home + '.db')
        else:
            raise RuntimeError('Unsupported DB format: %s' % param['DB-Format'])

        # Increase metadata sequence no
        param['seq_no'] += 1
        bucket.store('s3ql_seq_no_%d' % param['seq_no'], 'Empty')
        for i in seq_nos:
            if i < param['seq_no'] - 5:
                try:
                    del bucket['s3ql_seq_no_%d' % i ]
                except KeyError:
                    pass # Key list may not be up to date

        # Save parameters
        pickle.dump(param, open(home + '.params', 'wb'), 2)

        operations = fs.Operations(bucket, cachedir=home + '-cache', lock=lock,
                                   blocksize=param['blocksize'],
                                   cachesize=options.cachesize * 1024)

        log.info('Mounting filesystem...')
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

            if options.profile:
                prof.runcall(llfuse.main, options.single)
            else:
                llfuse.main(options.single)

        finally:
            llfuse.close()

        if options.compress == 'LZMA':
            param['DB-Format'] = 'dump'
            log.info("Saving metadata...")
            fh = tempfile.TemporaryFile()
            dump_metadata(fh)
            fh.seek(0)
        else:
            param['DB-Format'] = 'sqlite'
            dbcm.execute('VACUUM')
            fh = open(home + '.db', 'rb')

        if operations.encountered_errors:
            # Upload most recent version, but mark as damaged and still outdated 
            # since we keep the local version
            param['needs_fsck'] = True
            param['seq_no'] -= 1

        log.info("Compressing & uploading metadata..")
        cycle_metadata(bucket)
        bucket.store_fh("s3ql_metadata", fh, param)
        fh.close()

        if not operations.encountered_errors:
            log.debug("Cleaning up...")
            os.unlink(home + '.db')
            os.unlink(home + '.params')

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
                if opt in ('rw', 'defaults', 'auto', 'noauto', 'user', 'nouser', 'dev', 'nodev',
                           'suid', 'nosuid', 'atime', 'diratime', 'exec', 'noexec', 'group',
                           'mand', 'nomand', '_netdev', 'nofail', 'norelatime', 'strictatime',
                           'owner', 'users', 'nobootwait'):
                    continue
                elif opt == 'ro':
                    raise QuietError('Read-only mounting not supported.')
                args.insert(pos, '--' + opt)

    parser = OptionParser(
        usage="%prog  [options] <storage-url> <mountpoint>\n"
              "       %prog --help",
        description="Mount an S3QL file system.")

    parser.add_option("--homedir", type="string",
                      default=os.path.expanduser("~/.s3ql"),
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
                      help='Like `--allow_other`, but restrict access to the mounting '
                           'user and the root user.')
    parser.add_option("--fg", action="store_true", default=False,
                      help="Do not daemonize, stay in foreground")
    parser.add_option("--single", action="store_true", default=False,
                      help="Run in single threaded mode. If you don't understand this, "
                           "then you don't need it.")
    parser.add_option("--profile", action="store_true", default=False,
                      help="Create profiling information. If you don't understand this, "
                           "then you don't need it.")
    parser.add_option("--compress", action="store", default='lzma',
                      choices=('lzma', 'bzip2', 'zlib', 'none'),
                      help="Compression algorithm to use when storing new data. Allowed "
                           "values: LZMA, BZIP2, ZLIB, None. (default: LZMA)")
    (options, pps) = parser.parse_args(args)

    #
    # Verify parameters
    #
    if len(pps) != 2:
        parser.error("Incorrect number of arguments.")
    options.storage_url = pps[0]
    options.mountpoint = pps[1]

    if options.allow_other and options.allow_root:
        parser.error("--allow-other and --allow-root are mutually exclusive.")

    if options.profile:
        options.single = True

    if options.compress == 'none':
        options.compress = None

    return options


if __name__ == '__main__':
    main(sys.argv[1:])
