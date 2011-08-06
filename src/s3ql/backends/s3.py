'''
s3.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from __future__ import division, print_function, absolute_import

from .common import AbstractBucket, NoSuchObject
from s3ql.common import AsyncFn
import logging
import httplib
import re
import time
from base64 import b64encode
import hmac
import hashlib
import urllib
import xml.etree.cElementTree as ElementTree

log = logging.getLogger("backend.s3")

C_DAY_NAMES = [ 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun' ]
C_MONTH_NAMES = [ 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec' ]

NAMESPACE = 'http://s3.amazonaws.com/doc/2006-03-01/'

class Bucket(AbstractBucket):
    """A bucket stored in Amazon S3 and compatible services

    This class is threadsafe. All methods (except for internal methods
    starting with underscore) may be called concurrently by different
    threads.    
    """

    def __init__(self, bucket_name, aws_key_id, aws_key, prefix, use_ssl):
        super(Bucket, self).__init__()
        
        self.bucket_name = bucket_name
        self.prefix = prefix
        self.aws_key = aws_key
        self.aws_key_id = aws_key_id
        if use_ssl:
            self.conn = httplib.HTTPSConnection('%s.s3.amazonaws.com' % bucket_name)
        else:
            self.conn = httplib.HTTPConnection('%s.s3.amazonaws.com' % bucket_name)
   
        
    def delete(self, key, force=False):
        '''Delete the specified object'''
        
        log.debug('delete(%s): start', key)
        self._auth_request('DELETE', '/%s%s' % (self.prefix, key))
        try:
            self._check_success()
        except S3Error as exc:
            if exc.code == 'NoSuchKey':
                if not force:
                    raise NoSuchObject(key)
            else:
                raise
                 
    def list(self, prefix=''):
        
        log.debug('list(%s): start', prefix)
        marker = ''
        keys_remaining = True
        
        while keys_remaining:
            log.debug('list(%s): requesting with marker=%s', prefix, marker)
            self._auth_request('GET', '/', { 'prefix': prefix,
                                             'marker': marker,
                                             'max-keys': 1000 })
            
            resp = self._get_reponse()
            if resp.getheader('Content-Type').lower() != 'application/xml':
                raise RuntimeError('unexpected content type: %s' % resp.getheader('Content-Type'))
            
            itree = iter(ElementTree.iterparse(resp, events=("start", "end")))
            (event, root) = itree.next()

            namespace = re.sub(r'^\{(.+)\}.+$', r'\1', root.tag)
            if namespace != NAMESPACE:
                raise RuntimeError('Unsupported namespace: %s' % namespace)
            
            keys_remaining = None
            try:
                for (event, el) in itree:
                    if event != 'end':
                        continue
                    
                    if el.tag == '{%s}IsTruncated' % NAMESPACE:
                        keys_remaining = (el.text == 'true')
                    
                    elif el.tag == '{%s}Contents' % NAMESPACE:
                        marker = el.findtext('{%s}Key' % NAMESPACE)
                        yield marker
                        root.clear()
                        
            except GeneratorExit:
                # Need to read rest of response
                while True:
                    buf = resp.read(8192)
                    if buf == '':
                        break
                break # Abort completely
            
            if keys_remaining is None:
                raise RuntimeError('Could not parse body')
        
    def lookup(self, key):
        """Return metadata for given key.

        If the key does not exist, `NoSuchObject` is raised.
        """

        pass


    def open_read(self, key):
        """Open object for reading

        Return a tuple of a file-like object and metadata. Bucket
        contents can be read from the file-like object. 
        """
        
        pass
    
    def open_write(self, key, val, metadata=None):
        """Open object for writing

        `metadata` can be a dict of additional attributes to store with the
        object. Returns a file-like object.
        """
        
        pass
            
    def read_after_create_consistent(self):
        '''Does this backend provide read-after-create consistency?'''
        pass
    
    def read_after_write_consistent(self):
        '''Does this backend provide read-after-write consistency?'''
        pass
        
    def read_after_delete_consistent(self):
        '''Does this backend provide read-after-delete consistency?'''
        pass

    def list_after_delete_consistent(self):
        '''Does this backend provide list-after-delete consistency?'''
        pass
        
    def list_after_create_consistent(self):
        '''Does this backend provide list-after-create consistency?'''
        pass

    def contains(self, key):
        '''Check if `key` is in bucket'''
        pass

    def copy(self, src, dest):
        """Copy data stored under key `src` to key `dest`
        
        If `dest` already exists, it will be overwritten. The copying
        is done on the remote side. If the backend does not support
        this operation, raises `UnsupportedError`.
        """
        pass


    def _check_success(self):
        '''Read response and raise exception if request failed
        
        Response body is read and discarded.
        '''

        self._get_reponse().read()

    def _get_reponse(self):
        '''Read and return response 
        
        Returns a file handle where the response body can be read from.
        '''
                
        resp = self.conn.getresponse()
        
        log.debug('_check_success(): x-amz-request-id: %s, x-aamz-id-2: %s', 
                  resp.getheader('x-amz-request-id'), resp.getheader('x-aamz-id-2'))

        if resp.status == httplib.OK:
            return resp
        
        if resp.getheader('Content-Type').lower() != 'application/xml':
            raise RuntimeError('unexpected content type %s for status %d'
                               % (resp.getheader('Content-Type'), resp.status)) 
        
        # Error
        tree = ElementTree.parse(resp).getroot()
        raise get_S3Error(tree.findtext('Code'), tree.findtext('Message'))
        
    def clear(self):
        """Delete all objects in bucket
        
        This function starts multiple threads."""

        threads = list()
        for (no, s3key) in enumerate(self):
            if no != 0 and no % 1000 == 0:
                log.info('Deleted %d objects so far..', no)

            log.debug('Deleting key %s', s3key)

            # Ignore missing objects when clearing bucket
            t = AsyncFn(self.delete, s3key, True)
            t.start()
            threads.append(t)

            if len(threads) > 50:
                log.debug('50 threads reached, waiting..')
                threads.pop(0).join_and_raise()

        log.debug('Waiting for removal threads')
        for t in threads:
            t.join_and_raise()

    def __str__(self):
        return '<s3 bucket, name=%r>' % self.bucket_name

    def _auth_request(self, method, url, query_string=None, 
                      body=None, headers=None):
        '''Make authenticated request
        
        *query_string* and *headers* must be dictionaries or *None*.
        '''
             
        # See http://docs.amazonwebservices.com/AmazonS3/latest/dev/RESTAuthentication.html
        
        # Lowercase headers
        if headers:
            headers = dict((x.lower(), y) for (x,y) in headers.iteritems())
        else:
            headers = dict()
        
        # Date
        now = time.gmtime()
        # Can't use strftime because it's locale dependent
        headers['date'] = ('%s, %02d %s %04d %02d:%02d:%02d GMT' 
                           % (C_DAY_NAMES[now.tm_wday],
                              now.tm_mday,
                              C_MONTH_NAMES[now.tm_mon - 1],
                              now.tm_year, now.tm_hour, 
                              now.tm_min, now.tm_sec))

        headers['connection'] = 'keep-alive'
            
        auth_strs = [method, '\n']
        
        for hdr in ('content-md5', 'content-type', 'date'):
            if hdr in headers:
                auth_strs.append(headers[hdr])
            auth_strs.append('\n')
    
        for hdr in sorted(x for x in headers if x.startswith('x-amz-')):
            val = ' '.join(re.split(r'\s*\n\s*', headers[hdr].strip()))
            auth_strs.append('%s:%s\n' % (hdr,val))
    
        auth_strs.append('/' + self.bucket_name)
        auth_strs.append(url)
        
        # False positive, hashlib *does* have sha1 member
        #pylint: disable=E1101
        signature = b64encode(hmac.new(self.aws_key, ''.join(auth_strs), hashlib.sha1).digest())
         
        headers['Authorization'] = 'AWS %s:%s' % (self.aws_key_id, signature)
    
        full_url = urllib.quote(url)
        if query_string:
            full_url += '?%s' % urllib.urlencode(query_string, doseq=True)
            
        return self.conn.request(method, full_url, body, headers)
    
  
def get_S3Error(code, msg):
    '''Instantiate most specific S3Error subclass'''
    
    return getattr(globals(), code, S3Error)(code, msg)
    
          
class S3Error(Exception):
    '''
    Represents an error returned by S3. For possible codes, see
    http://docs.amazonwebservices.com/AmazonS3/latest/API/ErrorResponses.html
    '''
    
    def __init__(self, code, msg):
        super(S3Error, self).__init__()
        self.code = code
        self.msg = msg
        
    def __str__(self):
        return self.msg
    
class NoSuchKey(S3Error): pass
class AccessDenied(S3Error): pass
class BadDigest(S3Error): pass
class EntityTooSmall(S3Error): pass
class EntityTooLarge(S3Error): pass
class ExpiredToken(S3Error): pass
class IncompleteBody(S3Error): pass
class InternalError(S3Error): pass
class InvalidAccessKeyId(S3Error): pass
class InvalidBucketName(S3Error): pass
class InvalidSecurity(S3Error): pass
class OperationAborted(S3Error): pass
class RequestTimeout(S3Error): pass
class RequestTimeTooSkewed(S3Error): pass
class SignatureDoesNotMatch(S3Error): pass


