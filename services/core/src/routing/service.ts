import type { Pool, PoolClient } from 'pg';

import { inTransaction, writeAudit } from '../audit.js';
import type { AuditContext } from '../audit.js';
import { effectiveOwner, normalizedDeadline, resolvedDeadline } from '../obligation-values.js';
import {
  effectiveAction,
  effectiveCitations,
  hasPendingStaleness,
} from '../relations/staleness.js';
import { changeAssignment } from './assignments.js';
import type { Assignment } from './assignments.js';
import type { RoutingEngine, RoutingFacts } from './engine.js';

interface Candidate extends RoutingFacts {
  rowId: number;
  externalId: string;
  docId: string;
  orgSlug: string;
}

export class PostgresRoutingService {
  constructor(
    private readonly pool: Pool,
    private readonly engines: ReadonlyMap<string, RoutingEngine>,
    private readonly clock: () => Date = () => new Date(),
  ) {}

  enabled(orgSlug: string): boolean {
    return this.engines.has(orgSlug);
  }

  async assign(
    client: PoolClient,
    candidate: Candidate,
    audit: AuditContext,
    keepExisting = false,
  ): Promise<Assignment | null> {
    const engine = this.engines.get(candidate.orgSlug);
    if (!engine) return null;
    let decision = engine.decide(candidate, this.clock());
    let userId = null;
    if (decision.target.kind === 'user') {
      const { rows } = await client.query<{ id: number }>(
        'SELECT id FROM users WHERE org_id = $1 AND email = $2 AND active FOR SHARE',
        [audit.orgId, decision.target.value.toLowerCase()],
      );
      userId = rows[0]?.id ?? null;
      if (userId === null) decision = engine.review(decision.ruleId);
    }
    const { target } = decision;
    const { before, after, changed } = await changeAssignment(
      client,
      candidate.rowId,
      {
        assignee_user_id: userId,
        queue_type:
          target.kind === 'user' ? null : target.kind === 'role_queue' ? 'role' : 'department',
        queue_name: target.kind === 'user' ? null : target.value,
        rule_id: decision.ruleId,
        reason: decision.reason,
        escalation_due_at: decision.escalateAt,
      },
      keepExisting,
    );
    if (!changed) return after;
    await writeAudit(client, audit, {
      action: 'obligation.route',
      entityType: 'obligation',
      entityId: candidate.externalId,
      before,
      after: { ...after, doc_id: candidate.docId },
    });
    return after;
  }

  async reroute(
    docId: string,
    obligationId: string,
    audit: AuditContext,
  ): Promise<Assignment | 'not_found' | 'disabled' | 'rejected' | 'stale'> {
    return inTransaction(this.pool, (client) =>
      this.routeStored(client, docId, obligationId, audit),
    );
  }

  async routeStored(
    client: PoolClient,
    docId: string,
    obligationId: string,
    audit: AuditContext,
  ): Promise<Assignment | 'not_found' | 'disabled' | 'rejected' | 'stale'> {
    const { rows } = await client.query<Candidate & { reviewRejected: boolean }>(
      'SELECT o.id AS "rowId", o.external_id AS "externalId", d.external_id AS "docId", ' +
        'org.slug AS "orgSlug", o.obligation_type AS "obligationType", ' +
        effectiveOwner +
        '->>\'resolved_department\' AS "ownerDepartment", ' +
        'COALESCE(' +
        effectiveOwner +
        "->'resolved' = 'true'::jsonb, false) AS \"ownerResolved\", " +
        'd.doc_type AS "documentType", ' +
        effectiveAction +
        ' AS action, o.condition, o.confidence, ' +
        'CASE WHEN jsonb_array_length(' +
        effectiveCitations +
        ") > 0 THEN o.status ELSE 'needs_review' END AS status, " +
        'COALESCE(o.review_decision = \'approved\', false) AS "humanApproved", ' +
        'COALESCE(o.review_decision = \'rejected\', false) AS "reviewRejected", ' +
        'CASE WHEN ' +
        resolvedDeadline +
        ' THEN ' +
        normalizedDeadline +
        ' END AS deadline ' +
        'FROM obligations o JOIN documents d ON d.id = o.document_id JOIN organizations org ON org.id = d.org_id ' +
        'WHERE d.org_id = $1 AND d.external_id = $2 AND o.external_id = $3 AND o.retired_at IS NULL FOR UPDATE OF o',
      [audit.orgId, docId, obligationId],
    );
    const candidate = rows[0];
    if (!candidate) return 'not_found';
    if (await hasPendingStaleness(client, candidate.rowId)) return 'stale';
    if (candidate.reviewRejected) return 'rejected';
    const assignment = await this.assign(client, candidate, audit);
    if (!assignment) return 'disabled';
    if (
      !candidate.humanApproved &&
      (assignment.reason === 'review' || assignment.reason === 'unavailable_user')
    ) {
      await client.query(
        "UPDATE obligations SET status = 'needs_review' WHERE id = $1 AND status <> 'needs_review'",
        [candidate.rowId],
      );
    }
    return assignment;
  }
}
