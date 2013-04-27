'''
deltadump.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

# This is a wrapper for _deltadump to work around
# http://trac.cython.org/cython_trac/ticket/475

#@PydevCodeAnalysisIgnore

import apsw
from . import _deltadump
from ._deltadump import *
import subprocess
import re

def get_libraries(pathname):
    '''Return shared libraries required for *pathname*'''

    libs = dict()
    with subprocess.Popen(['ldd', pathname], stdout=subprocess.PIPE,
                          universal_newlines=True) as ldd:
        for line in ldd.stdout:
            if '=>' in line:
                (soname, path) = line.split('=>')
            else:
                path = line.strip()
                soname = None
    
            hit = re.match(r'^\s*(.+)\s+\(0x[0-9a-fA-F]+\)$', path)
            if hit:
                path = hit.group(1).strip()
            else:
                path = path.strip()
    
            if path == 'not found':
                path = None
    
            if not soname:
                soname = path
    
            libs[soname.strip()] = path

    if ldd.returncode != 0:
        raise ImportError('ldd call failed')

    return libs


# We need to make sure that apsw and _deltadump are linked against the same
# sqlite library.
apsw_libs = get_libraries(apsw.__file__)
s3ql_libs = get_libraries(_deltadump.__file__)

if 'libsqlite3.so.0' not in apsw_libs:
    raise ImportError("python-apsw must be linked dynamically to sqlite3")

if 'libsqlite3.so.0' not in s3ql_libs:
    raise ImportError('s3ql._deltadump must be linked dynamically to sqlite3')

if apsw_libs['libsqlite3.so.0'] != s3ql_libs['libsqlite3.so.0']:
    raise ImportError('python-apsw and s3ql._deltadump not linked against same sqlite3 library')
