import SwaggerParser from '@apidevtools/swagger-parser';
import yaml from 'js-yaml';
import { readFileSync } from 'node:fs';

const input = yaml.load(readFileSync(0, 'utf8'), {schema: yaml.JSON_SCHEMA});
const api = await SwaggerParser.validate(input, {resolve:{external:false,file:false,http:false}});
const methods = new Set(['get','put','post','delete','options','head','patch','trace']);
console.log(JSON.stringify({
  title:api.info.title, version:api.info.version, specification:api.openapi || api.swagger,
  endpoints:Object.entries(api.paths ?? {}).flatMap(([path,item]) =>
    Object.keys(item).filter(method => methods.has(method)).map(method => ({path,method}))),
  schemas:Object.keys(api.components?.schemas ?? api.definitions ?? {})
}));
