'''
$Id$

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function, absolute_import

import os
import stat 
import time
from optparse import OptionParser
from s3ql.common import (init_logging_from_options, get_credentials, get_cachedir, get_dbfile,
                         unlock_bucket, QuietError)
from s3ql.database import WrappedConnection
import logging
from s3ql import s3, fsck
import sys
import apsw

  
def parse_args(args):
    
    parser = OptionParser(
        usage="%prog  [options] <bucketname>\n"
        "%prog --help",
        description="Checks and repairs an s3ql filesystem.")
    
    parser.add_option("--awskey", type="string",
                      help="Amazon Webservices access key to use. If not "
                      "specified, tries to read ~/.awssecret or the file given by --credfile.")
    parser.add_option("--debuglog", type="string",
                      help="Write debugging information in specified file.")
    parser.add_option("--credfile", type="string", default=os.environ["HOME"].rstrip("/") 
                      + "/.awssecret",
                      help='Try to read AWS access key and key id from this file. '
                      'The file must be readable only be the owner and should contain '
                      'the key id and the secret key separated by a newline. '
                      'Default: ~/.awssecret')
    parser.add_option("--cachedir", type="string", default=os.environ["HOME"].rstrip("/") + "/.s3ql",
                      help="Specifies the directory for cache files. Different S3QL file systems "
                      '(i.e. located in different S3 buckets) can share a cache location, even if ' 
                      'they are mounted at the same time. '
                      'You should try to always use the same location here, so that S3QL can detect '
                      'and, as far as possible, recover from unclean unmounts. Default is ~/.s3ql.')
    parser.add_option("--checkonly", action="store_true", default=None,
                      help="Only check, do not fix errors.")
    parser.add_option("--force-remote", action="store_true", default=False,
                      help="Force checking even if local metadata is not available.")
    parser.add_option("--force-old", action="store_true", default=False,
                      help="Force checking even if metadata is outdated.")
    parser.add_option("--force-local", action="store_true", default=False,
                      help="Force checking even if local metadata is outdated.")
    parser.add_option("--debug", action="append", 
                      help="Activate debugging output from specified facility. "
                            "This option can be specified multiple times.")
    parser.add_option("--quiet", action="store_true", default=False,
                      help="Be really quiet")
    
    (options, pps) = parser.parse_args(args)
    
    if not len(pps) == 1:
        parser.error("bucketname not specificed")
        
    options.bucketname = pps[0]
    
    return options

 
def main(args):

    try:
        import psyco
        psyco.profile()
    except ImportError:
        pass
    
    options = parse_args(args)
    dbfile = get_dbfile(options.bucketname, options.cachedir)
    cachedir = get_cachedir(options.bucketname, options.cachedir)

    init_logging_from_options(options)
    log = logging.getLogger("frontend")
    
    # Set if we need to reupload the metadata
    commit_required = False
    
    (awskey, awspass) = get_credentials(options.credfile, options.awskey)
    conn = s3.Connection(awskey, awspass)
    if not conn.bucket_exists(options.bucketname):
        log.error("Bucket does not exist.")
        sys.exit(1)
    bucket = conn.get_bucket(options.bucketname)
    
    try:
        unlock_bucket(bucket)
    except s3.ChecksumError:
        log.error('Checksum error - incorrect password?')
        raise QuietError(1)
    
    #
    # Check if fs is dirty and we lack metadata
    #
    if bucket["s3ql_dirty"] == "yes" and \
            (not os.path.exists(cachedir) or
             not os.path.exists(dbfile)):
        if not options.force_remote:
            print('Filesystem is marked dirty, but there is no cached metadata available.\n'
                   'You should run fsck.s3ql on the system and user id where the\n'
                   'filesystem has been mounted most recently.\n\n'
                   'This message can also appear if changes from the last mount have not\n'
                   'had sufficient time to propagate in S3. In this case this message\n'
                   'should disappear when retrying later.\n\n'    
                   'Use --force-remote if you want to force a check on this machine. This\n'
                   'may result in dataloss.\n', file=sys.stderr)
            sys.exit(1)
        else:
            log.warn("Dirty filesystem and no local metadata - continuing anyway.")
        
        
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
        local = os.stat(dbfile).st_mtime - time.timezone
        remote = bucket.lookup_key("s3ql_dirty")['last-modified']
    
        log.debug('Local metadata timestamp: %.3f', local)
        log.debug('Remote metadata timestamp: %.3f', remote)
        if remote > local:
            # remote metadata is newer
            if not options.force_local:
                print('The metadata stored with the filesystem is never than the\n'
                      'locally cached data. Probably the filesystem has been mounted\n'
                      'and changed on a different system. You should run fsck.s3ql\n'
                      'on that system.\n\n'  
                      'Use --force-local if you want to force a check on this machine using the\n'
                      'cached data. This will result in dataloss.\n\n'  
                      'You can also remove the local cache before calling fsck.s3ql to\n'
                      'perform the check with the newer metadata stored on S3. This\n'
                      'may also result in dataloss.\n', file=sys.stderr)
                sys.exit(1)
            elif options.checkonly:
                print('Cannot overwrite local metadata in checkonly mode, exiting.\n',
                      file=sys.stderr)
                sys.exit(1)
            else:
                log.warn("Remote metadata is never than cache - continuing anyway.")
    
        # Continue with local metadata from here, make sure that we upload it at the end
        commit_required = True
        log.info('Using local metadata from cache')
    
    else:
        if (not options.force_local and bucket.lookup_key("s3ql_metadata")['last-modified'] 
            < bucket.lookup_key("s3ql_dirty")['last-modified']):
            if not options.force_old:
                print('Metadata from most recent mount has not yet propagated through Amazon S3.\n'
                      'Please try again later.\n\n'
                      'Use --force-old if you want to check the file system with  the (outdated)\n'
                      'metadata that is available right now. This will result in data loss.\n',
                      file=sys.stderr)
                sys.exit(1)
            else:
                log.warn('Metadata has not yet propagated through Amazon S3.'
                         'Continuing with outdated metadata...')
                commit_required = True
                    
        # Download remote metadata
        log.info('Downloading metadata..')
        os.mknod(dbfile, 0600 | stat.S_IFREG)
        bucket.fetch_fh("s3ql_metadata", open(dbfile, 'w'))
    
    
    conn = WrappedConnection(apsw.Connection(dbfile), retrytime=0)
    
    # Check filesystem revision
    rev = conn.get_val("SELECT version FROM parameters")
    if rev < 1:
        log.error("This version of S3QL is too old for the filesystem!")
        sys.exit(1)
    
    
    # Now we can check
    fsck.fsck(conn, cachedir, bucket, options.checkonly)
    if fsck.found_errors:
        commit_required = True
    
    if conn.get_val("SELECT needs_fsck FROM parameters"):
        commit_required = True
    
    if commit_required and not options.checkonly:
        # Commit metadata and mark fs as clean, both internally and as object
        log.info("Committing data to S3...")
        conn.execute("UPDATE parameters SET needs_fsck=?, last_fsck=?, "
                     "mountcnt=?", (False, time.time() - time.timezone, 0))
    
        conn.execute("VACUUM")
        log.debug("Uploading database..")
        if bucket.has_key("s3ql_metadata_bak_2"):
            bucket.copy("s3ql_metadata_bak_2", "s3ql_metadata_bak_3")
        if bucket.has_key("s3ql_metadata_bak_1"):
            bucket.copy("s3ql_metadata_bak_1", "s3ql_metadata_bak_2")
        bucket.copy("s3ql_metadata", "s3ql_metadata_bak_1")
        
        bucket.store("s3ql_dirty", "no")
        bucket.store_fh("s3ql_metadata", open(dbfile, 'r'))
        
    
        
    os.unlink(dbfile)
    os.rmdir(cachedir)
      
if __name__ == '__main__':
    main(sys.argv[1:])    
