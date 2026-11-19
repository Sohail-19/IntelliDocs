import { readFileSync } from 'node:fs';

import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { describe, expect, expectTypeOf, it } from 'vitest';

import { IntelligenceClient } from '../src/intelligence/client.js';
import type { ExtractionResult, Obligation } from '../src/intelligence/client.js';

const ajv = new Ajv2020({ strict: true });
addFormats.default(ajv);
for (const name of [
  'citation',
  'obligation-owner',
  'obligation-deadline',
  'obligation',
  'extraction-stats',
  'extraction-result',
]) {
  ajv.addSchema(
    JSON.parse(
      readFileSync(
        new URL(`../../../contracts/schemas/${name}.v1.schema.json`, import.meta.url),
        'utf8',
      ),
    ) as object,
  );
}
const validate = ajv.compile<ExtractionResult>({ $ref: 'extraction-result.v1.schema.json' });

function example(name: string): unknown {
  return JSON.parse(
    readFileSync(new URL(`../../../contracts/examples/${name}.v1.json`, import.meta.url), 'utf8'),
  ) as unknown;
}

describe('extraction contracts', () => {
  it('accepts enriched obligations and returns the typed ExtractionResult from the client', async () => {
    const value = example('extraction-result');
    expect(validate(value)).toBe(true);
    const client = new IntelligenceClient('https://intelligence.example', () =>
      Promise.resolve(new Response(JSON.stringify(value), { status: 200 })),
    );
    const result = await client.extractObligations({ doc_id: 'doc-001' });
    expectTypeOf(result).toEqualTypeOf<ExtractionResult>();
    expect(result).toEqual(value);
  });

  it('keeps the generated structured branch usable with nullable resolution fields', () => {
    const obligation: Obligation = {
      id: 'obl-test',
      doc_id: 'doc-test',
      action: 'Submit the report',
      owner: {
        raw_text: 'the recipient',
        resolved_department: null,
        resolved_role: null,
        resolved: false,
      },
      deadline: {
        raw_text: 'within five days',
        normalized_date: null,
        anchor_date: null,
        is_relative: true,
        resolved: false,
      },
      citations: [],
      confidence: 0.5,
      status: 'needs_review',
      obligation_type: 'reporting',
      extractor_version: 'test-v1',
    };
    expect(ajv.validate('obligation.v1.schema.json', obligation)).toBe(true);
    const legacy = example('extract-obligations-response') as { obligations: unknown[] };
    expect(ajv.validate('obligation.v1.schema.json', legacy.obligations[0])).toBe(true);
  });

  it('rejects malformed dates and mixed legacy/structured representations', () => {
    const value = example('extraction-result') as ExtractionResult;
    const obligation = value.obligations[0];
    if (!obligation || typeof obligation.deadline === 'string')
      throw new Error('Expected structured deadline');
    obligation.deadline.normalized_date = '2026-02-30';
    expect(validate(value)).toBe(false);
    const mixed = { ...(example('obligation') as object), owner: 'legacy owner' };
    expect(ajv.validate('obligation.v1.schema.json', mixed)).toBe(false);
  });
});
