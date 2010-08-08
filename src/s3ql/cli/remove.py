'''
remove.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function, absolute_import

from s3ql import libc
import os
import logging
from s3ql.common import (add_stdout_logging, setup_excepthook, CTRL_NAME, QuietError)
from s3ql.optparse import OptionParser
import cPickle as pickle
import textwrap
import sys

log = logging.getLogger("remove")

def parse_args(args):
    '''Parse command line'''

    parser = OptionParser(
        usage="%prog [options] <name(s)>\n"
              "%prog --help",
        description=textwrap.dedent('''\
        Recursively delete files and directories in an S3QL file system,
        including immutable entries. 
        '''))

    parser.add_option("--debug", action="store_true",
                      help="Activate debugging output")
    parser.add_option("--quiet", action="store_true", default=False,
                      help="Be really quiet")

    (options, pps) = parser.parse_args(args)

    # Verify parameters
    if len(pps) < 1:
        parser.error("Incorrect number of arguments.")
    options.pps = [ x.rstrip('/') for x  in pps ]

    return options

def main(args=None):
    '''Recursively delete files and directories in an S3QL file system'''

    if args is None:
        args = sys.argv[1:]

    options = parse_args(args)

    # Initialize logging if not yet initialized
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        handler = add_stdout_logging(options.quiet)
        setup_excepthook()  
        if options.debug:
            root_logger.setLevel(logging.DEBUG)
            handler.setLevel(logging.DEBUG)
        else:
            root_logger.setLevel(logging.INFO)     
    else:
        log.info("Logging already initialized.")

    for name in options.pps:
        if not os.path.exists(name):
            raise QuietError('%r does not exist' % name)
    
        parent = os.path.dirname(os.path.abspath(name))
        fstat_p = os.stat(parent)
        fstat = os.stat(name)
    
        if fstat_p.st_dev != fstat.st_dev:
            raise QuietError('%s is a mount point itself.' % name)
        
        ctrlfile = os.path.join(parent, CTRL_NAME)
        if not (CTRL_NAME not in libc.listdir(parent) and os.path.exists(ctrlfile)):
            raise QuietError('%s is not on an S3QL file system' % name)
    
        if os.stat(ctrlfile).st_uid != os.geteuid():
            raise QuietError('Only root and the mounting user may run s3qlrm.')
    
        libc.setxattr(ctrlfile, 'rmtree', pickle.dumps((fstat_p.st_ino, 
                                                        os.path.basename(name)),
                                                       pickle.HIGHEST_PROTOCOL))


if __name__ == '__main__':
    main(sys.argv[1:])
