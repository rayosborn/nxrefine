# -----------------------------------------------------------------------------
# Copyright (c) 2022, Argonne National Laboratory.
#
# Distributed under the terms of an Open Source License.
#
# The full license is in the file LICENSE.pdf, distributed with this software.
# -----------------------------------------------------------------------------

from pathlib import Path

import numpy as np
from nexpy.gui.dialogs import GridParameters, NXDialog
from nexpy.gui.utils import confirm_action, report_error
from nexpy.gui.widgets import NXLabel, NXLineEdit
from nexusformat.nexus import (NeXusError, NXfield, NXlink, NXprocess, NXroot,
                               nxopen)

from nxrefine.nxrefine import NXRefine
from nxrefine.nxsettings import NXSettings


def show_dialog():
    try:
        dialog = ScanDialog()
        dialog.show()
    except NeXusError as error:
        report_error("Creating Scan", error)


class ScanDialog(NXDialog):

    def __init__(self, parent=None):
        super().__init__(parent)

        self.positions = 1
        self.entries = {}

        self.set_layout(self.filebox('Choose Parent File'),
                        self.close_layout(close=True))
        self.set_title('Creating New Scan(s)')

        self.set_title('New Scan')

    def choose_file(self):
        super().choose_file()
        self.parent_file = Path(self.get_filename())
        self.copy_parent()
        self.scan_box = NXLineEdit('300', align='right')
        self.scan_layout = self.make_layout(NXLabel(self.scan_label),
                                            self.scan_box,
                                            NXLabel(self.scan_units))
        self.insert_layout(1, self.scan_layout)
        self.insert_layout(2, self.action_buttons(('Make Scan File',
                                                   self.make_scan)))

    def copy_parent(self):
        self.parent_root = NXroot()
        with nxopen(self.parent_file, 'r') as root:
            self.scan_path = root['entry/nxreduce/scan_path'].nxvalue
            self.scan_units = root['entry/nxreduce/scan_units'].nxvalue
            self.scan_parent = root['entry/nxreduce/parent'].nxvalue
            for entry in root.entries:
                self.parent_root[entry] = root[entry]
        self.parent_root['entry/nxscans'] = self.scan_info()

    def scan_info(self):
        return NXprocess(parent=self.parent_file.name)

    @property
    def experiment_directory(self):
        return self.parent_file.parent.parent.parent

    @property
    def task_directory(self):
        return self.experiment_directory / 'tasks'

    @property
    def sample(self):
        return self.parent_root['entry/sample/name'].nxvalue

    @property
    def label(self):
        return self.parent_root['entry/sample/label'].nxvalue

    @property
    def scan_value(self):
        return float(self.scan_box.text())

    @property
    def scan_label(self):
        if self.scan_path:
            return Path(self.scan_path).name.replace('_', ' ').title()
        else:
            return 'Scan Value'

    @property
    def scan_directory(self):
        value = self.scan_value
        prefix = 'm' if value < 0 else ''
        value = abs(value)
        if isinstance(value, float):
            if value.is_integer():
                value_str = str(int(value))
            else:
                value_str = str(value).replace('.', 'p')
        else:
            value_str = str(value)
        scan_prefix = self.scan_parent.replace(self.sample, '').replace(
            'parent', '').strip('_')
        return f"{scan_prefix}_{prefix}{value_str}{self.scan_units}"

    @property
    def scan_name(self):
        return self.sample + '_' + self.scan_directory + '.nxs'

    def create_scan(self):
        scan_root = NXroot()
        for entry in self.parent_root.entries:
            scan_root[entry] = self.parent_root[entry]
            if entry != 'entry':
                data_link = scan_root[f"{entry}/data/data"]
                _target, _filename = data_link._target, data_link._filename
                scan_root[f"{entry}/data/data"] = NXlink(_target, _filename)
        scan_root[self.scan_path] = NXfield(self.scan_value,
                                            units=self.scan_units)
        from nexusformat.nexus.tree import string_dtype
        with nxopen(self.parent_file, 'rw') as parent_root:
            scan_info = parent_root['entry/nxscans']
            if 'filenames' not in scan_info:
                scan_info['filenames'] = NXfield(
                    [self.scan_name], dtype=string_dtype, maxshape=(None,))
                scan_info['select'] = NXfield(
                    [1], dtype=bool, maxshape=(None,))
            elif self.scan_name not in scan_info['filenames']:
                current_count = scan_info['filenames'].shape[0]
                scan_info['filenames'].resize((current_count + 1,))
                scan_info['filenames'][current_count] = self.scan_name
                scan_info['select'].resize((current_count + 1,))
                scan_info['select'][current_count] = 1
        return scan_root

    def make_scan(self):
        self.mainwindow.default_directory = str(self.experiment_directory)
        label_directory = self.experiment_directory / self.sample / self.label
        scan_directory = label_directory / self.scan_directory
        scan_directory.mkdir(exist_ok=True)
        scan_file = label_directory / self.scan_name
        if scan_file.exists() and not confirm_action(
                "Overwrite existing scan file?",
                f"'{scan_file}' already exists."):
            return
        scan_root = self.create_scan()
        scan_root.save(scan_file, 'w')
        new_scan_path = scan_file.relative_to(self.experiment_directory.parent)
        self.status_message.setText(f"Created scan file '{new_scan_path}'")
        if scan_file.stem in self.treeview.tree:
            self.treeview.tree[scan_file.stem].reload()
        else:
            self.treeview.tree.load(scan_file, 'rw')
        if self.parent_file.stem in self.treeview.tree:
            self.treeview.tree[self.parent_file.stem].reload()
