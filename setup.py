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
import re
import logging
import ctypes.util


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
cflags = ['-std=c99', '-Wall', '-Wextra', '-pedantic', '-Wswitch-enum',
          '-Wswitch-default']
lzma_c_files = list()
for file_ in ['liblzma.c', 'liblzma_compressobj.c', 'liblzma_decompressobj.c',
              'liblzma_file.c', 'liblzma_fileobj.c', 'liblzma_options.c',
              'liblzma_util.c']:
    lzma_c_files.append(os.path.join('src', 's3ql', 'lzma', file_))

# Add S3QL sources
basedir = os.path.abspath(os.path.dirname(sys.argv[0]))
sys.path.insert(0, os.path.join(basedir, 'src'))
import s3ql.common

# Import setuptools
import ez_setup
ez_setup.use_setuptools()
import setuptools

def main():
    with open(os.path.join(basedir, 'doc', 'txt', 'about.txt'), 'r') as fh:
        long_desc = fh.read()

    version_define = [('VERSION', '"%s"' % '0.5.2')]
    compile_args = list()
    compile_args.extend(cflags)
    compile_args.extend(get_cflags('liblzma'))
    extens = [setuptools.Extension('s3ql.lzma', lzma_c_files, extra_compile_args=compile_args,
                                 extra_link_args=get_cflags('liblzma', False, True),
                                 define_macros=version_define)]

    setuptools.setup(
          name='s3ql',
          zip_safe=True,
          version=s3ql.common.VERSION,
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
          provides=['s3ql'],
          entry_points={ 'console_scripts':
                          ['cp.s3ql = s3ql.cli.cp:main',
                           'fsck.s3ql = s3ql.cli.fsck:main',
                           'mkfs.s3ql = s3ql.cli.mkfs:main',
                           'mount.s3ql = s3ql.cli.mount:main',
                           'stat.s3ql = s3ql.cli.statfs:main',
                           'tune.s3ql = s3ql.cli.tune:main',
                           'umount.s3ql = s3ql.cli.umount:main' ]
                          },
          requires=['apsw', 'pycryptopp' ],
          ext_modules=extens,
          cmdclass={'test': run_tests,
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

        cflags = get_cflags('fuse')
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


def get_cflags(pkg, cflags=True, ldflags=False):
    '''Get cflags required to compile with fuse library'''

    cmd = ['pkg-config', pkg ]
    if cflags:
        cmd.append('--cflags')
    if ldflags:
        cmd.append('--libs')

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    cflags = proc.stdout.readline().rstrip()
    proc.stdout.close()
    if proc.wait() != 0:
        sys.stderr.write('Failed to execute pkg-config. Exit code: %d.\n'
                         % proc.returncode)
        sys.stderr.write('Check that the %s development package been installed properly.\n'
                         % pkg)
        sys.exit(1)

    return cflags.split()


# Add as subcommand of build
distutils.command.build.build.sub_commands.insert(0, ('build_ctypes', None))

class run_tests(setuptools.Command):

    description = "Run self-tests"
    user_options = [('debug=', None, 'Activate debugging for specified modules '
                                    '(separated by commas, specify "all" for all modules)'),
                    ('awskey=', None, 'Specify AWS access key to use, secret key will be asked for. '
                                      'If this option is not specified, tests requiring access '
                                      'to Amazon Web Services will be skipped.'),
                    ('tests=', None, 'Run only the specified tests (separated by commas)')
                ]
    boolean_options = []

    def initialize_options(self):
        self.debug = None
        self.awskey = None
        self.tests = None

    def finalize_options(self):
        if self.debug:
            self.debug = [ x.strip() for x  in self.debug.split(',') ]

        if self.tests:
            self.tests = [ x.strip() for x  in self.tests.split(',') ]


    def run(self):

        # Enforce correct APSW version
        import apsw
        tmp = apsw.apswversion()
        tmp = tmp[:tmp.index('-')]
        apsw_ver = tuple([ int(x) for x in tmp.split('.') ])
        if apsw_ver < (3, 6, 14):
            raise StandardError('APSW version too old, must be 3.6.14 or newer!\n')

        # Enforce correct SQLite version
        sqlite_ver = tuple([ int(x) for x in apsw.sqlitelibversion().split('.') ])
        if sqlite_ver < (3, 6, 19):
            raise StandardError('SQLite version too old, must be 3.6.17 or newer!\n')

        # Build extensions in-place
        self.reinitialize_command('build_ext', inplace=1)
        self.run_command('build_ext')

        # Add test modules
        sys.path.insert(0, os.path.join(basedir, 'tests'))
        import unittest
        import _common
        import s3ql.common
        from s3ql.common import init_logging
        from getpass import getpass

        # Init Logging
        logfile = os.path.join(basedir, 'setup.log')
        stdout_level = logging.WARN
        if self.debug and 'all' in self.debug:
            file_level = logging.DEBUG
            file_loggers = None
        elif self.debug:
            file_level = logging.DEBUG
            file_loggers = self.debug
        else:
            file_level = logging.INFO
            file_loggers = None
        init_logging(logfile, stdout_level, file_level, file_loggers)

        # Init AWS
        if self.awskey:
            if sys.stdin.isatty():
                pw = getpass("Enter AWS password: ")
            else:
                pw = sys.stdin.readline().rstrip()
            _common.aws_credentials = (self.awskey, pw)

        # Find and import all tests
        testdir = os.path.join(basedir, 'tests')
        modules_to_test = [ name[:-3] for name in os.listdir(testdir)
                            if name.endswith(".py") and name.startswith('t') ]
        modules_to_test.sort()

        base_module = sys.modules[__name__]
        for name in modules_to_test:
            # Note that __import__ itself does not add the modules to the namespace
            module = __import__(name)
            setattr(base_module, name, module)

        if self.tests:
            modules_to_test = self.tests

        # Run tests
        runner = unittest.TextTestRunner(verbosity=2)
        tests = unittest.defaultTestLoader.loadTestsFromNames(modules_to_test, module=base_module)
        result = runner.run(tests)
        if not result.wasSuccessful():
            sys.exit(1)

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
