'''
mkfs.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function, absolute_import

import sys
import os
from getpass import getpass
import shutil
from optparse import OptionParser
import logging
from s3ql import mkfs, CURRENT_FS_REV
from s3ql.common import (init_logging_from_options, get_backend, get_bucket_home,
                         QuietError, dump_metadata)
import s3ql.database as dbcm
from s3ql.backends.boto.s3.connection import Location
from s3ql.backends import s3
import time
import tempfile

log = logging.getLogger("mkfs")

def parse_args(args):

    parser = OptionParser(
        usage="%prog  [options] <storage-url>\n" \
            "       %prog --help",
        description="Initializes an S3QL file system")

    parser.add_option("--s3-location", type="string", default='EU',
                      help="Specify storage location for new bucket. Allowed values: EU,"
                           'us-west-1, or us-standard. The later is not recommended, please '
                           'refer to the FAQ at http://code.google.com/p/s3ql/ for more information.')
    parser.add_option("--homedir", type="string",
                      default=os.path.expanduser("~/.s3ql"),
                      help='Directory for log files, cache and authentication info. '
                      'Default: ~/.s3ql')
    parser.add_option("-L", type="string", default='', help="Filesystem label",
                      dest="label")
    parser.add_option("--blocksize", type="int", default=10240,
                      help="Maximum block size in KB (default: %default)")
    parser.add_option("--plain", action="store_true", default=False,
                      help="Create unencrypted file system.")
    parser.add_option("--debug", action="append",
                      help="Activate debugging output from specified module. Use 'all' "
                           "to get debug messages from all modules. This option can be "
                           "specified multiple times.")
    parser.add_option("--quiet", action="store_true", default=False,
                      help="Be really quiet")


    (options, pps) = parser.parse_args(args)

    if options.s3_location not in ('EU', 'us-west-1', 'us-standard'):
        parser.error("Invalid S3 storage location. Allowed values: EU, us-west-1, us-standard")

    if options.s3_location == 'us-standard':
        options.s3_location = Location.DEFAULT

    if len(pps) != 1:
        parser.error("Incorrect number of arguments.")
    options.storage_url = pps[0]

    return options

def main(args=None):

    if args is None:
        args = sys.argv[1:]

    options = parse_args(args)
    init_logging_from_options(options, 'mkfs.log')

    with get_backend(options) as (conn, bucketname):
        if conn.bucket_exists(bucketname):
            raise QuietError("Bucket already exists!\n"
                             "(you can delete an existing bucket with tune.s3ql --delete)\n")

        if isinstance(conn, s3.Connection):
            bucket = conn.create_bucket(bucketname, location=options.s3_location)
        else:
            bucket = conn.create_bucket(bucketname)

        if not options.plain:
            if sys.stdin.isatty():
                wrap_pw = getpass("Enter encryption password: ")
                if not wrap_pw == getpass("Confirm encryption password: "):
                    raise QuietError("Passwords don't match.")
            else:
                wrap_pw = sys.stdin.readline().rstrip()

            # Generate data encryption passphrase
            log.info('Generating random encryption key...')
            fh = open('/dev/urandom', "rb", 0) # No buffering
            data_pw = fh.read(32)
            fh.close()

            bucket.passphrase = wrap_pw
            bucket['s3ql_passphrase'] = data_pw
            bucket.passphrase = data_pw

        # Setup database
        home = get_bucket_home(options.storage_url, options.homedir)

        # There can't be a corresponding bucket, so we can safely delete
        # these files.
        if os.path.exists(home + '.db'):
            os.unlink(home + '.db')
        if os.path.exists(home + '-cache'):
            shutil.rmtree(home + '-cache')

        try:
            log.info('Creating metadata tables...')
            dbcm.init(home + '.db')
            mkfs.setup_tables()
            mkfs.create_indices()
            mkfs.init_tables()

            param = dict()
            param['revision'] = CURRENT_FS_REV
            param['seq_no'] = 0
            param['label'] = options.label
            param['blocksize'] = options.blocksize * 1024
            param['needs_fsck'] = False
            param['DB-Format'] = 'dump'
            param['last_fsck'] = time.time() - time.timezone
            bucket.store('s3ql_seq_no_%d' % param['seq_no'], 'Empty')

            fh = tempfile.TemporaryFile()
            dump_metadata(fh)
            fh.seek(0)
            log.info("Uploading database..")
            bucket.store_fh("s3ql_metadata", fh, param)
            fh.close()

        finally:
            os.unlink(home + '.db')


if __name__ == '__main__':
    main(sys.argv[1:])
