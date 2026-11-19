import type { Pool, PoolClient } from 'pg';

import type { ExtractionResult, Obligation } from './intelligence/client.js';

export const extractionStatuses = ['extracted', 'verified', 'needs_review', 'rejected'] as const;
type ObligationStatus = (typeof extractionStatuses)[number];
export type ExtractedObligation = Extract<Obligation, { status: ObligationStatus }>;
export type DocumentExtractionResult = Omit<ExtractionResult, 'obligations'> & {
  obligations: ExtractedObligation[];
};
export type DocumentExtractionState = {
  status: 'parsed' | 'indexed' | 'failed';
  extraction_status: 'pending' | 'completed' | 'failed';
  extraction_error: string | null;
};

export interface ObligationFilters {
  doc_id?: string;
  status?: ObligationStatus;
  owner_department?: string;
  due_before?: string;
  due_after?: string;
}

export interface ObligationRepository {
  documentState(docId: string): Promise<DocumentExtractionState | undefined>;
  replace(result: DocumentExtractionResult): Promise<void>;
  recordFailure(docId: string): Promise<void>;
  list(filters: ObligationFilters): Promise<ExtractedObligation[]>;
}

export const extractionFailure = 'Obligation extraction failed';

export class PostgresObligationRepository implements ObligationRepository {
  constructor(private readonly pool: Pool) {}

  async documentState(docId: string): Promise<DocumentExtractionState | undefined> {
    const { rows } = await this.pool.query<DocumentExtractionState>(
      'SELECT status, extraction_status, extraction_error FROM documents WHERE external_id = $1',
      [docId],
    );
    return rows[0];
  }

  async replace(result: DocumentExtractionResult): Promise<void> {
    await this.transaction(async (connection) => {
      const documentId = await this.setExtractionState(connection, result.doc_id, 'completed');
      await connection.query('DELETE FROM obligations WHERE document_id = $1', [documentId]);
      for (const obligation of result.obligations) {
        await connection.query(
          'INSERT INTO obligations (document_id, external_id, action, owner, owner_details, deadline, condition, obligation_type, citations, confidence, status, extractor_version) ' +
            'VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9::jsonb, $10, $11, $12)',
          [
            documentId,
            obligation.id,
            obligation.action,
            obligation.owner.raw_text,
            JSON.stringify(obligation.owner),
            JSON.stringify(obligation.deadline),
            obligation.condition ?? null,
            obligation.obligation_type,
            JSON.stringify(obligation.citations),
            obligation.confidence,
            obligation.status,
            obligation.extractor_version,
          ],
        );
      }
      await this.audit(connection, {
        doc_id: result.doc_id,
        status: 'completed',
        stats: result.stats,
        warnings: result.warnings,
      });
    });
  }

  async recordFailure(docId: string): Promise<void> {
    await this.transaction(async (connection) => {
      await this.setExtractionState(connection, docId, 'failed');
      await this.audit(connection, { doc_id: docId, status: 'failed', error: extractionFailure });
    });
  }

  async list(filters: ObligationFilters): Promise<ExtractedObligation[]> {
    const clauses = ['o.external_id IS NOT NULL'];
    const values: string[] = [];
    for (const [value, expression] of [
      [filters.doc_id, 'd.external_id ='],
      [filters.status, 'o.status ='],
      [filters.owner_department, "o.owner_details->>'resolved_department' ="],
      [filters.due_before, "o.deadline->>'normalized_date' <"],
      [filters.due_after, "o.deadline->>'normalized_date' >"],
    ] as const) {
      if (value === undefined) continue;
      values.push(value);
      clauses.push(expression + ' $' + values.length);
    }
    const { rows } = await this.pool.query<{ obligation: ExtractedObligation }>(
      "SELECT jsonb_strip_nulls(jsonb_build_object('condition', o.condition)) || jsonb_build_object(" +
        "'id', o.external_id, 'doc_id', d.external_id, 'action', o.action, 'owner', o.owner_details, " +
        "'deadline', o.deadline, 'obligation_type', o.obligation_type, 'citations', o.citations, " +
        "'confidence', o.confidence, 'status', o.status, 'extractor_version', o.extractor_version" +
        ') AS obligation FROM obligations o JOIN documents d ON d.id = o.document_id WHERE ' +
        clauses.join(' AND ') +
        " ORDER BY o.deadline->>'normalized_date' NULLS LAST, d.external_id, o.external_id",
      values,
    );
    return rows.map((row) => row.obligation);
  }

  private async setExtractionState(
    connection: PoolClient,
    docId: string,
    status: 'completed' | 'failed',
  ): Promise<number> {
    // The update locks the document row until the snapshot and audit commit.
    const { rows } = await connection.query<{ id: number }>(
      'UPDATE documents SET extraction_status = $2, extraction_error = $3 ' +
        "WHERE external_id = $1 AND status = 'indexed' RETURNING id",
      [docId, status, status === 'failed' ? extractionFailure : null],
    );
    const id = rows[0]?.id;
    if (id === undefined) throw new Error('Indexed document not found');
    return id;
  }

  private async audit(connection: PoolClient, payload: object): Promise<void> {
    await connection.query('INSERT INTO audit_log (event_type, payload) VALUES ($1, $2::jsonb)', [
      'obligations.extraction',
      JSON.stringify(payload),
    ]);
  }

  private async transaction(run: (connection: PoolClient) => Promise<void>): Promise<void> {
    const connection = await this.pool.connect();
    try {
      await connection.query('BEGIN');
      await run(connection);
      await connection.query('COMMIT');
    } catch (error) {
      await connection.query('ROLLBACK');
      throw error;
    } finally {
      connection.release();
    }
  }
}
