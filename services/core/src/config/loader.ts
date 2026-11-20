import { readFile, readdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import YAML from 'yaml';
import type { components } from '../../../../contracts/intelligence.openapi.v1.js';

import { configuredRouting } from '../routing/config.js';

export type OrgConfig = components['schemas']['org-config.v1.schema'];

export class OrgConfigError extends Error {}

const moduleDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(moduleDirectory, '../../../../');
const schemaDirectory = resolve(repositoryRoot, 'contracts/schemas');
const defaultOrgConfigDirectory = resolve(repositoryRoot, 'config/orgs');

async function validator() {
  const readSchema = async (filename: string) =>
    JSON.parse(await readFile(resolve(schemaDirectory, filename), 'utf8')) as object;
  const [schema, metadata, profile] = await Promise.all([
    readSchema('org-config.v1.schema.json'),
    readSchema('upload-metadata.v1.schema.json'),
    readSchema('summary-profile.v1.schema.json'),
  ]);
  const ajv = new Ajv2020({ allErrors: true, strict: true });
  addFormats.default(ajv);
  ajv.addSchema([metadata, profile]);
  return ajv.compile<OrgConfig>(schema);
}

export async function loadOrgConfig(path: string): Promise<OrgConfig> {
  const contents = await readFile(path, 'utf8');
  let candidate: unknown;
  try {
    candidate = YAML.parse(contents);
  } catch (error) {
    const detail = error instanceof Error ? error.message : 'Invalid YAML';
    throw new OrgConfigError('Invalid organization config ' + path + ': ' + detail);
  }
  const validate = await validator();
  if (!validate(candidate)) {
    throw new OrgConfigError(
      `Invalid organization config ${path}: ${JSON.stringify(validate.errors)}`,
    );
  }
  try {
    configuredRouting(candidate);
  } catch (error) {
    const detail = error instanceof Error ? error.message : 'Invalid routing configuration';
    throw new OrgConfigError('Invalid organization config ' + path + ': ' + detail);
  }
  return candidate;
}

export async function loadOrgConfigs(directory = defaultOrgConfigDirectory): Promise<OrgConfig[]> {
  return (await loadNamedOrgConfigs(directory)).map(({ config }) => config);
}

export async function loadNamedOrgConfigs(directory = defaultOrgConfigDirectory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const configPaths = entries
    .filter((entry) => entry.isFile() && /\.ya?ml$/i.test(entry.name))
    .map((entry) => entry.name)
    .sort();
  return Promise.all(
    configPaths.map(async (name) => ({
      slug: name.replace(/\.ya?ml$/i, ''),
      config: await loadOrgConfig(resolve(directory, name)),
    })),
  );
}
