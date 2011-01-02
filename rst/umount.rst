.. -*- mode: rst -*-

==========
Unmounting
==========

To unmount an S3QL file system, use the command::

 umount.s3ql [options] <mountpoint>

This will block until all data has been committed to the storage
backend.

Only the user who mounted the file system with `mount.s3ql` is able to
unmount it again. If you are root and want to unmount an S3QL file
system mounted by an ordinary user, you have to use the `fusermount
-u` or `umount` command instead. Note that these commands do not block
until all data has been uploaded, so if you use them instead of
`umount.s3ql` then you should manually wait for the `mount.s3ql`
process to terminate before shutting down the system.

The most important options are:

  -z, --lazy         Lazy umount. Detaches the file system immediately, even
                     if there are still open files. The data will be uploaded
                     in the background once all open files have been closed.

For a full list of available options, run `umount.s3ql --help`.

If, for some reason, the `umount.sql` command does not work, the file
system can also be unmounted with `fusermount -u -z`. Note that this
command will return immediately and the file system may continue to
upload data in the background for a while longer.
