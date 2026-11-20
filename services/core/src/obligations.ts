import type { Pool, PoolClient } from 'pg';
import type { Citation } from '../../../contracts/core-api.v1.js';

import { inTransaction, writeAudit } from './audit.js';
import type { AuditContext } from './audit.js';
import type {
  ExtractionResult,
  Obligation,
  ObligationDeadline,
  ObligationOwner,
} from './intelligence/client.js';
import { effectiveDeadline, effectiveOwner, normalizedDeadline } from './obligation-values.js';
import {
  effectiveAction,
  effectiveCitations,
  flagStaleness,
  hasPendingStaleness,
  pendingStale,
  pendingStaleIds,
  readConfirmedChanges,
} from './relations/staleness.js';
import { changeAssignment } from './routing/assignments.js';
import type { Assignment } from './routing/assignments.js';
import type { PostgresRoutingService } from './routing/service.js';

export const extractionStatuses = ['extracted', 'verified', 'needs_review', 'rejected'] as const;
type ObligationStatus = (typeof extractionStatuses)[number];
export type ExtractedObligation = Extract<Obligation, { status: ObligationStatus }>;
export type ReviewDecision = 'approved' | 'rejected';
export type StoredObligation = ExtractedObligation & {
  stale?: boolean;
  stale_relation_ids?: string[];
  assigned_user_id?: number | null;
  assignment?: Assignment | null;
  task_status?: string;
  extracted_owner?: ObligationOwner;
  extracted_deadline?: ObligationDeadline;
  extracted_status?: ObligationStatus;
  owner_override?: ObligationOwner | null;
  deadline_override?: ObligationDeadline | null;
  review_decision?: ReviewDecision | null;
  review_reason?: string | null;
};
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

export type ObligationMutation = { status: ObligationStatus } | { assignedUserId: number | null };
type MutationOutcome =
  'updated' | 'missing' | 'invalid_assignee' | 'missing_citation' | 'use_review' | 'stale';
type ObligationSnapshot = { id: string | null; status: string; assigned_user_id: number | null };
type ExtractionSnapshot = {
  extraction_status: DocumentExtractionState['extraction_status'];
  extraction_error: string | null;
  obligations: ObligationSnapshot[];
};

export interface ObligationRepository {
  documentState(docId: string, orgId: number): Promise<DocumentExtractionState | undefined>;
  replace(result: DocumentExtractionResult, audit: AuditContext): Promise<void>;
  recordFailure(docId: string, audit: AuditContext): Promise<void>;
  mutate(
    docId: string,
    id: string,
    change: ObligationMutation,
    audit: AuditContext,
  ): Promise<MutationOutcome>;
  list(filters: ObligationFilters, orgId: number): Promise<StoredObligation[]>;
}

export const extractionFailure = 'Obligation extraction failed';

export class PostgresObligationRepository implements ObligationRepository {
  constructor(
    private readonly pool: Pool,
    private readonly routing?: PostgresRoutingService,
  ) {}

  async documentState(docId: string, orgId: number): Promise<DocumentExtractionState | undefined> {
    const { rows } = await this.pool.query<DocumentExtractionState>(
      'SELECT status, extraction_status, extraction_error FROM documents WHERE external_id = $1 AND org_id = $2',
      [docId, orgId],
    );
    return rows[0];
  }

  async replace(result: DocumentExtractionResult, audit: AuditContext): Promise<void> {
    const remaining = new Set(result.obligations.map((item) => item.id));
    if (remaining.size !== result.obligations.length) throw new Error('Duplicate obligation IDs');
    await inTransaction(this.pool, async (connection) => {
      const { id, before, stored, docType, orgSlug } = await this.extractionSnapshot(
        connection,
        result.doc_id,
        audit.orgId,
      );
      const changes = await readConfirmedChanges(connection, audit.orgId, [result.doc_id]);
      for (const item of stored) {
        if (item.id !== null && remaining.has(item.id)) continue;
        await changeAssignment(connection, item.row_id, null);
        await connection.query(
          'UPDATE obligations SET retired_at = CURRENT_TIMESTAMP WHERE id = $1',
          [item.row_id],
        );
      }
      const applied: ObligationSnapshot[] = [];
      for (const item of result.obligations) {
        const { rows } = await connection.query<{
          id: number;
          assigned_user_id: number | null;
          owner_override: ObligationOwner | null;
          deadline_override: ObligationDeadline | null;
          review_decision: ReviewDecision | null;
          task_status: string;
          stale_action_override: string | null;
          stale_citations_override: Citation[] | null;
        }>(
          'INSERT INTO obligations (document_id, external_id, action, owner, owner_details, deadline, condition, obligation_type, citations, confidence, status, extracted_status, extractor_version, confidence_breakdown) ' +
            'VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9::jsonb, $10, $11::text, $11::text, $12, $13::jsonb) ' +
            'ON CONFLICT (document_id, external_id) DO UPDATE SET action = EXCLUDED.action, owner = EXCLUDED.owner, ' +
            'owner_details = EXCLUDED.owner_details, deadline = EXCLUDED.deadline, condition = EXCLUDED.condition, ' +
            'obligation_type = EXCLUDED.obligation_type, citations = EXCLUDED.citations, confidence = EXCLUDED.confidence, ' +
            'confidence_breakdown = EXCLUDED.confidence_breakdown, ' +
            'status = EXCLUDED.status, extracted_status = EXCLUDED.extracted_status, extractor_version = EXCLUDED.extractor_version, ' +
            'retired_at = NULL, updated_at = CURRENT_TIMESTAMP ' +
            'RETURNING id, assigned_user_id, owner_override, deadline_override, review_decision, ' +
            'task_status, stale_action_override, stale_citations_override',
          [
            id,
            item.id,
            item.action,
            item.owner.raw_text,
            JSON.stringify(item.owner),
            JSON.stringify(item.deadline),
            item.condition ?? null,
            item.obligation_type,
            JSON.stringify(item.citations),
            item.confidence,
            item.status,
            item.extractor_version,
            item.confidence_breakdown === undefined
              ? null
              : JSON.stringify(item.confidence_breakdown),
          ],
        );
        const row = rows[0];
        if (!row) throw new Error('Obligation upsert returned no row');
        let assigned = row.assigned_user_id;
        const citations = row.stale_citations_override ?? item.citations;
        const decision =
          row.review_decision === 'approved' && !citations.length ? null : row.review_decision;
        let status = item.status;
        if (decision === 'approved') status = 'verified';
        else if (decision === 'rejected') status = 'rejected';
        const action = row.stale_action_override ?? item.action;
        await flagStaleness(
          connection,
          {
            id: row.id,
            doc_id: result.doc_id,
            obligation_id: item.id,
            task_status: row.task_status,
            status,
            action,
            citations,
          },
          changes,
          audit,
        );
        if (this.routing && decision !== 'rejected') {
          const owner = row.owner_override ?? item.owner;
          const deadline = row.deadline_override ?? item.deadline;
          const held =
            !decision && (!citations.length || row.owner_override || row.deadline_override);
          const assignment = await this.routing.assign(
            connection,
            {
              rowId: row.id,
              externalId: item.id,
              docId: result.doc_id,
              orgSlug,
              documentType: docType,
              obligationType: item.obligation_type,
              ownerDepartment: owner.resolved_department,
              ownerResolved: owner.resolved,
              action,
              condition: item.condition ?? null,
              deadline: deadline.resolved ? deadline.normalized_date : null,
              confidence: item.confidence,
              status: held ? 'needs_review' : status,
              humanApproved: decision === 'approved',
            },
            audit,
            true,
          );
          if (assignment) {
            assigned = assignment.assignee_user_id;
            if (
              !decision &&
              (assignment.reason === 'review' || assignment.reason === 'unavailable_user')
            ) {
              status = 'needs_review';
            }
          }
        }
        if (status !== item.status || decision !== row.review_decision) {
          await connection.query(
            'UPDATE obligations SET status = $2, review_decision = $3 WHERE id = $1',
            [row.id, status, decision],
          );
        }
        applied.push({ id: item.id, status, assigned_user_id: assigned });
      }
      await this.setExtractionState(connection, id, 'completed');
      await writeAudit(connection, audit, {
        action: 'obligations.extraction',
        entityType: 'document',
        entityId: result.doc_id,
        before,
        after: {
          extraction_status: 'completed',
          extraction_error: null,
          obligations: applied,
          stats: result.stats,
        },
      });
    });
  }

  async recordFailure(docId: string, audit: AuditContext): Promise<void> {
    await inTransaction(this.pool, async (connection) => {
      const { id, before } = await this.extractionSnapshot(connection, docId, audit.orgId);
      await this.setExtractionState(connection, id, 'failed');
      await writeAudit(connection, audit, {
        action: 'obligations.extraction',
        entityType: 'document',
        entityId: docId,
        before,
        after: { ...before, extraction_status: 'failed', extraction_error: extractionFailure },
      });
    });
  }

  async mutate(
    docId: string,
    id: string,
    change: ObligationMutation,
    audit: AuditContext,
  ): Promise<MutationOutcome> {
    return inTransaction(this.pool, async (client) => {
      const { rows } = await client.query<{
        id: number;
        status: string;
        assigned_user_id: number | null;
        has_citation: boolean;
        org_slug: string;
        review_decision: ReviewDecision | null;
      }>(
        'SELECT o.id, o.status, o.assigned_user_id, o.review_decision, org.slug AS org_slug, jsonb_array_length(o.citations) > 0 AS has_citation ' +
          'FROM obligations o JOIN documents d ON d.id = o.document_id JOIN organizations org ON org.id = d.org_id ' +
          'WHERE d.external_id = $1 AND o.external_id = $2 AND d.org_id = $3 AND o.retired_at IS NULL FOR UPDATE OF o',
        [docId, id, audit.orgId],
      );
      const target = rows[0];
      if (!target) return 'missing';
      if (await hasPendingStaleness(client, target.id)) return 'stale';
      const routingEnabled = this.routing?.enabled(target.org_slug);
      if (
        ('status' in change &&
          (target.review_decision ||
            (routingEnabled &&
              (target.status === 'needs_review' || change.status !== 'needs_review')))) ||
        ('assignedUserId' in change &&
          (target.review_decision === 'rejected' ||
            (target.status === 'needs_review' && routingEnabled)))
      ) {
        return 'use_review';
      }
      if ('status' in change) {
        if (change.status === 'verified' && !target.has_citation) return 'missing_citation';
        await client.query(
          'UPDATE obligations SET status = $2, updated_at = CURRENT_TIMESTAMP WHERE id = $1',
          [target.id, change.status],
        );
        await writeAudit(client, audit, {
          action: 'obligation.status',
          entityType: 'obligation',
          entityId: id,
          before: { doc_id: docId, status: target.status },
          after: { doc_id: docId, status: change.status },
        });
        if (routingEnabled) await this.routing?.routeStored(client, docId, id, audit);
      } else {
        if (change.assignedUserId !== null) {
          const user = await client.query(
            'SELECT id FROM users WHERE id = $1 AND org_id = $2 AND active FOR SHARE',
            [change.assignedUserId, audit.orgId],
          );
          if (user.rows.length === 0) return 'invalid_assignee';
        }
        const { before, after } = await changeAssignment(
          client,
          target.id,
          change.assignedUserId === null
            ? null
            : {
                assignee_user_id: change.assignedUserId,
                queue_type: null,
                queue_name: null,
                rule_id: null,
                escalation_due_at: null,
                reason: 'manual',
              },
        );
        await writeAudit(client, audit, {
          action: 'obligation.assignment',
          entityType: 'obligation',
          entityId: id,
          before: { doc_id: docId, assigned_user_id: target.assigned_user_id, assignment: before },
          after: { doc_id: docId, assigned_user_id: change.assignedUserId, assignment: after },
        });
      }
      return 'updated';
    });
  }

  async list(filters: ObligationFilters, orgId: number): Promise<StoredObligation[]> {
    return listStoredObligations(this.pool, filters, orgId);
  }

  private async extractionSnapshot(
    client: PoolClient,
    docId: string,
    orgId: number,
  ): Promise<{
    id: number;
    before: ExtractionSnapshot;
    docType: string | null;
    orgSlug: string;
    stored: (ObligationSnapshot & { row_id: number })[];
  }> {
    const { rows } = await client.query<
      Omit<ExtractionSnapshot, 'obligations'> & {
        id: number;
        doc_type: string | null;
        slug: string;
      }
    >(
      'SELECT d.id, d.extraction_status, d.extraction_error, d.doc_type, org.slug FROM documents d ' +
        'JOIN organizations org ON org.id = d.org_id ' +
        "WHERE d.external_id = $1 AND d.org_id = $2 AND d.status = 'indexed' FOR UPDATE OF d",
      [docId, orgId],
    );
    const document = rows[0];
    if (!document) throw new Error('Indexed document not found');
    const { rows: stored } = await client.query<ObligationSnapshot & { row_id: number }>(
      'SELECT id AS row_id, external_id AS id, status, assigned_user_id FROM obligations ' +
        'WHERE document_id = $1 AND retired_at IS NULL ORDER BY row_id FOR UPDATE',
      [document.id],
    );
    const obligations = stored.map(({ id, status, assigned_user_id }) => ({
      id,
      status,
      assigned_user_id,
    }));
    return {
      id: document.id,
      docType: document.doc_type,
      orgSlug: document.slug,
      stored,
      before: {
        extraction_status: document.extraction_status,
        extraction_error: document.extraction_error,
        obligations,
      },
    };
  }

  private async setExtractionState(
    client: PoolClient,
    id: number,
    status: 'completed' | 'failed',
  ): Promise<void> {
    await client.query(
      'UPDATE documents SET extraction_status = $2, extraction_error = $3 WHERE id = $1',
      [id, status, status === 'failed' ? extractionFailure : null],
    );
  }
}

export async function listStoredObligations(
  client: Pool | PoolClient,
  filters: ObligationFilters,
  orgId: number,
  obligationId?: string,
): Promise<StoredObligation[]> {
  const clauses = ['o.external_id IS NOT NULL', 'o.retired_at IS NULL', 'd.org_id = $1'];
  const values: (string | number)[] = [orgId];
  for (const [value, expression] of [
    [obligationId, 'o.external_id ='],
    [filters.doc_id, 'd.external_id ='],
    [filters.status, 'o.status ='],
    [filters.owner_department, effectiveOwner + "->>'resolved_department' ="],
    [filters.due_before, normalizedDeadline + ' <'],
    [filters.due_after, normalizedDeadline + ' >'],
  ] as const) {
    if (value === undefined) continue;
    values.push(value);
    clauses.push(expression + ' $' + values.length);
  }
  const { rows } = await client.query<{ obligation: StoredObligation }>(
    "SELECT jsonb_strip_nulls(jsonb_build_object('condition', o.condition, 'confidence_breakdown', o.confidence_breakdown)) || jsonb_build_object(" +
      "'id', o.external_id, 'doc_id', d.external_id, 'action', " +
      effectiveAction +
      ", 'owner', " +
      effectiveOwner +
      ', ' +
      "'deadline', " +
      effectiveDeadline +
      ", 'obligation_type', o.obligation_type, 'citations', " +
      effectiveCitations +
      ", 'stale', " +
      pendingStale +
      ", 'stale_relation_ids', " +
      pendingStaleIds +
      ', ' +
      "'confidence', o.confidence, 'status', o.status, 'extractor_version', o.extractor_version, " +
      "'extracted_owner', o.owner_details, 'extracted_deadline', o.deadline, 'extracted_status', COALESCE(o.extracted_status, o.status), " +
      "'owner_override', o.owner_override, 'deadline_override', o.deadline_override, " +
      "'review_decision', o.review_decision, 'review_reason', o.review_reason, " +
      "'task_status', o.task_status, " +
      "'assigned_user_id', o.assigned_user_id, 'assignment', " +
      "(SELECT to_jsonb(a) - 'obligation_id' FROM assignments a WHERE a.obligation_id = o.id AND a.status = 'active')" +
      ') AS obligation FROM obligations o JOIN documents d ON d.id = o.document_id WHERE ' +
      clauses.join(' AND ') +
      ' ORDER BY ' +
      normalizedDeadline +
      ' NULLS LAST, d.external_id, o.external_id',
    values,
  );
  return rows.map((row) => row.obligation);
}
