'''
$Id$

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function, absolute_import
from optparse import OptionParser
from .. import s3, mkfs, fsck
from .mount import run_server, add_common_mount_opts
from ..common import init_logging_from_options, QuietError
from ..database import ConnectionManager
from ..s3cache import SynchronizedS3Cache
from time import sleep
import logging
import os
import sys
import tempfile


log = logging.getLogger("mount")

def main(args):
    '''Run main program.
    
    This function writes to stdout/stderr and calls `system.exit()` instead
    of returning.
    '''

    options = parse_args(args)
    init_logging_from_options(options)

    # Check mountpoint
    if not os.path.exists(options.mountpoint):
        raise QuietError('Mountpoint does not exist.\n')

    # Initialize local bucket and database
    bucket = s3.LocalConnection().create_bucket('foobar', 'brazl')
    dbfile = tempfile.NamedTemporaryFile()

    dbcm = ConnectionManager(dbfile.name, initsql='PRAGMA temp_store = 2; PRAGMA synchronous = off')
    with dbcm() as conn:
        mkfs.setup_db(conn, options.blocksize * 1024)
    log.debug("Temporary database in " + dbfile.name)

    # Create cache directory
    cachedir = tempfile.mkdtemp() + b'/'

    # Run server
    options.s3timeout = s3.LOCAL_PROP_DELAY * 1.1
    options.bucketname = ':local:'
    # TODO: We should run multithreaded at some point
    cache = SynchronizedS3Cache(bucket, cachedir, int(options.cachesize * 1024), dbcm,
                                timeout=options.s3timeout)
    try:
        operations = run_server(bucket, cache, dbcm, options)
    finally:
        log.info('Clearing cache...')
        cache.clear()
    if operations.encountered_errors:
        log.warn('Some errors occured while handling requests. '
                 'Please examine the logs for more information.')

    # We have to make sure that all changes have been committed by the
    # background threads
    sleep(s3.LOCAL_PROP_DELAY * 1.1)

    # Do fsck
    if options.fsck:
        fsck.fsck(dbcm, cachedir, bucket)
        if fsck.found_errors:
            log.warn("fsck found errors")


    os.rmdir(cachedir)

    # Kill bucket
    del s3.local_buckets['foobar']

    if operations.encountered_errors or fsck.found_errors:
        raise QuietError(1)



def parse_args(args):
    '''Parse command line
    
    This function writes to stdout/stderr and may call `system.exit()` instead 
    of throwing an exception if it encounters errors.
    '''

    parser = OptionParser(
        usage="%prog  [options] <mountpoint>\n"
              "       %prog --help",
        description="Emulates S3QL filesystem using in-memory storage"
        "instead of actually connecting to S3. Only for testing purposes.")

    add_common_mount_opts(parser)

    parser.add_option("--blocksize", type="int", default=1,
                      help="Maximum size of s3 objects in KB (default: %default)")
    parser.add_option("--fsck", action="store_true", default=False,
                      help="Runs fsck after the filesystem is unmounted.")
    parser.add_option("--cachesize", type="int", default=10,
                      help="Cache size in kb (default: %default). Should be at least 10 times "
                      "the blocksize of the filesystem, otherwise an object may be retrieved and "
                      "written several times during a single write() or read() operation.")

    (options, pps) = parser.parse_args(args)

    #
    # Verify parameters
    #
    if not len(pps) == 1:
        parser.error("Wrong number of parameters")
    options.mountpoint = pps[0]

    return options

if __name__ == '__main__':
    main(sys.argv[1:])
