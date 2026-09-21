import json

from imswitch.imcommon.framework import Signal, SignalInterface

# Sentinel prefix marking an HDF5/Zarr attribute value that was JSON-encoded
# because the storage backend can't hold it natively (e.g. a dict or ragged
# list). Written by RecordingManager's storers, decoded back here so values
# like per-device ScanTTL timing maps survive a save/load round trip instead
# of being silently dropped.
JSON_ATTR_PREFIX = '__imswitch_json__:'

# Attribute category holding free text the operator wrote about this session
# rather than a value some device reported (Tools -> Session notes...). It is a
# category of its own so the recording storers can find it again -- both to
# write it into the structured metadata group and to turn it into an OME
# annotation, which is the only way it reaches an OME-TIFF at all.
NOTES_ATTR_CATEGORY = 'notes'

#: The note that is a sentence rather than a key/value pair.
SESSION_NOTE_KEY = (NOTES_ATTR_CATEGORY, 'session')


class SharedAttributes(SignalInterface):
    sigAttributeSet = Signal(object, object)  # (key, value)

    def __init__(self):
        super().__init__()
        self._data = {}

    def getHDF5Attributes(self):
        """ Returns a dictionary of HDF5 attributes representing this object.
        """
        attrs = {}
        for key, value in self._data.items():
            attrs[':'.join(key)] = value

        return attrs

    def getJSON(self):
        """ Returns a JSON representation of this instance. """
        attrs = {}
        for key, value in self._data.items():
            parent = attrs
            for i in range(len(key) - 1):
                if key[i] not in parent:
                    parent[key[i]] = {}
                parent = parent[key[i]]

            parent[key[-1]] = value

        return json.dumps(attrs)

    def getSessionNote(self) -> str:
        """The operator's free-text note for this session, '' when unset."""
        value = self._data.get(SESSION_NOTE_KEY, '')
        return '' if value is None else str(value)

    def setSessionNote(self, note) -> None:
        """Publish the operator's free-text note for this session.

        Clearing the note stores an empty string rather than removing the key:
        every consumer already treats blank as "no note", and a recording made
        after the note was cleared should not silently inherit it.
        """
        self[SESSION_NOTE_KEY] = '' if note is None else str(note)

    def update(self, data):
        """ Updates this object with the data in the given dictionary or
        SharedAttributes object. """
        if isinstance(data, SharedAttributes):
            data = data._data

        for key, value in data.items():
            self[key] = value

    def __getitem__(self, key):
        self._validateKey(key)
        return self._data[key]

    def __setitem__(self, key, value):
        self._validateKey(key)
        self._data[key] = value
        self.sigAttributeSet.emit(key, value)

    def __iter__(self):
        yield from self._data.items()

    @classmethod
    def fromHDF5File(cls, file, dataset):
        """ Loads the attributes from a HDF5 file into a SharedAttributes
        object. """
        return cls._fromStructuredGroup(file[dataset])

    @classmethod
    def fromZarrStore(cls, root, dataset):
        """ Loads the attributes from a Zarr store into a SharedAttributes
        object. Twin of fromHDF5File for the Zarr recording layout. """
        return cls._fromStructuredGroup(root[dataset])

    @classmethod
    def _fromStructuredGroup(cls, group):
        """ Reads attrs from a detector group written by RecordingManager's
        HDF5Storer/ZarrStorer: flat legacy attrs directly on the group, plus
        the current structured ``metadata/<category>/`` subgroup layout. """
        attrs = cls()
        for key, value in dict(group.attrs).items():
            cls._setFlatKey(attrs, key, value)

        metaGroup = group.get('metadata') if hasattr(group, 'get') else None
        if metaGroup is not None:
            cls._collectMetadataAttrs(attrs, metaGroup, [])
        return attrs

    @classmethod
    def _collectMetadataAttrs(cls, attrs, group, prefix):
        for key, value in dict(group.attrs).items():
            cls._setFlatKey(attrs, ':'.join([*prefix, key]), value)
        for name in group.keys():
            child = group[name]
            if hasattr(child, 'keys'):  # subgroup (h5py.Group / zarr.Group)
                cls._collectMetadataAttrs(attrs, child, [*prefix, name])

    @classmethod
    def _setFlatKey(cls, attrs, key, value):
        attrs[tuple(key.split(':'))] = cls._decodeAttrValue(value)

    @staticmethod
    def _decodeAttrValue(value):
        if isinstance(value, str) and value.startswith(JSON_ATTR_PREFIX):
            try:
                return json.loads(value[len(JSON_ATTR_PREFIX):])
            except (json.JSONDecodeError, TypeError, ValueError):
                return value
        return value

    @staticmethod
    def _validateKey(key):
        if type(key) is not tuple:
            raise TypeError('Key must be a tuple of strings')

        for keySegment in key:
            if not isinstance(keySegment, str):
                raise TypeError('Key must be a tuple of strings')

            if ':' in keySegment:
                raise KeyError('Key must not contain ":"')


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
