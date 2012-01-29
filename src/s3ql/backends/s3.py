'''
s3.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from __future__ import division, print_function, absolute_import
from . import s3c
from s3ql.backends.common import retry
from s3ql.common import QuietError
import xml.etree.cElementTree as ElementTree
import logging
import re

log = logging.getLogger("backend.s3")

# Pylint goes berserk with false positives
#pylint: disable=E1002,E1101


# These regions provide read after write consistency for new objects
# http://docs.amazonwebservices.com/AmazonS3/2006-03-01/dev/LocationSelection.html
GOOD_REGIONS=('EU', 'us-west-1', 'us-west-2', 'ap-southeast-1', 'ap-northeast-1', 'sa-east-1')

# These don't
BAD_REGIONS=('us-standard',)
              
class Bucket(s3c.Bucket):
    """A bucket stored in Amazon S3
    
    This class uses standard HTTP connections to connect to S3.
    
    The bucket guarantees get after create consistency, i.e. a newly created
    object will be immediately retrievable. Additional consistency guarantees
    may or may not be available and can be queried for with instance methods.    
    """

    def __init__(self, storage_url, login, password, use_ssl):
        super(Bucket, self).__init__(storage_url, login, password, use_ssl)

        self.region = self._get_region()
        if self.region in BAD_REGIONS:
            log.warn('Warning: bucket provides insufficient consistency guarantees!')
        elif self.region not in GOOD_REGIONS:
            log.warn('Unknown region: %s - please file a bug report.', self.region)

    @staticmethod
    def _parse_storage_url(storage_url, use_ssl):
        hit = re.match(r'^s3s?://([^/]+)(?:/(.*))?$', storage_url)
        if not hit:
            raise QuietError('Invalid storage URL')

        bucket_name = hit.group(1)
        hostname = '%s.s3.amazonaws.com' % bucket_name
        prefix = hit.group(2) or ''
        port = 443 if use_ssl else 80
        return (hostname, port, bucket_name, prefix)

    @retry
    def _get_region(self):
        ''''Return bucket region'''

        log.debug('_get_region()')
        resp = self._do_request('GET', '/', subres='location')

        region = ElementTree.parse(resp).getroot().text

        if not region:
            region = 'us-standard'

        return region

    def __str__(self):
        return 's3://%s/%s' % (self.bucket_name, self.prefix)

    def is_get_consistent(self):
        '''If True, objects retrievals are guaranteed to be up-to-date
        
        If this method returns True, then creating, deleting, or overwriting an
        object is guaranteed to be immediately reflected in subsequent object
        retrieval attempts.
        '''

        return False

    def is_list_create_consistent(self):
        '''If True, new objects are guaranteed to show up in object listings
        
        If this method returns True, creation of objects will immediately be
        reflected when retrieving the list of available objects.
        '''

        return self.region in GOOD_REGIONS
