from lisa import schema
from lisa.transformer import Transformer
from lisa.transformers.kernel_source_installer import SourceInstaller, SourceInstallerSchema
import json
from datetime import datetime
from typing import Dict, Any, Optional, List, Type, cast
from dataclasses import dataclass, field
from dataclasses_json import dataclass_json
import os
from pathlib import PurePosixPath
from lisa.transformers.kernel_source_installer import BaseLocationSchema

from lisa.transformers.deployment_transformer import (
    DeploymentTransformer,
    DeploymentTransformerSchema,
)
@dataclass_json()
@dataclass
class KernelSourcePackagerSchema(DeploymentTransformerSchema):
    use_cache: bool = field(default=False)
    location: Optional[BaseLocationSchema] = field(
        default=None, metadata={"required": True}
    )


class KernelSourcePackager(DeploymentTransformer):
    @classmethod
    def type_name(cls) -> str:
        return "kernel_source_packager"

    @classmethod
    def type_schema(cls) -> Type[schema.TypedSchema]:
        return KernelSourcePackagerSchema

    @property
    def _output_names(self) -> List[str]:
        return ["package_path"]


    def _get_location_factory(self):
        from lisa.util import subclasses
        from lisa.transformers.kernel_source_installer import BaseLocation
        return subclasses.Factory[BaseLocation](BaseLocation)

    def _internal_run(self) -> Dict[str, Any]:
        runbook: KernelSourcePackagerSchema = self.runbook
        self._log.info(f"use_cache value: {runbook.use_cache} (type: {type(runbook.use_cache)})")

        # Use SourceInstaller logic for build steps
        source_installer_runbook = SourceInstallerSchema(
            location=runbook.location,
        )
        source_installer = SourceInstaller(
            runbook=source_installer_runbook,
            node=self._node,
            parent_log=self._log,
        )

            # 1. Clone and checkout the source to get the actual commit_id and kernel_version
        factory = self._get_location_factory()

        source = factory.create_by_runbook(
            runbook=runbook.location, node=self._node, parent_log=self._log
        )
        self._code_path = source.get_source_code()
        git = self._node.tools["Git"]
        commit_id = git.get_latest_commit_id(cwd=self._code_path)

        # 2. Get kernel version
        result = self._node.execute("make kernelversion 2>/dev/null", cwd=self._code_path, shell=True)
        result.assert_exit_code(0, f"failed on get kernel version: {result.stdout}")
        kernel_version = result.stdout.strip()

        if runbook.use_cache:
            self._log.info("Checking for cached kernel packages...")
            if self._check_cache(commit_id, kernel_version):
                self._log.info("Cache hit: using cached package.")
                package_path = self._update_cache(commit_id=commit_id)
                return {"package_path" : package_path}

            else:
                self._log.info("Cache miss: building and packaging kernel.")
                package_path = self._build_and_package(source_installer, commit_id, kernel_version)
                return {"package_path" : package_path}
        else:
            self._log.info("No-cache mode: building and packaging kernel.")
            package_path = self._build_and_package(source_installer, commit_id, kernel_version)
            return {"package_path" : package_path}

    def _check_cache(
        self,
        commit_id: str,
        kernel_version: str,
        cache_json_path: str = "/default/cache/kernel_cache.json"
    ) -> bool:
        """
        Checks the cache JSON for an entry matching the given commit_id and kernel_version.
        If found, verifies that the package_path exists and contains a .deb file.
        Returns True if valid .deb package is present, else False.
        """
        node = self._node
        try:
            cache_content = node.execute(f"cat {cache_json_path}", shell=True)
            cache = json.loads(cache_content.stdout)
        except Exception as e:
            self._log.error(f"Failed to load cache: {e}")
            cache = []

        for entry in cache:
            if (
                entry.get("commit_id") == commit_id
                and entry.get("kernel_version") == kernel_version
                and entry.get("package_type") == "deb"
            ):
                package_paths = entry.get("package_paths")
                if not package_paths or not isinstance(package_paths, list):
                    return False
                # Check that at least one .deb file exists
                for package_path in package_paths:
                    if (
                        package_path.endswith(".deb")
                        and node.execute(f"test -f {package_path}", shell=True).exit_code == 0
                    ):
                        return True
                return False
        return False

    def _update_cache(
        self,
        cache_json_path: str = "/default/cache/kernel_cache.json",
        metadata: Optional[Dict[str, Any]] = None,
        commit_id: Optional[str] = None,
        max_cache_size: int = 100,
    ) -> Optional[List[str]]:
        """
        Updates the kernel cache JSON file.
        1. If metadata is provided, creates a new entry at the top (removes last if full).
        2. If only commit_id is provided, moves the entry to the top and updates last_used_time.
        Returns the package_paths of the updated or created entry, or None if not found.
        """
        node = self._node
        now = datetime.utcnow().isoformat() + "Z"
        # Load cache
        try:
            if node.shell.exists(cache_json_path):
                cache_content = node.execute(f"cat {cache_json_path}", shell=True)
                cache: List[Dict[str, Any]] = json.loads(cache_content.stdout)
            else:
                cache = []
        except Exception as e:
            self._log.error(f"Failed to load cache: {e}")
            cache = []

        updated = False
        package_paths = None

        if metadata:
            # Remove any existing entry with the same commit_id
            cache = [entry for entry in cache if entry.get("commit_id") != metadata.get("commit_id")]
            # Set last_used_time
            metadata["last_used_time"] = now
            # Insert new entry at the top
            cache.insert(0, metadata)
            package_paths = metadata.get("package_paths")
            # Trim cache if over max size
            if len(cache) > max_cache_size:
                removed = cache.pop()
                self._log.info(f"Cache full. Removed oldest entry: {removed.get('commit_id', 'unknown')}")
            self._log.info("Created new entry in cache.")
            updated = True

        elif commit_id:
            for idx, entry in enumerate(cache):
                if entry.get("commit_id") == commit_id:
                    entry["last_used_time"] = now
                    # Move entry to top
                    cache.pop(idx)
                    cache.insert(0, entry)
                    package_paths = entry.get("package_paths")
                    self._log.info("Updated last used time for cache entry.")
                    updated = True
                    break
            if not updated:
                self._log.warning(f"No cache entry found for commit_id: {commit_id}")

        # Save cache if updated
        if updated:
            try:
                cache_str = json.dumps(cache, indent=2)
                node.execute(f"echo '{cache_str}' | sudo tee {cache_json_path}", shell=True)
            except Exception as e:
                self._log.error(f"Failed to write cache: {e}")

        # Find the main kernel image .deb (not headers or dbg)
        if not package_paths:
            raise Exception("No package_paths found in cache for the given commit_id.")
        image_deb = next((p for p in package_paths if "linux-image" in p and "dbg" not in p), None)
        if not image_deb:
            raise Exception("No main linux-image .deb found in built packages.")
        return image_deb



    def _build_and_package(self, source_installer, commit_id: str, kernel_version: str) -> str:
        """
        Builds the kernel from source (using already cloned and checked-out code),
        creates a deb package, collects metadata, moves the package to a commit-id-named folder,
        updates the cache, and returns the first package path.
        """
        node = self._node
        runbook: KernelSourcePackagerSchema = self.runbook
        parent_dir = str(self._code_path.parent)
        # Clean all files in parent directory before build
        node.execute(f"rm -f {parent_dir}/*", shell=True)
        # ...rest of your build and packaging logic...

        # 1. Install required build tools (reuse SourceInstaller)
        source_installer._install_build_tools(node)

        # 2. Use the already set self._code_path (repo is already cloned and checked out)
        assert node.shell.exists(self._code_path), f"cannot find code path: {self._code_path}"
        self._log.info(f"kernel code path: {self._code_path}")

        # 3. Apply code modifications/patches if any (reuse SourceInstaller)
        source_installer._modify_code(node=node, code_path=self._code_path)

        # 3.5. Branch verification: ensure correct branch is checked out
        expected_branch = getattr(runbook, "ref", None)
        if expected_branch:
            git = node.tools["Git"]
            current_branch = git.get_current_branch(cwd=self._code_path)
            if current_branch != expected_branch:
                raise Exception(
                    f"Kernel source is on branch '{current_branch}', expected '{expected_branch}'."
                )
            self._log.info(f"Verified kernel source is on branch '{current_branch}'.")

        # 4. Build the kernel (reuse SourceInstaller, but do NOT install)
        kconfig_file = getattr(runbook, "kernel_config_file", None)
        source_installer._build_code(
            node=node,
            code_path=self._code_path,
            kconfig_file=kconfig_file,
            kernel_version=kernel_version,
            skip_plain_make=True,  # Skip plain make, we will use make deb-pkg
        )

        # 5. Package the kernel as a DEB package
        make = node.tools["Make"]
        make.make(arguments="bindeb-pkg", cwd=self._code_path, timeout=60*60*2)

        # 6. Find the generated .deb package(s)

        deb_dir = str(self._code_path.parent)
        result = node.execute(f'ls {deb_dir}/*.deb', shell=True)
        if result.exit_code != 0:
            raise Exception(f"Failed to list .deb files in {deb_dir}: {result.stderr}")
        deb_files = [os.path.basename(line.strip()) for line in result.stdout.splitlines() if line.strip().endswith(".deb")]
        if not deb_files:
            raise Exception("No .deb package was generated in the kernel build process.")


        # 7. Move the .deb file(s) to the cache/packages/<commit_id> directory
        cache_root = "/default/cache"
        packages_dir = f"{cache_root}/packages"
        commit_dir = f"{packages_dir}/commit_id-{commit_id}"
        if not node.shell.exists(commit_dir):
            node.execute(f"sudo mkdir -p {commit_dir}", shell=True)
            node.execute(f"sudo chmod 777 {commit_dir}", shell=True)

        package_paths = []
        for deb_file in deb_files:
            src_path = f"{deb_dir}/{deb_file}"
            dest_path = f"{commit_dir}/{deb_file}"
            node.execute(f"sudo mv {src_path} {dest_path}", shell=True)
            package_paths.append(dest_path)

        # 8. Collect metadata for cache
        metadata = {
            "commit_id": commit_id,
            "kernel_version": kernel_version,
            "package_type": "deb",
            "package_paths": package_paths,  # Store all .deb paths as a list
            "build_time": datetime.utcnow().isoformat() + "Z",
            "builder_vm": node.name if hasattr(node, "name") else "unknown",
            "os_distribution": str(node.os),
            # "last_used_time" will be set by _update_cache
        }

        # 9. Update the cache and return the first package path
        self._update_cache(metadata=metadata)
        # Find the main kernel image .deb (not headers or dbg)
        image_deb = next((p for p in package_paths if "linux-image" in p and "dbg" not in p), None)
        if not image_deb:
            raise Exception("No main linux-image .deb found in built packages.")
        return image_deb

