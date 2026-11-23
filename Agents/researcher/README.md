# Researcher agent

The Researcher runs a nine-node LangGraph workflow. GoModel's `router` produces validated research questions, inspection observations, findings, and citation audits. The collection workers call GoModel MCPs and public source APIs.

From the workspace root, with GoModel running:

```powershell
uv venv research_output/.researcher-venv
uv pip install --python research_output/.researcher-venv/Scripts/python.exe -r Agents/researcher/requirements.txt
npm.cmd --prefix Agents/researcher ci --ignore-scripts
research_output/.researcher-venv/Scripts/python.exe -m Agents.researcher.main --acceptance --progress
```

For a custom question, replace `--acceptance` with the quoted research request. The workspace's root `config.yaml` owns MCP server and tool allowlists. GoModel 0.1.109 rejects application-specific YAML keys, so `researcher.yaml` owns agent settings, source routes, endpoint URLs and output formats. Both files are validated by the Researcher before graph execution. Set `GOMODEL_URL` for a different gateway; credentials come from `GOMODEL_API_KEY` or `GOMODEL_MASTER_KEY` in the environment or workspace `.env`.

The live sources include DuckDuckGo, Crawl4AI, Context7, GitHub, Research Rabbit, Fetch, deps.dev, PyPI, npm, and Swagger Parser. Filesystem is a GoModel utility and is excluded from Researcher routing. Research Rabbit discovers and verifies bibliographic metadata through arXiv, OpenAlex and PubMed. Its configured tools do not expose a citation-network traversal operation. Sourcegraph and Exa are optional and require credentials if enabled.

The installed DuckDuckGo and Crawl4AI skills remain upstream copies. The Researcher uses them as planning context and calls MCP tools through its own allowlisted dispatcher. It does not execute commands found in skill files or sources.

Run reports are stored under a UUID in `research_output`. JSON includes questions, jobs, evidence, source errors, implementation excerpts, version comparisons and citation audit results. A failed citation audit loops once by default. After the last attempt, rejected findings can be omitted if the audit completed and at least one supported finding remains. If there is no supported report to publish, the CLI exits with code 2. A source failure is recorded and other workers can still collect evidence.
