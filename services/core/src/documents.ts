import type { Pool } from 'pg';

import type { IngestResponse, ParseResult } from './intelligence/client.js';

export interface DocumentRepository {
  save(response: IngestResponse): Promise<void>;
  setStatus(docId: string, status: 'parsed' | 'indexed' | 'failed', error?: string): Promise<void>;
}

export class PostgresDocumentRepository implements DocumentRepository {
  constructor(private readonly pool: Pool) {}

  async save(response: IngestResponse): Promise<void> {
    const connection = await this.pool.connect();
    try {
      await connection.query('BEGIN');
      const insert = async (result: ParseResult, parentId: number | null): Promise<void> => {
        const document = result.document;
        const rows = await connection.query<{ id: number }>(
          `INSERT INTO documents (external_id, filename, status, parent_document_id, content_hash)
           VALUES ($1, $2, $3, $4, $5) RETURNING id`,
          [document.id, document.filename, 'parsed', parentId, document.content_hash ?? null],
        );
        const id = rows.rows[0]?.id;
        if (id === undefined) throw new Error('Document insert returned no ID');
        for (const attachment of result.attachments ?? []) await insert(attachment, id);
      };
      await insert(response.parse_result, null);
      await connection.query('COMMIT');
    } catch (error) {
      await connection.query('ROLLBACK');
      throw error;
    } finally {
      connection.release();
    }
  }

  async setStatus(
    docId: string,
    status: 'parsed' | 'indexed' | 'failed',
    error?: string,
  ): Promise<void> {
    const result = await this.pool.query(
      `UPDATE documents SET status = $2::text, index_error = $3,
       indexed_at = CASE WHEN $2::text = 'indexed' THEN CURRENT_TIMESTAMP ELSE NULL END
       WHERE external_id = $1`,
      [docId, status, error ?? null],
    );
    if (result.rowCount !== 1) throw new Error('Document status update found no matching row');
  }
}
