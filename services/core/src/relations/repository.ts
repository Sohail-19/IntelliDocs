import type { Pool, PoolClient } from 'pg';
import type {
  Citation,
  DocumentAuthorityStatus,
  RelationListStatus,
  StaleDecision,
  StoredDocumentRelation,
} from '../../../../contracts/core-api.v1.js';

import { inTransaction, writeAudit } from '../audit.js';
import type { AuditContext } from '../audit.js';
import type { DocumentRelation, RelationResult } from '../intelligence/client.js';
import { changeAssignment } from '../routing/assignments.js';
import {
  authorityWarnings,
  effectiveCitations,
  flagStaleness,
  isFullChange,
  openObligation,
  readConfirmedChanges,
  staleObligationFields,
} from './staleness.js';
import type { AuthorityChange, StaleObligation } from './staleness.js';

type RelationType = DocumentRelation['relation_type'];
export type RelationReview =
  | { action: 'type'; relationType: RelationType }
  | { action: 'confirm' }
  | { action: 'dismiss'; reason: string };

export class RelationError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

type RelationRow = Omit<
  StoredDocumentRelation,
  'created_at' | 'reviewed_at' | 'confidence_breakdown' | 'needs_review'
> & {
  source_filename: string;
  target_filename: string;
  source_date: string | null;
  target_date: string | null;
  created_at: Date;
  reviewed_at: Date | null;
  confidence_breakdown: DocumentRelation['confidence_breakdown'] | null;
  needs_review: boolean | null;
};

const relations =
  'SELECT r.id, r.relation_type, r.scope, r.source_citations, r.target_citations, r.confidence, ' +
  'r.confidence_breakdown, r.status, r.needs_review, r.detected_by, r.created_at, r.reviewed_by, r.reviewed_at, r.review_reason, ' +
  'source.external_id AS source_doc_id, target.external_id AS target_doc_id, ' +
  'source.filename AS source_filename, target.filename AS target_filename, ' +
  'source.doc_date::text AS source_date, target.doc_date::text AS target_date FROM document_relations r ' +
  'JOIN documents source ON source.id = r.source_id JOIN documents target ON target.id = r.target_id ';

function view({
  created_at,
  reviewed_at,
  confidence_breakdown,
  needs_review,
  ...row
}: RelationRow): StoredDocumentRelation {
  const {
    id,
    source_doc_id,
    target_doc_id,
    source_filename,
    target_filename,
    relation_type,
    scope,
    source_citations,
    target_citations,
    confidence,
    status,
    detected_by,
    reviewed_by,
    review_reason,
  } = row;
  return {
    id,
    source_doc_id,
    target_doc_id,
    source_filename,
    target_filename,
    relation_type,
    scope,
    source_citations,
    target_citations,
    confidence,
    status,
    detected_by,
    reviewed_by,
    review_reason,
    created_at: created_at.toISOString(),
    reviewed_at: reviewed_at?.toISOString() ?? null,
    ...(confidence_breakdown ? { confidence_breakdown } : {}),
    ...(needs_review === null ? {} : { needs_review }),
  };
}

async function lockOrganization(client: PoolClient, orgId: number) {
  // Serialize relation writes without blocking audit foreign-key checks on the org row.
  await client.query('SELECT pg_advisory_xact_lock(811, $1)', [orgId]);
}

async function relationById(client: Pool | PoolClient, orgId: number, id: string) {
  const { rows } = await client.query<RelationRow>(
    relations + 'WHERE r.org_id = $1 AND r.id = $2',
    [orgId, id],
  );
  const row = rows[0];
  if (!row) throw new RelationError(404, 'Relation not found');
  return row;
}

function changeFor(row: RelationRow): AuthorityChange | undefined {
  const { id, source_doc_id, target_doc_id, relation_type, scope, target_citations } = row;
  if (relation_type === 'references' || relation_type === 'conflicts_with') return;
  return { id, source_doc_id, target_doc_id, relation_type, scope, target_citations };
}

export class PostgresRelationRepository {
  constructor(private readonly pool: Pool) {}

  async list(
    orgId: number,
    status: RelationListStatus = 'proposed',
  ): Promise<StoredDocumentRelation[]> {
    const { rows } = await this.pool.query<RelationRow>(
      relations + 'WHERE r.org_id = $1 AND r.status = $2 ORDER BY r.created_at DESC, r.id',
      [orgId, status],
    );
    return rows.map(view);
  }

  async hasDocument(orgId: number, id: string): Promise<boolean> {
    const { rows } = await this.pool.query(
      'SELECT 1 FROM documents d JOIN document_relations r ON r.org_id = d.org_id ' +
        'AND (r.source_id = d.id OR r.target_id = d.id) WHERE d.org_id = $1 AND d.external_id = $2 ' +
        "AND r.status IN ('proposed', 'confirmed') LIMIT 1",
      [orgId, id],
    );
    return rows.length > 0;
  }

  async get(orgId: number, id: string): Promise<StoredDocumentRelation> {
    return view(await relationById(this.pool, orgId, id));
  }

  async store(result: RelationResult, audit: AuditContext) {
    await inTransaction(this.pool, async (client) => {
      await lockOrganization(client, audit.orgId);
      const { rows } = await client.query<{ id: number; external_id: string }>(
        'SELECT id, external_id FROM documents WHERE org_id = $1 AND external_id = ANY($2::text[])',
        [audit.orgId, [result.doc_id, ...result.relations.map((row) => row.target_doc_id)]],
      );
      const documents = new Map(rows.map((row) => [row.external_id, row.id]));
      const source = documents.get(result.doc_id);
      if (source === undefined) throw new RelationError(404, 'Document not found');
      const affected = new Set<string>();
      let inserted = 0;
      let ignored = 0;
      for (const relation of result.relations) {
        const target = documents.get(relation.target_doc_id);
        if (target === undefined) {
          ignored++;
          continue;
        }
        const saved = await client.query(
          'INSERT INTO document_relations (org_id,id,source_id,target_id,relation_type,scope,source_citations,' +
            'target_citations,confidence,confidence_breakdown,needs_review,detected_by,created_at) ' +
            'VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9,$10::jsonb,$11,$12,$13) ' +
            'ON CONFLICT (org_id,id) DO NOTHING RETURNING id',
          [
            audit.orgId,
            relation.id,
            source,
            target,
            relation.relation_type,
            relation.scope,
            JSON.stringify(relation.source_citations),
            JSON.stringify(relation.target_citations),
            relation.confidence,
            relation.confidence_breakdown ? JSON.stringify(relation.confidence_breakdown) : null,
            relation.needs_review ?? null,
            relation.detected_by,
            relation.created_at,
          ],
        );
        if (!saved.rows.length) continue;
        inserted++;
        affected.add(relation.target_doc_id);
        await writeAudit(client, audit, {
          action: 'relation.propose',
          entityType: 'document_relation',
          entityId: relation.id,
          before: null,
          after: relation,
        });
      }
      for (const id of [...affected].sort()) await this.refreshAuthority(client, id, audit);
      await writeAudit(client, audit, {
        action: 'relation.detect',
        entityType: 'document',
        entityId: result.doc_id,
        before: null,
        after: { status: 'completed', inserted, ignored, warnings: result.warnings },
      });
    });
  }

  async recordFailure(id: string, audit: AuditContext) {
    await inTransaction(this.pool, (client) =>
      writeAudit(client, audit, {
        action: 'relation.detect',
        entityType: 'document',
        entityId: id,
        before: null,
        after: { status: 'failed', error: 'Relation detection failed' },
      }),
    );
  }

  async review(id: string, command: RelationReview, audit: AuditContext) {
    return inTransaction(this.pool, async (client) => {
      await lockOrganization(client, audit.orgId);
      const before = await relationById(client, audit.orgId, id);
      if (before.status !== 'proposed')
        throw new RelationError(409, 'Relation review has already been decided');
      if (command.action === 'confirm') {
        if (!before.source_citations.length || !before.target_citations.length) {
          throw new RelationError(
            409,
            'Confirmation requires verified citations on both documents',
          );
        }
        if (before.source_date && before.target_date && before.source_date <= before.target_date) {
          throw new RelationError(
            409,
            'The source document must be newer than the target document',
          );
        }
      }
      if (command.action === 'type') {
        await client.query(
          'UPDATE document_relations SET relation_type = $3 WHERE org_id = $1 AND id = $2',
          [audit.orgId, id, command.relationType],
        );
      } else {
        await client.query(
          'UPDATE document_relations SET status = $3, reviewed_by = $4, reviewed_at = CURRENT_TIMESTAMP, ' +
            "review_reason = $5, needs_review = CASE WHEN $3 = 'confirmed' THEN false ELSE needs_review END WHERE org_id = $1 AND id = $2",
          [
            audit.orgId,
            id,
            command.action === 'confirm' ? 'confirmed' : 'dismissed',
            audit.actorUserId,
            command.action === 'dismiss' ? command.reason : null,
          ],
        );
      }
      const after = await relationById(client, audit.orgId, id);
      await this.refreshAuthority(client, after.target_doc_id, audit);
      const change = command.action === 'confirm' ? changeFor(after) : undefined;
      if (change) {
        // The target document lock taken by refreshAuthority coordinates with extraction.
        const { rows } = await client.query<StaleObligation>(
          'SELECT ' +
            staleObligationFields +
            'FROM obligations o JOIN documents d ON d.id = o.document_id ' +
            "WHERE d.org_id = $1 AND o.external_id IS NOT NULL AND o.retired_at IS NULL AND o.status <> 'rejected' " +
            "AND o.task_status NOT IN ('done','rejected') AND " +
            effectiveCitations +
            ' @> $2::jsonb ORDER BY o.id FOR UPDATE OF o',
          [audit.orgId, JSON.stringify([{ doc_id: change.target_doc_id }])],
        );
        for (const task of rows) await flagStaleness(client, task, [change], audit);
      }
      await writeAudit(client, audit, {
        action: 'relation.' + command.action,
        entityType: 'document_relation',
        entityId: id,
        before: view(before),
        after: view(after),
      });
      return view(after);
    });
  }

  async staleObligations(orgId: number, relationId: string) {
    const { rows } = await this.pool.query<StaleObligation>(
      'SELECT ' +
        staleObligationFields +
        'FROM obligation_staleness s JOIN obligations o ON o.id = s.obligation_id JOIN documents d ON d.id = o.document_id ' +
        "WHERE s.org_id = $1 AND d.org_id = $1 AND s.relation_id = $2 AND s.decision = 'pending' " +
        'AND o.retired_at IS NULL ORDER BY o.id',
      [orgId, relationId],
    );
    return rows;
  }

  async decideStaleness(
    relationId: string,
    id: number,
    decision: StaleDecision,
    audit: AuditContext,
  ) {
    await inTransaction(this.pool, async (client) => {
      await lockOrganization(client, audit.orgId);
      const relation = await relationById(client, audit.orgId, relationId);
      const {
        rows: [task],
      } = await client.query<StaleObligation>(
        'SELECT ' +
          staleObligationFields +
          'FROM obligations o JOIN documents d ON d.id = o.document_id JOIN obligation_staleness s ON s.obligation_id = o.id ' +
          "WHERE o.id = $1 AND d.org_id = $2 AND s.org_id = $2 AND s.relation_id = $3 AND s.decision = 'pending' " +
          'AND o.retired_at IS NULL FOR UPDATE OF o',
        [id, audit.orgId, relationId],
      );
      if (!task) throw new RelationError(404, 'Pending stale obligation not found');
      if (!openObligation(task)) throw new RelationError(409, 'Obligation is closed');
      let after: object = task;
      if (decision.decision === 'update') {
        const citations = relation.source_citations;
        const changes = await readConfirmedChanges(client, audit.orgId, [relation.source_doc_id]);
        if (!citations.length || authorityWarnings(changes, citations).length) {
          throw new RelationError(409, 'The relation has no current replacement citation');
        }
        await client.query(
          'UPDATE obligations SET stale_action_override = $2, stale_citations_override = $3::jsonb, updated_at = CURRENT_TIMESTAMP WHERE id = $1',
          [id, decision.action, JSON.stringify(citations)],
        );
        after = { ...task, action: decision.action, citations };
      } else if (decision.decision === 'close') {
        const assignment = await changeAssignment(client, id, null);
        await client.query(
          "UPDATE obligations SET task_status = 'rejected', task_rejection_reason = $2, status = 'rejected', " +
            "review_decision = 'rejected', review_reason = $2, updated_at = CURRENT_TIMESTAMP WHERE id = $1",
          [id, decision.reason],
        );
        after = {
          ...task,
          task_status: 'rejected',
          status: 'rejected',
          assignment: assignment.after,
        };
      }
      const resolved = await client.query<{ relation_id: string }>(
        'UPDATE obligation_staleness SET decision = $3, decided_by = $4, decided_at = CURRENT_TIMESTAMP, reason = $5 ' +
          "WHERE obligation_id = $1 AND org_id = $2 AND decision = 'pending' AND (relation_id = $6 OR $3 = 'close') RETURNING relation_id",
        [id, audit.orgId, decision.decision, audit.actorUserId, decision.reason, relationId],
      );
      await writeAudit(client, audit, {
        action: 'obligation.stale.' + decision.decision,
        entityType: 'obligation',
        entityId: task.obligation_id,
        before: { ...task, decision: 'pending' },
        after: {
          ...after,
          decision: decision.decision,
          reason: decision.reason,
          relation_id: relationId,
          resolved_relations: resolved.rows.map((row) => row.relation_id),
        },
      });
    });
  }

  confirmedChanges(orgId: number, citations: Citation[]) {
    return this.documentChanges(orgId, [...new Set(citations.map((citation) => citation.doc_id))]);
  }

  documentChanges(orgId: number, docIds: string[]) {
    return readConfirmedChanges(this.pool, orgId, docIds);
  }

  async refreshAuthority(client: PoolClient, id: string, audit: AuditContext) {
    const {
      rows: [target],
    } = await client.query<{ id: number; authority_status: DocumentAuthorityStatus }>(
      'SELECT id, authority_status FROM documents WHERE org_id = $1 AND external_id = $2 FOR UPDATE',
      [audit.orgId, id],
    );
    if (!target) throw new RelationError(404, 'Target document not found');
    const changes = await readConfirmedChanges(client, audit.orgId, [id]);
    const full = changes.find(isFullChange);
    let status: DocumentAuthorityStatus = 'in_force';
    if (full) status = full.relation_type === 'withdraws' ? 'withdrawn' : 'superseded';
    else if (changes.length) status = 'partially_amended';
    else {
      const { rows } = await client.query(
        "SELECT 1 FROM document_relations WHERE org_id = $1 AND target_id = $2 AND status = 'proposed' AND relation_type <> 'references' LIMIT 1",
        [audit.orgId, target.id],
      );
      if (rows.length) status = 'under_review';
    }
    if (status === target.authority_status) return;
    await client.query('UPDATE documents SET authority_status = $2 WHERE id = $1', [
      target.id,
      status,
    ]);
    await writeAudit(client, audit, {
      action: 'document.authority',
      entityType: 'document',
      entityId: id,
      before: { authority_status: target.authority_status },
      after: { authority_status: status },
    });
  }
}
