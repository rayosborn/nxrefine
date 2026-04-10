# -----------------------------------------------------------------------------
# Copyright (c) 2026, Argonne National Laboratory.
#
# Distributed under the terms of an Open Source License.
#
# The full license is in the file LICENSE.pdf, distributed with this software.
# -----------------------------------------------------------------------------
from pathlib import Path as Path

from nexusformat.nexus import NXfield, NXprocess, nxconsolidate, nxopen

class NXParent:

    def __init__(self, filename=None):
        if not str(filename).endswith('_parent.nxs'):
            raise ValueError("Parent file must end with '_parent.nxs'")
        self.filename = Path(filename).resolve()
        self.name = self.filename.name
        self.root = nxopen(self.filename)

    def __repr__(self):
        return f"NXParent('{self.filename.name}')"

    @property
    def sample(self):
        return self.filename.parent.parent.name

    @property
    def label(self):
        return self.filename.parent.name

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
            return [str(self.filename.parent / f) for f, s
                    in zip(self.filenames, self.selected) if s]
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
            with nxopen(self.filename, 'rw') as root:
                if data_path in root:
                    del root[data_path]
                root[data_path] = nxconsolidate(self.selected_files,
                                                data_path, self.scan_path)
        else:
            return None

    def add_filename(self, filename, selected=True):
        filename = Path(filename)
        if not filename.is_absolute():
            filename = self.filename.parent / filename
        if not filename.is_file():
            raise ValueError(f"File '{filename}' does not exist.")
        from nexusformat.nexus.tree import string_dtype
        with nxopen(self.filename, 'rw') as parent_root:
            scan_info = parent_root['entry/nxscans']
            if 'filenames' not in scan_info:
                scan_info['filenames'] = NXfield(
                    [filename.name], dtype=string_dtype, maxshape=(None,))
                scan_info['selected'] = NXfield([selected], dtype=bool,
                                                maxshape=(None,))
            elif filename.name not in scan_info['filenames']:
                current_count = scan_info['filenames'].shape[0]
                scan_info['filenames'].resize((current_count + 1,))
                scan_info['filenames'][current_count] = filename.name
                scan_info['selected'].resize((current_count + 1,))
                scan_info['selected'][current_count] = selected

    def add_filenames(self, selected=True):
        directory = self.filename.parent
        pattern = self.filename.name.replace('_parent.nxs', '_*.nxs')
        for filename in [f for f in directory.glob(pattern)
                         if f.name != self.name]:
            self.add_filename(filename, selected)
