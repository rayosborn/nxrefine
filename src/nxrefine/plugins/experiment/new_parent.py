# -----------------------------------------------------------------------------
# Copyright (c) 2026, Argonne National Laboratory.
#
# Distributed under the terms of an Open Source License.
#
# The full license is in the file LICENSE.pdf, distributed with this software.
# -----------------------------------------------------------------------------

from pathlib import Path

from matplotlib.pyplot import sca
import numpy as np
from nexpy.gui.dialogs import GridParameters, NXDialog
from nexpy.gui.plotview import NXPlotView
from nexpy.gui.utils import confirm_action, report_error
from nexusformat.nexus import (NeXusError, NXdata, NXfield, NXparameters,
                               NXprocess, NXroot, NXsample, nxopen)

from nxrefine.nxsettings import NXSettings
from nxrefine.nxutils import detector_flipped


def show_dialog():
    try:
        dialog = ParentDialog()
        dialog.show()
    except NeXusError as error:
        report_error("Creating New Parent", error)


class ParentDialog(NXDialog):

    def __init__(self, parent=None):
        super().__init__(parent)

        self.set_layout(self.directorybox('Choose Experiment Directory',
                                          default=False), 
                        self.close_buttons(save=True))
        self.set_title('New Parent')
        self.parent_root = None
        settings = NXSettings().settings
        self.analysis_path = settings['instrument']['analysis_path']

    def choose_directory(self):
        super().choose_directory()
        self.settings = NXSettings(self.task_directory).settings
        self.mainwindow.default_directory = str(self.experiment_directory)
        self.sample_box = self.select_box(self.get_samples())
        self.sample_layout = self.make_layout(
            self.action_buttons(('Choose Sample', self.choose_sample)),
            self.sample_box)
        self.insert_layout(1, self.sample_layout)
        self.activate()

    def get_samples(self):
        if self.experiment_directory.exists():
            sample_directories = [f for 
                                  f in self.experiment_directory.iterdir()
                                  if f.is_dir()]
        else:
            return []
        samples = []
        for sample_directory in sample_directories:
            label_directories = [f for f in sample_directory.iterdir()
                                 if f.is_dir()]
            for label_directory in label_directories:
                samples.append(
                    label_directory.relative_to(self.experiment_directory))
        return sorted([str(sample) for sample in samples])

    def choose_sample(self):
        self.configuration_box = self.select_box(self.get_configurations())
        self.configuration_layout = self.make_layout(
            self.action_buttons(('Choose Experiment Configuration',
                                 self.choose_configuration)),
            self.configuration_box)
        self.insert_layout(2, self.configuration_layout)

    def get_configurations(self):
        directory = self.experiment_directory / 'configurations'
        if directory.exists():
            return sorted([str(f.name) for f in directory.glob('*.nxs')])
        else:
            return []

    def choose_configuration(self):
        config_file = (self.experiment_directory / 'configurations' /
                       self.configuration)

        default = self.settings['nxreduce']

        self.parameters = GridParameters()
        self.parameters.add('parent', self.sample, 'Parent Prefix')
        self.parameters.add('threshold', default['threshold'],
                            'Peak Threshold')
        self.parameters.add('first', default['first_frame'], 'First Frame')
        self.parameters.add('last', default['last_frame'], 'Last Frame')
        self.parameters.add('polar_max', default['polar_max'],
                            'Max. Polar Angle')
        self.parameters.add('hkl_tolerance', default['hkl_tolerance'],
                            'HKL Tolerance (Å-1)')
        self.parameters.add('monitor', default['monitor'],
                            'Normalization Monitor')
        self.parameters['monitor'].value = default['monitor']
        self.parameters.add('norm', default['norm'], 'Normalization Value')
        self.parameters.add('qmin', default['qmin'],
                            'Minimum Scattering  Q (Å-1)')
        self.parameters.add('qmax', default['qmax'], 'Maximum Taper Q (Å-1)')
        self.parameters.add('radius', default['radius'], 'Punch Radius (Å)')
        self.parameters.add('scan_path', default['scan_path'], 'Scan Path')
        self.parameters.add('scan_units', default['scan_units'], 'Scan Units')

        self.parameters_grid = self.parameters.grid(header=False, width=200)
        self.parameters_grid.setHorizontalSpacing(10)
        self.parameters_layout = self.make_layout(self.parameters_grid)

        self.parent_root = NXroot()
        with nxopen(config_file, 'r') as root:
            for entry in root.entries:
                self.parent_root[entry] = root[entry]
        if 'nxreduce' in self.parent_root['entry']:
            for p in [p for p in self.parent_root['entry/nxreduce']
                      if p in self.parameters]:
                self.parameters[p].value = (
                    self.parent_root['entry/nxreduce'][p].nxvalue)

        self.insert_layout(3, self.parameters_layout)
        self.insert_layout(4, self.action_buttons(('Plot Q-Limits',
                                                   self.plot_Q_limits)))

    @property
    def experiment_directory(self):
        directory = Path(self.get_directory())
        if self.analysis_path and directory.name != self.analysis_path:
            directory = directory / self.analysis_path
        return directory

    @property
    def task_directory(self):
        return self.experiment_directory / 'tasks'

    @property
    def configuration(self):
        return self.configuration_box.currentText()

    @property
    def sample(self):
        return Path(self.sample_box.currentText()).parent.name

    @property
    def label(self):
        return Path(self.sample_box.currentText()).name

    @property
    def instrument(self):
        entry = [e for e in self.parent_root if e != 'entry'][0]
        return self.parent_root[f'{entry}/instrument']

    @property
    def wavelength(self):
        return self.instrument['monochromator/wavelength'].nxvalue

    @property
    def distance(self):
        return self.instrument['detector/distance'].nxvalue

    @property
    def pixel_size(self):
        return self.instrument['detector/pixel_size'].nxvalue

    @property
    def xc(self):
        return self.instrument['detector/beam_center_x'].nxvalue

    @property
    def yc(self):
        return self.instrument['detector/beam_center_y'].nxvalue

    @property
    def shape(self):
        return self.instrument['detector/pixel_mask'].shape

    @property
    def qmin(self):
        return float(self.parameters['qmin'].value)

    @property
    def qmax(self):
        return float(self.parameters['qmax'].value)

    @property
    def pv(self):
        if 'Q-Limits' in self.plotviews:
            return self.plotviews['Q-Limits']
        else:
            return NXPlotView('Q-Limits')


    def transmission_coordinates(self):
        """
        Generate a mask array for excluding pixels outside of the
        specified transmission coordinate range.

        Parameters
        ----------
        None

        Returns
        -------
        array-like
            A 2D boolean mask array with the same shape as the data. The
            mask is True for pixels with transmission coordinates
            outside of the specified range and False otherwise.
        """
        min_radius = (self.qmin * self.wavelength * self.distance
                      / (2 * np.pi * self.pixel_size))
        max_radius = (self.qmax * self.wavelength * self.distance
                      / (2 * np.pi * self.pixel_size))
        x = np.arange(self.shape[1])
        y = np.arange(self.shape[0])
        min_mask = ((x[np.newaxis, :]-self.xc)**2
                    + (y[:, np.newaxis]-self.yc)**2 < min_radius**2)
        max_mask = ((x[np.newaxis, :]-self.xc)**2
                    + (y[:, np.newaxis]-self.yc)**2 > max_radius**2)
        return min_mask | max_mask

    def plot_Q_limits(self):
        self.pv.plot(NXdata(self.transmission_coordinates(),
                            (NXfield(np.arange(self.shape[0]), name='y'),
                             NXfield(np.arange(self.shape[1]), name='x')),
                            title=f'Q-Limits'))
        self.pv.aspect = 'equal'
        self.pv.ytab.flipped = detector_flipped(self.parent_root['entry'])

    @property
    def sample_directory(self):
        return self.experiment_directory / self.sample / self.label

    @property
    def parent_file(self):
        parent_name = self.parameters['parent'].value + '_parent.nxs'
        return self.sample_directory.joinpath(parent_name)

    def scan_info(self):
        scan_info = NXprocess()
        scan_info['parent'] = self.parent_file.name
        return scan_info

    def create_parent(self):
        if 'sample' not in self.parent_root['entry']:
            self.parent_root['entry/sample'] = NXsample()
        self.parent_root['entry/sample/name'] = self.sample
        self.parent_root['entry/sample/label'] = self.label
        if 'nxreduce' not in self.parent_root['entry']:
            self.parent_root['entry/nxreduce'] = NXparameters()
        for p in self.parameters:
            self.parent_root['entry/nxreduce'][p] = self.parameters[p].value
        if 'nxscans' not in self.parent_root['entry']:
            self.parent_root['entry/nxscans'] = self.scan_info()

    def accept(self):
        if self.parent_root is None:
            report_error("Defining New Sample",
                         "Choose a configuration file first.")
        if self.parent_file.exists() and not confirm_action(
                "Overwrite parent file?", 
                f"'{self.parent_file}' already exists."):
            return
        self.create_parent()
        self.parent_root.save(self.parent_file, 'w')
        self.treeview.tree.load(self.parent_file, 'rw')
        super().accept()
