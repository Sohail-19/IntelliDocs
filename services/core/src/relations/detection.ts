import { readFileSync } from 'node:fs';

import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';

import type { AuditContext } from '../audit.js';
import type { IntelligenceClient, RelationResult } from '../intelligence/client.js';
import { llmProblem } from '../intelligence/client.js';
import type { PostgresRelationRepository } from './repository.js';

const ajv = new Ajv2020({ strict: true });
addFormats.default(ajv);
for (const name of ['citation', 'document-relation', 'relation-result']) {
  const path = new URL(`../../../../contracts/schemas/${name}.v1.schema.json`, import.meta.url);
  ajv.addSchema(JSON.parse(readFileSync(path, 'utf8')) as object);
}
const validate = ajv.compile<RelationResult>({ $ref: 'relation-result.v1.schema.json' });

export type DetectRelations = (id: string, audit: AuditContext) => Promise<void>;

export function runDetection(
  repository: PostgresRelationRepository,
  detect: IntelligenceClient['detectRelations'],
): DetectRelations {
  return async (id, audit) => {
    try {
      const result: unknown = await detect({
        doc_id: id,
        ...(audit.orgSlug && { org_id: audit.orgSlug }),
      });
      if (!validate(result) || result.doc_id !== id) throw new Error('Invalid relation response');
      const ids = new Set<string>();
      for (const item of result.relations) {
        if (
          ids.has(item.id) ||
          item.status !== 'proposed' ||
          item.source_doc_id !== id ||
          item.target_doc_id === id ||
          item.source_citations.some((citation) => citation.doc_id !== id) ||
          item.target_citations.some((citation) => citation.doc_id !== item.target_doc_id) ||
          (item.confidence_breakdown &&
            Math.abs(
              Object.values(item.confidence_breakdown).reduce((sum, value) => sum + value, 0) -
                item.confidence,
            ) > 1e-9)
        )
          throw new Error('Invalid relation evidence or confidence');
        ids.add(item.id);
      }
      await repository.store(result, audit);
    } catch (error) {
      if (!llmProblem(error)) await repository.recordFailure(id, audit);
      throw error;
    }
  };
}
