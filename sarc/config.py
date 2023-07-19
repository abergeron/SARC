import json
import os
import zoneinfo
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from typing import Any, Union

import tzlocal
from bson import ObjectId
from pydantic import BaseModel as _BaseModel
from pydantic import Extra, validator

MTL = zoneinfo.ZoneInfo("America/Montreal")
PST = zoneinfo.ZoneInfo("America/Vancouver")
UTC = zoneinfo.ZoneInfo("UTC")
TZLOCAL = zoneinfo.ZoneInfo(tzlocal.get_localzone_name())


class ConfigurationError(Exception):
    pass


def validate_date(value: Union[str, date, datetime]) -> date:
    if isinstance(value, str):
        if "T" in value:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").date()

        return datetime.strptime(value, "%Y-%m-%d").date()

    if isinstance(value, datetime):
        return value.date()

    return value


class BaseModel(_BaseModel):
    class Config:
        # Forbid extra fields that are not explicitly defined
        extra = Extra.forbid
        # Ignore cached_property, this avoids errors with serialization
        keep_untouched = (cached_property,)
        # Serializer for mongo's object ids
        json_encoders = {ObjectId: str}
        # Allow types like ZoneInfo
        arbitrary_types_allowed = True

    def dict(self, *args, **kwargs) -> dict[str, Any]:
        d = super().dict(*args, **kwargs)
        for k, v in list(d.items()):
            if isinstance(getattr(type(self), k, None), cached_property):
                del d[k]
                continue

        for k, v in d.items():
            if isinstance(v, date) and not isinstance(v, datetime):
                d[k] = datetime(
                    year=v.year,
                    month=v.month,
                    day=v.day,
                )
        return d

    def replace(self, **replacements):
        new_arguments = {**self.dict(), **replacements}
        return type(self)(**new_arguments)


class ClusterConfig(BaseModel):
    host: str
    timezone: Union[str, zoneinfo.ZoneInfo]  # | does not work with Pydantic's eval
    prometheus_url: str = None
    prometheus_headers_file: str = None
    name: str = None
    sacct_bin: str = "sacct"
    accounts: list[str] = None
    sshconfig: Path = None
    duc_inodes_command: str = None
    duc_storage_command: str = None
    diskusage_report_command: str = None

    @validator("timezone")
    def _timezone(cls, value):
        if isinstance(value, str):
            return zoneinfo.ZoneInfo(value)
        else:
            return value

    @cached_property
    def ssh(self):
        from fabric import Config as FabricConfig
        from fabric import Connection
        from paramiko import SSHConfig

        if self.sshconfig is None:
            fconfig = FabricConfig()
        else:
            fconfig = FabricConfig(ssh_config=SSHConfig.from_path(self.sshconfig))
        fconfig["run"]["pty"] = True
        fconfig["run"]["in_stream"] = False
        return Connection(self.host, config=fconfig)

    @cached_property
    def prometheus(self):
        from prometheus_api_client import PrometheusConnect

        if self.prometheus_headers_file is not None:
            headers = json.load(
                open(  # pylint: disable=consider-using-with
                    self.prometheus_headers_file, "r", encoding="utf-8"
                )
            )
        else:
            headers = {}

        if self.prometheus_url is None:
            raise ConfigurationError(
                f"No prometheus URL provided for cluster '{self.name}'"
            )
        return PrometheusConnect(url=self.prometheus_url, headers=headers)


class MongoConfig(BaseModel):
    connection_string: str
    database_name: str

    @cached_property
    def database_instance(self):
        from pymongo import MongoClient

        client = MongoClient(self.connection_string)
        return client.get_database(self.database_name)


class LDAPConfig(BaseModel):
    local_private_key_file: str
    local_certificate_file: str
    ldap_service_uri: str
    mongo_collection_name: str
    group_to_prof_json_path: str = None
    exceptions_json_path: str = None

    @validator("group_to_prof_json_path")
    def _relative_group_to_prof(cls, value):
        return relative_filepath(value)

    @validator("exceptions_json_path")
    def _relative_exception(cls, value):
        return relative_filepath(value)


class AccountMatchingConfig(BaseModel):
    drac_members_csv_path: Path
    drac_roles_csv_path: Path
    make_matches_config: Path


class Config(BaseModel):
    mongo: MongoConfig
    ldap: LDAPConfig
    account_matching: AccountMatchingConfig
    sshconfig: Path = None
    cache: Path = None
    clusters: dict[str, ClusterConfig]

    @validator("cache", "sshconfig")
    def _absolute_path(cls, value):
        return value and value.expanduser().absolute()

    @validator("clusters")
    def _complete_cluster_fields(cls, value, values):
        for name, cluster in value.items():
            if not cluster.name:
                cluster.name = name
            if not cluster.sshconfig and "sshconfig" in values:
                cluster.sshconfig = values["sshconfig"]
        return value


config_var = ContextVar("config", default=None)


_config_folder = None


def relative_filepath(path):
    """Allows files to be relative to the config"""
    if path is None:
        return path

    if "$SELF" in path:
        return path.replace("$SELF", str(_config_folder))

    return path


def parse_config(config_path):
    # pylint: disable=global-statement
    global _config_folder
    config_path = Path(config_path)

    _config_folder = str(config_path.parent)

    if not config_path.exists():
        raise ConfigurationError(
            f"Cannot read SARC configuration file: '{config_path}'"
            " Use the $SARC_CONFIG environment variable to choose the config file."
        )

    try:
        cfg = Config.parse_file(config_path)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"'{config_path}' contains malformed JSON") from exc

    return cfg


def config():
    if (current := config_var.get()) is not None:
        return current
    cfg = parse_config(os.environ.get("SARC_CONFIG", "config/sarc-dev.json"))
    config_var.set(cfg)
    return cfg


@contextmanager
def using_config(cfg: Union[str, Path, Config]):
    if isinstance(cfg, (str, Path)):
        cfg = parse_config(cfg)
    token = config_var.set(cfg)
    yield cfg
    config_var.reset(token)
