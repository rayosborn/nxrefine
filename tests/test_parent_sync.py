import shutil

import numpy as np
import pytest
from nexusformat.nexus import (NeXusError, NXcollection, NXdata, NXdetector,
                               NXentry, NXfield, NXinstrument, NXmonitor,
                               NXsample, nxgetconfig, nxopen, nxsetconfig)

from nxrefine.nxparent import NXParent, groups_equal
from nxrefine.scripts import nxparent as nxparent_script
from test_settings import make_directory_structure

DATA_PATH = '/entry/result'


def make_registry(path, scans=(), selected=None, settings=None):
    """Create a parent file listing the given scans."""
    parent = NXParent(path)
    parent.initialize()
    parent.root.save(path, 'w')
    if settings:
        parent.write_settings(**settings)
    with nxopen(path, 'rw') as root:
        info = root['entry/nxscans']
        for i, scan in enumerate(scans):
            info['scans'].resize((i + 1,))
            info['scans'][i] = scan
            info['selected'].resize((i + 1,))
            info['selected'][i] = True if selected is None else selected[i]
    return NXParent(path)


def make_scan(path, value, group='result'):
    """Create a scan file with a NXdata group of constant values."""
    with nxopen(path, 'w') as root:
        root['entry'] = NXentry()
        root['entry/sample'] = NXsample()
        root['entry/sample/temperature'] = 10.0 * value
        if group:
            root['entry'][group] = NXdata(
                NXfield(np.full(3, value, dtype=float), name='y'),
                NXfield(np.arange(3), name='x'))


def add_entry_groups(path, value, entry_name='f1'):
    """Add the groups that NXReduce consolidates to a numbered entry."""
    def field(name):
        return NXfield(np.full(3, value, dtype=float), name=name)

    axis = NXfield(np.arange(3), name='x')
    with nxopen(path, 'rw') as root:
        root[entry_name] = NXentry()
        root[entry_name]['frame_sums'] = NXcollection()
        root[entry_name]['frame_sums/summed_data'] = NXdata(
            field('summed_data'), axis)
        root[entry_name]['monitor'] = NXmonitor(field('MCS2'), axis)
        root[entry_name]['peaks'] = NXdata(field('peaks'), axis)


def add_orientation(path, entry_name, matrix):
    with nxopen(path, 'rw') as root:
        root[entry_name] = NXentry()
        root[entry_name]['instrument'] = NXinstrument()
        root[entry_name]['instrument/detector'] = NXdetector()
        root[entry_name]['instrument/detector/orientation_matrix'] = matrix


def consolidated(path):
    with nxopen(path) as root:
        return root['entry/result'].nxsignal.nxvalue


@pytest.fixture
def lock_timeout():
    """Turn on file locking, which is off by default."""
    previous = nxgetconfig('lock')
    nxsetconfig(lock=10)
    yield
    nxsetconfig(lock=previous)


@pytest.fixture
def sample_dir(tmp_path):
    scan_dir, wrapper_file, parent_file, task_dir = (
        make_directory_structure(tmp_path))
    return wrapper_file.parent


class TestCreateScanData:

    def test_scans_registered_after_instance_was_created_are_included(
            self, sample_dir):
        parent_file = sample_dir / 'sample_scans.nxs'
        make_scan(sample_dir / 'sample_a.nxs', 1.0)
        make_registry(parent_file, ['sample_a'])
        stale = NXParent(parent_file)
        assert stale.scans == ['sample_a']
        make_scan(sample_dir / 'sample_b.nxs', 2.0)
        with nxopen(parent_file, 'rw') as root:
            info = root['entry/nxscans']
            info['scans'].resize((2,))
            info['scans'][1] = 'sample_b'
            info['selected'].resize((2,))
            info['selected'][1] = True
        stale.create_scan_data(DATA_PATH)
        assert consolidated(parent_file).tolist() == [[1.0] * 3, [2.0] * 3]

    def test_scan_files_are_stored_relative_to_the_parent(self, sample_dir):
        parent_file = sample_dir / 'sample_scans.nxs'
        (sample_dir / '30K').mkdir()
        make_scan(sample_dir / 'sample_a.nxs', 1.0)
        make_scan(sample_dir / '30K' / 'sample_b.nxs', 2.0)
        parent = make_registry(parent_file, ['sample_a', '30K/sample_b'])
        parent.create_scan_data(DATA_PATH)
        with nxopen(parent_file) as root:
            sources = root.nxfile['entry/result/y'].virtual_sources()
        assert [s.file_name for s in sources] == [
            'sample_a.nxs', '30K/sample_b.nxs']

    def test_consolidated_data_are_found_after_moving_the_directory(
            self, sample_dir):
        parent_file = sample_dir / 'sample_scans.nxs'
        (sample_dir / '30K').mkdir()
        make_scan(sample_dir / 'sample_a.nxs', 1.0)
        make_scan(sample_dir / '30K' / 'sample_b.nxs', 2.0)
        parent = make_registry(parent_file, ['sample_a', '30K/sample_b'])
        parent.create_scan_data(DATA_PATH)
        moved = sample_dir.parent / 'copy' / 'deeper'
        moved.parent.mkdir()
        shutil.move(sample_dir, moved)
        assert (consolidated(moved / 'sample_scans.nxs').tolist()
                == [[1.0] * 3, [2.0] * 3])

    def test_scan_list_is_read_with_the_consolidation_lock_held(
            self, sample_dir, lock_timeout, monkeypatch):
        parent_file = sample_dir / 'sample_scans.nxs'
        make_scan(sample_dir / 'sample_a.nxs', 1.0)
        parent = make_registry(parent_file, ['sample_a'])
        held = []
        original = NXParent.valid_scans

        def valid_scans(self, scan_group):
            held.append(self.consolidation_lock.lock_file.exists())
            return original(self, scan_group)

        monkeypatch.setattr(NXParent, 'valid_scans', valid_scans)
        parent.create_scan_data(DATA_PATH)
        assert held == [True]
        assert not parent.consolidation_lock.lock_file.exists()

    def test_lock_is_released_after_an_error(self, sample_dir, lock_timeout,
                                             monkeypatch):
        parent_file = sample_dir / 'sample_scans.nxs'
        parent = make_registry(parent_file, [])

        def valid_scans(self, scan_group):
            raise RuntimeError('failed')

        monkeypatch.setattr(NXParent, 'valid_scans', valid_scans)
        with pytest.raises(RuntimeError):
            parent.create_scan_data(DATA_PATH)
        assert not parent.consolidation_lock.lock_file.exists()


class TestDataPaths:

    def test_paths_are_found_in_any_selected_scan(self, sample_dir):
        make_scan(sample_dir / 'sample_a.nxs', 1.0, group=None)
        make_scan(sample_dir / 'sample_b.nxs', 2.0)
        parent = make_registry(sample_dir / 'sample_scans.nxs',
                               ['sample_a', 'sample_b'])
        assert list(parent._data_paths()) == [DATA_PATH]

    def test_paths_are_not_repeated(self, sample_dir):
        make_scan(sample_dir / 'sample_a.nxs', 1.0)
        make_scan(sample_dir / 'sample_b.nxs', 2.0)
        parent = make_registry(sample_dir / 'sample_scans.nxs',
                               ['sample_a', 'sample_b'])
        assert list(parent._data_paths()) == [DATA_PATH]

    def test_unreadable_scans_are_skipped(self, sample_dir):
        make_scan(sample_dir / 'sample_b.nxs', 2.0)
        parent = make_registry(sample_dir / 'sample_scans.nxs',
                               ['sample_missing', 'sample_b'])
        assert list(parent._data_paths()) == [DATA_PATH]

    def test_groups_of_numbered_entries_are_found(self, sample_dir):
        make_scan(sample_dir / 'sample_a.nxs', 1.0)
        add_entry_groups(sample_dir / 'sample_a.nxs', 1.0)
        parent = make_registry(sample_dir / 'sample_scans.nxs', ['sample_a'])
        assert list(parent._data_paths()) == [
            DATA_PATH, '/f1/frame_sums/summed_data', '/f1/monitor']

    def test_update_returns_the_paths_it_consolidated(self, sample_dir):
        make_scan(sample_dir / 'sample_a.nxs', 1.0)
        add_entry_groups(sample_dir / 'sample_a.nxs', 1.0)
        parent_file = sample_dir / 'sample_scans.nxs'
        parent = make_registry(parent_file, ['sample_a'])
        assert parent.update_scan_data() == [
            DATA_PATH, '/f1/frame_sums/summed_data', '/f1/monitor']
        with nxopen(parent_file) as root:
            assert root['f1/frame_sums/summed_data'].nxsignal.shape == (1, 3)
            assert 'peaks' not in root['f1']

    def test_update_adds_data_that_appear_later(self, sample_dir):
        make_scan(sample_dir / 'sample_a.nxs', 1.0, group=None)
        make_scan(sample_dir / 'sample_b.nxs', 2.0, group=None)
        parent_file = sample_dir / 'sample_scans.nxs'
        parent = make_registry(parent_file, ['sample_a', 'sample_b'])
        assert parent.update_scan_data() == []
        make_scan(sample_dir / 'sample_b.nxs', 2.0)
        assert parent.update_scan_data() == [DATA_PATH]
        assert consolidated(parent_file).tolist() == [[2.0] * 3]

    def test_update_logs_a_failure_and_continues(self, sample_dir, caplog,
                                                 monkeypatch):
        make_scan(sample_dir / 'sample_a.nxs', 1.0)
        add_entry_groups(sample_dir / 'sample_a.nxs', 1.0)
        parent = make_registry(sample_dir / 'sample_scans.nxs', ['sample_a'])
        original = NXParent.create_scan_data

        def create_scan_data(self, data_path):
            if data_path == DATA_PATH:
                raise NeXusError('cannot consolidate')
            return original(self, data_path)

        monkeypatch.setattr(NXParent, 'create_scan_data', create_scan_data)
        assert parent.update_scan_data() == [
            '/f1/frame_sums/summed_data', '/f1/monitor']
        assert 'cannot consolidate' in caplog.text

    def test_update_builds_data_from_scans_that_have_it(self, sample_dir):
        make_scan(sample_dir / 'sample_a.nxs', 1.0, group=None)
        make_scan(sample_dir / 'sample_b.nxs', 2.0)
        parent_file = sample_dir / 'sample_scans.nxs'
        parent = make_registry(parent_file, ['sample_a', 'sample_b'])
        parent.update_scan_data()
        assert consolidated(parent_file).tolist() == [[2.0] * 3]


class TestMergeAuthored:

    def make_source(self, sample_dir):
        source = sample_dir / 'source_scans.nxs'
        make_registry(source, ['sample_a', 'sample_b'], selected=[True, False],
                      settings={'threshold': 50000, 'monitor': 'monitor2'})
        with nxopen(source, 'rw') as root:
            root['entry/nxscans/transform'] = NXdata(axes=(
                NXfield([0.0, 1.0], name='Ql'),
                NXfield([0.0, 1.0], name='Qk'),
                NXfield([0.0, 1.0], name='Qh')))
        add_orientation(source, 'f1', np.eye(3))
        return source

    def test_creates_the_parent_with_authored_content(self, sample_dir):
        source = self.make_source(sample_dir)
        with nxopen(source, 'rw') as root:
            root['entry/result'] = NXdata(NXfield([1.0, 2.0], name='y'))
        parent_file = sample_dir / 'sample_scans.nxs'
        changes = NXParent(parent_file).merge_authored(source)
        assert changes
        parent = NXParent(parent_file)
        assert parent.scans == ['sample_a', 'sample_b']
        assert parent.selected == [True, False]
        assert parent.get_setting('threshold') == 50000
        assert parent.get_setting('monitor') == 'monitor2'
        assert parent.has_transform_grid()
        assert parent.has_orientation('f1')
        assert 'result' not in parent.entry

    def test_leaves_consolidated_datasets_untouched(self, sample_dir):
        make_scan(sample_dir / 'sample_a.nxs', 1.0)
        parent_file = sample_dir / 'sample_scans.nxs'
        parent = make_registry(parent_file, ['sample_a'])
        parent.update_scan_data()
        source = self.make_source(sample_dir)
        parent.merge_authored(source)
        assert consolidated(parent_file).tolist() == [[1.0] * 3]
        assert NXParent(parent_file).scans == ['sample_a', 'sample_b']

    def test_overwrite_replaces_differing_values(self, sample_dir):
        source = self.make_source(sample_dir)
        parent_file = sample_dir / 'sample_scans.nxs'
        make_registry(parent_file, ['sample_a'], settings={
            'threshold': 10, 'radius': 0.2})
        add_orientation(parent_file, 'f1', 2 * np.eye(3))
        NXParent(parent_file).merge_authored(source, overwrite=True)
        parent = NXParent(parent_file)
        assert parent.get_setting('threshold') == 50000
        assert parent.get_setting('radius') == 0.2
        with nxopen(parent_file) as root:
            matrix = root['f1/instrument/detector/orientation_matrix']
            assert matrix.nxvalue.tolist() == np.eye(3).tolist()
        assert parent.selected == [True, False]

    def test_selection_of_a_listed_scan_is_updated(self, sample_dir):
        source = self.make_source(sample_dir)
        parent_file = sample_dir / 'sample_scans.nxs'
        make_registry(parent_file, ['sample_a', 'sample_b'],
                      selected=[True, True])
        NXParent(parent_file).merge_authored(source, overwrite=True)
        assert NXParent(parent_file).selected == [True, False]

    def test_existing_content_is_kept_by_default(self, sample_dir):
        source = self.make_source(sample_dir)
        parent_file = sample_dir / 'sample_scans.nxs'
        make_registry(parent_file, ['sample_a'], selected=[False],
                      settings={'threshold': 10, 'radius': 0.2})
        add_orientation(parent_file, 'f1', 2 * np.eye(3))
        NXParent(parent_file).merge_authored(source)
        parent = NXParent(parent_file)
        assert parent.get_setting('threshold') == 10
        assert parent.get_setting('monitor') == 'monitor2'
        assert parent.scans == ['sample_a', 'sample_b']
        assert parent.selected == [False, False]
        with nxopen(parent_file) as root:
            matrix = root['f1/instrument/detector/orientation_matrix']
            assert matrix.nxvalue.tolist() == (2 * np.eye(3)).tolist()
        assert parent.has_transform_grid()

    def test_unchanged_merge_does_not_modify_the_file(self, sample_dir):
        source = self.make_source(sample_dir)
        parent_file = sample_dir / 'sample_scans.nxs'
        parent = NXParent(parent_file)
        assert parent.merge_authored(source)
        before = parent_file.read_bytes()
        assert parent.merge_authored(source) == []
        # Reading a file with nxopen changes its modification time, so the
        # content is compared instead.
        assert parent_file.read_bytes() == before

    def test_merged_position_entries_share_the_sample(self, sample_dir):
        source = self.make_source(sample_dir)
        parent_file = sample_dir / 'sample_scans.nxs'
        NXParent(parent_file).merge_authored(source)
        with nxopen(parent_file) as root:
            assert 'sample' in root['f1']
            assert root['f1/sample/name'].nxvalue == 'sample'

    def test_overwrite_updates_the_sample_seen_by_position_entries(
            self, sample_dir):
        source = self.make_source(sample_dir)
        with nxopen(source, 'rw') as root:
            root['entry/sample/name'] = 'other'
        parent_file = sample_dir / 'sample_scans.nxs'
        parent = NXParent(parent_file)
        parent.merge_authored(source)
        parent.merge_authored(source, overwrite=True)
        with nxopen(parent_file) as root:
            assert root['f1/sample/name'].nxvalue == 'other'

    def test_scan_files_need_not_exist_where_the_parent_is_merged(
            self, sample_dir):
        # Listed scans whose files have not arrived are ignored when the
        # datasets are built.
        source = self.make_source(sample_dir)
        parent_file = sample_dir / 'sample_scans.nxs'
        parent = NXParent(parent_file)
        parent.merge_authored(source)
        make_scan(sample_dir / 'sample_a.nxs', 1.0)
        parent.update_scan_data()
        assert consolidated(parent_file).tolist() == [[1.0] * 3]


class TestGroupsEqual:

    def make_group(self, value, units='mm'):
        group = NXdata(NXfield([1.0, value], name='y'))
        group['y'].attrs['units'] = units
        return group

    def test_equal(self):
        assert groups_equal(self.make_group(2.0), self.make_group(2.0))

    def test_different_value(self):
        assert not groups_equal(self.make_group(2.0), self.make_group(3.0))

    def test_different_attribute(self):
        assert not groups_equal(self.make_group(2.0),
                                self.make_group(2.0, units='cm'))

    def test_different_entries(self):
        other = self.make_group(2.0)
        other['z'] = NXfield([1.0])
        assert not groups_equal(self.make_group(2.0), other)


class TestNXParentScript:

    def run(self, monkeypatch, tmp_path, *args):
        monkeypatch.setattr('sys.argv', [
            'nxparent', '-d', str(tmp_path), '-s', 'sample', '-l', 'label',
            *args])
        nxparent_script.main()

    def test_merge_and_update(self, tmp_path, monkeypatch, capsys):
        sample_dir = make_directory_structure(tmp_path)[1].parent
        source = sample_dir / 'source_scans.nxs'
        make_scan(sample_dir / 'sample_a.nxs', 1.0)
        make_registry(source, ['sample_a'], settings={'threshold': 7})
        self.run(monkeypatch, tmp_path, '--merge', str(source), '--update')
        output = capsys.readouterr().out
        assert 'add scan sample_a' in output
        assert DATA_PATH in output.split()
        parent_file = sample_dir / 'sample_scans.nxs'
        assert NXParent(parent_file).get_setting('threshold') == 7
        assert consolidated(parent_file).tolist() == [[1.0] * 3]

    def test_merge_keeps_existing_settings_unless_overwrite(
            self, tmp_path, monkeypatch):
        sample_dir = make_directory_structure(tmp_path)[1].parent
        source = sample_dir / 'source_scans.nxs'
        make_registry(source, [], settings={'threshold': 7})
        parent_file = sample_dir / 'sample_scans.nxs'
        make_registry(parent_file, [], settings={'threshold': 3})
        self.run(monkeypatch, tmp_path, '--merge', str(source))
        assert NXParent(parent_file).get_setting('threshold') == 3
        self.run(monkeypatch, tmp_path, '--merge', str(source), '--overwrite')
        assert NXParent(parent_file).get_setting('threshold') == 7

    def test_update_syncs_scans_in_the_chosen_subentry(
            self, tmp_path, monkeypatch):
        sample_dir = make_directory_structure(tmp_path)[1].parent
        parent_file = sample_dir / 'sample_scans.nxs'
        make_registry(parent_file, [])
        make_scan(sample_dir / 'sample_a.nxs', 1.0)
        registered = []

        def sync_scans(self, selected=True):
            registered.append(self.subentry_name)
            return []

        monkeypatch.setattr(NXParent, 'sync_scans', sync_scans)
        self.run(monkeypatch, tmp_path, '--update')
        assert registered == ['']
        self.run(monkeypatch, tmp_path, '--update', '--subentry', 'other')
        assert registered == ['', 'other']

    def test_update_requires_an_existing_parent(self, tmp_path, monkeypatch):
        make_directory_structure(tmp_path)
        with pytest.raises(SystemExit):
            self.run(monkeypatch, tmp_path, '--update')


def make_setup_parent(path, complete=True):
    """Create a parent with a transform grid, limits, and an orientation."""
    make_registry(path, [], settings={'qmin': 0.5, 'qmax': 5.0}
                  if complete else None)
    add_orientation(path, 'f1', np.eye(3))
    if complete:
        with nxopen(path, 'rw') as root:
            root['entry/nxscans/transform'] = NXdata(axes=(
                NXfield([0.0, 1.0], name='Ql'),
                NXfield([0.0, 1.0], name='Qk'),
                NXfield([0.0, 1.0], name='Qh')))


class TestSetup:

    def test_missing_setup(self, tmp_path):
        path = tmp_path / 'sample_scans.nxs'
        make_setup_parent(path, complete=False)
        assert NXParent(path).missing_setup() == [
            'transform grid', 'qmin', 'qmax']
        complete = tmp_path / 'complete_scans.nxs'
        make_setup_parent(complete)
        assert NXParent(complete).missing_setup() == []

    def test_missing_orientation(self, tmp_path):
        path = tmp_path / 'sample_scans.nxs'
        make_setup_parent(path)
        with nxopen(path, 'rw') as root:
            del root['f1/instrument/detector/orientation_matrix']
        assert NXParent(path).missing_setup() == [
            'orientation matrix for f1']

    def test_registration_with_a_stale_root(self, tmp_path):
        path = tmp_path / 'sample_scans.nxs'
        make_scan(tmp_path / 'sample_a.nxs', 1.0)
        make_scan(tmp_path / 'sample_b.nxs', 2.0)
        make_registry(path, [])
        first, second = NXParent(path), NXParent(path)
        first.add_scan(tmp_path / 'sample_a.nxs')
        second.add_scan(tmp_path / 'sample_b.nxs')
        assert NXParent(path).scans == ['sample_a', 'sample_b']

    def test_script_check_and_parent_file(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / 'sample_scans.nxs'
        make_setup_parent(path, complete=False)
        monkeypatch.setattr('sys.argv', ['nxparent', '-P', str(path),
                                         '--check'])
        with pytest.raises(SystemExit) as exit_info:
            nxparent_script.main()
        assert exit_info.value.code == 1
        assert 'transform grid' in capsys.readouterr().err
