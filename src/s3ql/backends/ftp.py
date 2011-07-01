'''
__init__.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from __future__ import division, print_function, absolute_import

from .common import AbstractConnection
from ..common import QuietError
import logging

log = logging.getLogger("backend.ftp")

class Connection(AbstractConnection):

    def __init__(self, host, port, login, password):
        super(Connection, self).__init__()
        raise QuietError('FTP backend is not yet implemented.')

class TLSConnection(Connection):

    def __init__(self, host, port, login, password):
        super(Connection, self).__init__()
        raise QuietError('FTP backend is not yet implemented.')
