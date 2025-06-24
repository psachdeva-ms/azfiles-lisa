# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
import os
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Dict, List, Optional, Type

from dataclasses_json import dataclass_json

from lisa import schema
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
            raise ValueError("'files' must be provided.")

        if not os.path.exists(runbook.source):
            raise ValueError(f"source {runbook.source} doesn't exist.")

    def _internal_run(self) -> Dict[str, Any]:
        runbook: FileUploaderTransformerSchema = self.runbook
        result: Dict[str, Any] = dict()
        copy = self._node.tools[RemoteCopy]
        uploaded_files: List[str] = []

        # If source_node is specified, use it as the source for remote-to-remote copy
        if runbook.source_node:
            from lisa.node import Node
            src_conn = runbook.source_node.get_connection_info()
            src_node = Node.create(connection_info=src_conn)
            dest_node = self._node
            for name in runbook.files:
                src_path = PurePath(getattr(runbook.source_node, "path", "/")) / name
                dest_path = PurePath(runbook.destination)
                self._log.debug(f"remote-to-remote: '{src_path}' to '{dest_path}'")
                copy.copy_between_remotes(
                    src_node=src_node,
                    src_path=src_path,
                    dest_node=dest_node,
                    dest_path=dest_path,
                    recurse=False,
                )
                uploaded_files.append(name)
        else:
            # fallback to local-to-remote
            self._log.debug(f"checking destination {runbook.destination}")
            ls = self._node.tools[Ls]
            if not ls.path_exists(runbook.destination):
                self._log.debug(f"creating directory {runbook.destination}")
                mkdir = self._node.tools[Mkdir]
                mkdir.create_directory(runbook.destination)
            for name in runbook.files:
                local_path = PurePath(runbook.source) / name
                remote_path = PurePath(runbook.destination)
                self._log.debug(f"uploading file from '{local_path}' to '{remote_path}'")
                copy.copy_to_remote(local_path, remote_path)
                uploaded_files.append(name)

        result[UPLOADED_FILES] = uploaded_files
        return result
