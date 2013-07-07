.. -*- mode: rst -*-

.. _storage_backends:

==================
 Storage Backends
==================

S3QL supports different *backends* to store data at different service
providers and using different protocols. A *storage url* specifies a
backend together with some backend-specific information and uniquely
identifies an S3QL file system. The form of the storage url depends on
the backend and is described for every backend below.

All storage backends respect the ``http_proxy`` and ``https_proxy``
environment variables.

Google Storage
==============

`Google Storage <http://code.google.com/apis/storage/>`_ is an online
storage service offered by Google. To use the Google Storage backend,
you need to have (or sign up for) a Google account, and then `activate
Google Storage <http://code.google.com/apis/storage/docs/signup.html>`_
for your account. The account is free, you will pay only for the
amount of storage and traffic that you actually use. Once you have
created the account, make sure to `activate legacy access
<http://code.google.com/apis/storage/docs/reference/v1/apiversion1.html#enabling>`_.

To create a Google Storage bucket, you can use e.g. the `Google
Storage Manager <https://sandbox.google.com/storage/>`_. The storage
URL for accessing the bucket in S3QL is then ::

   gs://<bucketname>/<prefix>

Here *bucketname* is the name of the bucket, and *prefix* can be
an arbitrary prefix that will be prepended to all object names used by
S3QL. This allows you to store several S3QL file systems in the same
Google Storage bucket.

Note that the backend login and password for accessing your Google
Storage bucket are not your Google account name and password, but the
*Google Storage developer access key* and *Google Storage developer
secret* that you can manage with the `Google Storage key management
tool <https://code.google.com/apis/console/#:storage:legacy>`_.


Amazon S3
=========

`Amazon S3 <http://aws.amazon.com/s3>`_ is the online storage service
offered by `Amazon Web Services (AWS) <http://aws.amazon.com/>`_. To
use the S3 backend, you first need to sign up for an AWS account. The
account is free, you will pay only for the amount of storage and
traffic that you actually use. After that, you need to create a bucket
that will hold the S3QL file system, e.g. using the `AWS Management
Console <https://console.aws.amazon.com/s3/home>`_. For best
performance, it is recommend to create the bucket in the
geographically closest storage region, but not the US Standard region
(see below).

The storage URL for accessing S3 buckets in S3QL has the form ::

    s3://<bucketname>/<prefix>

Here *bucketname* is the name of the bucket, and *prefix* can be an
arbitrary prefix that will be prepended to all object names used by
S3QL. This allows you to store several S3QL file systems in the same
S3 bucket.

Note that the backend login and password for accessing S3 are not the
user id and password that you use to log into the Amazon Webpage, but
the *AWS access key id* and *AWS secret access key* shown under `My
Account/Access Identifiers
<https://aws-portal.amazon.com/gp/aws/developer/account/index.html?ie=UTF8&action=access-key>`_.


Reduced Redundancy Storage (RRS)
--------------------------------

S3QL does not allow the use of `reduced redundancy storage
<http://aws.amazon.com/s3/#protecting>`_. The reason for that is a
combination of three factors:

* RRS has a relatively low reliability, on average you lose one
  out of every ten-thousand objects a year. So you can expect to
  occasionally lose some data.

* When `fsck.s3ql` asks S3 for a list of the stored objects, this list
  includes even those objects that have been lost. Therefore
  `fsck.s3ql` *can not detect lost objects* and lost data will only
  become apparent when you try to actually read from a file whose data
  has been lost. This is a (very unfortunate) peculiarity of Amazon
  S3.

* Due to the data de-duplication feature of S3QL, unnoticed lost
  objects may cause subsequent data loss later in time (see
  :ref:`backend_reliability` for details).


OpenStack/Swift
===============

OpenStack_ is an open-source cloud server application suite. Swift_ is
the cloud storage module of OpenStack. Swift/OpenStack storage is
offered by many different companies.

The storage URL for the OpenStack backend has the form ::
  
   swift://<hostname>[:<port>]/<container>[/<prefix>]

Note that the storage container must already exist. Most OpenStack
providers offer a web frontend that you can use to create storage
containers. *prefix* can be an arbitrary prefix that will be prepended
to all object names used by S3QL. This allows you to store several
S3QL file systems in the same container.

.. _OpenStack: http://www.openstack.org/
.. _Swift: http://openstack.org/projects/storage/


Rackspace CloudFiles
====================

Rackspace_ CloudFiles uses OpenStack_ internally, so you can use the
OpenStack/Swift backend (see above). The hostname for CloudFiles
containers is ``auth.api.rackspacecloud.com``. Use your normal
Rackspace user name for the backend login, and your Rackspace API key
as the backend passphrase. You can create a storage container for S3QL
using the `Control Panel <https://manage.rackspacecloud.com/>`_ (go to
*Cloud Files* under *Hosting*).


.. NOTE::

   As of January 2012, Rackspace does not give any durability or
   consistency guarantees (see :ref:`durability` for why this is
   important).  However, Rackspace support agents seem prone to claim
   very high guarantees.  Unless explicitly backed by their terms of
   service, any such statement should thus be viewed with
   suspicion. S3QL developers have also `repeatedly experienced
   <http://www.rath.org/Tales%20from%20the%20Rackspace%20Support>`_
   similar issues with the credibility and competence of the Rackspace
   support.


.. _Rackspace: http://www.rackspace.com/


S3 compatible
=============

The S3 compatible backend allows S3QL to access any storage service
that uses the same protocol as Amazon S3. The storage URL has the form ::

   s3c://<hostname>:<port>/<bucketname>/<prefix>

Here *bucketname* is the name of an (existing) bucket, and *prefix*
can be an arbitrary prefix that will be prepended to all object names
used by S3QL. This allows you to store several S3QL file systems in
the same bucket.


Local
=====

S3QL is also able to store its data on the local file system. This can
be used to backup data on external media, or to access external
services that S3QL can not talk to directly (e.g., it is possible to
store data over SSH by first mounting the remote system using sshfs_
and then using the local backend to store the data in the sshfs
mountpoint).

The storage URL for local storage is ::

   local://<path>
   
Note that you have to write three consecutive slashes to specify an
absolute path, e.g. `local:///var/archive`. Also, relative paths will
automatically be converted to absolute paths before the authentication
file (see :ref:`authinfo`) is read, i.e. if you are in the
`/home/john` directory and try to mount `local://s3ql`, the
corresponding section in the authentication file must match the
storage url `local:///home/john/s3ql`.


.. _sshfs: http://fuse.sourceforge.net/sshfs.html
