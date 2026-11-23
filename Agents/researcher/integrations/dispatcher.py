"""Execute selected sources and normalize their actual results."""
import asyncio
import base64
import json
import os
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx

from .fetch import fetch, public_address
from .mcp_gateway import IntegrationError
from .package_sources import package_metadata
from .registry import SourceRegistry
from .swagger_parser import SwaggerParserAdapter
from ..state.schemas import Evidence


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script','style','noscript'}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {'script','style','noscript'} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def repository(url):
    if not url:
        return None
    value = urlsplit(str(url))
    parts = value.path.strip('/').split('/')
    if value.hostname == 'github.com' and len(parts) >= 2 and all(
            re.fullmatch(r'[A-Za-z0-9_.-]+', item) and item not in {'.','..'} for item in parts[:2]):
        return tuple(parts[:2])
    return None


def resolved_repository(job, evidence):
    if '/issues/' in str(job.repository_url or ''):
        return job
    for item in evidence:
        if item.question_id != job.question_id or item.source not in {'pypi','npm'}:
            continue
        try:
            data = json.loads(item.content)
        except ValueError:
            continue
        urls = data.get('project_urls') or {}
        for label,url in urls.items():
            if label.lower() in {'source','source code','repository','github','code'} and repository(url):
                return job.model_copy(update={'repository_url':url})
    return job


class SourceDispatcher:
    MCP_TOOLS = {'duckduckgo':{'search'}, 'crawl4ai':{'crawl_url'},
                 'context7':{'resolve-library-id','query-docs'},
                 'github':{'search_repositories','get_file_contents'},
                 'research-rabbit':{'search_papers'}}

    def __init__(self, settings, gateway, http):
        self.settings, self.gateway, self.http = settings, gateway, http
        self.slots = asyncio.Semaphore(settings.concurrency)
        self.registry = SourceRegistry()
        adapters = {'duckduckgo':self.search, 'crawl4ai':self.crawl, 'context7':self.docs,
                    'github':self.github, 'research-rabbit':self.academic, 'fetch':self.page,
                    'pypi':self.package, 'npm':self.package, 'deps_dev':self.package,
                    'swagger_parser':self.openapi}
        for source, adapter in adapters.items():
            expected = self.MCP_TOOLS.get(source)
            if expected is None or expected <= gateway.catalog.get(source, {}).keys():
                self.registry.register(source, adapter)
        if settings.sourcegraph and os.getenv('SOURCEGRAPH_TOKEN'):
            self.registry.register('sourcegraph', self.sourcegraph)
        if settings.exa and os.getenv('EXA_API_KEY'):
            self.registry.register('exa', self.exa)
        self.parser = SwaggerParserAdapter(settings.timeout)

    def record(self, source, job, kind, url, title, content, **kwargs):
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        content = content.strip()[:self.settings.content_limit]
        if str(urlsplit(str(url)).scheme) != 'https' or urlsplit(str(url)).username is not None:
            raise IntegrationError('Source returned an unsafe citation URL')
        identity = uuid5(NAMESPACE_URL, f'{job.question_id}/{source}/{kind}/{url}/{content}')
        return Evidence(evidence_id=identity.hex, question_id=job.question_id, source=source,
                        source_type=kind, url=url, title=title, content=content,
                        provenance=kwargs.pop('provenance',urlsplit(str(url)).hostname), **kwargs)

    async def collect(self, source, job, target=None):
        adapter = self.registry.get(source)
        for attempt in range(self.settings.attempts):
            try:
                async with self.slots, asyncio.timeout(self.settings.timeout):
                    evidence = await adapter(source, job, target)
                if not evidence:
                    raise IntegrationError('Source returned no relevant evidence')
                return evidence
            except (TimeoutError, httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt + 1 == self.settings.attempts or isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {429,502,503,504}:
                    raise
                await asyncio.sleep(2 ** attempt)

    async def request(self, method, url, **kwargs):
        async with self.http.stream(method, url, **kwargs) as response:
            response.raise_for_status()
            data = bytearray()
            async for part in response.aiter_bytes():
                data.extend(part)
                if len(data) > self.settings.content_limit * 150:
                    raise IntegrationError('Provider response exceeds size limit')
            return json.loads(data)

    async def search(self, source, job, query):
        text = await self.gateway.invoke(source, 'search', {'query':query or job.question,'max_results':5})
        hits = re.findall(r'(?ms)^\d+\. (.+?)\n\s+URL: (https://\S+)\n\s+Summary: (.*?)(?=\n\s*\d+\. |\Z)', str(text))
        return [self.record(source, job, 'search_snippet', url, title, snippet)
                for title,url,snippet in hits if snippet.strip()]

    async def crawl(self, source, job, target):
        await public_address(target)
        text = await self.gateway.invoke(source, 'crawl_url', {'url':target,'profile':'fast'})
        if not isinstance(text, str) or text.lstrip().lower().startswith(('error','failed to','crawl failed')):
            raise IntegrationError('Crawler did not return Markdown')
        heading = re.search(r'(?m)^#{1,3}\s+(.+)$', text)
        return [self.record(source, job, 'web_page', target, heading[1] if heading else target, text)]

    async def docs(self, source, job, target):
        name = target or job.subject or job.package_name
        if not name:
            raise IntegrationError('Context7 needs a library name')
        result = await self.gateway.invoke(source, 'resolve-library-id', {'libraryName':name,'query':job.question})
        match = re.search(r'Context7-compatible library ID:\s*`?(/[\w./@+:-]+)', str(result), re.I)
        if not match:
            raise IntegrationError('Context7 found no library identifier')
        library = match[1]
        text = await self.gateway.invoke(source, 'query-docs', {'libraryId':library,'query':job.question})
        if not isinstance(text, str) or len(text.strip()) < 30:
            raise IntegrationError('Context7 returned no documentation')
        return [self.record(source, job, 'documentation', f'https://context7.com{library}', name,
                            text, provenance=f'project:{name.lower()}')]

    async def page(self, source, job, target):
        target = target or job.documentation_url
        if not target:
            raise IntegrationError('Fetch needs a source URL')
        url, text, mime = await fetch(self.http, str(target), self.settings.content_limit * 100)
        if 'html' in mime:
            parser = PageText()
            parser.feed(text)
            text = '\n'.join(parser.parts)
        return [self.record(source, job, 'retrieved_page', url, url, text)]

    async def package(self, source, job, target):
        url, data = await package_metadata(self.request, self.settings.endpoints, source, job)
        return [self.record(source, job, 'package_metadata', url, job.package_name, data,
                            version=data['version'], published_at=data.get('released_at'),
                            provenance=f'package:{job.package_ecosystem}:{job.package_name.lower()}',
                            metadata={'ecosystem':job.package_ecosystem,'package':job.package_name})]

    async def academic(self, source, job, target):
        query = target or job.question
        response = await self.gateway.invoke(source, 'search_papers', {
            'query':query,'sources':['arxiv','openalex','pubmed'],'limit':5})
        papers = response['papers']
        return [self.record(source,job,'paper_metadata',paper['url'],paper['title'],
                            paper.get('abstract') or {'title':paper['title'],'authors':paper.get('authors',[]),'doi':paper.get('doi')},
                            authors=paper.get('authors',[]),published_at=str(paper['year']) if paper.get('year') else None,
                            provenance=paper.get('doi') or paper['id'],
                            metadata={key:paper.get(key) for key in ('id','source','doi')})
                for paper in papers if str(paper.get('url','')).startswith('https://')]

    async def github(self, source, job, target):
        repo = repository(job.repository_url)
        if not repo:
            raise IntegrationError('GitHub needs a repository URL')
        owner, name = repo
        base = f'https://github.com/{owner}/{name}'
        common = {'owner':owner,'repo':name}
        provenance = f'project:{name.lower()}'
        issue = re.fullmatch(r'/[^/]+/[^/]+/issues/(\d+)/?',urlsplit(str(job.repository_url)).path)
        if issue and target is None:
            data = await self.gateway.invoke(source,'issue_read',{
                **common,'issue_number':int(issue[1]),'method':'get'})
            if isinstance(data,dict):
                data = data.get('issue',data)
            if not isinstance(data,dict) or not data.get('title'):
                raise IntegrationError('GitHub did not return issue metadata')
            fields = ('title','body','state','created_at','updated_at','closed_at')
            return [self.record(source,job,'issue',f'{base}/issues/{issue[1]}',data['title'],
                                {key:data.get(key) for key in fields},published_at=data.get('created_at'),
                                provenance=provenance)]
        if target:
            path, ref = target
            text = await self.gateway.invoke(source,'get_file_contents',{**common,'path':path,'ref':ref})
            if isinstance(text,dict) and text.get('encoding')=='base64':
                text = base64.b64decode(text['content']).decode('utf-8',errors='replace')
            if not isinstance(text,str):
                raise IntegrationError('GitHub did not return readable file content')
            if job.code_symbol:
                match = re.search(rf'(?m)^\s*(?:class|def|async def) {re.escape(job.code_symbol)}\b',text)
                if match:
                    text = text[max(0,match.start()-250):]
            return [self.record(source,job,'source_code',f'{base}/blob/{ref}/{quote(path)}',path,text,provenance=provenance)]
        result = await self.gateway.invoke(source,'search_repositories',{
            'query':f'repo:{owner}/{name}','perPage':1,'minimal_output':False})
        rows = result.get('items',[])
        row = next((item for item in rows if item.get('full_name','').lower()==f'{owner}/{name}'.lower()),None)
        if not row:
            raise IntegrationError('GitHub returned no matching repository')
        keys = ('full_name','default_branch','description','license','pushed_at','archived','homepage')
        return [self.record(source,job,'repository_metadata',base,name,{key:row.get(key) for key in keys},provenance=provenance)]

    async def code_targets(self, job):
        owner, name = repository(job.repository_url)
        symbol = job.code_symbol or 'Agent'
        if not re.fullmatch(r'[A-Za-z_][\w.]{1,70}',symbol):
            raise IntegrationError('Invalid code search symbol')
        language = {'pypi':'Python','npm':'TypeScript'}.get(job.package_ecosystem)
        query = f'{symbol} repo:{owner}/{name}' + (f' language:{language}' if language else '')
        payload = await self.gateway.invoke('github','search_code',{
            'query':query,'perPage':5,
            'fields':['path','repository','text_matches']})
        targets = []
        for item in payload.get('items',[]):
            path = item.get('path','')
            if not path.endswith(('.py','.ts','.js')) or any(part in {'','..','.'} for part in path.split('/')):
                continue
            if str(item.get('repository','')).lower() != f'{owner}/{name}'.lower():
                continue
            for match in item.get('text_matches',[]):
                refs = parse_qs(urlsplit(match.get('object_url','')).query).get('ref',[])
                if refs and re.fullmatch(r'[0-9a-f]{40}',refs[0]):
                    targets.append((path,refs[0]))
                    break
        return list(dict.fromkeys(targets))[:2]

    async def openapi(self, source, job, target):
        if not job.api_spec_url:
            raise IntegrationError('OpenAPI inspection needs a specification URL')
        url, text, _ = await fetch(self.http,str(job.api_spec_url),self.settings.content_limit * 100)
        result = await self.parser.collect(text)
        return [self.record(source,job,'openapi',url,result['title'],result,version=result['version'])]

    async def sourcegraph(self,source,job,target):
        repo = repository(job.repository_url)
        if not repo:
            raise IntegrationError('Sourcegraph needs a repository URL')
        name = '/'.join(repo)
        symbol = job.code_symbol or 'Agent'
        if not re.fullmatch(r'[A-Za-z_][\w.]{1,70}',symbol):
            raise IntegrationError('Invalid code search symbol')
        endpoint = str(self.settings.endpoints[source])
        async with self.http.stream('GET',endpoint,params={'q':f'repo:^github[.]com/{re.escape(name)}$ {symbol} count:5'},
                                    headers={'Authorization':f"token {os.environ['SOURCEGRAPH_TOKEN']}"}) as response:
            response.raise_for_status()
            rows = []
            async for line in response.aiter_lines():
                if line.startswith('data: ['):
                    rows.extend(item for item in json.loads(line[6:]) if item.get('type')=='content')
                    if len(rows)>=5:
                        break
        return [self.record(source,job,'source_code_search',f"https://github.com/{name}/blob/{row['commit']}/{quote(row['path'])}",
                            row['path'],row.get('lineMatches') or row) for row in rows[:5]]

    async def exa(self,source,job,target):
        result = await self.request('POST',str(self.settings.endpoints[source]),
                                    headers={'x-api-key':os.environ['EXA_API_KEY']},
                                    json={'query':job.question,'numResults':5,'contents':{'text':{'maxCharacters':self.settings.content_limit}}})
        return [self.record(source,job,'web_page',row['url'],row.get('title') or row['url'],row['text'])
                for row in result['results'] if row.get('text')]
