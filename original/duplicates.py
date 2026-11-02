# File: duplicates.py
# Find duplicate files.
#
# Debajyoti Nandi <debajyoti.nandi@gmail.com>
# Created: 2016-11-06
# Modifeid: 2016-11-09

# License: MIT License

from __future__ import division
from __future__ import print_function

import os
import hashlib

def md5sum(fpath):
    """Compute the md5sum hash of the content of a given file."""
    blocksize = 65536
    hash_md5 = hashlib.md5()
    with open(fpath, "rb") as f:
        for block in iter(lambda: f.read(blocksize), b""):
            hash_md5.update(block)
    return hash_md5.hexdigest()


def find_dup(path='.', opt=0):
    """Find duplicate files in a given path (or, in a given directory
subtree given by path) by md5sum hash.

    Returns:
      md5_dict  = a dictionary containing the file name(s) or path(s)
                  (value) and the md5sum hash (key)
      dup_count = a dictionary containing the number of duplicates
                  (value) and the md5sum hash (key).

    Note:
      (1) If there are 3 files of same content, the number of
          duplicate is 2.

      (2) len(md5_dict)  = # distinct files
          len(dup_count) = # extra files
    """

    if opt:
        return find_dup1(path)
    else:
        return find_dup0(path)


def find_dup0(path='.'):
    """Find duplicate files in a given path by md5sum hash.

    Returns:
      md5_dict  = a dictionary containing the filename(s) (value) and
                  the md5sum hash (key)
      dup_count = a dictionary containing the number of duplicates
                  (value) and the md5sum hash (key).

    Note:
      (1) If there are 3 files of same content, the number of
          duplicate is 2.

      (2) len(md5_dict)  = # distinct files
          len(dup_count) = # extra files
    """
    md5_dict = dict()
    dup_count = dict()
    for fname in next(os.walk(path))[2]:
        fpath = os.path.join(path, fname)
        md5 = md5sum(fpath)
        if md5 in md5_dict:
            md5_dict[md5].append(fname)
            if md5 in dup_count:
                dup_count[md5] += 1
            else:
                dup_count[md5] = 1
        else:
            md5_dict[md5] = [fname]
    return md5_dict, dup_count


def find_dup1(root='.'):
    """Find duplicate files in the directory tree rooted at a given
root by md5sum hash.

    Returns:
      md5_dict  = a dictionary containing the filepath(s) (value) and
                  the md5sum hash (key)
      dup_count = a dictionary containing the number of duplicates
                  (value) and the md5sum hash (key).

    Note:
      (1) If there are 3 files of same content, the number of
          duplicate is 2.

      (2) len(md5_dict)  = # distinct files
          len(dup_count) = # extra files
    """
    md5_dict = dict()
    dup_count = dict()
    for path, dirs, files in os.walk(root):
        for fname in files:
            fpath = os.path.join(path, fname)
            md5 = md5sum(fpath)
            if md5 in md5_dict:
                md5_dict[md5].append(fpath)
                if md5 in dup_count:
                    dup_count[md5] += 1
                else:
                    dup_count[md5] = 1
            else:
                md5_dict[md5] = [fpath]
    return md5_dict, dup_count


def dup_summary(md5_dict, dup_count):
    """Print summary from find_dup()."""

    num_total_files = sum([len(_) for _ in md5_dict.values()])
    num_distinct_files = len(md5_dict)
    num_duplicates = sum(dup_count.values())

    s1 = str(num_total_files)
    s2 = str(num_distinct_files)
    s3 = str(num_duplicates)

    maxwidth = max(len(s1), len(s2), len(s3))

    str_total_files = s1.rjust(maxwidth)
    str_distinct_files = s2.rjust(maxwidth)
    str_duplicates = s3.rjust(maxwidth)

    print('Total Files    : {}'.format(str_total_files))
    print('Distinct Files : {}'.format(str_distinct_files))
    print('Duplicate Files: {}'.format(str_duplicates))


def print_dup_files(filelist):
    """Print the names of the duplicate files."""

    for fname in filelist:
        print('    ' + fname)
    print('')


def dup_report(md5_dict, dup_count):
    """Print detailed report from find_dup()."""

    for md5 in dup_count:
        print('{} files with md5 = {}'.format(dup_count[md5] + 1, md5))
        print_dup_files(md5_dict[md5])

    dup_summary(md5_dict, dup_count)
