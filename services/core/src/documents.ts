import type { Pool } from 'pg';

import { inTransaction, writeAudit } from './audit.js';
import type { AuditContext } from './audit.js';
import type { IngestResponse, ParseResult } from './intelligence/client.js';

type IndexState = {
  status: 'parsed' | 'indexed' | 'failed';
  index_error: string | null;
  indexed_at: Date | null;
};

export interface DocumentRepository {
  save(response: IngestResponse, audit: AuditContext): Promise<void>;
  setStatus(
    docId: string,
    audit: AuditContext,
    status: IndexState['status'],
    error?: string,
  ): Promise<void>;
}

export class PostgresDocumentRepository implements DocumentRepository {
  constructor(private readonly pool: Pool) {}

  async save(response: IngestResponse, audit: AuditContext): Promise<void> {
    await inTransaction(this.pool, async (connection) => {
      const insert = async (
        result: ParseResult,
        parentId: number | null,
        parentExternalId: string | null,
      ): Promise<void> => {
        const document = result.document;
        const rows = await connection.query<{ id: number }>(
          'INSERT INTO documents (external_id, filename, status, parent_document_id, content_hash, org_id, doc_type) ' +
            'VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id',
          [
            document.id,
            document.filename,
            'parsed',
            parentId,
            document.content_hash ?? null,
            audit.orgId,
            document.doc_type ?? null,
          ],
        );
        const id = rows.rows[0]?.id;
        if (id === undefined) throw new Error('Document insert returned no ID');
        await writeAudit(connection, audit, {
          action: 'document.upload',
          entityType: 'document',
          entityId: document.id,
          before: null,
          after: {
            status: 'parsed',
            parent_document_id: parentExternalId,
            content_hash: document.content_hash ?? null,
          },
        });
        for (const attachment of result.attachments ?? [])
          await insert(attachment, id, document.id);
      };
      await insert(response.parse_result, null, null);
    });
  }

  async setStatus(
    docId: string,
    audit: AuditContext,
    status: IndexState['status'],
    error?: string,
  ): Promise<void> {
    await inTransaction(this.pool, async (connection) => {
      const { rows: previous } = await connection.query<IndexState>(
        'SELECT status, index_error, indexed_at FROM documents WHERE external_id = $1 AND org_id = $2 FOR UPDATE',
        [docId, audit.orgId],
      );
      const before = previous[0];
      if (!before) throw new Error('Document status update found no matching row');
      const { rows } = await connection.query<IndexState>(
        'UPDATE documents SET status = $2::text, index_error = $3, ' +
          "indexed_at = CASE WHEN $2::text = 'indexed' THEN CURRENT_TIMESTAMP ELSE NULL END " +
          'WHERE external_id = $1 AND org_id = $4 RETURNING status, index_error, indexed_at',
        [docId, status, error ?? null, audit.orgId],
      );
      const after = rows[0];
      if (!after) throw new Error('Document status update failed');
      await writeAudit(connection, audit, {
        action: status === 'parsed' ? 'document.status' : 'document.index',
        entityType: 'document',
        entityId: docId,
        before,
        after,
      });
    });
  }
}
