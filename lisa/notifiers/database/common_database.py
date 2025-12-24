import struct
from urllib.parse import quote_plus
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, cast

from assertpy.assertpy import assert_that
from dataclasses_json import dataclass_json
from retry import retry  # type: ignore
from sqlalchemy import MetaData, create_engine, exc
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.orm import sessionmaker

from lisa import schema
from lisa.sut_orchestrator.azure.credential import AzureCredentialSchema
from lisa.testsuite import TestResultMessage
from lisa.util import InitializableMixin, LisaException
from lisa.util.logger import get_logger

from .common import get_cross_tenant_credential, get_typed_credential
from .triage import Triage


@dataclass_json()
@dataclass
class DatabaseSchema(schema.Notifier):  # type: ignore
    # default is used by subtypes like in lsg_kusto
    type: str = "default"
    server: str = ""
    database: str = ""
    username: str = ""
    password: str = ""
    msi_client_id: str = ""
    enterprise_app_client_id: str = ""
    tenant_id: str = ""
    azure_sql_access_token: str = ""
    use_typed_credential: bool = False
    azure_credential: Optional[AzureCredentialSchema] = None


class DatabaseMixin(InitializableMixin):  # type: ignore
    """
    This mixin contains common database util functions.
    """

    def __init__(self, runbook: DatabaseSchema, tables: List[str]) -> None:
        InitializableMixin.__init__(self)
        self.database_schema = runbook
        self.tables = tables
        # As defined in msodbcsql.h
        self.SQL_COPT_SS_ACCESS_TOKEN = 1256

    def get_token(self) -> bytes:
        try:
            credential: Any
            if self.database_schema.use_typed_credential:
                credential = get_typed_credential(
                    self._log,
                    self.database_schema.azure_credential,
                )
            elif self.database_schema.azure_sql_access_token:
                self._log.info("Authenticating DB using Azure SQL access token...")
                token = self.database_schema.azure_sql_access_token.encode("utf-16-le")
                token_struct = struct.pack(f"=I{len(token)}s", len(token), token)
                return bytes(token_struct)
            elif (
                self.database_schema.msi_client_id
                and self.database_schema.tenant_id
                and self.database_schema.enterprise_app_client_id
            ):
                self._log.info("Authenticating DB using ClientAssertionCredential...")
                credential = get_cross_tenant_credential(
                    msi_client_id=self.database_schema.msi_client_id,
                    enterprise_app_client_id=(
                        self.database_schema.enterprise_app_client_id
                    ),
                    tenant_id=self.database_schema.tenant_id,
                )
            else:
                self._log.info("Authenticating DB using DefaultAzureCredential...")
                credential = get_typed_credential(
                    self._log,
                    AzureCredentialSchema(allow_all_tenants=True),
                )

            cre_token = credential.get_token(
                "https://database.windows.net/.default"
            ).token
            token = cre_token.encode("utf-16-le")
            token_struct = struct.pack(f"=I{len(token)}s", len(token), token)
            return bytes(token_struct)
        except Exception as e:
            raise RuntimeError("Failed to obtain Azure AD token") from e

    def create_engine(self, driver: str) -> Any:
        if self.database_schema.username and self.database_schema.password:
            return self.create_engine_with_pass(driver)
        else:
            return self.create_engine_with_token(driver)

    def create_engine_with_pass(self, driver: str) -> Any:
        try:
            self._log.info("Authenticating DB using username and password...")
            server = self.database_schema.server
            port = ""
            if "," in server:
                server, port = server.split(",")
                port = port.strip()

            user = quote_plus(self.database_schema.username)
            password = quote_plus(self.database_schema.password)
            driver_encoded = quote_plus(driver)

            connection_string = f"mssql+pyodbc://{user}:{password}@{server}"
            if port:
                connection_string += f":{port}"
            connection_string += f"/{self.database_schema.database}?driver={driver_encoded}"

            return create_engine(
                connection_string,
                pool_recycle=300,
            )
        except Exception as e:
            raise RuntimeError(
                "Failed to create engine with username and password"
            ) from e

    def create_engine_with_token(self, driver: str) -> Any:
        try:
            odbc_connect = (
                f"DRIVER={driver};DATABASE={self.database_schema.database};"
                f"SERVER={self.database_schema.server}"
            )
            odbc_connect_encoded = quote_plus(odbc_connect)
            connection_string = f"mssql+pyodbc:///?odbc_connect={odbc_connect_encoded}"


            connect_args = {
                "attrs_before": {self.SQL_COPT_SS_ACCESS_TOKEN: self.get_token()}
            }
            return create_engine(
                connection_string,
                connect_args=connect_args,
                pool_recycle=300,
            )
        except Exception as e:
            raise RuntimeError("Failed to create engine with Azure AD token") from e

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        # many windows server instances have sql17 installed already
        # it's better to use latest since it's patched, if no other option
        # is available, fall back to odbc17
        drivers = ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"]
        errors = []
        self._log = get_logger("db")

        for driver in drivers:
            try:
                engine = self.create_engine(driver)
                # verify connection
                with engine.connect():
                    pass
                self.engine = engine
                break
            except exc.InterfaceError as err:
                self._log.warning(f"Could not initialize {driver}")
                errors.append(err)

        # raise the last exception if we're out of retries
        # if len(errors) == len(drivers):
        if not hasattr(self, "engine"):
            self._log.error("Could not initialize the ODBC driver for db notifier.")
            if errors:
                raise errors[-1]
            # raise errors[-1]
            raise LisaException("No valid ODBC driver found.")

        # create session to operate data
        self._session_maker = sessionmaker(bind=self.engine, expire_on_commit=False)

        # reflect tables metadata from database

        metadata = MetaData()
        try:
            metadata.reflect(
                self.engine,
                only=self.tables,
            )
        except Exception as e:
            raise LisaException(
                f"Failed to connect to database {self.database_schema.database}, "
                f"please check if it exists and the username has access to it. {e}"
            )

        # load schema from database, so that it doesn't to be redefined in code
        self.base = automap_base(metadata=metadata)
        self.base.prepare()

    def create_session(self) -> Any:
        return self._session_maker()

    def commit_and_close_session(self, session: Any) -> None:
        try:
            session.commit()
        finally:
            session.close()


class TestProject(DatabaseMixin):
    """
    This mixin contains common database functionalities for TestProject
    and TestPass.
    """

    def __init__(self, runbook: DatabaseSchema) -> None:
        super().__init__(runbook, ["TestPass", "TestProject"])

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        super()._initialize()

        self.TestPass = self.base.classes.TestPass
        self.TestProject = self.base.classes.TestProject

    def get_test_project(self, test_project_name: str) -> Any:
        session = self.create_session()
        test_project = (
            session.query(self.TestProject)
            .filter_by(Name=test_project_name)
            .one_or_none()
        )
        assert_that(test_project).is_not_none()
        self.commit_and_close_session(session)
        return test_project

    # Retry to avoid concurrent inserting test pass error.
    @retry(tries=10, delay=0.5)  # type: ignore
    def add_or_get_test_pass(
        self,
        test_pass_name: str,
        date: datetime,
        test_project: Any,
    ) -> Any:
        session = self.create_session()
        test_pass = (
            session.query(self.TestPass)
            .filter_by(Name=test_pass_name, ProjectId=test_project.Id)
            .one_or_none()
        )
        if test_pass is None:
            test_pass = self.TestPass(
                Name=test_pass_name,
                ProjectId=test_project.Id,
                StartedDate=date,
                CreatedDate=date,
                UpdatedDate=date,
            )
            session.add(test_pass)
            self.commit_and_close_session(session)
        return test_pass


class TestCases(DatabaseMixin):
    """
    This mixin contains common database functionalities for TestCase. It
    maintains a Dict of test cases.
    """

    def __init__(self, runbook: DatabaseSchema) -> None:
        super().__init__(runbook, ["TestCase"])
        self._test_cases_cache: Dict[str, Any] = dict()

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        super()._initialize()

        self.TestCase = self.base.classes.TestCase

    def get_id_by_name(self, full_name: str) -> int:
        return int(self.get_by_name(full_name).Id)

    def get_by_name(self, full_name: str) -> Any:
        return self._test_cases_cache[full_name]

    # in case there is concurrency issue on insert, just try to load again.
    @retry(Exception, tries=2)  # type: ignore
    def add_or_update_test_case(self, message: TestResultMessage) -> Any:
        date = datetime.utcnow()
        session = self.create_session()
        test_case_name = message.full_name
        test_case = (
            session.query(self.TestCase).filter_by(Name=test_case_name).one_or_none()
        )
        information = message.information
        if test_case is None:
            test_case = self.TestCase(
                Name=test_case_name,
                CreatedDate=date,
                CreatedBy="",
                Deleted=0,
                # the owner will be maintained internally, so just update it
                # first time.
                Owner=information.pop("owner", ""),
            )
            session.add(test_case)
        else:
            # remove owner field from results.
            information.pop("owner", "")
        test_case.Area = information.pop("area", "")
        test_case.Category = information.pop("category", "")
        test_case.Tag = ",".join(information.pop("tags", ""))
        test_case.Priority = information.pop("priority", 2)
        test_case.Description = information.pop("description", "")
        test_case.UpdatedDate = date

        self.commit_and_close_session(session)

        self._test_cases_cache[test_case_name] = test_case

        return test_case


class TriageDbTable(DatabaseMixin):
    _tables = ["TestFailure"]

    def __init__(self, runbook: DatabaseSchema) -> None:
        super().__init__(runbook, self._tables)
        self._log = get_logger("triage")

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        super()._initialize()

        self._log.debug("initializing failure table...")
        session = self.create_session()
        testFailureTable = self.base.classes.TestFailure

        try:
            # initialize failure table
            db_failures = (
                session.query(testFailureTable)
                .filter_by(Status="Active")
                .order_by(testFailureTable.Priority)
                .all()
            )
        finally:
            self.commit_and_close_session(session)

        raw_failures: List[Dict[str, Any]] = []
        self._db_failures: Dict[int, Any] = {}

        for db_failure in db_failures:
            raw_failure = {
                "id": db_failure.Id,
                "pattern": db_failure.Pattern,
                "action": db_failure.Action,
                "case_id": db_failure.CaseId,
                "priority": db_failure.Priority,
                "category": db_failure.Category,
                "reason": db_failure.Reason,
                "description": db_failure.Description,
                "bug_url": db_failure.BugURL,
            }
            raw_failures.append(raw_failure)
            self._db_failures[db_failure.Id] = db_failure
        self.raw_failures = raw_failures


_test_project: Optional[TestProject] = None
_test_cases: Optional[TestCases] = None
_triage_table: Optional[TriageDbTable] = None


def _get_single_object(
    object: Any, type: type, runbook: DatabaseSchema, **kwargs: Any
) -> Any:
    if not object:
        temp_object = type(runbook, **kwargs)
        temp_object.initialize()
        object = temp_object
    return object


def get_test_project(runbook: DatabaseSchema) -> TestProject:
    return cast(TestProject, _get_single_object(_test_project, TestProject, runbook))


def get_test_cases(runbook: DatabaseSchema) -> TestCases:
    return cast(TestCases, _get_single_object(_test_cases, TestCases, runbook))


def get_test_failure(runbook: DatabaseSchema) -> TriageDbTable:
    return cast(
        TriageDbTable, _get_single_object(_triage_table, TriageDbTable, runbook)
    )


def get_triage_from_db(
    runbook: DatabaseSchema, test_project_name: str, test_pass_name: str
) -> Triage:
    global _triage_table
    if not _triage_table:
        _triage_table = get_test_failure(runbook)

    return Triage(
        test_project_name=test_project_name,
        test_pass_name=test_pass_name,
        failures=_triage_table.raw_failures,
    )
