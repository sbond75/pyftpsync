"""
(c) 2012-2024 Martin Wendt; see https://github.com/mar10/pyftpsync
Licensed under the MIT license: https://www.opensource.org/licenses/mit-license.php
"""

import json
import logging
import os
import stat
import time
from posixpath import join as join_url
from posixpath import normpath as normpath_url
from posixpath import relpath as relpath_url
from tempfile import SpooledTemporaryFile
from unittest.mock import patch

import paramiko
import pysftp

from ftpsync.metadata import DirMetadata, IncompatibleMetadataVersionError
from ftpsync.resources import DirectoryEntry, FileEntry
from ftpsync.targets import _get_encoding_opt, _Target
from ftpsync.util import (
    CliSilentRuntimeError,
    get_credentials_for_url,
    is_native,
    prompt_for_password,
    save_password,
    write,
    write_error,
)


class PatchedPysftpConnection(pysftp.Connection):
    """
    Patched version that fixes exception on connect errors:
    `AttributeError: 'Connection' object has no attribute '_sftp_live'`
    https://stackoverflow.com/a/65060184
    """

    def __init__(self, *args, **kwargs):
        self._sftp_live = False
        self._transport = None
        super().__init__(*args, **kwargs)


# ===============================================================================
# SFTPTarget
# ===============================================================================
class SFTPTarget(_Target):
    """Represents a synchronization target on an SFTP server.

    Attributes:
        path (str): Current working directory on SFTP server.
        sftp (pysftp.Connection): Instance of pysftp.Connection.
        host (str): hostname of SFTP server
        port (int): SFTP port (defaults to 22)
        username (str):
        password (str):
    """

    DEFAULT_BLOCKSIZE = 8 * 1024  # ftplib uses 8k chunks by default
    # keep open_readable() buffer in memory if smaller than 100kB
    MAX_SPOOL_MEM = 100 * 1024

    def __init__(
        self,
        path,
        host,
        port=22,
        username=None,
        password=None,
        timeout=None,
        extra_opts=None,
    ):
        """Create SFTP target with host, initial path, optional credentials and options.

        Args:
            path (str): root path on SFTP server, relative to *host*
            host (str): hostname of SFTP server
            port (int): SFTP port (defaults to 22)
            username (str):
            password (str):
            timeout (int): the timeout to set against the ftp socket (seconds)
            extra_opts (dict):
        """
        self.encoding = _get_encoding_opt(None, extra_opts, "utf-8")
        # path = self.to_unicode(path)
        path = path or "/"
        assert is_native(path)
        super().__init__(path, extra_opts)

        self.sftp = None
        self.host = host
        self.port = port or 22
        self.username = username
        self.password = password
        self.timeout = timeout
        #: dict: written to ftp target root folder before synchronization starts.
        #: set to False, if write failed. Default: None
        self.lock_data = None
        self.lock_write_time = None
        #: Time difference between <local upload time> and the mtime that the
        #: server reports afterwards.
        #: The value is added to the 'u' time stored in meta data.
        #: (This is only a rough estimation, derived from the lock-file.)
        self.server_time_ofs = None
        self.ftp_socket_connected = False
        # self.support_set_time = False
        # #: Optionally define an encoding for this server
        # encoding = self.get_option("encoding", "utf-8")
        # self.encoding = codecs.lookup(encoding).name
        return

    def __str__(self):
        return "<{} + {}>".format(
            self.get_base_name(), relpath_url(self.cur_dir or "/", self.root_dir)
        )

    def get_base_name(self):
        scheme = "sftp"
        return f"{scheme}://{self.host}{self.root_dir}"

    def open(self):
        assert not self.ftp_socket_connected

        super().open()

        options = self.get_options_dict()
        no_prompt = self.get_option("no_prompt", True)
        store_password = self.get_option("store_password", False)
        verbose = self.get_option("verbose", 3)
        verify_host_keys = not self.get_option("no_verify_host_keys", False)
        if self.get_option("ftp_active", False):
            raise RuntimeError("SFTP does not have active/passive mode.")

        if verbose <= 3:
            logging.getLogger("paramiko.transport").setLevel(logging.WARNING)

        cnopts = pysftp.CnOpts()
        cnopts.log = self.get_option("ftp_debug", False)
        if not verify_host_keys:
            cnopts.hostkeys = None

        if self.username is None or self.password is None:
            creds = get_credentials_for_url(
                self.host, options, force_user=self.username
            )
            if creds:
                self.username, self.password = creds

        write(f"Connecting {self.username}:*** to sftp://{self.host}")

        assert self.sftp is None
        while True:
            try:
                self.sftp = PatchedPysftpConnection(
                    self.host,
                    username=self.username,
                    password=self.password,
                    port=self.port,
                    cnopts=cnopts,
                )
                break
            except paramiko.ssh_exception.AuthenticationException as e:
                write_error(f"Could not login to {self.username}@{self.host}: {e}")
                if no_prompt or not self.username:
                    raise
                creds = prompt_for_password(self.host, self.username)
                self.username, self.password = creds
                # Continue while-loop
            except paramiko.ssh_exception.SSHException as e:
                raise CliSilentRuntimeError(
                    f"{e}: Try `ssh-keyscan HOST` to add it to `USER/.ssh/known_hosts` "
                    "(or pass `--no-verify-host-keys` if you don't care about security).",
                    min_verbosity=4,
                )

        if verbose >= 4:
            write(
                "Login as '{}'.".format(self.username if self.username else "anonymous")
            )
        if self.sftp.logfile:
            write(f"Logging to {self.sftp.logfile}")
        self.sftp.timeout = self.timeout
        self.ftp_socket_connected = True

        try:
            self.sftp.cwd(self.root_dir)
        except OSError as e:
            # '550 No such directory' is not reliably detectable with SFTP?

            # Implement --create-folder option for remote targets:
            if self.is_unbound():
                # E.g. 'tree' command
                write_error(
                    f"Could not change directory to {self.root_dir} ({e}): missing permissions?"
                )
            elif self.is_local():
                write_error(
                    f"Could not change local directory to {self.root_dir} ({e}): missing permissions?"
                )
            else:
                parent = os.path.dirname(self.root_dir)
                subfolder = os.path.basename(self.root_dir)
                if not self.get_option("create_folder", False):
                    msg = (
                        f"Could not change remote directory to {self.root_dir!r} ({e!r}). "
                        "This may be due to missing permissions or because the folder does not exist. "
                        f"Pass `--create-folder` if you want to create {subfolder!r} within {parent!r}."
                    )
                    raise CliSilentRuntimeError(msg, min_verbosity=4)

                write_error(
                    f"Could not change remote directory to {self.root_dir!r} ({e!r}). "
                    f"`--create-folder` was passed: creating {subfolder!r} within {parent!r}..."
                )
                self.sftp.cwd(parent)
                self.mkdir(subfolder)
                # Must work now:
                self.sftp.cwd(self.root_dir)

        pwd = self.pwd()
        if pwd != self.root_dir:
            raise RuntimeError(
                "Unable to navigate to working directory {!r} (now at {!r})".format(
                    self.root_dir, pwd
                )
            )

        self.cur_dir = pwd

        # Successfully authenticated: store password
        if store_password:
            save_password(self.host, self.username, self.password)

        self._lock()

        return

    def close(self):
        if self.lock_data:
            self._unlock(closing=True)

        if self.ftp_socket_connected:
            try:
                self.sftp.close()
            except (ConnectionError, EOFError) as e:
                write_error(f"sftp.close() failed: {e}")
            self.ftp_socket_connected = False

        super().close()

    def _lock(self, break_existing=False):
        """Write a special file to the target root folder."""
        # write("_lock")
        data = {"lock_time": time.time(), "lock_holder": None}

        try:
            assert self.cur_dir == self.root_dir
            self.write_text(DirMetadata.LOCK_FILE_NAME, json.dumps(data))
            self.lock_data = data
            self.lock_write_time = time.time()
        except Exception as e:
            write_error(f"Could not write lock file: {e}")
            # Set to False, so we don't try to remove later
            self.lock_data = False

    def _unlock(self, closing=False):
        """Remove lock file to the target root folder."""
        # write("_unlock", closing)
        try:
            if self.cur_dir != self.root_dir:
                if closing:
                    write(
                        "Changing to ftp root folder to remove lock file: {}".format(
                            self.root_dir
                        )
                    )
                    self.cwd(self.root_dir)
                else:
                    write_error(
                        "Could not remove lock file, because CWD != ftp root: {}".format(
                            self.cur_dir
                        )
                    )
                    return

            if self.lock_data is False:
                if self.get_option("verbose", 3) >= 4:
                    write("Skip remove lock file (was not written).")
            else:
                # direct delete, without updating metadata or checking for target access:
                self.sftp.remove(DirMetadata.LOCK_FILE_NAME)
                # self.remove_file(DirMetadata.LOCK_FILE_NAME)

            self.lock_data = None
        except Exception as e:
            write_error(f"Could not remove lock file: {e}")
            raise

    def _probe_lock_file(self, reported_mtime):
        """Called by get_dir"""
        delta = reported_mtime - self.lock_data["lock_time"]
        # delta2 = reported_mtime - self.lock_write_time
        self.server_time_ofs = delta
        if self.get_option("verbose", 3) >= 4:
            write(f"Server time offset: {delta:.2f} seconds.")
            # write("Server time offset2: {:.2f} seconds.".format(delta2))

    def get_id(self):
        return self.host + self.root_dir

    def cwd(self, dir_name):
        assert is_native(dir_name)
        path = normpath_url(join_url(self.cur_dir, dir_name))
        if not path.startswith(self.root_dir):
            # paranoic check to prevent that our sync tool goes berserk
            raise RuntimeError(
                f"Tried to navigate outside root {self.root_dir!r}: {path!r}"
            )
        self.sftp.cwd(dir_name)
        self.cur_dir = path
        self.cur_dir_meta = None
        return self.cur_dir

    def pwd(self):
        """Return current working dir as native `str` (uses fallback-encoding)."""
        pwd = self.sftp.pwd
        if pwd != "/":  # #38
            pwd = pwd.rstrip("/")
        return pwd

    def mkdir(self, dir_name):
        assert is_native(dir_name)
        self.check_write(dir_name)
        self.sftp.mkdir(dir_name)

    def _rmdir_impl(self, dir_name, keep_root_folder=False, predicate=None):
        # FTP does not support deletion of non-empty directories.
        assert is_native(dir_name)
        self.check_write(dir_name)
        names = []

        attr_list = self.sftp.listdir_attr(dir_name)

        # write(f"rmdir({dir_name}): {attr_list}")
        for dir_attr in attr_list:
            name = dir_attr.filename
            # name = self.re_encode_to_native(name)
            if "/" in name:
                name = os.path.basename(name)
            if name in (".", ".."):
                continue
            if predicate and not predicate(name):
                continue
            names.append(name)

        if len(names) > 0:
            self.sftp.cwd(dir_name)
            try:
                for name in names:
                    try:
                        # try to delete this as a file
                        self.sftp.remove(name)
                    except OSError:  # ftplib.all_errors as _e:
                        # write(f"    sftp.remove({name}) failed (not empty?), trying recursive...", debug=True)
                        # assume <name> is a folder
                        self.rmdir(name)
            finally:
                if dir_name != ".":
                    self.sftp.cwd("..")
        #        write("sftp.rmd(%s)..." % (dir_name, ))
        if not keep_root_folder:
            self.sftp.rmdir(dir_name)
        return

    def rmdir(self, dir_name):
        # self.check_write(dir_name)
        # return self.sftp.rmdir(dir_name)
        return self._rmdir_impl(dir_name)

    try:
        _paramiko_py3compat_u = paramiko.util.u
    except AttributeError:
        _paramiko_py3compat_u = paramiko.py3compat.u

    @staticmethod
    def _paramiko_py3compat_u_wrapper(s, encoding="utf8"):
        try:
            return SFTPTarget._paramiko_py3compat_u(s, encoding)
        except UnicodeDecodeError:
            write_error(f"Failed to decode {s} using {encoding}. Trying cp1252...")
            s = s.decode("cp1252")
        return s

    def get_dir(self):
        # Fallback to cp1252 if utf8 fails
        with patch("paramiko.message.u", SFTPTarget._paramiko_py3compat_u_wrapper):
            res = self._get_dir_impl()
        return res

    def _get_dir_impl(self):
        entry_list = []
        entry_map = {}
        has_meta = False

        attr_list = self.sftp.listdir_attr()

        for de in attr_list:
            is_dir = stat.S_ISDIR(de.st_mode)
            name = de.filename
            entry = None
            if name in (".", ".."):
                continue  # #74: some servers may send those
            elif is_dir:
                entry = DirectoryEntry(
                    self, self.cur_dir, name, de.st_size, de.st_mtime, unique=None
                )
            elif name == DirMetadata.META_FILE_NAME:
                # the meta-data file is silently ignored
                has_meta = True
            elif name == DirMetadata.LOCK_FILE_NAME and self.cur_dir == self.root_dir:
                # this is the root lock file. Compare reported mtime with
                # local upload time
                self._probe_lock_file(de.st_mtime)
            else:
                entry = FileEntry(
                    self, self.cur_dir, name, de.st_size, de.st_mtime, unique=None
                )

            if entry:
                entry_map[name] = entry
                entry_list.append(entry)

        # load stored meta data if present
        self.cur_dir_meta = DirMetadata(self)

        if has_meta:
            try:
                self.cur_dir_meta.read()
            except IncompatibleMetadataVersionError:
                raise  # this should end the script (user should pass --migrate)
            except Exception as e:
                write_error(f"Could not read meta info {self.cur_dir_meta}: {e}")

            meta_files = self.cur_dir_meta.list

            # Adjust file mtime from meta-data if present
            missing = []
            for n in meta_files:
                meta = meta_files[n]
                if n in entry_map:
                    # We have a meta-data entry for this resource
                    upload_time = meta.get("u", 0)

                    # Discard stored meta-data if
                    #   1. the reported files size is different than the
                    #      size we stored in the meta-data
                    #      or
                    #   2. the the mtime reported by the SFTP server is later
                    #      than the stored upload time (which indicates
                    #      that the file was modified directly on the server)
                    if entry_map[n].size != meta.get("s"):
                        if self.get_option("verbose", 3) >= 5:
                            write(
                                "Removing meta entry {} (size changed from {} to {}).".format(
                                    n, entry_map[n].size, meta.get("s")
                                )
                            )
                        missing.append(n)
                    elif (entry_map[n].mtime - upload_time) > self.mtime_compare_eps:
                        if self.get_option("verbose", 3) >= 5:
                            write(
                                "Removing meta entry {} (modified {} > {}).".format(
                                    n,
                                    time.ctime(entry_map[n].mtime),
                                    time.ctime(upload_time),
                                )
                            )
                        missing.append(n)
                    else:
                        # Use meta-data mtime instead of the one reported by SFTP server
                        entry_map[n].meta = meta
                        entry_map[n].mtime = meta["m"]
                else:
                    # File is stored in meta-data, but no longer exists on SFTP server
                    # write("META: Removing missing meta entry %s" % n)
                    missing.append(n)
            # Remove missing or invalid files from cur_dir_meta
            for n in missing:
                self.cur_dir_meta.remove(n)
        # print("entry_list", entry_list)
        return entry_list

    def open_readable(self, name):
        """Open cur_dir/name for reading.

        Note: we read everything into a buffer that supports .read().

        Args:
            name (str): file name, located in self.curdir
        Returns:
            file-like (must support read() method)
        """
        # print("SFTP open_readable({})".format(name))
        assert is_native(name)
        # TODO: use sftp.open() instead?
        out = SpooledTemporaryFile(max_size=self.MAX_SPOOL_MEM, mode="w+b")
        self.sftp.getfo(name, out)
        out.seek(0)
        return out

    def write_file(self, name, fp_src, mtime=None, blocksize=DEFAULT_BLOCKSIZE, callback=None):
        """Write file-like `fp_src` to cur_dir/name.

        Args:
            name (str): file name, located in self.curdir
            fp_src (file-like): must support read() method
            mtime (str, optional): the file's modification timestamp in 'YYYYMMDDHHMMSS' format.
            blocksize (int, optional):
            callback (function, optional):
                Called like `func(buf)` for every written chunk
        """
        # print("SFTP write_file({})".format(name), blocksize)
        assert is_native(name)
        self.check_write(name)
        self.sftp.putfo(fp_src, name)  # , callback)
        # TODO: check result
        # TODO: use `mtime`

    def copy_to_file(self, name, fp_dest, callback=None):
        """Write cur_dir/name to file-like `fp_dest`.

        Args:
            name (str): file name, located in self.curdir
            fp_dest (file-like): must support write() method
            callback (function, optional):
                Called like `func(buf)` for every written chunk
        """
        assert is_native(name)
        self.sftp.getfo(name, fp_dest)

    def remove_file(self, name):
        """Remove cur_dir/name."""
        assert is_native(name)
        self.check_write(name)
        # self.cur_dir_meta.remove(name)
        self.sftp.remove(name)
        self.remove_sync_info(name)

    def set_mtime(self, name, mtime, size):
        assert is_native(name)
        self.check_write(name)
        # write("META set_mtime(%s): %s" % (name, time.ctime(mtime)))
        # We cannot set the mtime on SFTP servers, so we store this as additional
        # meta data in the same directory
        # TODO: try "SITE UTIME", "MDTM (set version)", or "SRFT" command
        self.cur_dir_meta.set_mtime(name, mtime, size)
