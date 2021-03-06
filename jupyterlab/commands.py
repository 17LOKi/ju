# coding: utf-8
"""JupyterLab command handler"""

# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
from __future__ import print_function

from distutils.version import LooseVersion
import errno
import glob
import hashlib
import json
import logging
import os
import os.path as osp
import re
import shutil
import site
import sys
import tarfile
from threading import Event

from ipython_genutils.tempdir import TemporaryDirectory
from ipython_genutils.py3compat import which
from jupyter_core.paths import jupyter_config_path
from notebook.nbextensions import GREEN_ENABLED, GREEN_OK, RED_DISABLED, RED_X

from .semver import Range, gte, lt, lte, gt
from .jlpmapp import YARN_PATH, HERE
from .process import Process, WatchHelper


# The regex for expecting the webpack output.
WEBPACK_EXPECT = re.compile(r'.*/index.out.js')

# The dev mode directory.
DEV_DIR = osp.realpath(os.path.join(HERE, '..', 'dev_mode'))


def pjoin(*args):
    """Join paths to create a real path.
    """
    return osp.realpath(osp.join(*args))


def get_user_settings_dir():
    """Get the configured JupyterLab app directory.
    """
    settings_dir = os.environ.get('JUPYTERLAB_SETTINGS_DIR')
    settings_dir = settings_dir or pjoin(
        jupyter_config_path()[0], 'lab', 'user-settings'
    )
    return osp.realpath(settings_dir)


def get_app_dir():
    """Get the configured JupyterLab app directory.
    """
    # Default to the override environment variable.
    if os.environ.get('JUPYTERLAB_DIR'):
        return osp.realpath(os.environ['JUPYTERLAB_DIR'])

    # Use the default locations for data_files.
    app_dir = pjoin(sys.prefix, 'share', 'jupyter', 'lab')

    # Check for a user level install.
    # Ensure that USER_BASE is defined
    if hasattr(site, 'getuserbase'):
        site.getuserbase()
    userbase = getattr(site, 'USER_BASE', None)
    if HERE.startswith(userbase) and not app_dir.startswith(userbase):
        app_dir = pjoin(userbase, 'share', 'jupyter', 'lab')

    # Check for a system install in '/usr/local/share'.
    elif (sys.prefix.startswith('/usr') and not
          osp.exists(app_dir) and
          osp.exists('/usr/local/share/jupyter/lab')):
        app_dir = '/usr/local/share/jupyter/lab'

    return osp.realpath(app_dir)


def ensure_dev(logger=None):
    """Ensure that the dev assets are available.
    """
    parent = pjoin(HERE, '..')

    if not osp.exists(pjoin(parent, 'node_modules')):
        yarn_proc = Process(['node', YARN_PATH], cwd=parent, logger=logger)
        yarn_proc.wait()

    if not osp.exists(pjoin(parent, 'dev_mode', 'build')):
        yarn_proc = Process(['node', YARN_PATH, 'build'], cwd=parent,
                            logger=logger)
        yarn_proc.wait()


def watch_dev(logger=None):
    """Run watch mode in a given directory.

    Parameters
    ----------
    logger: :class:`~logger.Logger`, optional
        The logger instance.

    Returns
    -------
    A list of `WatchHelper` objects.
    """
    parent = pjoin(HERE, '..')

    if not osp.exists(pjoin(parent, 'node_modules')):
        yarn_proc = Process(['node', YARN_PATH], cwd=parent, logger=logger)
        yarn_proc.wait()

    logger = logger or logging.getLogger('jupyterlab')
    ts_dir = osp.realpath(osp.join(HERE, '..', 'packages', 'metapackage'))

    # Run typescript watch and wait for compilation.
    ts_regex = r'.* Compilation complete\. Watching for file changes\.'
    ts_proc = WatchHelper(['node', YARN_PATH, 'run', 'watch'],
        cwd=ts_dir, logger=logger, startup_regex=ts_regex)

    # Run the metapackage file watcher.
    tsf_regex = 'Watching the metapackage files...'
    tsf_proc = WatchHelper(['node', YARN_PATH, 'run', 'watch:files'],
        cwd=ts_dir, logger=logger, startup_regex=tsf_regex)

    # Run webpack watch and wait for compilation.
    wp_proc = WatchHelper(['node', YARN_PATH, 'run', 'watch'],
        cwd=DEV_DIR, logger=logger,
        startup_regex=WEBPACK_EXPECT)

    return [ts_proc, tsf_proc, wp_proc]


def watch(app_dir=None, logger=None):
    """Watch the application.

    Parameters
    ----------
    app_dir: string, optional
        The application directory.
    logger: :class:`~logger.Logger`, optional
        The logger instance.

    Returns
    -------
    A list of processes to run asynchronously.
    """
    handler = _AppHandler(app_dir, logger)
    return handler.watch()


def install_extension(extension, app_dir=None, logger=None):
    """Install an extension package into JupyterLab.

    The extension is first validated.
    """
    handler = _AppHandler(app_dir, logger)
    return handler.install_extension(extension)


def uninstall_extension(name, app_dir=None, logger=None):
    """Uninstall an extension by name or path.
    """
    handler = _AppHandler(app_dir, logger)
    return handler.uninstall_extension(name)


def clean(app_dir=None):
    """Clean the JupyterLab application directory."""
    app_dir = app_dir or get_app_dir()
    if app_dir == pjoin(HERE, 'dev'):
        raise ValueError('Cannot clean the dev app')
    if app_dir == pjoin(HERE, 'core'):
        raise ValueError('Cannot clean the core app')
    for name in ['staging']:
        target = pjoin(app_dir, name)
        if osp.exists(target):
            shutil.rmtree(target)


def build(app_dir=None, name=None, version=None, logger=None,
        command='build:prod', kill_event=None,
        clean_staging=False):
    """Build the JupyterLab application.
    """
    handler = _AppHandler(app_dir, logger, kill_event=kill_event)
    return handler.build(name=name, version=version,
                  command=command, clean_staging=clean_staging)


def get_app_info(app_dir=None, logger=None):
    """Get a dictionary of information about the app.
    """
    handler = _AppHandler(app_dir, logger)
    return handler.info


def enable_extension(extension, app_dir=None, logger=None):
    """Enable a JupyterLab extension.
    """
    handler = _AppHandler(app_dir, logger)
    return handler.toggle_extension(extension, False)


def disable_extension(extension, app_dir=None, logger=None):
    """Disable a JupyterLab package.
    """
    handler = _AppHandler(app_dir, logger)
    return handler.toggle_extension(extension, True)


def build_check(app_dir=None, logger=None):
    """Determine whether JupyterLab should be built.

    Returns a list of messages.
    """
    handler = _AppHandler(app_dir, logger)
    return handler.build_check()


def list_extensions(app_dir=None, logger=None):
    """List the extensions.
    """
    handler = _AppHandler(app_dir, logger)
    return handler.list_extensions()


def link_package(path, app_dir=None, logger=None):
    """Link a package against the JupyterLab build."""
    handler = _AppHandler(app_dir, logger)
    return handler.link_package(path)


def unlink_package(package, app_dir=None, logger=None):
    """Unlink a package from JupyterLab by path or name.
    """
    handler = _AppHandler(app_dir, logger)
    return handler.unlink_package(package)


def get_app_version():
    """Get the application version."""
    return _get_core_data()['jupyterlab']['version']


# ----------------------------------------------------------------------
# Implementation details
# ----------------------------------------------------------------------


class _AppHandler(object):

    def __init__(self, app_dir, logger=None, kill_event=None):
        if app_dir and app_dir.startswith(HERE):
            raise ValueError('Cannot run lab extension commands in core app')
        self.app_dir = app_dir or get_app_dir()
        self.sys_dir = get_app_dir()
        self.logger = logger or logging.getLogger('jupyterlab')
        self.info = self._get_app_info()
        self.kill_event = kill_event or Event()

    def install_extension(self, extension, existing=None):
        """Install an extension package into JupyterLab.

        The extension is first validated.
        """
        extension = _normalize_path(extension)
        extensions = self.info['extensions']

        # Check for a core extensions.
        if extension in self.info['core_extensions']:
            config = self._read_build_config()
            uninstalled = config.get('uninstalled_core_extensions', [])
            if extension in uninstalled:
                uninstalled.remove(extension)
                config['uninstalled_core_extensions'] = uninstalled
                self._write_build_config(config)
            return

        # Create the app dirs if needed.
        self._ensure_app_dirs()

        # Install the package using a temporary directory.
        with TemporaryDirectory() as tempdir:
            info = self._install_extension(extension, tempdir)

        name = info['name']

        # Local directories get name mangled and stored in metadata.
        if info['is_dir']:
            config = self._read_build_config()
            local = config.setdefault('local_extensions', dict())
            local[name] = info['source']
            self._write_build_config(config)

        # Remove an existing extension with the same name and different path
        if name in extensions:
            other = extensions[name]
            if other['path'] != info['path'] and other['location'] == 'app':
                os.remove(other['path'])

    def build(self, name=None, version=None, command='build:prod',
            clean_staging=False):
        """Build the application.
        """
        # Set up the build directory.
        app_dir = self.app_dir

        self._populate_staging(
            name=name, version=version, clean=clean_staging
        )

        staging = pjoin(app_dir, 'staging')

        # Make sure packages are installed.
        self._run(['node', YARN_PATH, 'install'], cwd=staging)

        # Build the app.
        self._run(['node', YARN_PATH, 'run', command], cwd=staging)

    def watch(self):
        """Start the application watcher and then run the watch in
        the background.
        """
        staging = pjoin(self.app_dir, 'staging')

        self._populate_staging()

        # Make sure packages are installed.
        self._run(['node', YARN_PATH, 'install'], cwd=staging)

        proc = WatchHelper(['node', YARN_PATH, 'run', 'watch'],
            cwd=pjoin(self.app_dir, 'staging'),
            startup_regex=WEBPACK_EXPECT,
            logger=self.logger)
        return [proc]

    def list_extensions(self):
        """Print an output of the extensions.
        """
        logger = self.logger
        info = self.info

        logger.info('JupyterLab v%s' % info['version'])

        if info['extensions']:
            info['compat_errors'] = self._get_extension_compat()
            logger.info('Known labextensions:')
            self._list_extensions(info, 'app')
            self._list_extensions(info, 'sys')
        else:
            logger.info('No installed extensions')

        local = info['local_extensions']
        if local:
            logger.info('\n   local extensions:')
            for name in sorted(local):
                logger.info('        %s: %s' % (name, local[name]))

        linked_packages = info['linked_packages']
        if linked_packages:
            logger.info('\n   linked packages:')
            for key in sorted(linked_packages):
                source = linked_packages[key]['source']
                logger.info('        %s: %s' % (key, source))

        uninstalled_core = info['uninstalled_core']
        if uninstalled_core:
            logger.info('\nUninstalled core extensions:')
            [logger.info('    %s' % item) for item in sorted(uninstalled_core)]

        disabled_core = info['disabled_core']
        if disabled_core:
            logger.info('\nDisabled core extensions:')
            [logger.info('    %s' % item) for item in sorted(disabled_core)]

        messages = self.build_check(fast=True)
        if messages:
            logger.info('\nBuild recommended:')
            [logger.info('    %s' % item) for item in messages]

    def build_check(self, fast=False):
        """Determine whether JupyterLab should be built.

        Returns a list of messages.
        """
        app_dir = self.app_dir
        local = self.info['local_extensions']
        linked = self.info['linked_packages']
        messages = []

        # Check for no application.
        pkg_path = pjoin(app_dir, 'static', 'package.json')
        if not osp.exists(pkg_path):
            return ['No built application']

        with open(pkg_path) as fid:
            static_data = json.load(fid)

        old_jlab = static_data['jupyterlab']
        old_deps = static_data.get('dependencies', dict())

        # Look for mismatched version.
        static_version = old_jlab.get('version', '')
        core_version = old_jlab['version']
        if LooseVersion(static_version) != LooseVersion(core_version):
            msg = 'Version mismatch: %s (built), %s (current)'
            return [msg % (static_version, core_version)]

        # Look for mismatched extensions.
        new_package = self._get_package_template(silent=fast)
        new_jlab = new_package['jupyterlab']
        new_deps = new_package.get('dependencies', dict())

        for ext_type in ['extensions', 'mimeExtensions']:
            # Extensions that were added.
            for ext in new_jlab[ext_type]:
                if ext not in old_jlab[ext_type]:
                    messages.append('%s needs to be included' % ext)

            # Extensions that were removed.
            for ext in old_jlab[ext_type]:
                if ext not in new_jlab[ext_type]:
                    messages.append('%s needs to be removed' % ext)

        # Look for mismatched dependencies
        for (pkg, dep) in new_deps.items():
            if pkg not in old_deps:
                continue
            # Skip local and linked since we pick them up separately.
            if pkg in local or pkg in linked:
                continue
            if old_deps[pkg] != dep:
                msg = '%s changed from %s to %s'
                messages.append(msg % (pkg, old_deps[pkg], new_deps[pkg]))

        # Look for updated local extensions.
        for (name, source) in local.items():
            if fast:
                continue
            dname = pjoin(app_dir, 'extensions')
            if self._check_local(name, source, dname):
                messages.append('%s content changed' % name)

        # Look for updated linked packages.
        for (name, item) in linked.items():
            if fast:
                continue
            dname = pjoin(app_dir, 'staging', 'linked_packages')
            if self._check_local(name, item['source'], dname):
                messages.append('%s content changed' % name)

        return messages

    def uninstall_extension(self, name):
        """Uninstall an extension by name.
        """
        # Allow for uninstalled core extensions.
        data = self.info['core_data']
        if name in self.info['core_extensions']:
            self.logger.info('Uninstalling core extension %s' % name)
            config = self._read_build_config()
            uninstalled = config.get('uninstalled_core_extensions', [])
            if name not in uninstalled:
                uninstalled.append(name)
                config['uninstalled_core_extensions'] = uninstalled
                self._write_build_config(config)
            return True

        local = self.info['local_extensions']

        for (extname, data) in self.info['extensions'].items():
            path = data['path']
            if extname == name:
                msg = 'Uninstalling %s from %s' % (name, osp.dirname(path))
                self.logger.info(msg)
                os.remove(path)
                # Handle local extensions.
                if extname in local:
                    config = self._read_build_config()
                    data = config.setdefault('local_extensions', dict())
                    del data[extname]
                    self._write_build_config(config)
                return True

        self.logger.warn('No labextension named "%s" installed' % name)
        return False

    def link_package(self, path):
        """Link a package at the given path.
        """
        path = _normalize_path(path)
        if not osp.exists(path) or not osp.isdir(path):
            msg = 'Can install "%s" only link local directories'
            raise ValueError(msg % path)

        with TemporaryDirectory() as tempdir:
            info = self._extract_package(path, tempdir)

        messages = _validate_extension(info['data'])
        if not messages:
            return self.install_extension(path)

        # Warn that it is a linked package.
        self.logger.warn('Installing %s as a linked package:', path)
        [self.logger.warn(m) for m in messages]

        # Add to metadata.
        config = self._read_build_config()
        linked = config.setdefault('linked_packages', dict())
        linked[info['name']] = info['source']
        self._write_build_config(config)

    def unlink_package(self, path):
        """Link a package by name or at the given path.
        """
        path = _normalize_path(path)
        config = self._read_build_config()
        linked = config.setdefault('linked_packages', dict())

        found = None
        for (name, source) in linked.items():
            if name == path or source == path:
                found = name

        if found:
            del linked[found]
        else:
            local = config.setdefault('local_extensions', dict())
            for (name, source) in local.items():
                if name == path or source == path:
                    found = name
            if found:
                del local[found]
                path = self.info['extensions'][found]['path']
                os.remove(path)

        if not found:
            raise ValueError('No linked package for %s' % path)

        self._write_build_config(config)

    def toggle_extension(self, extension, value):
        """Enable or disable a lab extension.
        """
        config = self._read_page_config()
        disabled = config.setdefault('disabledExtensions', [])
        if value and extension not in disabled:
            disabled.append(extension)
        if not value and extension in disabled:
            disabled.remove(extension)
        self._write_page_config(config)

    def _get_app_info(self):
        """Get information about the app.
        """

        info = dict()
        info['core_data'] = core_data = _get_core_data()
        info['extensions'] = extensions = self._get_extensions(core_data)
        page_config = self._read_page_config()
        info['disabled'] = page_config.get('disabledExtensions', [])
        info['local_extensions'] = self._get_local_extensions()
        info['linked_packages'] = self._get_linked_packages()
        info['app_extensions'] = app = []
        info['sys_extensions'] = sys = []
        for (name, data) in extensions.items():
            data['is_local'] = name in info['local_extensions']
            if data['location'] == 'app':
                app.append(name)
            else:
                sys.append(name)

        info['uninstalled_core'] = self._get_uninstalled_core_extensions()
        info['version'] = core_data['jupyterlab']['version']
        info['sys_dir'] = self.sys_dir
        info['app_dir'] = self.app_dir

        info['core_extensions'] = core_extensions = _get_core_extensions()

        disabled_core = []
        for key in core_extensions:
            if key in info['disabled']:
                disabled_core.append(key)

        info['disabled_core'] = disabled_core
        return info

    def _populate_staging(self, name=None, version=None, clean=False):
        """Set up the assets in the staging directory.
        """
        app_dir = self.app_dir
        staging = pjoin(app_dir, 'staging')
        if clean and osp.exists(staging):
            self.logger.info("Cleaning %s", staging)
            shutil.rmtree(staging)

        self._ensure_app_dirs()
        if not version:
            version = self.info['core_data']['jupyterlab']['version']

        # Look for mismatched version.
        pkg_path = pjoin(staging, 'package.json')
        overwrite_lock = False

        if osp.exists(pkg_path):
            with open(pkg_path) as fid:
                data = json.load(fid)
            if data['jupyterlab'].get('version', '') != version:
                shutil.rmtree(staging)
                os.makedirs(staging)
            else:
                overwrite_lock = False

        for fname in ['index.js', 'webpack.config.js',
                'yarn.lock', '.yarnrc', 'yarn.js']:
            target = pjoin(staging, fname)
            if (fname == 'yarn.lock' and os.path.exists(target) and
                    not overwrite_lock):
                continue
            shutil.copy(pjoin(HERE, 'staging', fname), target)

        # Ensure a clean linked packages directory.
        linked_dir = pjoin(staging, 'linked_packages')
        if osp.exists(linked_dir):
            shutil.rmtree(linked_dir)
        os.makedirs(linked_dir)

        # Template the package.json file.
        # Update the local extensions.
        extensions = self.info['extensions']
        for (key, source) in self.info['local_extensions'].items():
            dname = pjoin(app_dir, 'extensions')
            self._update_local(key, source, dname, extensions[key],
                'local_extensions')

        # Update the linked packages.
        linked = self.info['linked_packages']
        for (key, item) in linked.items():
            dname = pjoin(staging, 'linked_packages')
            self._update_local(key, item['source'], dname, item,
                'linked_packages')

        # Then get the package template.
        data = self._get_package_template()

        if version:
            data['jupyterlab']['version'] = version

        if name:
            data['jupyterlab']['name'] = name

        pkg_path = pjoin(staging, 'package.json')
        with open(pkg_path, 'w') as fid:
            json.dump(data, fid, indent=4)

    def _get_package_template(self, silent=False):
        """Get the template the for staging package.json file.
        """
        logger = self.logger
        data = self.info['core_data']
        local = self.info['local_extensions']
        linked = self.info['linked_packages']
        extensions = self.info['extensions']
        jlab = data['jupyterlab']

        def format_path(path):
            path = osp.relpath(path, pjoin(self.app_dir, 'staging'))
            path = 'file:' + path.replace(os.sep, '/')
            if os.name == 'nt':
                path = path.lower()
            return path

        # Handle extensions
        compat_errors = self._get_extension_compat()
        for (key, value) in extensions.items():
            # Reject incompatible extensions with a message.
            errors = compat_errors[key]
            if errors:
                msg = _format_compatibility_errors(
                    key, value['version'], errors
                )
                if not silent:
                    logger.warn(msg + '\n')
                continue

            data['dependencies'][key] = format_path(value['path'])

            jlab_data = value['jupyterlab']
            for item in ['extension', 'mimeExtension']:
                ext = jlab_data.get(item, False)
                if not ext:
                    continue
                if ext is True:
                    ext = ''
                jlab[item + 's'][key] = ext

        jlab['linkedPackages'] = dict()

        # Handle local extensions.
        for (key, source) in local.items():
            jlab['linkedPackages'][key] = source

        # Handle linked packages.
        for (key, item) in linked.items():
            path = pjoin(self.app_dir, 'staging', 'linked_packages')
            path = pjoin(path, item['filename'])
            data['dependencies'][key] = format_path(path)
            jlab['linkedPackages'][key] = item['source']

        # Handle uninstalled core extensions.
        for item in self.info['uninstalled_core']:
            if item in jlab['extensions']:
                data['jupyterlab']['extensions'].pop(item)
            else:
                data['jupyterlab']['mimeExtensions'].pop(item)
            # Remove from dependencies as well.
            data['dependencies'].pop(item)

        return data

    def _check_local(self, name, source, dname):
        # Extract the package in a temporary directory.
        with TemporaryDirectory() as tempdir:
            info = self._extract_package(source, tempdir)
            # Test if the file content has changed.
            target = pjoin(dname, info['filename'])
            return not osp.exists(target)

    def _update_local(self, name, source, dname, data, dtype):
        """Update a local dependency.  Return `True` if changed.
        """
        # Extract the package in a temporary directory.
        existing = data['filename']
        with TemporaryDirectory() as tempdir:
            info = self._extract_package(source, tempdir)

            # Bail if the file content has not changed.
            if info['filename'] == existing:
                return existing

            shutil.move(info['path'], pjoin(dname, info['filename']))

        # Remove the existing tarball and return the new file name.
        if existing:
            os.remove(pjoin(dname, existing))

        data['filename'] = info['filename']
        data['path'] = pjoin(data['tar_dir'], data['filename'])
        return info['filename']

    def _get_extensions(self, core_data):
        """Get the extensions for the application.
        """
        app_dir = self.app_dir
        extensions = dict()

        # Get system level packages.
        sys_path = pjoin(self.sys_dir, 'extensions')
        app_path = pjoin(self.app_dir, 'extensions')

        extensions = self._get_extensions_in_dir(self.sys_dir, core_data)

        # Look in app_dir if different.
        app_path = pjoin(app_dir, 'extensions')
        if app_path == sys_path or not osp.exists(app_path):
            return extensions

        extensions.update(self._get_extensions_in_dir(app_dir, core_data))

        return extensions

    def _get_extensions_in_dir(self, dname, core_data):
        """Get the extensions in a given directory.
        """
        extensions = dict()
        location = 'app' if dname == self.app_dir else 'sys'
        for target in glob.glob(pjoin(dname, 'extensions', '*.tgz')):
            data = _read_package(target)
            deps = data.get('dependencies', dict())
            name = data['name']
            jlab = data.get('jupyterlab', dict())
            path = osp.realpath(target)
            extensions[name] = dict(path=path,
                                    filename=osp.basename(path),
                                    version=data['version'],
                                    jupyterlab=jlab,
                                    dependencies=deps,
                                    tar_dir=osp.dirname(path),
                                    location=location)
        return extensions

    def _get_extension_compat(self):
        """Get the extension compatibility info.
        """
        compat = dict()
        core_data = self.info['core_data']
        for (name, data) in self.info['extensions'].items():
            deps = data['dependencies']
            compat[name] = _validate_compatibility(name, deps, core_data)
        return compat

    def _get_local_extensions(self):
        """Get the locally installed extensions.
        """
        return self._get_local_data('local_extensions')

    def _get_linked_packages(self):
        """Get the linked packages.
        """
        info = self._get_local_data('linked_packages')
        dname = pjoin(self.app_dir, 'staging', 'linked_packages')
        for (name, source) in info.items():
            info[name] = dict(source=source, filename='', tar_dir=dname)

        if not osp.exists(dname):
            return info

        for path in glob.glob(pjoin(dname, '*.tgz')):
            path = osp.realpath(path)
            data = _read_package(path)
            name = data['name']
            if name not in info:
                self.logger.warn('Removing orphaned linked package %s' % name)
                os.remove(path)
                continue
            item = info[name]
            item['filename'] = osp.basename(path)
            item['path'] = path
            item['version'] = data['version']
            item['data'] = data
        return info

    def _get_uninstalled_core_extensions(self):
        """Get the uninstalled core extensions.
        """
        config = self._read_build_config()
        return config.get('uninstalled_core_extensions', [])

    def _ensure_app_dirs(self):
        """Ensure that the application directories exist"""
        dirs = ['extensions', 'settings', 'staging', 'schemas', 'themes']
        for dname in dirs:
            path = pjoin(self.app_dir, dname)
            if not osp.exists(path):
                try:
                    os.makedirs(path)
                except OSError as e:
                    if e.errno != errno.EEXIST:
                        raise

    def _list_extensions(self, info, ext_type):
        """List the extensions of a given type.
        """
        logger = self.logger
        names = info['%s_extensions' % ext_type]
        if not names:
            return

        dname = info['%s_dir' % ext_type]

        logger.info('   %s dir: %s' % (ext_type, dname))
        for name in sorted(names):
            logger.info(name)
            data = info['extensions'][name]
            version = data['version']
            errors = info['compat_errors'][name]
            extra = ''
            if _is_disabled(name, info['disabled']):
                extra += ' %s' % RED_DISABLED
            else:
                extra += ' %s' % GREEN_ENABLED
            if errors:
                extra += ' %s' % RED_X
            else:
                extra += ' %s' % GREEN_OK
            if data['is_local']:
                extra += '*'
            logger.info('        %s v%s%s' % (name, version, extra))
            if errors:
                msg = _format_compatibility_errors(
                    name, version, errors
                )
                logger.warn(msg + '\n')

    def _read_build_config(self):
        """Get the build config data for the app dir.
        """
        target = pjoin(self.app_dir, 'settings', 'build_config.json')
        if not osp.exists(target):
            return {}
        else:
            with open(target) as fid:
                return json.load(fid)

    def _write_build_config(self, config):
        """Write the build config to the app dir.
        """
        self._ensure_app_dirs()
        target = pjoin(self.app_dir, 'settings', 'build_config.json')
        with open(target, 'w') as fid:
            json.dump(config, fid, indent=4)

    def _read_page_config(self):
        """Get the page config data for the app dir.
        """
        target = pjoin(self.app_dir, 'settings', 'page_config.json')
        if not osp.exists(target):
            return {}
        else:
            with open(target) as fid:
                return json.load(fid)

    def _write_page_config(self, config):
        """Write the build config to the app dir.
        """
        self._ensure_app_dirs()
        target = pjoin(self.app_dir, 'settings', 'page_config.json')
        with open(target, 'w') as fid:
            json.dump(config, fid, indent=4)

    def _get_local_data(self, source):
        """Get the local data for extensions or linked packages.
        """
        config = self._read_build_config()

        data = config.setdefault(source, dict())
        dead = []
        for (name, source) in data.items():
            if not osp.exists(source):
                dead.append(name)

        for name in dead:
            link_type = source.replace('_', ' ')
            msg = '**Note: Removing dead %s "%s"' % (link_type, name)
            self.logger.warn(msg)
            del data[name]

        if dead:
            self._write_build_config(config)

        return data

    def _install_extension(self, extension, tempdir):
        """Install an extension with validation and return the name and path.
        """
        info = self._extract_package(extension, tempdir)
        data = info['data']

        # Verify that the package is an extension.
        messages = _validate_extension(data)
        if messages:
            msg = '"%s" is not a valid extension:\n%s'
            raise ValueError(msg % (extension, '\n'.join(messages)))

        # Verify package compatibility.
        core_data = _get_core_data()
        deps = data.get('dependencies', dict())
        errors = _validate_compatibility(extension, deps, core_data)
        if errors:
            msg = _format_compatibility_errors(
                data['name'], data['version'], errors
            )
            raise ValueError(msg)

        # Move the file to the app directory.
        target = pjoin(self.app_dir, 'extensions', info['filename'])
        if osp.exists(target):
            os.remove(target)

        shutil.move(info['path'], target)

        info['path'] = target
        return info

    def _extract_package(self, source, tempdir):
        # npm pack the extension
        is_dir = osp.exists(source) and osp.isdir(source)
        if is_dir and not osp.exists(pjoin(source, 'node_modules')):
            self._run(['node', YARN_PATH, 'install'], cwd=source)

        info = dict(source=source, is_dir=is_dir)

        ret = self._run([which('npm'), 'pack', source], cwd=tempdir)
        if ret != 0:
            msg = '"%s" is not a valid npm package'
            raise ValueError(msg % source)

        path = glob.glob(pjoin(tempdir, '*.tgz'))[0]
        info['data'] = _read_package(path)
        if is_dir:
            info['sha'] = sha = _tarsum(path)
            target = path.replace('.tgz', '-%s.tgz' % sha)
            shutil.move(path, target)
            info['path'] = target
        else:
            info['path'] = path

        info['filename'] = osp.basename(info['path'])
        info['name'] = info['data']['name']
        info['version'] = info['data']['version']

        return info

    def _run(self, cmd, **kwargs):
        """Run the command using our logger and abort callback.

        Returns the exit code.
        """
        if self.kill_event.is_set():
            raise ValueError('Command was killed')

        kwargs['logger'] = self.logger
        kwargs['kill_event'] = self.kill_event
        proc = Process(cmd, **kwargs)
        return proc.wait()


def _normalize_path(extension):
    """Normalize a given extension if it is a path.
    """
    extension = osp.expanduser(extension)
    if osp.exists(extension):
        extension = osp.abspath(extension)
    return extension


def _read_package(target):
    """Read the package data in a given target tarball.
    """
    tar = tarfile.open(target, "r:gz")
    f = tar.extractfile('package/package.json')
    data = json.loads(f.read().decode('utf8'))
    data['jupyterlab_extracted_files'] = [
        f.path[len('package/'):] for f in tar.getmembers()
    ]
    tar.close()
    return data


def _validate_extension(data):
    """Detect if a package is an extension using its metadata.

    Returns any problems it finds.
    """
    jlab = data.get('jupyterlab', None)
    if jlab is None:
        return ['No `jupyterlab` key']
    if not isinstance(jlab, dict):
        return ['The `jupyterlab` key must be a JSON object']
    extension = jlab.get('extension', False)
    mime_extension = jlab.get('mimeExtension', False)
    themeDir = jlab.get('themeDir', '')
    schemaDir = jlab.get('schemaDir', '')

    messages = []
    if not extension and not mime_extension:
        messages.append('No `extension` or `mimeExtension` key present')

    if extension == mime_extension:
        msg = '`mimeExtension` and `extension` must point to different modules'
        messages.append(msg)

    files = data['jupyterlab_extracted_files']
    main = data.get('main', 'index.js')
    if not main.endswith('.js'):
        main += '.js'

    if extension is True:
        extension = main
    elif extension and not extension.endswith('.js'):
        extension += '.js'

    if mime_extension is True:
        mime_extension = main
    elif mime_extension and not mime_extension.endswith('.js'):
        mime_extension += '.js'

    if extension and extension not in files:
        messages.append('Missing extension module "%s"' % extension)

    if mime_extension and mime_extension not in files:
        messages.append('Missing mimeExtension module "%s"' % mime_extension)

    if themeDir and not any(f.startswith(themeDir) for f in files):
        messages.append('themeDir is empty: "%s"' % themeDir)

    if schemaDir and not any(f.startswith(schemaDir) for f in files):
        messages.append('schemaDir is empty: "%s"' % schemaDir)

    return messages


def _tarsum(input_file):
    """
    Compute the recursive sha sum of a tar file.
    """
    tar = tarfile.open(input_file, "r:gz")
    chunk_size = 100 * 1024
    h = hashlib.new("sha1")

    for member in tar:
        if not member.isfile():
            continue
        f = tar.extractfile(member)
        data = f.read(chunk_size)
        while data:
            h.update(data)
            data = f.read(chunk_size)
    return h.hexdigest()


def _get_core_data():
    """Get the data for the app template.
    """
    with open(pjoin(HERE, 'staging', 'package.json')) as fid:
        return json.load(fid)


def _validate_compatibility(extension, deps, core_data):
    """Validate the compatibility of an extension.
    """
    core_deps = core_data['dependencies']
    singletons = core_data['jupyterlab']['singletonPackages']

    errors = []

    for (key, value) in deps.items():
        if key in singletons:
            overlap = _test_overlap(core_deps[key], value)
            if overlap is False:
                errors.append((key, core_deps[key], value))

    return errors


def _test_overlap(spec1, spec2):
    """Test whether two version specs overlap.

    Returns `None` if we cannot determine compatibility,
    otherwise whether there is an overlap
    """
    # Test for overlapping semver ranges.
    r1 = Range(spec1, True)
    r2 = Range(spec2, True)

    # If either range is empty, we cannot verify.
    if not r1.range or not r2.range:
        return

    x1 = r1.set[0][0].semver
    x2 = r1.set[0][-1].semver
    y1 = r2.set[0][0].semver
    y2 = r2.set[0][-1].semver

    o1 = r1.set[0][0].operator
    o2 = r2.set[0][0].operator

    # We do not handle (<) specifiers.
    if (o1.startswith('<') or o2.startswith('<')):
        return

    # Handle single value specifiers.
    lx = lte if x1 == x2 else lt
    ly = lte if y1 == y2 else lt
    gx = gte if x1 == x2 else gt
    gy = gte if x1 == x2 else gt

    # Handle unbounded (>) specifiers.
    def noop(x, y, z):
        return True

    if x1 == x2 and o1.startswith('>'):
        lx = noop
    if y1 == y2 and o2.startswith('>'):
        ly = noop

    # Check for overlap.
    return (
        gte(x1, y1, True) and ly(x1, y2, True) or
        gy(x2, y1, True) and ly(x2, y2, True) or
        gte(y1, x1, True) and lx(y1, x2, True) or
        gx(y2, x1, True) and lx(y2, x2, True)
    )


def _is_disabled(name, disabled=[]):
    """Test whether the package is disabled.
    """
    for pattern in disabled:
        if name == pattern:
            return True
        if re.compile(pattern).match(name) is not None:
            return True
    return False


def _format_compatibility_errors(name, version, errors):
    """Format a message for compatibility errors.
    """
    msgs = []
    l0 = 10
    l1 = 10
    for error in errors:
        pkg, jlab, ext = error
        jlab = str(Range(jlab, True))
        ext = str(Range(ext, True))
        msgs.append((pkg, jlab, ext))
        l0 = max(l0, len(pkg) + 1)
        l1 = max(l1, len(jlab) + 1)

    msg = '\n"%s@%s" is not compatible with the current JupyterLab'
    msg = msg % (name, version)
    msg += '\nConflicting Dependencies:\n'
    msg += 'JupyterLab'.ljust(l0)
    msg += 'Extension'.ljust(l1)
    msg += 'Package\n'

    for (pkg, jlab, ext) in msgs:
        msg += jlab.ljust(l0) + ext.ljust(l1) + pkg + '\n'

    return msg


def _get_core_extensions():
    """Get the core extensions.
    """
    data = _get_core_data()['jupyterlab']
    return list(data['extensions']) + list(data['mimeExtensions'])
