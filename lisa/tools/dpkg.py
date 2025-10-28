from lisa.executable import Tool
from pathlib import PurePath
from typing import List

class Dpkg(Tool):
    @property
    def command(self) -> str:
        return "dpkg"

    def is_valid_package(self, package_path: str) -> bool:
        # Check if the file is a valid deb package
        result = self.run(
            f"--info {package_path}",
            sudo=True,
            shell=True,
            no_error_log=True,
            no_info_log=True,
        )
        return result.exit_code == 0

    def install_local_package(self, package_path: str, force: bool = True) -> None:
        # Install a single deb package
        options = "-i"
        if force:
            options += " --force-all"
        self.run(
            f"{options} {package_path}",
            sudo=True,
            shell=True,
        )

    def install_packages_in_directory(self, directory_path: str, force: bool = True) -> None:
        # Install all .deb packages in the given directory
        options = "-i"
        if force:
            options += " --force-all"
        self.run(
            f"{options} {directory_path}/*.deb",
            sudo=True,
            shell=True,
        )
        # Optionally fix dependencies
        self.node.execute(
            "apt-get -f install -y",
            sudo=True,
            shell=True,
        )

    def validate_all_debs_in_directory(self, directory_path: str) -> List[str]:
        # Returns a list of invalid .deb files (empty if all are valid)
        result = self.node.execute(
            f"ls {directory_path}/*.deb",
            shell=True,
            sudo=False,
        )
        deb_files = result.stdout.strip().splitlines()
        invalid_files = []
        for deb in deb_files:
            if not self.is_valid_package(deb):
                invalid_files.append(deb)
        return invalid_files