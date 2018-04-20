from __future__ import unicode_literals
import os
import numpy as np
from operator import attrgetter
from nexusformat.nexus import *
from nexpy.gui.datadialogs import BaseDialog, GridParameters
from nexpy.gui.utils import report_error, confirm_action, natural_sort
from nexpy.gui.pyqt import getSaveFileName

def show_dialog():
    try:
        dialog = MakeDialog()
        dialog.show()
    except NeXusError as error:
        report_error("Making Scan Macro", error)


class MakeDialog(BaseDialog):

    def __init__(self, parent=None):
        super(MakeDialog, self).__init__(parent)
        self.scans = None
        self.set_layout(self.directorybox("Choose Sample Directory",
                                          self.choose_sample),
                        self.textboxes(('Scan Command', 'Pil2Mscan')),
                        self.action_buttons(('Select All', self.select_scans),
                                            ('Reverse All', self.reverse_scans),
                                            ('Clear All', self.clear_scans),
                                            ('Make Scan Macro', self.make_scans)),
                        self.close_buttons(close=True))
        self.set_title('Make Scans')

    def choose_sample(self):
        super(MakeDialog, self).choose_directory()
        self.sample_directory = self.get_directory()
        self.experiment_directory = os.path.dirname(os.path.dirname(self.sample_directory))
        self.macro_directory = os.path.join(self.experiment_directory, 'macros')
        label = os.path.basename(self.sample_directory)
        sample = os.path.basename(os.path.dirname(self.sample_directory))
        experiment = os.path.basename(self.experiment_directory)
        self.experiment_path = experiment
        self.scan_path = os.path.join(experiment, sample, label)
        self.setup_scans()

    def setup_scans(self):
        if self.scans:
            self.scans.delete_grid()
        self.scans = GridParameters()
        filenames = sorted([f for f in os.listdir(self.sample_directory) 
                            if f.endswith('.nxs')], key=natural_sort)
        for i, f in enumerate(filenames):
            scan = 'f%d' % i
            self.scans.add(scan, i+1, f, True, self.update_scans)
            self.scans[scan].checkbox.stateChanged.connect(self.update_scans)
        self.insert_layout(2, self.scans.grid(header=False))

    @property
    def scan_list(self):
        scan_list = []
        for scan in self.scans.values():
            if scan.checkbox.isChecked() and scan.value > 0:
                scan_list.append(scan)
            else:
                scan.value = 0
        return sorted(scan_list, key=attrgetter('value'))

    def update_scans(self):
        scan_list = self.scan_list
        scan_number = 0
        for scan in scan_list:
            scan_number += 1
            scan.value = scan_number                        
        for scan in self.scans.values():
            if scan.checkbox.isChecked() and scan.value == 0:
                scan.value = scan_number + 1
                scan_number += 1        

    def select_scans(self):
        for i, scan in enumerate(self.scans):
            self.scans[scan].value = i+1
            self.scans[scan].checkbox.setChecked(True)

    def reverse_scans(self):
        for i, scan in enumerate(reversed(self.scan_list)):
            scan.value = i+1
            scan.checkbox.setChecked(True)

    def clear_scans(self):
        for scan in self.scans:
            self.scans[scan].value = 0
            self.scans[scan].checkbox.setChecked(False)

    def make_scans(self):
        scans = [scan.label.text() for scan in self.scan_list]  
        scan_command = self.textbox['Scan Command'].text()
        scan_parameters = ['#command path detx dety temperature ' + 
                           'phi_start phi_step phi_end chi omega frame_rate']
        for scan in self.scan_list:
            nexus_file = scan.label.text()
            root = nxload(os.path.join(self.sample_directory, nexus_file))
            temperature = root.entry.sample.temperature
            for entry in [root[e] for e in root if e != 'entry']:
                phi = entry['instrument/goniometer/phi']
                phi_start, phi_step, phi_end = phi[0], phi[1]-phi[0], phi[-1]
                chi = entry['instrument/goniometer/chi']
                omega = entry['instrument/goniometer/omega']
                dx = entry['instrument/detector/translation_x']
                dy = entry['instrument/detector/translation_y']
                if ('frame_time' in entry['instrument/detector'] and
                    entry['instrument/detector/frame_time'] > 0.0):
                    frame_rate = 1.0 / entry['instrument/detector/frame_time']
                else:
                    frame_rate = 10.0                        
                scan_parameters.append(
                    '%s %s %s %.6g %.6g %.6g %.6g %.6g %.6g %.6g %.6g %.6g'
                    % (scan_command, self.scan_path, entry.nxname, 
                       dx, dy, temperature, 
                       phi_start, phi_step, phi_end,
                       chi, omega, frame_rate))
        if not os.path.exists(self.macro_directory):
            os.mkdir(os.path.join(self.experiment_directory, 'macros'))
        macro_filter = ';;'.join(("SPEC Macro (*.mac)", "Any Files (*.* *)"))
        macro = getSaveFileName(self, 'Open Macro', self.macro_directory,
                                macro_filter)
        with open(macro, 'w') as f:
            f.write('\n'.join(scan_parameters))
        f.close()