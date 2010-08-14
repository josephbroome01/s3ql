'''
thread_group.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2010 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function, absolute_import

import threading
import sys
import logging
from .common import EmbeddedException, ExceptionStoringThread

log = logging.getLogger("thread_group")

__all__ = [ 'Thread', 'ThreadGroup' ]

class Thread(ExceptionStoringThread):
    '''
    A thread that can be executed in a `ThreadGroup`.
    '''

    def __init__(self):
        super(Thread, self).__init__()
        self._group = None
    
    def run_protected(self):
        '''Perform task asynchronously
        
        This method must be overridden in derived classes. It will
        be called in a separate thread. Exceptions will be
        encapsulated in `EmbeddedException` and re-raised when
        the thread is joined.
        '''
        pass
    
    def finalize(self):
        '''Clean Up after successful run
        
        This method will be called from `join_one` after the
        `run_protected` method has finished successfully,  
        '''
        pass
    
    def handle_exc(self, exc_info):
        '''Clean Up after failed run
        
        This method will be called from `join_one` after the
        `run_protected` method has terminated with an exception. 
        `exc_info` is a tuple as returned by `sys.exc_info()`.
        '''        
        pass
    
    def start(self):
        if not self._group:
            raise ValueError('Must set _group attribute first')
        
        super(Thread, self).start()
        
    def run(self):
        try:
            try:
                self.run_protected()
            finally:
                log.debug('thread: waiting for lock')
                with self._group.lock:
                    log.debug('thread: calling notify()')
                    self._group.active_threads.remove(self)
                    self._group.finished_threads.append(self)
                    self._group.lock.notifyAll()
        except: 
            self._exc_info = sys.exc_info() # This creates a circular reference chain           

class ThreadGroup(object):
    '''Represents a group of threads.
    
    This class is threadsafe, instance methods can safely be
    called concurrently.'''
    
    def __init__(self, max_threads):
        '''Initialize thread group
        
        `max_active` specifies the maximum number of running threads
        in the group. 
        '''
        
        self.max_threads = max_threads
        self.active_threads = list()
        self.finished_threads = list()
        self.lock = threading.Condition(threading.RLock())
        
    def add_thread(self, t, max_threads=None):
        '''Add new thread
        
        `t` must be a `Thread` instance that has overridden
        the `run_protected` and possibly `finalize` methods. 
        
        This method waits until there are less than `max_threads` active
        threads, then it starts a new thread executing `fn`.
                
        If `max_threads` is `None`, the `max_threads` value passed
        to the constructor is used instead.       
        '''
        
        if not isinstance(t, Thread):
            raise TypeError('Parameter must be `Thread` instance')
        t._group = self
        
        if max_threads is None:
            max_threads = self.max_threads
                
        with self.lock:
            while len(self) >= max_threads:
                self.join_one()
                
            self.active_threads.append(t)
            
        t.start()
    
    def join_one(self):
        '''Wait for any one thread to finish
        
        When the thread has finished, its `finalize` method is called.
        
        If the thread terminated with an exception, the exception
        is encapsulated in `EmbeddedException` and raised again. In
        that case the `handle_exc` method is called instead of
        `finalize`.
        
        If there are no active threads, the call returns without doing
        anything.
                
        If more than one thread has called `join_one`, a single thread
        that finishes execution will cause all pending `join_one` calls
        to return. 
        '''
        
        with self.lock:

            # Make sure that at least 1 thread is joined
            if len(self) == 0:
                return
                        
            try:
                t = self.finished_threads.pop()
            except IndexError:
                # Wait for thread to terminate
                log.debug('join_one: wait()')
                self.lock.wait()
                try:
                    t = self.finished_threads.pop()
                except IndexError:
                    # Already joined by other waiting thread
                    return
                   
        try:
            t.join_and_raise()
        except EmbeddedException as exc:
            t.handle_exc(exc.exc_info)
            raise
        else:
            t.finalize()
                    
            
    def join_all(self):
        '''Call join_one() until all threads have terminated'''
        
        with self.lock:
            while len(self) > 0: 
                self.join_one()    
                
    def __len__(self):
        with self.lock:
            return len(self.active_threads) + len(self.finished_threads)
