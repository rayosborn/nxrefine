# -----------------------------------------------------------------------------
# Copyright (c) 2018-2026, Argonne National Laboratory.
#
# Distributed under the terms of an Open Source License.
#
# The full license is in the file LICENSE.pdf, distributed with this software.
# -----------------------------------------------------------------------------
"""NXServer — Parsl-backed task dispatcher (SPIKE).

This file is a sketch produced from the plan at
``~/.claude/plans/sparkling-strolling-swing.md``. It replaces the old
``NXController`` / ``NXWorker`` / per-task ``qsub`` wrapping with a
single Parsl-based dispatch layer that handles the same three server
modes:

* ``direct`` — in-process ``ThreadPoolExecutor`` (no daemon, no batch
  system). Tasks run sequentially-ish on the calling Python process.
* ``multicore`` — daemonised ``HighThroughputExecutor`` over a local
  ``LocalProvider`` with ``max_workers_per_node`` set to the configured
  core count.
* ``multinode`` — per-invocation ``HighThroughputExecutor`` over an
  ``SGEProvider`` (or other batch provider). Each ``submit_batch`` call
  explicitly provisions one block sized to the requested batch.

The public façade — constructor signature, ``start`` / ``stop`` /
``status`` / ``clear`` / ``kill`` / ``restart``, ``add_task`` /
``read_task`` / ``queued_tasks``, ``read_nodes`` / ``write_nodes`` /
``remove_nodes`` / ``set_cores``, plus the ``server_type``, ``cpus``,
``run_command``, ``server_log``, and ``directory`` attributes — is
preserved so callers in ``nxreduce.py`` and
``plugins/server/manage_workflows.py`` keep working.

What is **new**:

* :meth:`NXServer.submit_batch` — bind a list of commands to one
  allocation, the "one click = one qsub" gesture the dialog will
  eventually call.
* :func:`build_parsl_config` — assembles the right Parsl ``Config`` for
  the configured mode.
* :func:`run_command` — the single ``@bash_app`` that wraps every task.
* :class:`NXTask` — collapsed to a tiny dataclass carrying the command
  string and an optional ``batch_id``.

What is **gone**:

* ``NXController`` and ``NXWorker`` classes.
* ``NXTask.executable_command`` and its hand-built ``pdsh`` / ``qsub``
  / template wrapping.
* ``last_cpu`` round-robin file and the per-CPU ``cpuN.log`` files —
  Parsl's per-task ``AUTO_LOGNAME`` and the monitoring DB take over.

Open items flagged inline with ``TODO(spike):`` — these need
validation against a real Parsl install before this can leave spike
status. See the plan's "Risks & open questions" section for the full
list.
"""

import os
import shutil
import time
import uuid
from configparser import ConfigParser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Optional

import psutil
from nexusformat.nexus import NeXusError, NXLock
from persistqueue import Queue as FileQueue
from persistqueue.exceptions import Empty as FileEmpty
from persistqueue.serializers import json as queue_json

from .nxdaemon import NXDaemon
from .nxsettings import NXSettings

# Parsl is a hard dependency in the new architecture. The import is local
# to :func:`build_parsl_config` so that ``import nxrefine.nxserver`` still
# succeeds on machines that haven't installed Parsl yet (e.g. read-only
# CLI uses like ``nxserver status``). Anywhere we actually dispatch a
# task, we import Parsl explicitly.


# --------------------------------------------------------------------------- #
# Server mode catalogue
# --------------------------------------------------------------------------- #


def get_servers():
    """Return the list of available server types."""
    return ['direct', 'multicore', 'multinode']


# --------------------------------------------------------------------------- #
# Durable submission log
# --------------------------------------------------------------------------- #


class NXFileQueue(FileQueue):
    """A file-based queue with locked access.

    Carries ``(command, batch_id)`` tuples (serialised as JSON) so the
    batch boundary set by :meth:`NXServer.submit_batch` survives a
    daemon restart. Legacy entries that are bare strings continue to
    work — :meth:`NXServer._next_task` normalises them.
    """

    def __init__(self, directory, autosave=False):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o777, exist_ok=True)
        tempdir = self.directory / 'tempdir'
        tempdir.mkdir(mode=0o777, exist_ok=True)
        self.lock = NXLock(self.directory / 'filequeue')
        with self.lock:
            super().__init__(directory, serializer=queue_json,
                             autosave=autosave, tempdir=tempdir)
            self.fix_access()

    def __repr__(self):
        return f"NXFileQueue('{self.directory}')"

    def __enter__(self):
        self.lock.acquire()
        self.info = self._loadinfo()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.fix_access()
        self.lock.release()

    def put(self, item, block=True, timeout=None):
        """Add an item to the queue.

        ``item`` may be a string (legacy) or a ``(command, batch_id)``
        tuple. Tuples are stored as a dict so the JSON round-trip is
        lossless.
        """
        if isinstance(item, tuple):
            payload = {'command': item[0], 'batch_id': item[1]}
        else:
            payload = str(item)
        with self:
            super().put(payload, block=block, timeout=timeout)

    def get(self, block=True, timeout=None):
        """Return the next item as ``(command, batch_id)``."""
        with self:
            payload = super().get(block=block, timeout=timeout)
        if isinstance(payload, dict):
            return payload.get('command'), payload.get('batch_id')
        return str(payload), None

    def queued_items(self):
        """Return a list of remaining items (commands only, for display)."""
        with self:
            items = []
            while self.qsize() > 0:
                payload = super().get(timeout=0)
                if isinstance(payload, dict):
                    items.append(payload.get('command'))
                else:
                    items.append(str(payload))
        return items

    def fix_access(self):
        """Ensure that the file queue pointer is readable."""
        for f in [f for f in self.directory.iterdir() if f.is_file()]:
            try:
                self.directory.joinpath(f).chmod(0o666)
            except Exception:
                pass
        for f in [f for f in self.directory.iterdir() if f.is_dir()]:
            try:
                self.directory.joinpath(f).chmod(0o777)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Task model
# --------------------------------------------------------------------------- #


@dataclass
class NXTask:
    """A pending task — just the command and (optional) batch grouping."""

    command: str
    batch_id: Optional[str] = None
    name: str = field(init=False)

    def __post_init__(self):
        self.name = self.command.split()[0] if self.command else ''

    def __repr__(self):
        return f"NXTask({self.name!r}, batch_id={self.batch_id!r})"


# --------------------------------------------------------------------------- #
# Parsl wiring
# --------------------------------------------------------------------------- #


def build_parsl_config(server_type, settings, run_dir,
                       nodes_per_block=None):
    """Build a Parsl ``Config`` for the configured server mode.

    Parameters
    ----------
    server_type : str
        ``'direct'``, ``'multicore'``, or ``'multinode'``.
    settings : NXSettings
        Loaded settings; consulted for ``server.cores``, ``parsl.*``,
        and ``batch.*`` sections.
    run_dir : Path
        Where Parsl writes its run logs / monitoring DB. Conventionally
        ``<server.directory>/parsl``.
    nodes_per_block : int, optional
        For ``multinode``, the size of the SGE allocation to request
        for this invocation. ``None`` defers to the spike's default.

    Returns
    -------
    parsl.config.Config
    """
    from parsl.config import Config
    from parsl.executors import HighThroughputExecutor, ThreadPoolExecutor
    from parsl.monitoring import MonitoringHub
    from parsl.providers import LocalProvider

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    monitoring = MonitoringHub(
        hub_address='127.0.0.1',
        monitoring_debug=False,
        resource_monitoring_interval=10,
    )

    if server_type == 'direct':
        cores = int(settings.get('server', 'cores') or psutil.cpu_count())
        executor = ThreadPoolExecutor(label='nx-direct', max_threads=cores)

    elif server_type == 'multicore':
        cores = int(settings.get('server', 'cores') or psutil.cpu_count())
        executor = HighThroughputExecutor(
            label='nx-multicore',
            max_workers_per_node=cores,
            provider=LocalProvider(init_blocks=1, max_blocks=1),
        )

    elif server_type == 'multinode':
        # TODO(spike): expose queue, walltime, project_id, and scheduler_options
        # via the [parsl] settings section.
        from parsl.providers import GridEngineProvider as SGEProvider
        queue = settings.get('parsl', 'queue', fallback=None)
        walltime = settings.get('parsl', 'walltime', fallback='24:00:00')
        nodes = nodes_per_block or int(
            settings.get('batch', 'min_nodes', fallback='1'))
        provider = SGEProvider(
            queue=queue,
            walltime=walltime,
            nodes_per_block=nodes,
            init_blocks=0,
            min_blocks=0,
            max_blocks=1,
            parallelism=0,  # disable timing-based scaling — explicit only
        )
        executor = HighThroughputExecutor(
            label='nx-multinode',
            max_workers_per_node=1,
            provider=provider,
        )

    else:
        raise NeXusError(f"Unknown server_type {server_type!r}")

    return Config(
        executors=[executor],
        monitoring=monitoring,
        run_dir=str(run_dir),
        # TODO(spike): enable memoisation for "best-effort" durability.
        # checkpoint_mode='task_exit',
    )


def _build_run_command():
    """Define and return the ``@bash_app`` used to dispatch every task.

    Defined lazily so importing ``nxserver`` doesn't pull Parsl in.
    """
    import parsl
    from parsl.app.app import bash_app

    @bash_app
    def run_command(command: str,
                    stdout=parsl.AUTO_LOGNAME,
                    stderr=parsl.AUTO_LOGNAME) -> str:
        # Returning the command string is how @bash_app knows what to run.
        return command

    return run_command


# --------------------------------------------------------------------------- #
# NXServer — public façade
# --------------------------------------------------------------------------- #


class NXServer(NXDaemon):
    """Façade for queueing and dispatching nxrefine workflow tasks.

    See module docstring for the architectural shape. The public API is
    intentionally a near-superset of the pre-Parsl version so existing
    callers (NXReduce, manage_workflows dialog, the nxserver CLI script)
    keep working without changes.
    """

    def __init__(self, directory=None, server_type=None):
        self.pid_name = 'nxserver'
        self.initialize(directory, server_type)
        self._task_queue = None
        self._parsl_loaded = False
        self._run_command = None
        self._in_flight = {}        # future -> (command, batch_id)
        self._batches = {}          # batch_id -> [futures]
        if self.server_type != 'direct':
            super().__init__(self.pid_name, self.pid_file)

    def __repr__(self):
        return f"NXServer(directory='{self.directory}')"

    # --- settings / configuration --- #

    def get_directory(self):
        home_settings_file = Path.home() / '.nxserver' / 'settings.ini'
        if 'NX_SERVER' in os.environ:
            return Path(os.environ['NX_SERVER'])
        elif home_settings_file.exists():
            home_settings = ConfigParser()
            home_settings.read(home_settings_file)
            if home_settings.has_option('setup', 'directory'):
                return Path(home_settings.get('setup', 'directory'))
        return None

    def save_directory(self):
        Path.home().joinpath('.nxserver').mkdir(exist_ok=True)
        home_settings_file = Path.home() / '.nxserver' / 'settings.ini'
        home_settings = ConfigParser()
        if home_settings_file.exists():
            home_settings.read(home_settings_file)
        if 'setup' not in home_settings.sections():
            home_settings.add_section('setup')
        home_settings.set('setup', 'directory', str(self.directory))
        with open(home_settings_file, 'w') as f:
            home_settings.write(f)

    def initialize(self, directory, server_type):
        if directory is None:
            self.directory = self.get_directory()
            self.settings = NXSettings(directory=self.directory)
        else:
            self.settings = NXSettings(directory=directory)
            self.directory = self.settings.directory
            self.save_directory()

        if server_type:
            if server_type in ('None', 'none'):
                server_type = 'direct'
            self.server_type = server_type
            self.settings.set('server', 'type', server_type)
            self.settings.save()
        elif self.settings.has_option('server', 'type'):
            self.server_type = self.settings.get('server', 'type')
            if self.server_type in ('None', 'none'):
                self.server_type = 'direct'
        else:
            self.server_type = 'direct'

        if self.server_type == 'multinode':
            if 'nodes' not in self.settings.sections():
                self.settings.add_section('nodes')
            self.cpus = self.read_nodes()
        else:
            cpu_count = psutil.cpu_count()
            if self.settings.has_option('server', 'cores'):
                cpu_count = min(int(self.settings.get('server', 'cores')),
                                cpu_count)
            self.cpus = [f'cpu{i}' for i in range(1, cpu_count + 1)]

        # Legacy settings retained for read compatibility; the Parsl
        # dispatcher ignores them. See plan §risks #6.
        self.concurrent = self.settings.get('server', 'concurrent')
        self.run_command = self.settings.get('server', 'run_command')
        self.template = self.settings.get('server', 'template')

        self.server_log = self.directory / 'nxserver.log'
        self.pid_file = self.directory / 'nxserver.pid'
        self.queue_directory = self.directory / 'task_list'
        self.parsl_run_dir = self.directory / 'parsl'

    # --- queue access --- #

    @property
    def task_queue(self):
        if self._task_queue is None:
            if self.server_type == 'direct':
                self._task_queue = Queue()
            else:
                self._task_queue = NXFileQueue(self.queue_directory,
                                               autosave=True)
        return self._task_queue

    def add_task(self, tasks, batch_id=None):
        """Add one or more task strings to the queue.

        ``batch_id`` groups commands so the daemon's restart path can
        reconstruct a batch on resume. The Manage Workflow dialog will
        normally use :meth:`submit_batch` instead, which sets the
        ``batch_id`` itself.
        """
        if isinstance(tasks, str):
            tasks = tasks.split('\n')
        existing = set(self.queued_tasks())
        for task in tasks:
            if not task:
                continue
            if task == 'stop':
                self._put_raw(task)
            elif task not in existing:
                if batch_id is None:
                    self._put_raw(task)
                else:
                    self._put_raw((task, batch_id))
                existing.add(task)

    def _put_raw(self, item):
        """Queue.put indirection so direct-mode and durable queue share code."""
        if self.server_type == 'direct':
            # In-memory queue: store the tuple/string as-is.
            self.task_queue.put(item)
        else:
            self.task_queue.put(item)

    def read_task(self):
        """Read the next ``(command, batch_id)`` pair from the queue.

        Returns ``None`` if the queue is empty or unreadable.
        """
        try:
            if self.server_type == 'direct':
                item = self.task_queue.get(block=False)
            else:
                item = self.task_queue.get(block=False)
        except (FileEmpty, Exception) as error:  # noqa: BLE001
            if not isinstance(error, FileEmpty):
                self.log(str(error))
            return None
        if isinstance(item, tuple):
            return item
        if isinstance(item, dict):
            return item.get('command'), item.get('batch_id')
        return str(item), None

    def remove_task(self, task):
        """Remove ``task`` from the queue."""
        tasks = self.queued_tasks()
        if task in tasks:
            tasks.remove(task)
        self.clear()
        for cmd in tasks:
            self.add_task(cmd)

    def queued_tasks(self):
        """List the commands remaining on the queue (without batch IDs)."""
        if self.server_type == 'direct':
            return [item[0] if isinstance(item, tuple) else str(item)
                    for item in list(self.task_queue.queue)]
        queue = NXFileQueue(self.queue_directory, autosave=False)
        return queue.queued_items()

    # --- node / core management (multinode only on nodes) --- #

    def read_nodes(self):
        if 'nodes' in self.settings.sections():
            return sorted(self.settings.options('nodes'))
        return []

    def write_nodes(self, nodes):
        current = set(self.read_nodes())
        for node in [n for n in nodes if n not in current]:
            self.settings.set('nodes', node)
        self.settings.save()
        self.cpus = self.read_nodes()

    def remove_nodes(self, nodes):
        for node in nodes:
            self.settings.remove_option('nodes', node)
        self.settings.save()
        self.cpus = self.read_nodes()

    def set_cores(self, cpu_count):
        try:
            cpu_count = int(cpu_count)
        except ValueError:
            raise NeXusError('Number of cores must be a valid integer')
        self.settings.set('server', 'cores', cpu_count)
        self.settings.save()
        self.cpus = [f'cpu{i}' for i in range(1, cpu_count + 1)]

    # --- logging --- #

    def log(self, message):
        with NXLock(self.server_log, timeout=60, expiry=60):
            with open(self.server_log, 'a') as f:
                f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + ' '
                        + str(message) + '\n')

    # --- Parsl plumbing --- #

    def _ensure_parsl(self, nodes_per_block=None):
        """Load Parsl exactly once for this process."""
        if self._parsl_loaded:
            return
        import parsl
        parsl.load(build_parsl_config(self.server_type, self.settings,
                                      self.parsl_run_dir,
                                      nodes_per_block=nodes_per_block))
        self._run_command = _build_run_command()
        self._parsl_loaded = True

    def _dispatch(self, command, batch_id=None):
        """Submit one command through the active Parsl @bash_app."""
        if self._run_command is None:
            self._ensure_parsl()
        future = self._run_command(command)
        self._in_flight[future] = (command, batch_id)
        if batch_id is not None:
            self._batches.setdefault(batch_id, []).append(future)
        return future

    def _reap_done(self):
        """Drop finished futures from the in-flight map and log results."""
        done = [f for f in self._in_flight if f.done()]
        for f in done:
            command, batch_id = self._in_flight.pop(f)
            try:
                f.result()  # raises BashExitFailure on non-zero exit
                self.log(f"finished: {command}")
            except Exception as error:  # noqa: BLE001
                self.log(f"failed: {command}: {error}")
            if batch_id and batch_id in self._batches:
                self._batches[batch_id] = [
                    g for g in self._batches[batch_id] if g is not f]
                if not self._batches[batch_id]:
                    del self._batches[batch_id]

    # --- batch submission ("one click = one qsub") --- #

    def submit_batch(self, commands, min_nodes=None):
        """Submit a list of commands as a single batch.

        On ``multinode`` this provisions one allocation sized to the
        batch (padded to ``min_nodes`` if smaller) and submits all
        commands through it. On ``direct`` / ``multicore`` it's just a
        durable-queue ``add_task`` loop with a shared ``batch_id`` —
        the Parsl executor decides scheduling.

        Parameters
        ----------
        commands : list of str
            One shell command per scan, typically
            ``nxreduce -e f1 f2 f3 -d <dir> -C -P``.
        min_nodes : int, optional
            Facility minimum for the allocation. Defaults to the
            ``[batch] min_nodes`` setting.
        """
        commands = [c for c in commands if c]
        if not commands:
            return None
        batch_id = uuid.uuid4().hex[:12]
        for cmd in commands:
            self.add_task(cmd, batch_id=batch_id)

        if self.server_type == 'multinode':
            # TODO(spike): pad_policy = pad|refuse|wait (plan §risks #7).
            n = max(min_nodes or int(self.settings.get(
                'batch', 'min_nodes', fallback='1')), len(commands))
            self._provision_block(n, batch_id=batch_id)
        # direct / multicore: tasks will be picked up by the dispatch loop
        # against the long-running Parsl executor; no explicit allocation.
        return batch_id

    def _provision_block(self, nodes_per_block, batch_id):
        """Provision one Parsl block for a multinode batch.

        On ``multinode`` we want exactly one ``qsub`` per ``submit_batch``,
        sized to the batch (or ``min_nodes``, whichever is larger). The
        simplest robust shape is to (re)load Parsl with a fresh provider
        parameterised for this allocation. Alternative is to drive
        :py:meth:`HighThroughputExecutor.scale_out` against a pre-loaded
        executor — confirm during spike.
        """
        # TODO(spike): instead of reloading Parsl per batch, keep the DFK
        # alive and call executor.scale_out(blocks=1). This is the cleaner
        # shape but needs verification that scale_out against a fresh
        # provider returns a usable handle (plan §risks #8).
        self._ensure_parsl(nodes_per_block=nodes_per_block)

    # --- lifecycle --- #

    def run(self):
        """Daemon entry point: drain the durable queue forever.

        Used by direct (called inline from :meth:`start`) and multicore
        (called via :class:`NXDaemon`). Multinode uses
        :meth:`run_once` instead — see :meth:`start`.
        """
        self.log(f'Starting server (pid={os.getpid()}, type={self.server_type})')
        self._ensure_parsl()
        # Replay any tasks the queue picked up before we were running.
        self._replay_pending()
        try:
            while True:
                time.sleep(2)
                self._reap_done()
                item = self.read_task()
                if item is None:
                    continue
                command, batch_id = item
                if command == 'stop':
                    break
                self._dispatch(command, batch_id=batch_id)
            # graceful drain — wait for in-flight to finish
            while self._in_flight:
                self._reap_done()
                time.sleep(2)
        finally:
            self._shutdown_parsl()
            self.log("Stopping server")
            if self.server_type != 'direct':
                super().stop()

    def run_once(self):
        """Drain whatever is currently in the queue, then exit.

        This is the multinode lifecycle: ``nxserver run`` is invoked
        (manually, from cron, or as a one-shot script after
        ``submit_batch``); it reads pending entries grouped by batch_id,
        provisions one block per batch via Parsl, submits, waits for
        all to complete, and exits.
        """
        self.log(f'Single-shot run (pid={os.getpid()})')
        # Group queued entries by batch.
        groups = {}
        pending = []
        while True:
            item = self.read_task()
            if item is None:
                break
            command, batch_id = item
            if command == 'stop':
                continue
            pending.append((command, batch_id))
            if batch_id is not None:
                groups.setdefault(batch_id, []).append(command)
        # Per-batch submission preserves the "one allocation per batch"
        # contract across daemon restarts.
        for batch_id, cmds in groups.items():
            self.submit_batch(cmds)
        # Anything without a batch_id is treated as a singleton batch.
        for command, _ in [p for p in pending if p[1] is None]:
            self.submit_batch([command])
        try:
            import parsl
            parsl.wait_for_current_tasks()
        finally:
            self._shutdown_parsl()
            self.log("Single-shot run complete")

    def _replay_pending(self):
        """At daemon start, re-dispatch anything already queued."""
        groups = {}
        singletons = []
        while True:
            item = self.read_task()
            if item is None:
                break
            command, batch_id = item
            if command == 'stop':
                continue
            if batch_id is None:
                singletons.append(command)
            else:
                groups.setdefault(batch_id, []).append(command)
        for batch_id, cmds in groups.items():
            for cmd in cmds:
                self._dispatch(cmd, batch_id=batch_id)
        for cmd in singletons:
            self._dispatch(cmd)

    def _shutdown_parsl(self):
        if not self._parsl_loaded:
            return
        try:
            import parsl
            parsl.dfk().cleanup()
        except Exception as error:  # noqa: BLE001
            self.log(f"Parsl cleanup error: {error}")
        finally:
            self._parsl_loaded = False
            self._run_command = None

    def start(self):
        """Start the server.

        * ``direct`` — load Parsl in-process and start a thread that runs
          :meth:`run` (no daemon).
        * ``multicore`` — daemonise and run :meth:`run`.
        * ``multinode`` — call :meth:`run_once` synchronously and return.
        """
        if self.server_type == 'direct':
            from threading import Thread
            Thread(target=self.run, daemon=True).start()
        elif self.server_type == 'multicore':
            super().start()
        elif self.server_type == 'multinode':
            self.run_once()

    def stop(self):
        """Stop the server when active tasks are completed."""
        if self.is_running():
            self.add_task('stop')

    def restart(self):
        """Stop and start again. NXDaemon provides this for non-direct modes."""
        if self.server_type == 'direct':
            self.stop()
            self.start()
        else:
            super().restart()

    def clear(self):
        """Clear the durable queue (or in-memory queue for direct)."""
        if self.server_type == 'direct':
            self._task_queue = Queue()
        else:
            with self.task_queue.lock:
                if self.queue_directory.exists():
                    shutil.rmtree(self.queue_directory, ignore_errors=True)
            self._task_queue = NXFileQueue(self.queue_directory)

    def kill(self):
        """Forcibly terminate the daemon process (non-direct modes only)."""
        if self.server_type != 'direct':
            super().stop()

    def status(self):
        if self.server_type == 'direct':
            return "Server is configured to run commands directly"
        return super().status()

    def is_running(self):
        """Whether the server has an active dispatcher."""
        if self.server_type == 'direct':
            return self._parsl_loaded
        return super().is_running()
