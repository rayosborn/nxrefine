# -----------------------------------------------------------------------------
# Copyright (c) 2022, Argonne National Laboratory.
#
# Distributed under the terms of an Open Source License.
#
# The full license is in the file LICENSE.pdf, distributed with this software.
# -----------------------------------------------------------------------------

import os

from nexpy.gui.pyqt import getSaveFileName
from nexpy.gui.utils import natural_sort, report_error
from nexpy.gui.widgets import (NXDialog, NXLabel, NXLineEdit, NXScrollArea,
                               NXWidget)
from nexusformat.nexus import NeXusError

from nxrefine.nxreduce import NXMultiReduce
from nxrefine.nxserver import NXServer


def show_dialog():
    try:
        dialog = SumDialog()
        dialog.show()
    except NeXusError as error:
        report_error("Summing Scans", error)


class SumDialog(NXDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scans = None
        self.prefix_box = NXLineEdit()
        self.prefix_box.textChanged.connect(self.select_prefix)
        prefix_layout = self.make_layout(NXLabel('Prefix'),
                                         self.prefix_box)
        self.set_layout(
            self.directorybox("Choose Sample Directory", self.choose_sample),
            self.action_buttons(
                ('Select All', self.select_scans),
                ('Clear All', self.clear_scans),
                ('Sum Scans', self.sum_scans)),
            self.checkboxes(
                ('update', 'Update Existing Sums', False),
                ('overwrite', 'Overwrite Existing Sums', False)),
            prefix_layout, self.close_layout(close=True))
        self.set_title('Sum Files')

    def choose_sample(self):
        super().choose_directory()
        self.sample_directory = self.get_directory()
        self.experiment_directory = os.path.dirname(
            os.path.dirname(self.sample_directory))
        self.sample = os.path.basename(os.path.dirname(self.sample_directory))
        self.setup_scans()

    def setup_scans(self):
        scans = []
        all_files = [self.sample+'_'+d+'.nxs'
                     for d in os.listdir(self.sample_directory)
                     if os.path.isdir(os.path.join(self.sample_directory, d))]
        filenames = sorted([f for f in all_files if os.path.exists(
            os.path.join(self.sample_directory, f))], key=natural_sort)
        for i, f in enumerate(filenames):
            scan = f'f{i}'
            scans.append((scan, f, False))
        widget = NXWidget()
        widget.set_layout('stretch',
                          self.checkboxes(*scans, vertical=True),
                          'stretch')
        widget.layout.setSpacing(0)
        self.scroll_area = NXScrollArea(widget)
        self.insert_layout(4, self.scroll_area)
        scan_files = [self.checkbox[c].text() for c in self.scan_boxes]
        self.prefix_box.setText(os.path.commonprefix(scan_files))

    def select_prefix(self):
        prefix = self.prefix_box.text()
        for f in self.checkbox:
            if self.checkbox[f].text().startswith(prefix):
                self.checkbox[f].setChecked(True)
            else:
                self.checkbox[f].setChecked(False)

    @property
    def scan_boxes(self):
        return [s for s in self.checkbox if s not in ['update', 'overwrite']]

    @property
    def scan_list(self):
        scans = []
        for scan in self.scan_boxes:
            if self.checkbox[scan].isChecked():
                base_name = os.path.splitext(self.checkbox[scan].text())[0]
                scans.append(base_name.replace(self.sample+'_', ''))
        return scans

    @property
    def scan_files(self):
        scan_files = []
        for scan in self.scan_boxes:
            if self.checkbox[scan].isChecked():
                scan_files.append(self.checkbox[scan].text())
        return scan_files

    @property
    def overwrite(self):
        return self.checkbox['overwrite'].isChecked()

    @property
    def update(self):
        return self.checkbox['update'].isChecked()

    def get_label(self, scan_file):
        base_name = os.path.basename(os.path.splitext(scan_file)[0])
        return base_name.replace(self.sample+'_', '')

    def select_scans(self):
        for scan in self.scan_boxes:
            self.checkbox[scan].setChecked(True)

    def clear_scans(self):
        for scan in self.scan_boxes:
            self.checkbox[scan].setChecked(False)

    def sum_scans(self):
        if not self.scan_files:
            raise NeXusError("No files selected")
        server = NXServer()
        scan_filter = ';;'.join(("NeXus Files (*.nxs)", "Any Files (*.* *)"))
        scan_prefix = self.scan_files[0][:self.scan_files[0].rindex('_')]
        preferred_name = os.path.join(self.sample_directory,
                                      scan_prefix+'_sum.nxs')
        scan_file = getSaveFileName(self, 'Choose Summed File Name',
                                    preferred_name, scan_filter)
        if not scan_file:
            return
        prefix = self.sample + '_'
        if not os.path.basename(scan_file).startswith(prefix):
            raise NeXusError(f"Summed file name must start with '{prefix}'")
        self.scan_label = self.get_label(scan_file)
        scan_dir = os.path.join(self.sample_directory, self.scan_label)
        reduce = NXMultiReduce(directory=scan_dir, overwrite=True)
        reduce.nxsum(self.scan_list)
        self.treeview.tree.load(scan_file, 'rw')
        command = f'nxsum -d {scan_dir}'
        if self.update:
            command += ' -u'
        if self.overwrite:
            command += ' -o'
        for entry in reduce.entries:
            server.add_task('{} -e {} -s {}'.format(command, entry,
                                                    ' '.join(self.scan_list)))
