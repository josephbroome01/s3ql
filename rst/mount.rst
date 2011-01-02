.. -*- mode: rst -*-

==========
 Mounting
==========


A S3QL file system is mounted with the `mount.s3ql` command. It has
the following syntax::

  mount.s3ql [options] <storage url> <mountpoint>

.. NOTE::

   S3QL is not a network file system like `NFS
   <http://en.wikipedia.org/wiki/Network_File_System_%28protocol%29>`_
   or `CIFS <http://en.wikipedia.org/wiki/CIFS>`_. It can only be
   mounted on one computer at a time.

This command accepts the following options:

.. include:: autogen/mount-help.rst
   :start-after: show this help message and exit

.. _bucket_pw:

Storing Encryption Passwords
============================

If you are trying to mount an encrypted bucket, `mount.s3ql` will first
try to read the password from the `.s3ql/authinfo` file (the same file
that is used to read the backend authentication data) and prompt the
user to enter the password only if this fails.

The `authinfo` entries to specify bucket passwords are of the form ::

  storage-url <storage-url> password <password>

So to always use the password `topsecret` when mounting `s3://joes_bucket`,
the entry would be ::

  storage-url s3://joes_bucket password topsecret

.. NOTE::

   If you are using the local backend, the storage url will
   always be converted to an absolute path. So if you are in the
   `/home/john` directory and try to mount `local://bucket`, the matching
   `authinfo` entry has to have a storage url of
   `local:///home/john/bucket`.


Compression Algorithms
======================

S3QL supports three compression algorithms, LZMA, Bzip2 and zlib (with
LZMA being the default). The compression algorithm can be specified
freely whenever the file system is mounted, since it affects only the
compression of new data blocks.

Roughly speaking, LZMA is slower but achieves better compression
ratios than Bzip2, while Bzip2 in turn is slower but achieves better
compression ratios than zlib.

For maximum file system performance, the best algorithm therefore
depends on your network connection speed: the compression algorithm
should be fast enough to saturate your network connection.

To find the optimal algorithm for your system, S3QL ships with a
program called `benchmark.py` in the `contrib` directory. You should
run this program on a file that has a size that is roughly equal to
the block size of your file system and has similar contents. It will
then determine the compression speeds for the different algorithms and
the upload speeds for the specified backend and recommend the best
algorithm that is fast enough to saturate your network connection.

Obviously you should make sure that there is little other system load
when you run `benchmark.py` (i.e., don't compile software or encode
videos at the same time).


Parallel Compression
====================

If you are running S3QL on a system with multiple cores, you might
want to set ``--compression-threads`` to a value bigger than one. This
will instruct S3QL to compress and encrypt several blocks at the same
time.

If you want to do this in combination with using the LZMA compression
algorithm, you should keep an eye on memory usage though. Every
LZMA compression threads requires about 200 MB of RAM.


.. NOTE::

   To determine the optimal compression algorithm for your network
   connection when using multiple threads, you can pass the
   ``--compression-threads`` option to  `contrib/benchmark.py`.


Notes about Caching
===================

S3QL maintains a local cache of the file system data to speed up
access. The cache is block based, so it is possible that only parts of
a file are in the cache.

Maximum Number of Cache Entries
-------------------------------

The maximum size of the cache can be configured with the `--cachesize`
option. In addition to that, the maximum number of objects in the
cache is limited by the `--max-cache-entries` option, so it is
possible that the cache does not grow up to the maximum cache size
because the maximum number of cache elements has been reached. The
reason for this limit is that each cache entry requires one open
file descriptor, and Linux distributions usually limit the total
number of file descriptors per process to about a thousand.

If you specify a value for `--max-cache-entries`, you should therefore
make sure to also configure your system to increase the maximum number
of open file handles. This can be done temporarily with the `umask -n`
command. The method to permanently change this limit system-wide
depends on your distribution.



Cache Flushing and Expiration
-----------------------------

S3QL flushes changed blocks in the cache to the backend whenever a block
has not been accessed for at least 10 seconds. Note that when a block is
flushed, it still remains in the cache.

Cache expiration (i.e., removal of blocks from the cache) is only done
when the maximum cache size is reached. S3QL always expires the least
recently used blocks first.


Automatic Mounting
==================

If you want to mount and umount an S3QL file system automatically at
system startup and shutdown, you should do so with one dedicated S3QL
init script for each S3QL file system.

If your system is using upstart, an appropriate job can be defined
as follows (and should be placed in `/etc/init/`):

.. literalinclude:: ../contrib/s3ql.conf
   :linenos:
   :lines: 5-

.. NOTE::

   In principle, it is also possible to automatically mount an S3QL
   file system with an appropriate entry in `/etc/fstab`. However,
   this is not recommended for several reasons:

   * file systems mounted in `/etc/fstab` will be unmounted with the
     `umount` command, so your system will not wait until all data has
     been uploaded but shutdown (or restart) immediately (this is a
     FUSE limitation, see `issue 159
     <http://code.google.com/p/s3ql/issues/detail?id=159>`_).

   * There is no way to tell the system that mounting S3QL requires a
     Python interpreter to be available, so it may attempt to run
     `mount.s3ql` before it has mounted the volume containing the
     Python interpreter.

   * There is no standard way to tell the system that internet
     connection has to be up before the S3QL file system can be
     mounted.
     
