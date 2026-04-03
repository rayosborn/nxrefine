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
from nexusformat.nexus import (NeXusError, NXdata, NXfield, NXgoniometer,
                               NXlink, NXroot, NXsample, nxopen)

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
        self.parent_root = nxopen(self.parent_file, 'r')
        self.positions = len(self.parent_root.entries) - 1
        self.copy_parent()
        self.scan_path = self.parent_root['entry/nxreduce/scan_path'].nxvalue
        self.scan_units = self.parent_root['entry/nxreduce/scan_units'].nxvalue
        self.scan_box = NXLineEdit('300', align='right')
        self.scan_layout = self.make_layout(NXLabel(self.scan_label),
                                            self.scan_box,
                                            NXLabel(self.scan_units))
        self.insert_layout(1, self.scan_layout)
        self.insert_layout(2, self.action_buttons(('Make Scan File',
                                                   self.make_scan)))

    def copy_parent(self):
        self.scan_root = NXroot()
        for entry in self.parent_root.entries:
            self.scan_root[entry] = self.parent_root[entry]

    @property
    def experiment_directory(self):
        return self.parent_file.parent.parent.parent

    @property
    def task_directory(self):
        return self.experiment_directory / 'tasks'

    @property
    def sample(self):
        return self.scan_root['entry/sample/name'].nxvalue

    @property
    def label(self):
        return self.scan_root['entry/sample/label'].nxvalue

    @property
    def scan_value(self):
        return float(self.scan_box.text())

    @property
    def scan_label(self):
        if self.scan_path:
            return Path(self.scan_path).name.replace('_', ' ').capitalize()
        else:
            return 'scan'

    @property
    def scan_prefix(self):
        return self.parent_root['entry/nxreduce/parent'].nxvalue

    @property
    def scan_suffix(self):
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
        return f"{prefix}{value_str}{self.scan_units}"

    @property
    def scan_name(self):
        return self.scan_prefix + '_' + self.scan_suffix + '.nxs'

    def update_scan(self):
        self.scan_root[self.scan_path] = NXfield(self.scan_value,
                                                 units=self.scan_units)
        for entry in [e for e in self.scan_root.entries if e != 'entry']:
            data_link = self.scan_root[f"{entry}/data/data"]
            _target, _filename = data_link._target, data_link._filename
            del self.scan_root[f"{entry}/data/data"]
            self.scan_root[f"{entry}/data/data"] = NXlink(_target, _filename)

    def make_scan(self):
        self.mainwindow.default_directory = str(self.experiment_directory)
        label_directory = self.experiment_directory / self.sample / self.label
        self.scan_directory = label_directory / self.scan_suffix
        self.scan_directory.mkdir(exist_ok=True)
        scan_file = label_directory / self.scan_name
        if scan_file.exists() and not confirm_action(
                "Overwrite existing scan file?",
                f"'{scan_file}' already exists."):
            return
        self.update_scan()
        self.scan_root.save(scan_file, 'w')
        self.status_message.setText(f"Created scan file '{scan_file}'")
        self.treeview.tree.load(scan_file, 'rw')
