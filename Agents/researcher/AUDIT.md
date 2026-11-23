# Researcher engineering audit

## Scope

Inspected the Researcher entry point, nine graph nodes, graph routing, schemas, GoModel client, source selector, MCP and source registries, adapters, skill entry points, report renderers, storage stubs, prompts, and test files. Checked the root GoModel YAML, the Specification agent's gateway conventions, and installed Python and Node dependency versions. The original Researcher had no working dependency declaration. The initial normal entry point failed with `ModuleNotFoundError: No module named 'graph'`.

## Defects and repairs

| Severity | Root cause | Repair |
| --- | --- | --- |
| Critical | Top-level imports resolved against the working directory. The startup path also referenced undefined factories and required retired provider keys. | Restored the package CLI and application-owned clients in `main.py`. Wired the compiled graph to a shared source dispatcher. Removed the obsolete provider modules. |
| Critical | Routing and the citation node were empty. The verifier expected a service inside graph state, and worker jobs discarded package and repository fields. | Repaired `graph/workflow.py`, `graph/routing.py`, `state/`, and the node modules. Workers receive a complete job through LangGraph `Send`. Reducers accumulate evidence and errors. |
| High | MCP discovery used guessed environment endpoints and a flat tool-name map. The provider replacements have different response formats. | `integrations/mcp_gateway.py` discovers the per-server GoModel tools, intersects them with the YAML allowlists, validates tool arguments, and checks tool results. `integrations/dispatcher.py` parses numbered DuckDuckGo hits, Crawl4AI Markdown, Context7 results, GitHub search and file payloads, and Research Rabbit papers. |
| High | The original YAML settings for the Researcher were absent, and GoModel's parser rejects unknown root fields and multiple YAML documents. | `config.yaml` owns MCP configuration. `researcher.yaml` owns Researcher routes, settings, limits, formats, and endpoints. `config.py` validates both and rejects duplicate keys. GoModel started with the repaired root YAML. |
| High | Search snippets, inspected implementation, and registry metadata were treated interchangeably. Intentionally unrelated providers could also be selected. | Route structured questions by capability and live availability. Crawl selected search results, keep document provenance, and distinguish package metadata, commit-pinned files, issue records, and papers. Verify implementation excerpts against actual source text. Reconcile current package versions against both registries. |
| High | The model could confuse finding IDs and evidence IDs or add uncited factual prose in report sections. | Application code assigns finding IDs and builds section references. The auditor rejects missing and snippet-only sources, checks literal versions, dates and URLs, and semantically reviews every finding. Registry version and GitHub license and activity statements are built from source metadata. |
| High | Reports could overwrite a previous run or be exported with weak citations. | `graph/nodes/export.py` gates export on the citation verdict and stages the configured Markdown and JSON files before publishing a UUID directory. |
| High | The original Fetch adapter checked a DNS resolution separate from the actual connection, with weak response boundaries. The parser accepted external reference resolution. | Direct Fetch validates HTTPS and public addresses, pins the address with correct TLS SNI on each hop, and bounds redirects, response size and content type. Swagger Parser reads stdin and disables external file and HTTP resolution. Source text is passed as untrusted data to the model. |
| Medium | Empty files, retired providers, and integration credentials gave an appearance of coverage without runtime implementations. | Removed unused empty modules, retired Tavily, Firecrawl and Semantic Scholar, kept the published DuckDuckGo and Crawl4AI skills unchanged, and declared reproducible Python and Node dependencies. |

## Execution flow

`classify_research → decompose_research → select_sources → collect_evidence → inspect_implementations → cross_validate → synthesize_findings → validate_citations → export_report`

Collection fans out by question, then within each worker where requests are independent. Crawl4AI waits for DuckDuckGo URLs. When available, package metadata resolves the canonical GitHub repository before its lookup. Source failures remain in the run state. A failed citation audit can return to synthesis once with the default configuration. After the last attempt, the agent may publish only the supported findings if the audit is complete. An incomplete audit or lack of supported findings blocks export.

## Test matrix

| Check | Status | Evidence |
| --- | --- | --- |
| Module entry point, imports, syntax, schemas, unit tests | PASSED | Isolated Python 3.12 verified routing, upstream skill loading, unsafe URLs, citation rejection, removal of rejected findings, and graph edges. `compileall` succeeded. |
| Dependencies | PASSED in the isolated environment | `uv pip check` validated 62 packages. Node resolved Swagger Parser and js-yaml. The host environment reported an existing `langchain-mcp-adapters 0.3.2` versus `mcp 2.3.0` conflict and was left as-is. |
| GoModel router | PASSED | Live structured classification, decomposition, inspection, synthesis and citation audits completed through the `router` alias. |
| DuckDuckGo and Crawl4AI | PASSED | Five sample hits were normalized into evidence, followed by Markdown extraction from a discovered URL. Browser failures for some other URLs were surfaced in earlier runs. |
| Context7 and Fetch | PASSED | Resolved a library and retrieved documentation, and separately fetched a real public HTTPS document. |
| GitHub | PASSED for core calls and issue lookup | Real repository metadata and commit-pinned source records were normalized. `issue_read` retrieved CPython's PEP 703 tracking issue using the refreshed configuration. Some framework-specific queries did not resolve to code files. |
| Research Rabbit | PASSED for paper discovery and citation formatting | `search_papers` returned real records and paper identifiers. `format_citation` formatted a returned paper. Citation-network traversal is unavailable through the configured package interface. |
| deps.dev, PyPI, npm | PASSED | Each returned a live normalized registry record. PyPI and deps.dev were used in the comparison. npm was exercised separately. |
| Swagger Parser | PASSED | Fetched and parsed a live OpenAPI specification into API evidence. |
| Filesystem | PASSED as a gateway utility | Discovered and invoked `list_allowed_directories`. Filesystem is excluded from Researcher routing. |
| Citation failure path | PASSED | Initial live run found invalid section IDs and an unsupported license claim and declined to publish both files. |
| Sourcegraph and Exa | NOT RUN | Disabled in Researcher configuration. |
| Research Rabbit citation-network traversal | BLOCKED | The installed MCP exposes search and citation formatting, with no citation-graph traversal tool. |
| Fresh second GoModel instance with all stdio sources | FAILED | Temporary port 8084 accepted GitHub but multiple stdio MCPs exceeded its initial connection deadline. The established port 8080 gateway exposed all six servers for the live adapter check. |
| Additional GitHub tool contract | PASSED | `list_commits` returned a live LangGraph result. The comparison derived its repository activity dates from GitHub's verified push metadata. |
| Specification agent regression | PASSED for CLI imports and help | `python main.py --help` reached the existing Specification CLI. A new Specification generation run was not part of this audit. |

## Remaining security boundary

Direct Fetch pins a validated public address on each hop. Crawl4AI is an independent browser process. The Researcher checks the initial URL and its DNS answers but cannot enforce the browser's redirects, subresource requests, or final connection address. Run that service with its own network egress restrictions before treating arbitrary web targets as hardened against browser-level SSRF. No such deployed restriction was verified in this audit.

## Final live run

The requested framework comparison ran through GoModel on port 8080 and exited with code 0. Run ID: `36309a24d1be4cfb98e5f718c8f39498`. Open `research_output/36309a24d1be4cfb98e5f718c8f39498/report.md` and the corresponding `report.json`.

| Gate | Result |
| --- | --- |
| Repository understanding | PASSED. The normal entry point and its backing modules were traced and repaired. |
| Configuration integrity | PASSED. Both YAML files validated, and a fresh GoModel process loaded the root file and advertised the new GitHub issue tool. |
| Integration integrity | PASSED for the established gateway's research sources. A second-instance cold-start probe failed for multiple stdio MCPs. Research Rabbit citation-network traversal remained blocked. |
| Workflow integrity | PASSED. All nine nodes executed. The live comparison fanned out into six jobs and exercised the bounded citation retry. |
| Evidence integrity | PASSED for published findings. Verified 43 unique records, five exact inspection excerpts, 13 supported findings, and all Markdown links against JSON evidence. Final citations include one commit-pinned source file and three paper records. |
| Runtime integrity | PASSED with coverage gaps. Both exports exist. AutoGen code search yielded no commit-pinned files. Three unsupported comparative recommendations were omitted on the second audit attempt, so the final report has no recommendations section. |
| Regression integrity | PASSED for seven Researcher unit tests, compilation, dependency checks, and the Specification CLI import and help smoke test. Full Specification generation was NOT RUN. |

Live evidence counts: DuckDuckGo 9, Crawl4AI 3, Context7 4, Fetch 4, deps.dev 4, PyPI 4, GitHub 10, Research Rabbit 5. PyPI and deps.dev agreed for the exact queried distributions `langgraph`, `crewai`, `pyautogen`, and `pydantic-ai`. These checks establish package version records, with repository licenses reported separately from package terms. No contradiction was returned by the cross-validator in this run.

The temporary gateway on port 8084 was stopped after the issue lookup. The established port 8080 instance remains available. It needs to load the revised root YAML to advertise `issue_read`. Earlier UUID directories contain intermediate development runs.
