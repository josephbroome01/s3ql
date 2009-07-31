#!/usr/bin/env python
#
#    Copyright (C) 2008  Nikolaus Rath <Nikolaus@rath.org>
#
#    This program can be distributed under the terms of the GNU LGPL.
#

# Python boto uses several deprecated modules
import warnings
warnings.filterwarnings("ignore", "", DeprecationWarning, "boto")

from getpass  import getpass
import os, sys
import stat
import time
from optparse import OptionParser
from datetime import datetime
from s3ql.common import init_logging, get_credentials, get_cachedir, get_dbfile
from s3ql.cursor_manager import CursorManager
import logging

from s3ql import s3, fsck

# 
# Parse Command line
#
parser = OptionParser(
    usage="%prog  [options] <bucketname>\n"
    "%prog --help",
    description="Checks and repairs an s3ql filesystem.")

parser.add_option("--awskey", type="string",
                  help="Amazon Webservices access key to use. If not "
                  "specified, tries to read ~/.awssecret.")
parser.add_option("--encrypt", action="store_true", default=None,
                  help="Checks an encrypted filesystem")
parser.add_option("--checkonly", action="store_true", default=None,
                  help="Only check, do not fix errors.")
parser.add_option("--force", action="store_true", default=None,
                  help="Force checking even if current metadata is not available")
parser.add_option("--debug", action="append", 
                  help="Activate debugging output from specified facility. Valid facility names "
                        "are: fsck, s3, frontend. "
                        "This option can be specified multiple times.")


(options, pps) = parser.parse_args()

if not len(pps) == 1:
    parser.error("bucketname not specificed")
bucketname = pps[0]
dbfile = get_dbfile(bucketname)
cachedir = get_cachedir(bucketname)

# Activate logging
init_logging(True, False, options.debug)
log = logging.getLogger("frontend")


#
# Read password(s)
#
(awskey, awspass) = get_credentials(options.awskey)
if options.encrypt:
    if sys.stdin.isatty():
        options.encrypt = getpass("Enter encryption password: ")
    else:
        options.encrypt = sys.stdin.readline().rstrip()



#
# Open bucket
#
conn = s3.Connection(awskey, awspass, options.encrypt)
if not conn.bucket_exists(bucketname):
    print >> sys.stderr, "Bucket does not exists."
    sys.exit(1)
bucket = conn.get_bucket(bucketname)



#
# Check if fs is dirty and we lack metadata
#
if bucket["s3ql_dirty"] == "yes" and \
        (not os.path.exists(cachedir) or
         not os.path.exists(dbfile)):
    if not options.force:
        print >> sys.stderr, """
Filesystem is marked dirty, but there is no cached metadata available.
You should run fsck.s3ql on the system and user id where the
filesystem has been mounted most recently.

This message can also appear if changes from the last mount have not
had sufficient time to propagate in S3. In this case this message
should disappear when retrying later.

Use --force if you want to force a check on this machine. This
may result in dataloss.
"""
        sys.exit(1)
    else:
        print "Dirty filesystem and no local metadata - continuing anyway."


#
# Init cache
#
if not os.path.exists(cachedir):
    os.mkdir(cachedir, 0700)

#
# Init metadata
#
if os.path.exists(dbfile):
    # Compare against online metadata
    local = datetime.fromtimestamp(os.stat(dbfile).st_mtime)
    remote = bucket.lookup_key("s3ql_metadata").last_modified

    if remote > local:
        # remote metadata is newer
        if not options.force:
            print >> sys.stderr, """
The metadata stored with the filesystem is never than the
locally cached data. Probably the filesystem has been mounted
and changed on a different system. You should run fsck.s3ql
on that system.

Use --force if you want to force a check on this machine using the
cached data. This will result in dataloss.

You can also remove the local cache before calling fsck.s3ql to
perform the check with the newer metadata stored on S3. This
may also result in dataloss.
"""
            sys.exit(1)
        else:
            print "Remote metadata is never than cache - continuing anyway."

    # Continue with local metadata from here

else:
    # Download remote metadata
    os.mknod(dbfile, 0600 | stat.S_IFREG)
    bucket.fetch_to_file("s3ql_metadata", dbfile)


cursor = CursorManager(dbfile)

# Check filesystem revision
(rev,) = cursor.execute("SELECT version FROM parameters").next()
if rev < 1:
    print >> sys.stderr, "This version of S3QL is too old for the filesystem!\n"
    sys.exit(1)


# Now we can check
fsck.fsck(conn, cachedir, bucket, options.checkonly)


if not options.checkonly:
    # Commit metadata and mark fs as clean, both internally and as object
    log.info("Committing data to S3...")
    cursor.execute("UPDATE parameters SET needs_fsck=?, last_fsck=?, "
                   "mountcnt=?", (False, time.time(), 0))

    cursor.execute("VACUUM")
    log.debug("Uploading database..")
    if bucket.has_key("s3ql_metadata_bak_2"):
        bucket.copy("s3ql_metadata_bak_2", "s3ql_metadata_bak_3")
    if bucket.has_key("s3ql_metadata_bak_1"):
        bucket.copy("s3ql_metadata_bak_1", "s3ql_metadata_bak_2")
    bucket.copy("s3ql_metadata", "s3ql_metadata_bak_1")
    bucket.store_from_file("s3ql_metadata", dbfile)
    bucket.store("s3ql_dirty", "no")

    os.unlink(dbfile)
    os.rmdir(cachedir)
