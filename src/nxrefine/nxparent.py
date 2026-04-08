# -----------------------------------------------------------------------------
# Copyright (c) 2026, Argonne National Laboratory.
#
# Distributed under the terms of an Open Source License.
#
# The full license is in the file LICENSE.pdf, distributed with this software.
# -----------------------------------------------------------------------------
from pathlib import PosixPath as Path

from nexusformat.nexus import nxconsolidate, nxopen

class NXParent:

    def __init__(self, filename=None):
        self.parent_file = filename
        with nxopen(self.parent_file) as root:
            self.root = root

    def __repr__(self):
        return f"NXParent('{self.parent_file.name}')"

    @property
    def filenames(self):
        if ('nxscans' in self.root['entry'] and 
                'filenames' in self.root['entry/nxscans']):
            return self.root['entry/nxscans/filenames'].nxvalue
        else:
            return None

    @property
    def selected(self):
        if ('nxscans' in self.root['entry'] and 
                'selected' in self.root['entry/nxscans']):
            return self.root['entry/nxscans/selected'].nxvalue
        else:
            return None

    @property
    def selected_files(self):
        if self.filenames is not None and self.selected is not None:
            return [f for f, s in zip(self.filenames, self.selected) if s]
        else:
            return None

    @property
    def scan_path(self):
        if ('nxreduce' in self.root['entry'] and
                'scan_path' in self.root['entry/nxreduce']):
            return self.root['entry/nxreduce/scan_path'].nxvalue
        else:
            return '/entry/sample/temperature'

    @property
    def scan_units(self):
        if ('nxreduce' in self.root['entry'] and
                'scan_units' in self.root['entry/nxreduce']):
            return self.root['entry/nxreduce/scan_units'].nxvalue
        else:
            return 'K'

    def create_scan_data(self, data_path):
        """Create the consolidated scan data."""
        if self.selected_files is not None:
            with nxopen(self.parent_file, 'rw') as root:
                if data_path in root:
                    del root[data_path]
                root[data_path] = nxconsolidate(self.selected_files,
                                                data_path, self.scan_path)
        else:
            return None
