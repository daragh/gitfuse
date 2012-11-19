#!/usr/bin/env python
# coding: utf-8

import argparse
import errno
import logging
import os
import pygit2
import stat
import sys

from collections import namedtuple
from fuse import FuseOSError, FUSE, Operations, LoggingMixIn

Stat = namedtuple(
    'Stat',
    [
        'st_mode',
        'st_ino',
        'st_dev',
        'st_nlink',
        'st_uid',
        'st_gid',
        'st_size',
        'st_atime',
        'st_mtime',
        'st_ctime',
    ]
)


def copy_stat(st, **kwargs):
    result = Stat(*st)

    result = result._replace(**kwargs)
    result = result._replace(
        st_ino=0,
        # Remove any write bits from st_mode
        st_mode=result.st_mode & ~0222,
    )

    return result._asdict()


def git_tree_to_direntries(tree):
    for entry in tree:
        yield entry.name.encode('utf-8')


def git_tree_find(tree, path):
    parts = path.split('/')

    # Advance through sub-trees until end of path
    tree = reduce(
        lambda t, part: t[part].to_object() if t is not None else None,
        parts[:-1],
        tree,
    )

    # Get the entry for the last part of the path
    try:
        entry = tree[parts[-1]]
    except (TypeError, KeyError):
        # TypeError - reduce returned None
        # KeyError - file not found in tree
        entry = None
    return entry


class GitFS(Operations, LoggingMixIn):
    class GitFSError(Exception):
        pass

    def __init__(self, base_path):
        base_path = os.path.abspath(base_path)
        git_path = os.path.join(base_path, '.git')

        if os.path.exists(git_path):
            self.repo = pygit2.Repository(git_path)
        elif os.path.exists(base_path):
            self.repo = pygit2.Repository(base_path)
        else:
            raise self.GitFSError(
                'Path \'{0}\' does not point to a valid repository'.format(base_path)
            )

    @property
    def refs(self):
        """
        Gets a list of refs minus the leading 'refs' string.

        Example:
        >>> gitfs.refs
        ['/remotes/origin/master',
         '/remotes/origin/config-int-types',
         '/remotes/origin/index-open-cleanup',
         '/remotes/origin/attr-export',
         '/remotes/origin/HEAD']
        """
        return [r[4:].encode('utf-8') for r in self.repo.listall_references() if r.startswith('refs/')]

    def get_parent_ref(self, path):
        """
        Finds the parent ref for a path.

        Example:
        >>> gitfs.get_parent_ref('/remotes/origin/master/README.md')
        '/remotes/origin/master'
        """
        matches = filter(lambda r: path.startswith(r + '/'), self.refs)
        if len(matches) != 1:
            raise FuseOSError(errno.ENOENT)
        return matches[0]

    def get_child_refs(self, path):
        """
        Finds the refs under a path.

        Example:
        >>> gitfs.get_child_refs('/remotes')
        ['/remotes/origin/master',
         '/remotes/origin/config-int-types',
         '/remotes/origin/index-open-cleanup',
         '/remotes/origin/attr-export',
         '/remotes/origin/HEAD']
        """
        return filter(lambda r: r.startswith(path), self.refs)

    def get_reference_commit(self, ref_name):
        """
        Gets the commit object for a named reference.

        Example:
        >>> gitfs.get_reference_commit('/remotes/origin/master')
        <_pygit2.Commit object at 0xb741d150>
        """
        ref = self.repo.lookup_reference('refs' + ref_name)
        return self.repo[ref.oid]

    def getattr(self, path, fh=None):
        if path.startswith('/.'):
            raise FuseOSError(errno.ENOENT)

        repo_stat = os.lstat(self.repo.path)
        default_stat = copy_stat(repo_stat)

        # If there are any refs under this path, return default stat
        if path == '/' or self.get_child_refs(path):
            return default_stat

        # If a parent ref for this path is found, get the path's tree entry
        parent = self.get_parent_ref(path)
        commit = self.get_reference_commit(parent)
        entry = git_tree_find(
            commit.tree,
            path[len(parent) + 1:],
        )

        if entry is None:
            raise FuseOSError(errno.ENOENT)

        # If entry is directory, return default stat
        if entry.filemode & stat.S_IFDIR == stat.S_IFDIR:
            return default_stat

        # If stand-alone file, set extra file stats
        blob = self.repo[entry.oid]
        size = len(blob.data)
        return copy_stat(repo_stat, st_size=size, st_mode=entry.filemode)

    def readdir(self, path, fh):
        refs = self.refs

        # Special case for root directory
        if path == '/':
            parts = [r.split('/') for r in refs]
            first_parts = [p[1] for p in parts if p]
            return list(frozenset(first_parts))

        # Path is a parent of a ref?  Example: /remotes
        # Find all refs that start with this path
        matching = filter(lambda r: r.startswith(path + '/'), refs)
        if matching:
            path_len = len(path) + 1
            first_parts = [r[path_len:].split('/', 1)[0] for r in matching if len(r) > path_len]
            return list(frozenset(first_parts))

        # Path is ref? Example: /heads/master
        if path in refs:
            ref = self.repo.lookup_reference('refs' + path)
            ref = ref.resolve()
            commit = self.repo[ref.oid]
            return list(git_tree_to_direntries(commit.tree))

        # Path is a child of a ref?  Example: /heads/master/dir1/subdir
        matching = filter(lambda r: path.startswith(r + '/'), refs)
        if len(matching) == 1:
            ref_name = matching[0]  # /heads/master
            ref = self.repo.lookup_reference('refs' + ref_name)
            commit = self.repo[ref.oid]

            # Get path of file under ref
            file_path = path[len(ref_name) + 1:]  # dir1/subdir

            # Get tree entry for the file path
            entry = git_tree_find(commit.tree, file_path)

            if entry is None:
                raise FuseOSError(errno.ENOENT)

            if entry.filemode & stat.S_IFDIR == stat.S_IFDIR:
                subtree = self.repo[entry.oid]
                return list(git_tree_to_direntries(subtree))
        # If, for some reason, more than one ref is found...
        elif len(matching) > 1:
            raise self.GitFSError(
                'Duplicate refs matching path in readdir query: {0}'.format(matching)
            )

        return []

    def open(self, path, flags):
        if path.startswith('/.'):
            return FuseOSError(errno.ENOENT)

        if flags & os.O_RDONLY != os.O_RDONLY:
            return FuseOSError(errno.EACCES)

        return 0

    def read(self, path, size, offset, fh):
        if path.startswith('/.'):
            return FuseOSError(errno.ENOENT)

        refs = self.refs

        # Path is a child of a ref?  Example: /heads/master/README.txt
        matching = filter(lambda r: path.startswith(r + '/'), refs)
        if len(matching) == 1:
            ref_name = matching[0]  # /heads/master
            ref = self.repo.lookup_reference('refs' + ref_name)
            commit = self.repo[ref.oid]

            file_path = path[len(ref_name) + 1:]  # README.txt

            entry = git_tree_find(commit.tree, file_path)

            if entry is None:
                return FuseOSError(errno.ENOENT)

            blob = entry.to_object()

            if offset == 0 and len(blob.data) <= size:
                return blob.data

            return blob.data[offset:offset + size]
        # If, for some reason, more than one ref is found...
        elif len(matching) > 1:
            raise self.GitFSError(
                'Duplicate refs matching path in read query: {0}'.format(matching)
            )

        return FuseOSError(errno.ENOENT)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Mounts the contents of a git repository in read-only mode using FUSE.'
    )
    parser.add_argument('git_path', metavar='<git_path>', help='Path to git repository.')
    parser.add_argument('mount_path', metavar='<mount_path>', help='Path to mount point.')

    if len(sys.argv) != 3:
        parser.print_help()
        sys.exit(0)

    logging.getLogger().setLevel(logging.DEBUG)

    args = parser.parse_args()
    fuse = FUSE(GitFS(args.git_path), args.mount_path, foreground=True, debug=True)