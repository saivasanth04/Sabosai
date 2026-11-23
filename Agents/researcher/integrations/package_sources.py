"""Authoritative package metadata and deps.dev ecosystem data."""
from urllib.parse import quote

from .mcp_gateway import IntegrationError


async def package_metadata(call, endpoints, source, job):
    if not job.package_name or not job.package_ecosystem:
        raise IntegrationError('Package name and ecosystem are required')
    name = quote(job.package_name, safe='')
    base = str(endpoints[source]).rstrip('/')
    if source == 'pypi':
        url = f'{base}/{name}/json'
        result = await call('GET', url)
        info = result['info']
        data = {key:info.get(key) for key in ('name','version','summary','license','license_expression','project_urls','requires_python')}
        data['released_at'] = next((item.get('upload_time_iso_8601') for item in result['urls']), None)
    elif source == 'npm':
        url = f'{base}/{name}/latest'
        info = await call('GET', url)
        data = {key:info.get(key) for key in ('name','version','description','license','repository','dependencies')}
    else:
        url = f'{base}/{job.package_ecosystem.upper()}/packages/{name}'
        info = await call('GET', url)
        latest = next((row for row in info['versions'] if row.get('isDefault')), {})
        data = {'name':job.package_name, 'version':latest.get('versionKey',{}).get('version'),
                'released_at':latest.get('publishedAt'), 'versions_indexed':len(info['versions'])}
    return url, data
