import { readFile, readdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import YAML from 'yaml';

import { configuredRouting } from '../routing/config.js';
import type { RoutingPolicy, RoutingRule } from '../routing/engine.js';

interface LegacyRoutingRule {
  document_type: string;
  department: string;
}

export interface OrgConfig {
  access?: {
    bootstrap_role: string;
    registration_role: string;
    role_permissions: Record<string, string[]>;
  };
  upload_defaults?: { department?: string; doc_type?: string; doc_date?: string };
  languages: string[];
  ocr_engine: string;
  document_types: string[];
  departments: string[];
  obligation_detection?: Record<
    string,
    {
      cues?: string[];
      date_patterns?: string[];
      relative_deadline_patterns?: string[];
      absolute_deadline_wrappers?: string[];
    }
  >;
  relation_detection?: {
    reference_cues?: Record<string, string[]>;
    change_cues?: Record<string, Partial<Record<'supersedes' | 'amends' | 'withdraws', string[]>>>;
    nonoperative_cues?: Record<string, string[]>;
    related_document_types?: Record<string, string[]>;
  };
  department_aliases?: Record<string, string[]>;
  roles?: Record<string, { department?: string; aliases?: string[] }>;
  routing_policy?: RoutingPolicy;
  routing_rules: (RoutingRule | LegacyRoutingRule)[];
}

export class OrgConfigError extends Error {}

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
