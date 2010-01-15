'''
$Id: t1_ordered_dict.py 478 2010-01-12 23:12:54Z nikratio $

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function

import unittest
from s3ql.common import ExceptionStoringThread, EmbeddedException
from _common import TestCase 

class CommonTests(TestCase):
                      
    def test_02_threading(self):
        # Test our threading object
        def works():
            pass
        
        def fails():
            raise RuntimeError()
        
        t1 = ExceptionStoringThread(target=works)
        t2 = ExceptionStoringThread(target=fails)      
        t1.start()
        t2.start()  
        
        t1.join_and_raise()
        self.assertRaises(EmbeddedException, t2.join_and_raise)
        
      
def suite():
    return unittest.makeSuite(CommonTests)


if __name__ == "__main__":
    unittest.main()