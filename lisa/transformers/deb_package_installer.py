# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
from pathlib import PurePosixPath
from typing import Any, Dict, List, Type

from lisa import schema
from lisa.operating_system import Debian
from lisa.tools import Dpkg, Uname
from lisa.transformers.package_installer import PackageInstaller, PackageInstallerSchema
from lisa.util import UnsupportedDistroException


class DEBPackageInstallerTransformer(PackageInstaller):
    @classmethod
    def type_name(cls) -> str:
        return "deb_package_installer"

    @classmethod
    def type_schema(cls) -> Type[schema.TypedSchema]:
        return PackageInstallerSchema

    @property
    def _output_names(self) -> List[str]:
        return []

    def _validate(self) -> None:
        if not isinstance(self._node.os, Debian):
            raise UnsupportedDistroException(
                self._node.os,
                f"'{self.type_name()}' transformer only supports Debian-based Distros.",
            )
        runbook: PackageInstallerSchema = self.runbook
        if runbook.files == ["*"]:
            # Validate all .deb files in the directory
            directory = PurePosixPath(runbook.directory)
            self._log.debug(f"Validating all .deb files in {directory}")
            self._node.tools[Dpkg].validate_all_debs_in_directory(str(directory))
        else:
            super()._validate()

    def _validate_package(self, file: str) -> None:
        assert self._node.tools[Dpkg].is_valid_package(
            file
        ), f"Provided file {file} is not a deb"

    def _install_package(self, file: str) -> None:
        self._node.tools[Dpkg].install_local_package(file, force=True)

    def _internal_run(self) -> Dict[str, Any]:
        runbook: PackageInstallerSchema = self.runbook
        directory = PurePosixPath(runbook.directory)
        uname = self._node.tools[Uname]

        # Log kernel version before installation
        kernel_before = uname.get_linux_information().kernel_version_raw
        self._log.info(f"Kernel version before installation: {kernel_before}")

        # List and log all files in the directory
        files_in_dir = self._node.execute(f"ls -l {directory}", shell=True).stdout
        self._log.info(f"Contents of {directory} before installation:\n{files_in_dir}")

        if runbook.files == ["*"]:
            # Find .deb files in the directory
            deb_files = [
                line.split()[-1]
                for line in files_in_dir.splitlines()
                if line.strip().endswith(".deb")
            ]
            if not deb_files:
                self._log.warning(f"No .deb files found in {directory}. Skipping installation.")
                return {}
            # Install all .deb files in the directory
            self._log.info(f"Installing all .deb packages in {directory}")
            self._node.tools[Dpkg].install_packages_in_directory(str(directory))
        else:
            self._log.info(f"Installing packages: {runbook.files}")
            success = []
            failed = []
            for file in runbook.files:
                full_path = self._node.get_str_path(directory.joinpath(file))
                # Check if file exists on remote node
                result = self._node.execute(f"test -f {full_path}", shell=True, no_error_log=True)
                if result.exit_code != 0:
                    self._log.error(f"File not found: {full_path}. Skipping.")
                    failed.append(file)
                    continue
                try:
                    self._install_package(full_path)
                    success.append(file)
                except Exception as e:
                    self._log.error(f"Failed to install {full_path}: {e}")
                    failed.append(file)
            self._log.info(f"Successfully installed: {success}")
            if failed:
                self._log.warning(f"Failed to install: {failed}")

        if runbook.reboot:
            self._log.info("Rebooting node after package installation.")
            try:
                self._node.reboot(time_out=900)
            except Exception as e:
                self._log.error(f"Reboot failed: {e}")

        # Log kernel version after installation/reboot
        kernel_after = uname.get_linux_information().kernel_version_raw
        self._log.info(f"Kernel version after installation: {kernel_after}")

        return {}