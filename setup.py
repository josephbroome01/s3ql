#!/usr/bin/env python
'''
setup.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function

import distutils.command.build
import sys
import os
import tempfile
import subprocess
import logging
import ctypes.util

# Work around setuptools bug
# http://bitbucket.org/tarek/distribute/issue/152/
#pylint: disable=W0611
import multiprocessing
try:
    import psyco
except ImportError:
    pass

# These are the definitions that we need
fuse_export_regex = ['^FUSE_SET_.*', '^XATTR_.*', 'fuse_reply_.*' ]
fuse_export_symbols = ['fuse_mount', 'fuse_lowlevel_new', 'fuse_add_direntry',
                       'fuse_set_signal_handlers', 'fuse_session_add_chan',
                       'fuse_session_loop_mt', 'fuse_session_remove_chan',
                       'fuse_remove_signal_handlers', 'fuse_session_destroy',
                       'fuse_unmount', 'fuse_req_ctx', 'fuse_lowlevel_ops',
                       'fuse_session_loop', 'ENOATTR', 'ENOTSUP',
                       'fuse_version', 'fuse_lowlevel_notify_inval_inode',
                       'fuse_lowlevel_notify_inval_entry' ]
libc_export_symbols = [ 'setxattr', 'getxattr', 'readdir', 'opendir',
                       'closedir' ]

# C components
#cflags = ['-std=c99', '-Wall', '-Wextra', '-pedantic', '-Wswitch-enum',
#          '-Wswitch-default']
#lzma_c_files = list()
#for file_ in ['liblzma.c', 'liblzma_compressobj.c', 'liblzma_decompressobj.c',
#              'liblzma_file.c', 'liblzma_fileobj.c', 'liblzma_options.c',
#              'liblzma_util.c']:
#    lzma_c_files.append(os.path.join('src', 's3ql', 'lzma', file_))

# Add S3QL sources
basedir = os.path.abspath(os.path.dirname(sys.argv[0]))
sys.path.insert(0, os.path.join(basedir, 'src'))
import s3ql

# Import distribute
from distribute_setup import use_setuptools
use_setuptools(version='0.6.14', download_delay=5)
import setuptools
import setuptools.command.test as setuptools_test

def main():

    with open(os.path.join(basedir, 'doc', 'txt', 'about.txt'), 'r') as fh:
        long_desc = fh.read()

    #compile_args = list()
    #compile_args.extend(cflags)
    #compile_args.extend(get_cflags('liblzma'))
    #extens = [setuptools.Extension('s3ql.lzma', lzma_c_files, extra_compile_args=compile_args,
    #                             extra_link_args=get_cflags('liblzma', False, True))

    setuptools.setup(
          name='s3ql',
          zip_safe=True,
          version=s3ql.VERSION,
          description='a full-featured file system for online data storage',
          long_description=long_desc,
          author='Nikolaus Rath',
          author_email='Nikolaus@rath.org',
          url='http://code.google.com/p/s3ql/',
          download_url='http://code.google.com/p/s3ql/downloads/list',
          license='LGPL',
          classifiers=['Development Status :: 4 - Beta',
                       'Environment :: No Input/Output (Daemon)',
                       'Environment :: Console',
                       'License :: OSI Approved :: GNU Library or Lesser General Public License (LGPL)',
                       'Topic :: Internet',
                       'Operating System :: POSIX',
                       'Topic :: System :: Archiving'],
          platforms=[ 'POSIX', 'UNIX', 'Linux' ],
          keywords=['FUSE', 'backup', 'archival', 'compression', 'encryption',
                    'deduplication', 'aws', 's3' ],
          package_dir={'': 'src'},
          packages=setuptools.find_packages('src'),
          provides=['s3ql', 'llfuse', 'global_lock'],
          entry_points={ 'console_scripts':
                        [
                         'mkfs.s3ql = s3ql.cli.mkfs:main',
                         'fsck.s3ql = s3ql.cli.fsck:main',
                         'mount.s3ql = s3ql.cli.mount:main',
                         'umount.s3ql = s3ql.cli.umount:main',
                         's3qlcp = s3ql.cli.cp:main',
                         's3qlstat = s3ql.cli.statfs:main',
                         's3qladm = s3ql.cli.adm:main',
                         's3qlctrl = s3ql.cli.ctrl:main',
                         's3qllock = s3ql.cli.lock:main',
                         's3qlrm = s3ql.cli.remove:main',
                         ]
                          },
          install_requires=['apsw >= 3.7.0',
                            'pycryptopp',
                            'argparse',
                            'pyliblzma >= 0.5.3' ],
          tests_require=['apsw >= 3.7.0', 'unittest2',
                         'pycryptopp',
                         'argparse',
                         'pyliblzma >= 0.5.3' ],
          test_suite='tests',
          #ext_modules=extens,
          cmdclass={'test': test,
                    'build_ctypes': build_ctypes,
                    'upload_docs': upload_docs, }
         )

class build_ctypes(setuptools.Command):

    description = "Build ctypes interfaces"
    user_options = []
    boolean_options = []

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        self.create_fuse_api()
        self.create_libc_api()

    def create_fuse_api(self):
        '''Create ctypes API to local FUSE headers'''

         # Import ctypeslib
        sys.path.insert(0, os.path.join(basedir, 'src', 'ctypeslib.zip'))
        from ctypeslib import h2xml, xml2py
        from ctypeslib.codegen import codegenerator as ctypeslib

        print('Creating ctypes API from local fuse headers...')

        cflags = pkg_config('fuse', min_ver='2.8.0')
        print('Using cflags: %s' % ' '.join(cflags))

        fuse_path = 'fuse'
        if not ctypes.util.find_library(fuse_path):
            print('Could not find fuse library', file=sys.stderr)
            sys.exit(1)

        # Create temporary XML file
        tmp_fh = tempfile.NamedTemporaryFile()
        tmp_name = tmp_fh.name

        print('Calling h2xml...')
        argv = [ 'h2xml.py', '-o', tmp_name, '-c', '-q', '-I', os.path.join(basedir, 'src'),
                    'fuse_ctypes.h' ]
        argv += cflags
        ctypeslib.ASSUME_STRINGS = False
        ctypeslib.CDLL_SET_ERRNO = False
        ctypeslib.PREFIX = ('# Code autogenerated by ctypeslib. Any changes will be lost!\n\n'
                            '#pylint: disable-all\n'
                            '#@PydevCodeAnalysisIgnore\n\n')
        h2xml.main(argv)

        print('Calling xml2py...')
        api_file = os.path.join(basedir, 'src', 'llfuse', 'ctypes_api.py')
        argv = [ 'xml2py.py', tmp_name, '-o', api_file, '-l', fuse_path ]
        for el in fuse_export_regex:
            argv.append('-r')
            argv.append(el)
        for el in fuse_export_symbols:
            argv.append('-s')
            argv.append(el)
        xml2py.main(argv)

        # Delete temporary XML file
        tmp_fh.close()
        
        # Make sure that off_t is 64 bit (required by readdir)
        from llfuse import ctypes_api
        if ctypes_api.sizeof(ctypes_api.off_t) < 8:
            raise SystemExit('ERROR: S3QL requires the off_t type to be 64bit.')

        print('Code generation complete.')

    def create_libc_api(self):
        '''Create ctypes API to local libc'''

         # Import ctypeslib
        sys.path.insert(0, os.path.join(basedir, 'src', 'ctypeslib.zip'))
        from ctypeslib import h2xml, xml2py
        from ctypeslib.codegen import codegenerator as ctypeslib

        print('Creating ctypes API from local libc headers...')

        # We must not use an absolute path, see http://bugs.python.org/issue7760
        libc_path = b'c'
        if not ctypes.util.find_library(libc_path):
            print('Could not find libc', file=sys.stderr)
            sys.exit(1)

        # Create temporary XML file
        tmp_fh = tempfile.NamedTemporaryFile()
        tmp_name = tmp_fh.name

        print('Calling h2xml...')
        argv = [ 'h2xml.py', '-o', tmp_name, '-c', '-q', '-I', os.path.join(basedir, 'src'),
                    'libc_ctypes.h' ]
        ctypeslib.ASSUME_STRINGS = True
        ctypeslib.CDLL_SET_ERRNO = True
        ctypeslib.PREFIX = ('# Code autogenerated by ctypeslib. Any changes will be lost!\n\n'
                            '#pylint: disable-all\n'
                            '#@PydevCodeAnalysisIgnore\n\n')
        h2xml.main(argv)

        print('Calling xml2py...')
        api_file = os.path.join(basedir, 'src', 's3ql', 'libc_api.py')
        argv = [ 'xml2py.py', tmp_name, '-o', api_file, '-l', libc_path ]
        for el in libc_export_symbols:
            argv.append('-s')
            argv.append(el)
        xml2py.main(argv)

        # Delete temporary XML file
        tmp_fh.close()

        print('Code generation complete.')


def pkg_config(pkg, cflags=True, ldflags=False, min_ver=None):
    '''Frontend to ``pkg-config``'''

    if min_ver:
        cmd = ['pkg-config', pkg, '--atleast-version', min_ver ]
        
        if subprocess.call(cmd) != 0:
            cmd = ['pkg-config', '--modversion', pkg ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            version = proc.communicate()[0].strip()
            print('%s version too old (found: %s, required: %s)' 
                  % (pkg, version, min_ver), file=sys.stderr)
            sys.exit(1)
    
    cmd = ['pkg-config', pkg ]
    if cflags:
        cmd.append('--cflags')
    if ldflags:
        cmd.append('--libs')

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    cflags = proc.stdout.readline().rstrip()
    proc.stdout.close()
    if proc.wait() != 0:
        print('Failed to execute pkg-config. Exit code: %d.\n'
              'Check that the %s development package been installed properly.'
              % (proc.returncode, pkg), file=sys.stderr)
        sys.exit(1)

    return cflags.split()


# Add as subcommand of build
distutils.command.build.build.sub_commands.insert(0, ('build_ctypes', None))
  
class test(setuptools_test.test):
    # Attributes defined outside init, required by setuptools.
    # pylint: disable=W0201
    description = "Run self-tests"
    user_options = (setuptools_test.test.user_options + 
                    [('debug=', None, 'Activate debugging for specified modules '
                                    '(separated by commas, specify "all" for all modules)'),
                    ('awskey=', None, 'Specify AWS access key to use, secret key will be asked for. '
                                      'If this option is not specified, tests requiring access '
                                      'to Amazon Web Services will be skipped.')])


    def initialize_options(self):
        setuptools_test.test.initialize_options(self)
        self.debug = None
        self.awskey = None

    def finalize_options(self):
        setuptools_test.test.finalize_options(self)
        self.test_loader = "ScanningLoader"
        if self.debug:
            self.debug = [ x.strip() for x  in self.debug.split(',') ]


    def run_tests(self):

        # Add test modules
        sys.path.insert(0, os.path.join(basedir, 'tests'))
        import unittest2 as unittest
        import _common
        from s3ql.common import (setup_excepthook, add_file_logging, add_stdout_logging,
                                 LoggerFilter)
        from getpass import getpass

        # Initialize logging if not yet initialized
        root_logger = logging.getLogger()
        if not root_logger.handlers:
            add_stdout_logging(quiet=True)
            add_file_logging(os.path.join(basedir, 'setup.log'))
            setup_excepthook()  
            if self.debug:
                root_logger.setLevel(logging.DEBUG)
                if 'all' not in self.debug:
                    root_logger.addFilter(LoggerFilter(self.debug, logging.INFO))
            else:
                root_logger.setLevel(logging.INFO) 
        else:
            root_logger.debug("Logging already initialized.")
        
        # Init AWS
        if self.awskey:
            if sys.stdin.isatty():
                pw = getpass("Enter AWS password: ")
            else:
                pw = sys.stdin.readline().rstrip()
            _common.aws_credentials = (self.awskey, pw)

        # Define our own test loader to order modules alphabetically
        from pkg_resources import resource_listdir, resource_exists
        class ScanningLoader(unittest.TestLoader):
            # Yes, this is a nasty hack
            # pylint: disable=W0232,W0221,W0622
            def loadTestsFromModule(self, module):
                """Return a suite of all tests cases contained in the given module"""
                tests = []
                if module.__name__!='setuptools.tests.doctest':  # ugh
                    tests.append(unittest.TestLoader.loadTestsFromModule(self,module))
                if hasattr(module, "additional_tests"):
                    tests.append(module.additional_tests())
                if hasattr(module, '__path__'):
                    for file in sorted(resource_listdir(module.__name__, '')):
                        if file.endswith('.py') and file!='__init__.py':
                            submodule = module.__name__+'.'+file[:-3]
                        else:
                            if resource_exists(
                                module.__name__, file+'/__init__.py'
                            ):
                                submodule = module.__name__+'.'+file
                            else:
                                continue
                        tests.append(self.loadTestsFromName(submodule))
                if len(tests)!=1:
                    return self.suiteClass(tests)
                else:
                    return tests[0] # don't create a nested suite for only one return
                
        unittest.main(
            None, None, [unittest.__file__]+self.test_args,
            testLoader = ScanningLoader())
        

class upload_docs(setuptools.Command):
    user_options = []
    boolean_options = []
    description = "Upload documentation"

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        subprocess.check_call(['rsync', '-aHv', '--del', os.path.join(basedir, 'doc', 'html') + '/',
                               'ebox.rath.org:/var/www/s3ql-docs/'])

if __name__ == '__main__':
    main()
