"""Validated Researcher settings from the workspace's GoModel YAML."""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

ROOT = Path(__file__).resolve().parents[2]


class UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise ValueError(f'Duplicate YAML key on line {key_node.start_mark.line + 1}')
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str = 'router'
    gateway_url: HttpUrl = 'http://localhost:8080'
    output_dir: Path
    formats: set[str] = {'markdown', 'json'}
    concurrency: int = Field(4, ge=1, le=20)
    timeout: float = Field(70, gt=0, le=180)
    attempts: int = Field(2, ge=1, le=4)
    citation_attempts: int = Field(2, ge=1, le=4)
    max_questions: int = Field(6, ge=1, le=12)
    content_limit: int = Field(12000, ge=1000, le=100000)
    minimum_evidence: int = Field(1, ge=1, le=100)
    routes: dict[str, list[str]]
    endpoints: dict[str, HttpUrl]
    skills: dict[str, str]
    mcp_servers: dict[str, list[str]]
    sourcegraph: bool = False
    exa: bool = False

    @model_validator(mode='after')
    def validate_settings(self):
        if not self.model.strip() or not self.formats or self.formats - {'markdown', 'json'}:
            raise ValueError('Configure a model and valid report formats')
        if set(self.routes) != {'web','documentation','repository','library','academic','api'}:
            raise ValueError('Configure routes for all six research capabilities')
        if missing := {'pypi','npm','deps_dev','sourcegraph','exa'} - self.endpoints.keys():
            raise ValueError(f'Missing source endpoints: {sorted(missing)}')
        if unknown := {name for route in self.routes.values() for name in route} - (
                set(self.mcp_servers) | {'duckduckgo','crawl4ai','context7','github','research-rabbit',
                                        'fetch','swagger_parser','deps_dev','sourcegraph','exa'}):
            raise ValueError(f'Unknown source identifiers: {sorted(unknown)}')
        if self.gateway_url.username or self.gateway_url.password:
            raise ValueError('Set GoModel credentials in the environment')
        for name, tools in self.mcp_servers.items():
            if not tools or len(tools) != len(set(tools)):
                raise ValueError(f'Invalid MCP tool list for {name}')
        if any(url.scheme != 'https' or url.username or url.password for url in self.endpoints.values()):
            raise ValueError('Source endpoints must be HTTPS without embedded credentials')
        return self

    @property
    def gateway(self):
        return str(self.gateway_url).rstrip('/')


def load_settings(path=ROOT / 'config.yaml', researcher_path=Path(__file__).with_name('researcher.yaml')):
    load_dotenv(ROOT / '.env')
    root = yaml.load(Path(path).read_text(encoding='utf-8'), Loader=UniqueKeyLoader)
    application = yaml.load(Path(researcher_path).read_text(encoding='utf-8'), Loader=UniqueKeyLoader)
    values = dict(application['researcher'])
    mcp = root['mcp']
    values['mcp_servers'] = {name: value['allowed_tools'] for name, value in mcp['servers'].items()
                             if mcp.get('enabled') and value.get('enabled', True)}
    values['output_dir'] = ROOT / os.getenv('RESEARCH_OUTPUT_DIR', values['output_dir'])
    if os.getenv('GOMODEL_URL'):
        values['gateway_url'] = os.environ['GOMODEL_URL']
    if os.getenv('GOMODEL_MODEL'):
        values['model'] = os.environ['GOMODEL_MODEL']
    return Settings.model_validate(values)


def gateway_key():
    return os.getenv('GOMODEL_API_KEY') or os.getenv('GOMODEL_MASTER_KEY') or 'local'
