# Copyright 2006 Joe Wreschnig
#           2011-2021 Nick Boultbee
#           2013,2014 Christoph Reiter
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.


import os
import shutil
from typing import (Collection, TypeVar, Sequence, Iterable,
                    Optional, Iterator, Generic, MutableMapping, Tuple, Set)

from gi.repository import GObject

from quodlibet import util
from quodlibet.formats import (load_audio_files,
                               dump_audio_files, SerializationError)
from quodlibet.formats._audio import HasKey
from quodlibet.util.atomic import atomic_save
from quodlibet.util.collections import DictMixin
from quodlibet.util.dprint import print_d, print_w
from quodlibet.util.path import (mkdir, ishidden)
from senf import fsnative

K = TypeVar("K", covariant=True)
V = TypeVar("V", bound=HasKey)


class Library(GObject.GObject, DictMixin, Generic[K, V]):
    """A Library contains useful objects.

    The only required method these objects support is a .key
    attribute, but specific types of libraries may require more
    advanced interfaces.

    Every method which takes a sequence of items expects items to
    implement __iter__, __len__ and __contains__.

    Likewise the signals emit sequences which implement
    __iter__, __len__ and __contains__ e.g. set(), list() or tuple().

    WARNING: The library implements the dict interface with the exception
    that iterating over it yields values and not keys.
    """

    __gsignals__ = {
        'changed': (GObject.SignalFlags.RUN_LAST, None, (object,)),
        'removed': (GObject.SignalFlags.RUN_LAST, None, (object,)),
        'added': (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    librarian = None
    dirty = False

    def __init__(self, name: Optional[str] = None):
        super().__init__()
        self._contents: MutableMapping[K, V] = {}
        self._name = name
        if self.librarian is not None and name is not None:
            self.librarian.register(self, name)

    def __str__(self):
        return f"<{type(self).__name__} @ {hex(id(self))}>"

    def destroy(self):
        if self.librarian is not None and self._name is not None:
            self.librarian._unregister(self, self._name)

    def changed(self, items: Collection[V]):
        """Alert other users that these items have changed.

        This causes a 'changed' signal. If a librarian is available
        this function will call its changed method instead, and all
        libraries that librarian manages may fire a 'changed' signal.

        The item list may be filtered to those items actually in the
        library. If a librarian is available, it will handle the
        filtering instead. That means if this method is delegated to
        the librarian, this library's changed signal may not fire, but
        another's might.
        """

        if not items:
            return
        if self.librarian and self in self.librarian.libraries.values():
            print_d(f"Changing {len(items)} items via librarian.", self._name)
            self.librarian.changed(items)
        else:
            items = {item for item in items if item in self}
            if not items:
                return
            self._changed(items)

    def _changed(self, items: Collection[V]):
        """Called by the changed method and Librarians."""
        # assert isinstance(items, set)

        if not items:
            return
        print_d(f"Emitting changed for {len(items)} item(s) "
                f"(e.g. {list(items)[0].key!r}...) from {self}")
        self.dirty = True
        self.emit('changed', items)

    def __iter__(self) -> Iterator[V]:
        """Iterate over the items in the library."""
        return iter(self._contents.values())

    def iteritems(self) -> Iterator[Tuple[K, V]]:
        return iter(self._contents.items())

    def iterkeys(self) -> Iterator[K]:
        return iter(self._contents.keys())

    def itervalues(self) -> Iterator[V]:
        return iter(self._contents.values())

    def __len__(self) -> int:
        """The number of items in the library."""
        return len(self._contents)

    def __getitem__(self, key) -> V:
        """Find a item given its key."""
        return self._contents[key]

    def __contains__(self, item):
        """Check if a key or item is in the library."""
        try:
            return item in self._contents or item.key in self._contents
        except AttributeError:
            return False

    def get_content(self) -> Sequence[V]:
        """All items including hidden ones for saving the library
           (see FileLibrary with masked items)
        """
        return list(self.values())

    def keys(self) -> Iterable[K]:
        return self._contents.keys()

    def values(self) -> Iterable[V]:
        return self._contents.values()

    def _load_item(self, item: V) -> None:
        """Load (add) an item into this library"""
        # Subclasses should override this if they want to check
        # item validity; see `FileLibrary`.
        print_d(f"Loading {item.key!r}", self._name)
        self.dirty = True
        self._contents[item.key] = item

    def _load_init(self, items: Iterable[V]) -> None:
        """Load many items into the library (on start)"""
        # Subclasses should override this if they want to check
        # item validity; see `FileLibrary`.
        content = self._contents
        for item in items:
            content[item.key] = item

    def add(self, items: Iterable[V]) -> Set[V]:
        """Add items. This causes an 'added' signal.

        Return the sequence of items actually added, filtering out items
        already in the library.
        """

        items = {item for item in items if item not in self}
        if not items:
            return items
        if len(items) == 1:
            print_d(f"Adding {next(iter(items))}", self._name)
        else:
            print_d(f"Adding {len(items)} items.", self._name)
        for item in items:
            self._contents[item.key] = item

        self.dirty = True
        self.emit('added', items)
        return items

    def remove(self, items: Iterable[V]) -> Iterable[V]:
        """Remove items. This causes a 'removed' signal.

        Return the sequence of items actually removed.
        """

        items = {item for item in items if item in self}
        if not items:
            return items

        print_d(f"Removing {len(items)} item(s).", self._name)
        for item in items:
            del self._contents[item.key]

        self.dirty = True
        self.emit('removed', items)
        return items


def _load_items(filename) -> Iterable[V]:
    """Load items from disk.

    In case of an error returns default or an empty list.
    """

    try:
        with open(filename, "rb") as fp:
            data = fp.read()
    except EnvironmentError:
        print_w("Couldn't load library file from: %r" % filename)
        return []

    try:
        items = load_audio_files(data)
    except SerializationError:
        # there are too many ways this could fail
        util.print_exc()

        # move the broken file out of the way
        try:
            shutil.copy(filename, filename + ".not-valid")
        except EnvironmentError:
            util.print_exc()

        return []

    return items


class PicklingMixin:
    """A mixin to provide persistence of a library by pickling to disk"""

    filename = None

    def load(self, filename):
        """Load a library from a file, containing a picked list.

        Loading does not cause added, changed, or removed signals.
        """

        self.filename = filename
        print_d("Loading contents of %r." % filename, self)

        items = _load_items(filename)

        # this loads all items without checking their validity, but makes
        # sure that non-mounted items are masked
        self._load_init(items)

        print_d(f"Done loading contents of {filename!r}", self._name)

    def save(self, filename=None):
        """Save the library to the given filename, or the default if `None`"""

        if filename is None:
            filename = self.filename

        print_d(f"Saving contents to {filename!r}", self._name)

        try:
            dirname = os.path.dirname(filename)
            mkdir(dirname)
            with atomic_save(filename, "wb") as fileobj:
                fileobj.write(dump_audio_files(self.get_content()))
        except SerializationError:
            # Can happen when we try to pickle while the library is being
            # modified, like in the periodic 15min save.
            # Ignore, as it should try again later or on program exit.
            util.print_exc()
        except EnvironmentError:
            print_w(f"Couldn't save library to path {filename!r}")
        else:
            self.dirty = False


class PicklingLibrary(Library[K, V], PicklingMixin):
    """A library that pickles its contents to disk"""

    def __init__(self, name=None):
        print_d("Using pickling persistence for library \"%s\"" % name)
        PicklingMixin.__init__(self)
        Library.__init__(self, name)


def iter_paths(root, exclude=[], skip_hidden=True):
    """yields paths contained in root (symlinks dereferenced)

    Any path starting with any of the path parts included in exclude
    are ignored (before and after dereferencing symlinks)

    Directory symlinks are not followed (except root itself)

    Args:
        root (fsnative)
        exclude (List[fsnative])
        skip_hidden (bool): Ignore files which are hidden or where any
            of the parent directories are hidden.
    Yields:
        fsnative: absolute dereferenced paths
    """

    assert isinstance(root, fsnative)
    assert all((isinstance(p, fsnative) for p in exclude))
    assert os.path.abspath(root)

    def skip(path):
        if skip_hidden and ishidden(path):
            return True
        # FIXME: normalize paths..
        return any((path.startswith(p) for p in exclude))

    if skip_hidden and ishidden(root):
        return

    for path, dnames, fnames in os.walk(root):
        if skip_hidden:
            dnames[:] = list(filter(
                lambda d: not ishidden(os.path.join(path, d)), dnames))
        for filename in fnames:
            fullfilename = os.path.join(path, filename)
            if skip(fullfilename):
                continue
            fullfilename = os.path.realpath(fullfilename)
            if skip(fullfilename):
                continue
            yield fullfilename
