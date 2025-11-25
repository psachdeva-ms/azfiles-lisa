# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
import os
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Dict, List, Optional, Type

from dataclasses_json import dataclass_json

from lisa import schema
from lisa.node import Node, quick_connect
from lisa.tools import Ls, Mkdir, RemoteCopy
from lisa.transformers.deployment_transformer import (
    DeploymentTransformer,
    DeploymentTransformerSchema,
)

FILE_UPLOADER = "file_uploader"
UPLOADED_FILES = "uploaded_files"


@dataclass_json
@dataclass
class FileUploaderTransformerSchema(DeploymentTransformerSchema):
    # source path of files to be uploaded
    source: str = ""
    # destination path of files to be uploaded
    destination: str = ""
    # uploaded files
    files: List[str] = field(default_factory=list)
    # optional source node information, use schema.RemoteNode
    source_node: Optional[schema.RemoteNode] = None
    # local path to store kernel file
    local_path: Optional[str] = ""
    # flag to avoid wasting trying scp
    try_scp: Optional[bool] = False


class FileUploaderTransformer(DeploymentTransformer):
    """
    This transformer upload files from local to remote. It should be used when
    environment is connected.
    """

    @classmethod
    def type_name(cls) -> str:
        return FILE_UPLOADER

    @classmethod
    def type_schema(cls) -> Type[schema.TypedSchema]:
        return FileUploaderTransformerSchema

    @property
    def _output_names(self) -> List[str]:
        return [UPLOADED_FILES]

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        super()._initialize(*args, **kwargs)
        runbook: FileUploaderTransformerSchema = self.runbook
        if not runbook.source:
            raise ValueError("'source' must be provided.")
        if not runbook.destination:
            raise ValueError("'destination' must be provided.")
        if not runbook.files:
            raise ValueError("'files' must be provided")

        # If source_node is specified, check existence on remote node
        if runbook.source_node:
            src_node = quick_connect(
                runbook.source_node, runbook.name
            )
            ls = src_node.tools[Ls]
            if not ls.path_exists(runbook.source):
                raise ValueError(f"source {runbook.source} doesn't exist on remote node.")
            if ls.is_file(PurePath(runbook.source), sudo=True):
                file_purepath = PurePath(runbook.source)
                runbook.files = [file_purepath.name]
                runbook.source = str(file_purepath.parent)
            else: # its a directory
                if runbook.files == ["*"]:
                    runbook.files = []
                    files = ls.list(runbook.source, sudo=True)
                    if (len(files) == 0):
                        raise ValueError("incorrect file or directory name specified")
                    for file in files:
                        runbook.files.append(PurePath(file).name)

        else:
            if not os.path.exists(runbook.source):
                raise ValueError(f"source {runbook.source} doesn't exist.")
            if os.path.isfile(runbook.source):
                runbook.files = [os.path.basename(runbook.source)]
                runbook.source = str(PurePath(runbook.source).parent)
            else:
                if runbook.files == ["*"]:
                    runbook.files = []
                    files = os.listdir(runbook.source)
                    if (len(files) == 0):
                        raise ValueError("incorrect file or directory name specified")
                    for file in files:
                        runbook.files.append(PurePath(file).name)


        self._log.debug(f"files: {runbook.files}\t dir: {runbook.source}\t")


    def _internal_run(self) -> Dict[str, Any]:
        runbook: FileUploaderTransformerSchema = self.runbook
        result: Dict[str, Any] = dict()
        dest_copy = self._node.tools[RemoteCopy]
        uploaded_files: List[str] = []

        src_purepath = PurePath(runbook.source)
        dest_node = self._node
        dest_purepath = PurePath(runbook.destination)

        # checking destination existence
        self._log.debug(f"checking destination {runbook.destination}")
        ls = self._node.tools[Ls]
        if not ls.path_exists(runbook.destination):
            self._log.debug(f"creating directory {runbook.destination}")
            mkdir = self._node.tools[Mkdir]
            mkdir.create_directory(runbook.destination)

        if runbook.source_node:
            src_node = quick_connect(
                runbook.source_node, runbook.name
            )

            src_copy = src_node.tools[RemoteCopy]
            local_purepath = None
            if runbook.local_path:
                if not os.path.exists(runbook.local_path):
                    os.mkdir(runbook.local_path)

                local_purepath = PurePath(runbook.local_path) / src_purepath.name
                os.mkdir(str(local_purepath))


            scp_result = 0
            if not runbook.try_scp:
                scp_result = -1

            for file_name in runbook.files:
                # for now skip transfer of dbg files
                if "dbg" in file_name:
                    continue

                # If source_node is specified, use it as the source for remote-to-remote copy
                if scp_result == 0:
                    self._log.info(f"remote-to-remote: '{file_name}' to \
                		'{str(dest_purepath)}'")
                    scp_result = dest_copy.copy_between_remotes(
                        src_node=src_node,
                        src_path=src_purepath / file_name,
                        dest_node=dest_node,
                        dest_path=dest_purepath / file_name,
                        recurse=False,
                    )
                elif scp_result != 0:
                    assert local_purepath, "local path doesn't exist"
                    self._log.info(f"downloading '{file_name}'  from '{str(src_purepath)}")

                    # src_node.shell.copy_back(
                    #     node_path=src_purepath / file_name,
                    #     local_path=local_purepath / file_name
                    # )
                    src_copy.copy_to_local(
                        src=src_purepath / file_name,
                        dest=local_purepath,
                        recurse=False,
                        sudo=False
                    )
                    self._log.info(f"uploading '{file_name}' to '{str(dest_purepath)}'")
                    dest_copy.copy_to_remote(
		        src=local_purepath / file_name,
                        dest=dest_purepath,
                        recurse=False,
                        sudo=False
                    )
                uploaded_files.append(file_name)
        else:
            for file_name in runbook.files:
                # for now skip transfer of dbg files
                if "dbg" in file_name:
                    continue

                # fallback to local-to-remote
                self._log.info(f"uploading local files '{runbook.source}' to '{dest_purepath}'")
                dest_copy.copy_to_remote(
		    src=src_purepath / file_name,
                    dest=dest_purepath,
                    recurse=False
                )
                uploaded_files.append(file_name)

        result[UPLOADED_FILES] = uploaded_files
        return result
