import json
import logging
import time
from typing import Any, Dict, Iterable, List

from datahub.configuration import ConfigModel
from datahub.configuration.common import AllowDenyPattern
from datahub.ingestion.api.common import PipelineContext
from datahub.ingestion.api.source import Source, SourceReport
from datahub.ingestion.source.dbt_types import (
    POSTGRES_TYPES_MAP,
    SNOWFLAKE_TYPES_MAP,
    resolve_postgres_modified_type,
)
from datahub.ingestion.source.metadata_common import MetadataWorkUnit
from datahub.metadata.com.linkedin.pegasus2avro.common import AuditStamp
from datahub.metadata.com.linkedin.pegasus2avro.dataset import (
    DatasetLineageTypeClass,
    UpstreamClass,
    UpstreamLineage,
)
from datahub.metadata.com.linkedin.pegasus2avro.metadata.snapshot import DatasetSnapshot
from datahub.metadata.com.linkedin.pegasus2avro.mxe import MetadataChangeEvent
from datahub.metadata.com.linkedin.pegasus2avro.schema import (
    BooleanTypeClass,
    DateTypeClass,
    MySqlDDL,
    NullTypeClass,
    NumberTypeClass,
    SchemaField,
    SchemaFieldDataType,
    SchemaMetadata,
    StringTypeClass,
    TimeTypeClass,
)
from datahub.metadata.schema_classes import DatasetPropertiesClass

logger = logging.getLogger(__name__)


class DBTConfig(ConfigModel):
    manifest_path: str
    catalog_path: str
    env: str = "PROD"
    target_platform: str
    load_schemas: bool
    node_type_pattern: AllowDenyPattern = AllowDenyPattern.allow_all()


class DBTColumn:
    name: str
    comment: str
    index: int
    data_type: str

    def __repr__(self):
        fields = tuple("{}={}".format(k, v) for k, v in self.__dict__.items())
        return self.__class__.__name__ + str(tuple(sorted(fields))).replace("'", "")


class DBTNode:
    dbt_name: str
    database: str
    schema: str
    dbt_file_path: str
    node_type: str  # source, model
    materialization: str  # table, view, ephemeral
    name: str  # name, identifier
    columns: List[DBTColumn]
    upstream_urns: List[str]
    datahub_urn: str

    def __repr__(self):
        fields = tuple("{}={}".format(k, v) for k, v in self.__dict__.items())
        return self.__class__.__name__ + str(tuple(sorted(fields))).replace("'", "")


def get_columns(catalog_node: dict) -> List[DBTColumn]:
    columns = []

    raw_columns = catalog_node["columns"]

    for key in raw_columns:
        raw_column = raw_columns[key]

        dbtCol = DBTColumn()
        dbtCol.comment = raw_column["comment"]
        dbtCol.data_type = raw_column["type"]
        dbtCol.index = raw_column["index"]
        dbtCol.name = raw_column["name"]
        columns.append(dbtCol)
    return columns


def extract_dbt_entities(
    nodes: Dict[str, dict],
    catalog: Dict[str, dict],
    load_catalog: bool,
    target_platform: str,
    environment: str,
    node_type_pattern: AllowDenyPattern,
) -> List[DBTNode]:
    dbt_entities = []
    for key, node in nodes.items():
        dbtNode = DBTNode()

        # check if node pattern allowed based on config file
        if not node_type_pattern.allowed(node["resource_type"]):
            continue
        dbtNode.dbt_name = key
        dbtNode.database = node["database"]
        dbtNode.schema = node["schema"]
        dbtNode.dbt_file_path = node["original_file_path"]
        dbtNode.node_type = node["resource_type"]
        if "identifier" in node and load_catalog is False:
            dbtNode.name = node["identifier"]
        else:
            dbtNode.name = node["name"]

        if "materialized" in node["config"].keys():
            # It's a model
            dbtNode.materialization = node["config"]["materialized"]
            dbtNode.upstream_urns = get_upstreams(
                node["depends_on"]["nodes"],
                nodes,
                load_catalog,
                target_platform,
                environment,
            )
        else:
            # It's a source
            dbtNode.materialization = catalog[key]["metadata"][
                "type"
            ]  # get materialization from catalog? required?
            dbtNode.upstream_urns = []

        if (
            dbtNode.materialization != "ephemeral" and load_catalog
        ):  # we don't want columns if platform isn't 'dbt'
            logger.debug("Loading schema info")
            dbtNode.columns = get_columns(catalog[dbtNode.dbt_name])
        else:
            dbtNode.columns = []

        dbtNode.datahub_urn = get_urn_from_dbtNode(
            dbtNode.database,
            dbtNode.schema,
            dbtNode.name,
            target_platform,
            environment,
        )

        dbt_entities.append(dbtNode)

    return dbt_entities


def loadManifestAndCatalog(
    manifest_path: str,
    catalog_path: str,
    load_catalog: bool,
    target_platform: str,
    environment: str,
    node_type_pattern: AllowDenyPattern,
) -> List[DBTNode]:
    with open(manifest_path, "r") as manifest:
        with open(catalog_path, "r") as catalog:
            dbt_manifest_json = json.load(manifest)
            dbt_catalog_json = json.load(catalog)

            manifest_nodes = dbt_manifest_json["nodes"]
            manifest_sources = dbt_manifest_json["sources"]

            all_manifest_entities = {**manifest_nodes, **manifest_sources}

            catalog_nodes = dbt_catalog_json["nodes"]
            catalog_sources = dbt_catalog_json["sources"]

            all_catalog_entities = {**catalog_nodes, **catalog_sources}

            return extract_dbt_entities(
                all_manifest_entities,
                all_catalog_entities,
                load_catalog,
                target_platform,
                environment,
                node_type_pattern,
            )


def get_urn_from_dbtNode(
    database: str, schema: str, name: str, target_platform: str, env: str
) -> str:

    db_fqn = f"{database}.{schema}.{name}".replace('"', "")
    return f"urn:li:dataset:(urn:li:dataPlatform:{target_platform},{db_fqn},{env})"


def get_custom_properties(node: DBTNode) -> Dict[str, str]:
    return {
        "dbt_node_type": node.node_type,
        "materialization": node.materialization,
        "dbt_file_path": node.dbt_file_path,
    }


def get_upstreams(
    upstreams: List[str],
    all_nodes: Dict[str, dict],
    load_catalog: bool,
    target_platform: str,
    environment: str,
) -> List[str]:
    upstream_urns = []

    for upstream in upstreams:
        dbtNode_upstream = DBTNode()

        dbtNode_upstream.database = all_nodes[upstream]["database"]
        dbtNode_upstream.schema = all_nodes[upstream]["schema"]
        if "identifier" in all_nodes[upstream] and not load_catalog:
            dbtNode_upstream.name = all_nodes[upstream]["identifier"]
        else:
            dbtNode_upstream.name = all_nodes[upstream]["name"]

        upstream_urns.append(
            get_urn_from_dbtNode(
                dbtNode_upstream.database,
                dbtNode_upstream.schema,
                dbtNode_upstream.name,
                target_platform,
                environment,
            )
        )

    return upstream_urns


def get_upstream_lineage(upstream_urns: List[str]) -> UpstreamLineage:
    ucl: List[UpstreamClass] = []

    actor, sys_time = "urn:li:corpuser:dbt_executor", int(time.time()) * 1000

    for dep in upstream_urns:
        uc = UpstreamClass(
            dataset=dep,
            auditStamp=AuditStamp(actor=actor, time=sys_time),
            type=DatasetLineageTypeClass.TRANSFORMED,
        )
        ucl.append(uc)

    return UpstreamLineage(upstreams=ucl)


# See https://github.com/fishtown-analytics/dbt/blob/master/core/dbt/adapters/sql/impl.py
_field_type_mapping = {
    "boolean": BooleanTypeClass,
    "date": DateTypeClass,
    "time": TimeTypeClass,
    "numeric": NumberTypeClass,
    "text": StringTypeClass,
    "timestamp with time zone": DateTypeClass,
    "timestamp without time zone": DateTypeClass,
    "integer": NumberTypeClass,
    "float8": NumberTypeClass,
    **POSTGRES_TYPES_MAP,
    **SNOWFLAKE_TYPES_MAP,
}


def get_column_type(
    report: SourceReport, dataset_name: str, column_type: str
) -> SchemaFieldDataType:
    """
    Maps known DBT types to datahub types
    """
    TypeClass: Any = _field_type_mapping.get(column_type)

    if TypeClass is None:

        # attempt Postgres modified type
        TypeClass = resolve_postgres_modified_type(column_type)

    # if still not found, report the warning
    if TypeClass is None:

        report.report_warning(
            dataset_name, f"unable to map type {column_type} to metadata schema"
        )
        TypeClass = NullTypeClass

    return SchemaFieldDataType(type=TypeClass())


def get_schema_metadata(
    report: SourceReport, node: DBTNode, platform: str
) -> SchemaMetadata:
    canonical_schema: List[SchemaField] = []
    for column in node.columns:
        field = SchemaField(
            fieldPath=column.name,
            nativeDataType=column.data_type,
            type=get_column_type(report, node.dbt_name, column.data_type),
            description=column.comment,
            nullable=False,  # TODO: actually autodetect this
            recursive=False,
        )

        canonical_schema.append(field)

    actor, sys_time = "urn:li:corpuser:dbt_executor", int(time.time()) * 1000
    return SchemaMetadata(
        schemaName=node.dbt_name,
        platform=f"urn:li:dataPlatform:{platform}",
        version=0,
        hash="",
        platformSchema=MySqlDDL(tableSchema=""),
        created=AuditStamp(time=sys_time, actor=actor),
        lastModified=AuditStamp(time=sys_time, actor=actor),
        fields=canonical_schema,
    )


class DBTSource(Source):
    """Extract DBT metadata for ingestion to Datahub"""

    @classmethod
    def create(cls, config_dict, ctx):
        config = DBTConfig.parse_obj(config_dict)
        return cls(config, ctx, "dbt")

    def __init__(self, config: DBTConfig, ctx: PipelineContext, platform: str):
        super().__init__(ctx)
        self.config = config
        self.platform = platform
        self.report = SourceReport()

    def get_workunits(self) -> Iterable[MetadataWorkUnit]:
        platform = self.platform
        nodes = loadManifestAndCatalog(
            self.config.manifest_path,
            self.config.catalog_path,
            self.config.load_schemas,
            self.config.target_platform,
            self.config.env,
            self.config.node_type_pattern,
        )

        for node in nodes:

            dataset_snapshot = DatasetSnapshot(
                urn=node.datahub_urn,
                aspects=[],
            )
            custom_properties = get_custom_properties(node)

            dbt_properties = DatasetPropertiesClass(
                description=node.dbt_name,
                customProperties=custom_properties,
                tags=[],
            )
            dataset_snapshot.aspects.append(dbt_properties)

            upstreams = get_upstream_lineage(node.upstream_urns)
            if upstreams is not None:
                dataset_snapshot.aspects.append(upstreams)

            if self.config.load_schemas:
                schema_metadata = get_schema_metadata(self.report, node, platform)
                dataset_snapshot.aspects.append(schema_metadata)

            mce = MetadataChangeEvent(proposedSnapshot=dataset_snapshot)
            wu = MetadataWorkUnit(id=dataset_snapshot.urn, mce=mce)
            self.report.report_workunit(wu)

            yield wu

    def get_report(self):
        return self.report

    def close(self):
        pass
