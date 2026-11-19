import type { Pool } from 'pg';
import type { DocumentRecord } from '../../../contracts/core-api.v1.js';

import { inTransaction, writeAudit } from './audit.js';
import type { AuditContext } from './audit.js';
import type { IngestResponse, ParseResult } from './intelligence/client.js';
import { documentAuthorityWarnings, readConfirmedChanges } from './relations/staleness.js';

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

export interface DocumentReader {
  list(orgId: number): Promise<DocumentRecord[]>;
  get(orgId: number, id: string): Promise<DocumentRecord | undefined>;
}

type DocumentRow = Omit<DocumentRecord, 'created_at' | 'indexed_at'> & {
  created_at: Date;
  indexed_at: Date | null;
};

export class PostgresDocumentRepository implements DocumentRepository, DocumentReader {
  constructor(private readonly pool: Pool) {}

  async save(response: IngestResponse, audit: AuditContext): Promise<void> {
    await inTransaction(this.pool, async (connection) => {
      const insert = async (
        result: ParseResult,
        parentId: number | null,
        parentExternalId: string | null,
      ): Promise<void> => {
        const document = result.document;
        const languages = new Set(document.pages.map(({ language }) => language));
        if (document.language) languages.add(document.language);
        const rows = await connection.query<{ id: number }>(
          'INSERT INTO documents (external_id, filename, status, parent_document_id, content_hash, org_id, doc_type, department, doc_date, mime_type, languages, page_count) ' +
            'VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) RETURNING id',
          [
            document.id,
            document.filename,
            'parsed',
            parentId,
            document.content_hash ?? null,
            audit.orgId,
            document.doc_type ?? null,
            document.department ?? null,
            document.doc_date ?? null,
            document.mime_type,
            [...languages].sort(),
            document.pages.length,
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

  list(orgId: number): Promise<DocumentRecord[]> {
    return this.read(orgId);
  }

  async get(orgId: number, id: string): Promise<DocumentRecord | undefined> {
    const [document] = await this.read(orgId, id);
    if (
      !document ||
      document.authority_status === 'in_force' ||
      document.authority_status === 'under_review'
    )
      return document;
    return {
      ...document,
      authority_warnings: documentAuthorityWarnings(
        await readConfirmedChanges(this.pool, orgId, [id]),
      ),
    };
  }

  private async read(orgId: number, id?: string): Promise<DocumentRecord[]> {
    const { rows } = await this.pool.query<DocumentRow>(
      'SELECT d.external_id AS id, d.filename, d.department, d.doc_type, d.doc_date::text, d.mime_type, d.languages, ' +
        'd.page_count, d.status, d.authority_status, d.extraction_status, d.index_error, d.extraction_error, d.created_at, d.indexed_at, ' +
        'p.external_id AS parent_document_id FROM documents d ' +
        'LEFT JOIN documents p ON p.id = d.parent_document_id AND p.org_id = d.org_id ' +
        'WHERE d.org_id = $1 ' +
        (id === undefined ? 'ORDER BY d.created_at DESC, d.id DESC' : 'AND d.external_id = $2'),
      id === undefined ? [orgId] : [orgId, id],
    );
    return rows.map((row) => ({
      ...row,
      created_at: row.created_at.toISOString(),
      indexed_at: row.indexed_at?.toISOString() ?? null,
    }));
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
