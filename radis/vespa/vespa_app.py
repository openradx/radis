import xml.etree.ElementTree as ET
from os import PathLike
from pathlib import Path

from django.conf import settings
from vespa.application import Vespa
from vespa.package import (
    HNSW,
    ApplicationPackage,
    Component,
    Document,
    DocumentSummary,
    Field,
    FieldSet,
    GlobalPhaseRanking,
    Parameter,
    RankProfile,
    Schema,
    Summary,
)

REPORT_SCHEMA_NAME = "report"
BM25_RANK_PROFILE = "bm25"
SEMANTIC_RANK_PROFILE = "semantic"
FUSION_RANK_PROFILE = "fusion"
RETRIEVAL_SUMMARY = "retrieval-summary"
SEARCH_QUERY_PROFILE = "SearchProfile"
RETRIEVAL_QUERY_PROFILE = "RetrievalProfile"

# We set max hits to the same value as max offset as our search and retrieval
# provider (as most other full text search databases) only allow to set
# a maximum results (offset + limit). That way we make sure that the maximum
# results can really be reached regardless of the actual number of offset and
# limit.
MAX_SEARCH_HITS = 1000
MAX_SEARCH_OFFSET = 1000
SEARCH_TIMEOUT = 3
MAX_RETRIEVAL_HITS = 10000
MAX_RETRIEVAL_OFFSET = 10000
RETRIEVAL_TIMEOUT = 60


def _create_report_schema():
    return Schema(
        REPORT_SCHEMA_NAME,
        document=Document(
            fields=[
                Field(
                    name="document_id",
                    type="string",
                    indexing=["summary"],
                ),
                Field(
                    name="language",
                    type="string",
                    indexing=["set_language", "attribute"],
                ),
                Field(
                    name="groups",
                    type="array<int>",
                    indexing=["attribute"],
                ),
                Field(
                    name="pacs_aet",
                    type="string",
                    indexing=["attribute"],
                ),
                Field(
                    name="pacs_name",
                    type="string",
                    indexing=["summary"],
                ),
                Field(
                    name="pacs_link",
                    type="string",
                    indexing=["summary"],
                ),
                Field(
                    name="patient_birth_date",
                    type="int",
                    indexing=["summary", "attribute"],
                ),
                Field(
                    name="patient_age",
                    type="int",
                    indexing=["summary", "attribute"],
                ),
                Field(
                    name="patient_sex",
                    type="string",
                    indexing=["summary", "attribute"],
                ),
                Field(
                    name="study_description",
                    type="string",
                    indexing=["summary", "index"],
                ),
                Field(
                    name="study_datetime",
                    type="long",
                    indexing=["summary", "attribute"],
                ),
                Field(
                    name="modalities",
                    type="array<string>",
                    indexing=["summary", "attribute"],
                ),
                Field(
                    name="body",
                    type="string",
                    indexing=["summary", "index"],
                    index="enable-bm25",
                    summary=Summary(None, None, ["dynamic"]),
                ),
                Field(
                    name="embedding",
                    type="tensor<float>(x[384])",
                    indexing=["input body", "embed", "index", "attribute"],
                    ann=HNSW(distance_metric="angular"),
                    is_document_field=False,
                ),
            ]
        ),
        fieldsets=[
            FieldSet(name="default", fields=["body"]),
        ],
        rank_profiles=[
            RankProfile(name=BM25_RANK_PROFILE, first_phase="bm25(body)"),
            RankProfile(
                name=SEMANTIC_RANK_PROFILE,
                # TODO: fix issue with type hint https://github.com/vespa-engine/pyvespa/issues/676
                inputs=[("query(q)", "tensor<float>(x[384])")],
                first_phase="closeness(field, embedding)",
            ),
            RankProfile(
                name=FUSION_RANK_PROFILE,
                inherits=BM25_RANK_PROFILE,
                # TODO: fix issue with type hint https://github.com/vespa-engine/pyvespa/issues/676
                inputs=[("query(q)", "tensor<float>(x[384])")],
                first_phase="closeness(field, embedding)",
                global_phase=GlobalPhaseRanking(
                    expression="reciprocal_rank_fusion(bm25(body), closeness(field, embedding))",
                    rerank_count=100,
                ),
            ),
        ],
        document_summaries=[
            DocumentSummary(
                name=RETRIEVAL_SUMMARY,
                summary_fields=[Summary("document_id", "string")],
            )
        ],
    )


def _create_app_package(schemas: list[Schema]):
    return ApplicationPackage(
        name="radis",
        schema=schemas,
        components=[
            Component(
                id="e5",
                type="hugging-face-embedder",
                parameters=[
                    Parameter(
                        "transformer-model",
                        {"path": "files/models/model.onnx"},
                    ),
                    Parameter(
                        "tokenizer-model",
                        {"path": "files/models/tokenizer.json"},
                    ),
                ],
            )
        ],
    )


class VespaAppModifier:
    def __init__(self, app_folder: PathLike) -> None:
        self.query_profiles_folder = Path(app_folder) / "search" / "query-profiles"
        self.services_file = Path(app_folder) / "services.xml"
        self.services_doc = ET.parse(self.services_file)

    def apply(self):
        # We overwrite the generated default query profile
        self._add_query_profile(
            SEARCH_QUERY_PROFILE,
            MAX_SEARCH_HITS,
            MAX_SEARCH_OFFSET,
            SEARCH_TIMEOUT,
        )
        self._add_query_profile(
            RETRIEVAL_QUERY_PROFILE,
            MAX_RETRIEVAL_HITS,
            MAX_RETRIEVAL_OFFSET,
            RETRIEVAL_TIMEOUT,
        )
        self._add_bolding_config()
        self._add_dynamic_snippet_config()
        self._write()

    def _add_query_profile(self, profile_name: str, max_hits: int, max_offset: int, timeout: int):
        query_profile_el = ET.fromstring(
            f"""
            <query-profile id="{profile_name}">
                <field name="maxHits">{max_hits}</field>
                <field name="maxOffset">{max_offset}</field>
                <field name="timeout">{timeout}</field>
            </query-profile>
            """
        )
        tree = ET.ElementTree(query_profile_el)
        ET.indent(tree, space="\t", level=0)
        with open(self.query_profiles_folder / f"{profile_name}.xml", "wb") as f:
            tree.write(f, encoding="UTF-8")

    # https://docs.vespa.ai/en/reference/schema-reference.html#bolding
    def _add_bolding_config(self):
        config_el = ET.fromstring(
            """
            <config name="container.qr-searchers">
                <tag>
                    <bold>
                        <open>&lt;strong&gt;</open>
                        <close>&lt;/strong&gt;</close>
                    </bold>
                    <separator>&lt;em&gt;...&lt;/em&gt;</separator>
                </tag>
            </config>
            """
        )
        search_el = self.services_doc.find("./container/search")
        assert search_el is not None
        search_el.append(config_el)

    # https://docs.vespa.ai/en/document-summaries.html#dynamic-snippet-configuration
    # https://github.com/vespa-engine/vespa/blob/master/searchsummary/src/vespa/searchsummary/config/juniperrc.def
    def _add_dynamic_snippet_config(self):
        config_el = ET.fromstring(
            """
            <config name="vespa.config.search.summary.juniperrc">
                <length>500</length>
            </config>
            """
        )
        content_el = self.services_doc.find("./content")
        assert content_el is not None
        content_el.append(config_el)

    def _write(self):
        ET.indent(self.services_doc, "    ")
        self.services_doc.write(self.services_file, encoding="UTF-8", xml_declaration=True)


class VespaApp:
    _vespa_host = settings.VESPA_HOST
    _vespa_data_port = settings.VESPA_DATA_PORT

    _app_package: ApplicationPackage | None = None
    _client: Vespa | None = None

    def get_app_package(self) -> ApplicationPackage:
        if not self._app_package:
            report_schema = _create_report_schema()
            self._app_package = _create_app_package([report_schema])
        return self._app_package

    def get_client(self) -> Vespa:
        if not self._client:
            self._client = Vespa(
                f"http://{self._vespa_host}",
                self._vespa_data_port,
                application_package=self.get_app_package(),
            )
        return self._client


vespa_app = VespaApp()
