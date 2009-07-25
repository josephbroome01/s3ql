#!/usr/bin/env python
#
#    Copyright (C) 2008  Nikolaus Rath <Nikolaus@rath.org>
#
#    This program can be distributed under the terms of the GNU LGPL.
#

# Python boto uses several deprecated modules
import warnings
warnings.filterwarnings("ignore", "", DeprecationWarning, "boto")

from optparse import OptionParser
from getpass  import getpass
from s3ql import fs, s3
from s3ql.s3cache import S3Cache
from s3ql.common import init_logging, get_credentials, get_cachedir, get_dbfile, MyCursor
import sys
import os
import stat
import apsw
import logging

#
# Parse command line
#
parser = OptionParser(
    usage="%prog  [options] <bucketname> <mountpoint>\n"
          "       %prog --help",
    description="Mounts an amazon S3 bucket as a filesystem.")

parser.add_option("--awskey", type="string",
                  help="Amazon Webservices access key to use. The password is "
                  "read from stdin. If this option is not specified, both access key "
                  "and password are read from ~/.awssecret (separated by newlines).")
parser.add_option("--debug", action="append", 
                  help="Activate debugging output from specified facility. Valid facility names "
                        "are: fs, fs.fuse, s3, frontend. "
                        "This option can be specified multiple times.")
parser.add_option("--s3timeout", type="int", default=50,
                  help="Maximum time to wait for propagation in S3 (default: %default)")
parser.add_option("--allow_others", action="store_true", default=False,
                  help="Allow others users to access the filesystem")
parser.add_option("--allow_root", action="store_true", default=False,
                  help="Allow root to access the filesystem")
parser.add_option("--encrypt", action="store_true", default=None,
                  help="Mount an encrypted filesystem")
parser.add_option("--nonempty", action="store_true", default=False,
                  help="Allow mount if even mount point is not empty")
parser.add_option("--fg", action="store_true", default=False,
                  help="Do not daemonize, stay in foreground")
parser.add_option("--cachesize", type="int", default=50,
                  help="Cache size in MB (default: 50)")
parser.add_option("--single", action="store_true", default=False,
                  help="Single threaded operation only")

(options, pps) = parser.parse_args()

#
# Verify parameters
#
if not len(pps) == 2:
    parser.error("Wrong number of parameters")
bucketname = pps[0]
mountpoint = pps[1]


#
# Read password(s)
#
(awskey, awspass) = get_credentials(options.awskey)

if options.encrypt:
    if sys.stdin.isatty():
        options.encrypt = getpass("Enter encryption password: ")
        if not options.encrypt == getpass("Confirm encryption password: "):
            print >>sys.stderr, "Passwords don't match."
            sys.exit(1)
    else:
        options.encrypt = sys.stdin.readline().rstrip()


#
# Pass on fuse options
#
fuse_opts = dict()
if options.allow_others:
    fuse_opts["allow_others"] = True
if options.allow_root:
    fuse_opts["allow_root"] = True
if options.nonempty:
    fuse_opts["nonempty"] = True
if options.single:
    fuse_opts["nothreads"] = True
if options.fg:
    fuse_opts["foreground"] = True

# Activate logging
init_logging(options.fg, options.quiet, options.debug)
log = logging.getLogger("frontend")

#
# Connect to S3
#
conn = s3.Connection(awskey, awspass, options.encrypt)
bucket = conn.get_bucket(bucketname)

cachedir = get_cachedir(bucketname)
dbfile = get_dbfile(bucketname)

#
# Check consistency
#
log.debug("Checking consistency...")
if bucket["s3ql_dirty"] != "no":
    print >> sys.stderr, \
        "Metadata is dirty! Either some changes have not yet propagated\n" \
        "through S3 or the filesystem has not been umounted cleanly. In\n" \
        "the later case you should run s3fsck on the system where the\n" \
        "filesystem has been mounted most recently!\n"
    sys.exit(1)

# Init cache
if os.path.exists(cachedir) or os.path.exists(dbfile):
    print >> sys.stderr, \
        "Local cache files already exists! Either you are trying to\n" \
        "to mount a filesystem that is already mounted, or the filesystem\n" \
        "has not been umounted cleanly. In the later case you should run\n" \
        "s3fsck.\n"
    sys.exit(1)

# Init cache + get metadata
try:
    log.debug("Downloading metadata...")
    os.mknod(dbfile, 0600 | stat.S_IFREG)
    os.mkdir(cachedir, 0700)
    bucket.fetch_to_file("s3ql_metadata", dbfile)

    # Check that the fs itself is clean
    conn = apsw.Connection(dbfile)
    cur = MyCursor(conn.cursor())
    if cur.get_val("SELECT needs_fsck FROM parameters"):
        print >> sys.stderr, "Filesystem damaged, run s3fsk!\n"
        sys.exit(1)

    #
    # Start server
    #
    cache =  S3Cache(bucket, cachedir, options.cachesize * 1024 * 1024,
                     cur.get_val("SELECT blocksize FROM parameters"))
    server = fs.Server(cache, dbfile)
    server.main(mountpoint, **fuse_opts)
    cache.close(cur)


    # Upload database
    cur.execute("VACUUM")
    log.debug("Uploading database..")
    if bucket.has_key("s3ql_metadata_bak_2"):
        bucket.copy("s3ql_metadata_bak_2", "s3ql_metadata_bak_3");
    if bucket.has_key("s3ql_metadata_bak_1"):
        bucket.copy("s3ql_metadata_bak_1", "s3ql_metadata_bak_2");
    bucket.copy("s3ql_metadata", "s3ql_metadata_bak_1");
    bucket.store_from_file("s3ql_metadata", dbfile)
    bucket.store("s3ql_dirty", "no")

# Remove database
finally:
    # Ignore exceptions when cleaning up
    try:
        log.debug("Cleaning up...")
        os.unlink(dbfile)
        os.rmdir(cachedir)
    except:
        pass
