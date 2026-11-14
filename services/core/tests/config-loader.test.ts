import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { loadOrgConfig } from '../src/config/loader.js';

const examplePath = resolve(import.meta.dirname, '../../../config/orgs/example.yaml');

describe('organization config loader', () => {
  it('accepts the shared example config', async () => {
    await expect(loadOrgConfig(examplePath)).resolves.toMatchObject({ languages: ['en', 'hi'] });
  });

  it('rejects a config that violates the contract', async () => {
    const directory = await mkdtemp(resolve(tmpdir(), 'intellidocs-config-'));
    const brokenPath = resolve(directory, 'broken.yaml');
    await writeFile(brokenPath, 'languages: []\nocr_engine: test\n', 'utf8');

    await expect(loadOrgConfig(brokenPath)).rejects.toThrow('Invalid organization config');
  });
});
