'''
s3.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from ..logging import logging # Ensure use of custom logger class
from . import s3c
from .s3c import XML_CONTENT_RE, get_S3Error
from .common import NoSuchObject
from ..common import QuietError, BUFSIZE
from ..inherit_docstrings import copy_ancestor_docstring
from xml.sax.saxutils import escape as xml_escape
from xml.etree import ElementTree
import re

log = logging.getLogger(__name__)

# Maximum number of keys that can be deleted at once
MAX_KEYS = 1000

# Namespace used in server responses
S3NS = '{http://s3.amazonaws.com/doc/2006-03-01/}'

# Pylint goes berserk with false positives
#pylint: disable=E1002,E1101
              
class Backend(s3c.Backend):
    """A backend to store data in Amazon S3
    
    This class uses standard HTTP connections to connect to S3.
    
    The backend guarantees get after create consistency, i.e. a newly created
    object will be immediately retrievable. Additional consistency guarantees
    may or may not be available and can be queried for with instance methods.    
    """
    
    def __init__(self, storage_url, login, password, ssl_context):
        super().__init__(storage_url, login, password, ssl_context)

    @staticmethod
    def _parse_storage_url(storage_url, ssl_context):
        hit = re.match(r'^s3s?://([^/]+)(?:/(.*))?$', storage_url)
        if not hit:
            raise QuietError('Invalid storage URL')

        bucket_name = hit.group(1)
        
        # http://docs.amazonwebservices.com/AmazonS3/2006-03-01/dev/BucketRestrictions.html
        if not re.match('^[a-z0-9][a-z0-9.-]{1,60}[a-z0-9]$', bucket_name):
            raise QuietError('Invalid bucket name.')
        
        hostname = '%s.s3.amazonaws.com' % bucket_name
        prefix = hit.group(2) or ''
        port = 443 if ssl_context else 80
        return (hostname, port, bucket_name, prefix)

    def __str__(self):
        return 'Amazon S3 bucket %s, prefix %s' % (self.bucket_name, self.prefix)

    @copy_ancestor_docstring
    def delete_multi(self, keys, force=False):
        log.debug('delete_multi(%s)', keys)

        while len(keys) > 0:
            tmp = keys[:MAX_KEYS]
            try:
                self._delete_multi(tmp, force=force)
            finally:
                keys[:MAX_KEYS] = tmp


    def _delete_multi(self, keys, force=False):

        body = [ '<Delete>' ]
        esc_prefix = xml_escape(self.prefix)
        for key in keys:
            body.append('<Object><Key>%s%s</Key></Object>' % (esc_prefix, xml_escape(key)))
        body.append('</Delete>')
        body = '\n'.join(body).encode('utf-8')
        headers = { 'content-type': 'text/xml; charset=utf-8' }
        
        resp = self._do_request('POST', '/', subres='delete', body=body, headers=headers)
        try:
            if not XML_CONTENT_RE.match(resp.getheader('Content-Type')):
                raise RuntimeError('unexpected content type: %s' % resp.getheader('Content-Type'))
            
            root = ElementTree.parse(resp)

            error_tags = root.findall(S3NS + 'Error')
            if not error_tags:
                # No errors occured, everything has been deleted
                del keys[:]
                return

            # Some errors occured, so we need to determine what has
            # been deleted and what hasn't
            offset = len(self.prefix)
            for tag in root.findall(S3NS + 'Deleted'):
                fullkey = tag.find(S3NS + 'Key').text
                assert fullkey.startswith(self.prefix)
                keys.remove(fullkey[offset:])

            # If *force*, just modify the passed list and return without
            # raising an exception
            if force:
                return

            # Otherwise raise exception for the first error
            errcode = error_tags[0].find(S3NS + 'Code')
            errmsg = error_tags[0].find(S3NS + 'Message')
            errkey = error_tags[0].find(S3NS + 'Key')[offset:]

            if errcode == 'NoSuchKeyError':
                raise NoSuchObject(errkey)
            else:
                raise get_S3Error(errcode, 'Error deleting %s: %s' % (errkey, errmsg))

        finally:
            # Need to read rest of response
            while True:
                buf = resp.read(BUFSIZE)
                if buf == b'':
                    break
            
    
