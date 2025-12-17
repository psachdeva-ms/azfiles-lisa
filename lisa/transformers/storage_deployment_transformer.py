from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Type, List
from dataclasses_json import dataclass_json
from lisa.transformers.deployment_transformer import DeploymentTransformer
from lisa import schema
from lisa.sut_orchestrator.azure.common import check_or_create_storage_account, get_or_create_file_share
from lisa.util.logger import get_logger
from lisa.sut_orchestrator.azure.transformers import _load_platform

@dataclass_json
@dataclass
class FileShareServiceSchema(schema.TypedSchema):
    file_share_name: str = ""
    file_share_protocols: str = "SMB"
    file_share_quota_in_gb: int = 100


@dataclass_json
@dataclass
class StorageDeploymentTransformerSchema(schema.Transformer):
    storage_account_name: str = ""
    resource_group_name: Optional[str] = None
    location: Optional[str] = None
    sku: str = "Standard_LRS"
    kind: str = "StorageV2"
    enable_https_traffic_only: bool = True
    allow_shared_key_access: bool = False
    allow_blob_public_access: bool = False
    files: Optional[List[FileShareServiceSchema]] = None

class StorageDeploymentTransformer(DeploymentTransformer):
    @classmethod
    def type_name(cls) -> str:
        return "azure_storage_deploy"

    @classmethod
    def type_schema(cls) -> Type[schema.TypedSchema]:
        return StorageDeploymentTransformerSchema

    @property
    def _output_names(self) -> List[str]:
        return ["file_share_urls"]

    def _internal_run(self) -> Dict[str, Any]:
        runbook: StorageDeploymentTransformerSchema = self.runbook
        log = get_logger("azure_storage_deploy")
        platform = _load_platform(self._runbook_builder, self.type_name())

        # 1. Create/check storage account
        check_or_create_storage_account(
            credential=platform.credential,
            subscription_id=platform.subscription_id,
            cloud=platform.cloud,
            account_name=runbook.storage_account_name,
            resource_group_name=runbook.resource_group_name,
            location=runbook.location,
            log=log,
            sku=runbook.sku,
            kind=runbook.kind,
            enable_https_traffic_only=runbook.enable_https_traffic_only,
            allow_shared_key_access=runbook.allow_shared_key_access,
            allow_blob_public_access=runbook.allow_blob_public_access,
        )
        file_share_urls: List[str] = []
        # 2. Service-specific logic based on which schema is present
        if runbook.files:
            for file_share in runbook.files:
                share_url = get_or_create_file_share(
                    credential=platform.credential,
                    subscription_id=platform.subscription_id,
                    cloud=platform.cloud,
                    account_name=runbook.storage_account_name,
                    file_share_name=file_share.file_share_name,
                    resource_group_name=runbook.resource_group_name,
                    log=log,
                    protocols=file_share.file_share_protocols,
                    quota_in_gb=file_share.file_share_quota_in_gb,
                )

                file_share_urls.append(share_url)
        return {"file_share_urls": file_share_urls}

class FilesTransformer(DeploymentTransformer):
    @classmethod
    def type_name(cls) -> str:
        return "file_share"

    @classmethod
    def type_schema(cls) -> Type[schema.TypedSchema]:
        return FileShareServiceSchema

    def _internal_run(self) -> Dict[str, Any]:
        runbook: FileShareServiceSchema = self.runbook
        log = get_logger("azure_storage_files")
        # Expect storage account and resource group to be set in parent context or via connection
        storage_account_name = getattr(self, "storage_account_name", None)
        resource_group_name = getattr(self, "resource_group_name", None)
        assert storage_account_name, "storage_account_name must be provided in context or runbook"
        assert resource_group_name, "resource_group_name must be provided in context or runbook"
        share_url = get_or_create_file_share(
            credential=self._node.platform.credential,
            subscription_id=self._node.platform.subscription_id,
            cloud=self._node.platform.cloud,
            account_name=storage_account_name,
            file_share_name=runbook.file_share_name,
            resource_group_name=resource_group_name,
            log=log,
            protocols=runbook.file_share_protocols,
            quota_in_gb=runbook.file_share_quota_in_gb,
        )
        return {"file_share_url": share_url}
