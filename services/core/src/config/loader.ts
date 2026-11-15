import { readFile, readdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import YAML from 'yaml';

export interface RoutingRule {
  document_type: string;
  department: string;
}

export interface OrgConfig {
  upload_defaults?: { department?: string; doc_type?: string; doc_date?: string };
  languages: string[];
  ocr_engine: string;
  document_types: string[];
  departments: string[];
  routing_rules: RoutingRule[];
}

const moduleDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(moduleDirectory, '../../../../');
const orgConfigSchemaPath = resolve(repositoryRoot, 'contracts/schemas/org-config.v1.schema.json');
const defaultOrgConfigDirectory = resolve(repositoryRoot, 'config/orgs');

async function validator() {
  const schema = JSON.parse(await readFile(orgConfigSchemaPath, 'utf8')) as object;
  const metadata = JSON.parse(
    await readFile(
      resolve(repositoryRoot, 'contracts/schemas/upload-metadata.v1.schema.json'),
      'utf8',
    ),
  ) as object;
  const ajv = new Ajv2020({ allErrors: true, strict: true });
  addFormats.default(ajv);
  ajv.addSchema(metadata);
  return ajv.compile<OrgConfig>(schema);
}

export async function loadOrgConfig(path: string): Promise<OrgConfig> {
  const contents = await readFile(path, 'utf8');
  const candidate: unknown = YAML.parse(contents);
  const validate = await validator();
  if (!validate(candidate)) {
    throw new Error(`Invalid organization config ${path}: ${JSON.stringify(validate.errors)}`);
  }
  return candidate;
}

export async function loadOrgConfigs(directory = defaultOrgConfigDirectory): Promise<OrgConfig[]> {
  const entries = await readdir(directory, { withFileTypes: true });
  const configPaths = entries
    .filter((entry) => entry.isFile() && /\.ya?ml$/i.test(entry.name))
    .map((entry) => resolve(directory, entry.name))
    .sort();
  return Promise.all(configPaths.map(loadOrgConfig));
}
