import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { loadOrgConfig } from '../src/config/loader.js';

const examplePath = resolve(import.meta.dirname, '../../../config/orgs/example.yaml');

describe('organization config loader', () => {
  it('accepts the shared example config', async () => {
    const config = await loadOrgConfig(examplePath);
    expect(config.languages).toEqual(['en', 'hi']);
    expect(Object.keys(config.summary?.department_profiles ?? {}).sort()).toEqual(
      [...config.departments].sort(),
    );
    expect(config.summary).toMatchObject({
      settings: { max_length: 600, style: 'bullets' },
      role_profiles: {
        finance_officer: { glossary: { en: { invoice: ['bill', 'supplier invoice'] } } },
      },
    });
  });

  it('accepts legacy org configs and rejects malformed summary profiles', async () => {
    const directory = await mkdtemp(resolve(tmpdir(), 'intellidocs-config-'));
    const path = resolve(directory, 'summary.yaml');
    const legacy = {
      languages: ['en'],
      ocr_engine: 'test',
      document_types: ['agreement'],
      departments: ['legal'],
      routing_rules: [],
    };
    try {
      await writeFile(path, JSON.stringify(legacy));
      await expect(loadOrgConfig(path)).resolves.toEqual(legacy);
      await writeFile(
        path,
        JSON.stringify({
          ...legacy,
          summary: {
            settings: { max_length: 100, style: 'short_paragraph' },
            department_profiles: {
              legal: {
                focus_topics: ['Liability'],
                responsibilities: [],
                glossary: { en: { liability: 'risk' } },
              },
            },
          },
        }),
      );
      await expect(loadOrgConfig(path)).rejects.toThrow('Invalid organization config');
    } finally {
      await rm(directory, { recursive: true });
    }
  });

  it('rejects a config that violates the contract', async () => {
    const directory = await mkdtemp(resolve(tmpdir(), 'intellidocs-config-'));
    const brokenPath = resolve(directory, 'broken.yaml');
    await writeFile(brokenPath, 'languages: []\nocr_engine: test\n', 'utf8');

    await expect(loadOrgConfig(brokenPath)).rejects.toThrow('Invalid organization config');
  });

  it('validates explicit upload defaults including calendar dates', async () => {
    const directory = await mkdtemp(resolve(tmpdir(), 'intellidocs-config-'));
    const path = resolve(directory, 'defaults.yaml');
    const example = JSON.parse(await readFile(examplePath, 'utf8')) as Record<string, unknown>;
    try {
      await writeFile(
        path,
        JSON.stringify({ ...example, upload_defaults: { doc_date: '2026-11-15' } }),
      );
      await expect(loadOrgConfig(path)).resolves.toMatchObject({
        upload_defaults: { doc_date: '2026-11-15' },
      });
      await writeFile(
        path,
        JSON.stringify({ ...example, upload_defaults: { doc_date: '2026-02-30' } }),
      );
      await expect(loadOrgConfig(path)).rejects.toThrow('Invalid organization config');
    } finally {
      await rm(directory, { recursive: true });
    }
  });
});
