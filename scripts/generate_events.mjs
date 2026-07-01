// Generate both TypeScript and CSP-safe runtime validation from the protocol contract.
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../packages/api-client/package.json', import.meta.url));
const Ajv = require('ajv/dist/2020.js');
const addFormats = require('ajv-formats');
const standalone = require('ajv/dist/standalone');
const root = new URL('../', import.meta.url);
const read = path => JSON.parse(readFileSync(new URL(path, root), 'utf8'));
const event = read('contracts/events/run-event.schema.json');
const answer = read('contracts/schemas/answer.schema.json');
const ajv = new Ajv({ code: { source: true }, strict: true, allErrors: false });
addFormats(ajv);
ajv.addSchema(answer);
const validate = ajv.compile({ ...event, $id: 'https://evidencedesk.invalid/events/run-event.schema.json' });
writeFileSync(new URL('packages/api-client/validate-event.cjs', root), '// Generated. Do not edit.\n' + standalone(ajv, validate));
function type(schema) {
  if (schema.$ref) {
    if (schema.$ref.endsWith('/answer.schema.json')) return 'components["schemas"]["Answer"]';
    throw Error(`Unsupported reference ${schema.$ref}`);
  }
  if ('const' in schema) return JSON.stringify(schema.const);
  if (schema.enum) return schema.enum.map(x => JSON.stringify(x)).join(' | ');
  if (schema.oneOf || schema.anyOf) return (schema.oneOf || schema.anyOf).map(type).join(' | ');
  if (Array.isArray(schema.type)) return schema.type.map(t => type({ ...schema, type: t })).join(' | ');
  if (schema.type === 'object') return '{ ' + Object.entries(schema.properties || {}).map(([k, v]) => `${JSON.stringify(k)}${(schema.required || []).includes(k) ? '' : '?'}: ${type(v)};`).join(' ') + ' }';
  if (schema.type === 'array') return `Array<${type(schema.items)}>`;
  return ({ string: 'string', integer: 'number', number: 'number', boolean: 'boolean', null: 'null' })[schema.type] || 'unknown';
}
writeFileSync(new URL('packages/api-client/events.ts', root), '// Generated from contracts/events/run-event.schema.json.\nimport type { components } from "./schema";\nexport type RunEvent = ' + type(event) + ';\n');
writeFileSync(new URL('packages/api-client/validate-event.d.cts', root), 'import type { RunEvent } from "./events";\ndeclare function validate(value: unknown): value is RunEvent;\nexport = validate;\n');
console.log('Generated event types and standalone validator.');
