import type { Pool } from 'pg';
import type { ReviewOptions, ReviewOverride } from '../../../../contracts/core-api.v1.js';

import { inTransaction, writeAudit } from '../audit.js';
import type { AuditContext } from '../audit.js';
import type { OrgConfig } from '../config/loader.js';
import type { ObligationDeadline, ObligationOwner } from '../intelligence/client.js';
import { listStoredObligations } from '../obligations.js';
import type { ReviewDecision } from '../obligations.js';
import { changeAssignment } from '../routing/assignments.js';
import type { PostgresRoutingService } from '../routing/service.js';

type OwnerSelection = NonNullable<ReviewOverride['owner']>;
export type ReviewChange =
  | { action: 'edit'; owner?: OwnerSelection; dueDate?: string | null }
  | { action: 'approve' }
  | { action: 'reject'; reason: string };

export class ReviewError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

interface ReviewRow {
  id: number;
  org_slug: string;
  status: string;
  extracted_owner: ObligationOwner;
  extracted_deadline: ObligationDeadline;
  owner_override: ObligationOwner | null;
  deadline_override: ObligationDeadline | null;
  review_decision: ReviewDecision | null;
  review_reason: string | null;
  has_citation: boolean;
}

export class ObligationReviewRepository {
  constructor(
    private readonly pool: Pool,
    private readonly routing: PostgresRoutingService,
    private readonly configs: ReadonlyMap<string, Pick<OrgConfig, 'departments' | 'roles'>>,
  ) {}

  list(orgId: number, docId?: string) {
    return listStoredObligations(
      this.pool,
      { status: 'needs_review', ...(docId === undefined ? {} : { doc_id: docId }) },
      orgId,
    );
  }

  options(orgSlug: string): ReviewOptions | undefined {
    const config = this.configs.get(orgSlug);
    if (!config) return undefined;
    return {
      departments: config.departments,
      roles: Object.entries(config.roles ?? {}).map(([name, role]) => ({
        name,
        department: role.department ?? null,
      })),
    };
  }

  async change(docId: string, obligationId: string, change: ReviewChange, audit: AuditContext) {
    return inTransaction(this.pool, async (client) => {
      const { rows } = await client.query<ReviewRow>(
        'SELECT o.id, org.slug AS org_slug, o.status, o.owner_details AS extracted_owner, o.deadline AS extracted_deadline, ' +
          'o.owner_override, o.deadline_override, o.review_decision, o.review_reason, jsonb_array_length(o.citations) > 0 AS has_citation ' +
          'FROM obligations o JOIN documents d ON d.id = o.document_id JOIN organizations org ON org.id = d.org_id ' +
          'WHERE d.org_id = $1 AND d.external_id = $2 AND o.external_id = $3 AND o.retired_at IS NULL FOR UPDATE OF o',
        [audit.orgId, docId, obligationId],
      );
      const pending = rows[0];
      if (!pending) throw new ReviewError(404, 'Obligation not found');
      if (pending.status !== 'needs_review' || pending.review_decision) {
        throw new ReviewError(409, 'The obligation is not pending review');
      }

      const before = {
        doc_id: docId,
        status: pending.status,
        owner: pending.owner_override ?? pending.extracted_owner,
        deadline: pending.deadline_override ?? pending.extracted_deadline,
        review_decision: pending.review_decision,
        review_reason: pending.review_reason,
      };
      let assignmentChange;
      if (change.action === 'edit') {
        const owner =
          change.owner === undefined
            ? pending.owner_override
            : this.resolveOwner(pending, change.owner);
        const deadline =
          change.dueDate === undefined
            ? pending.deadline_override
            : {
                ...pending.extracted_deadline,
                normalized_date: change.dueDate,
                resolved: change.dueDate !== null,
                is_relative: false,
                anchor_date: null,
              };
        await client.query(
          'UPDATE obligations SET owner_override = $2::jsonb, deadline_override = $3::jsonb, updated_at = CURRENT_TIMESTAMP WHERE id = $1',
          [
            pending.id,
            owner === null ? null : JSON.stringify(owner),
            deadline === null ? null : JSON.stringify(deadline),
          ],
        );
      } else if (change.action === 'approve') {
        if (!pending.has_citation)
          throw new ReviewError(409, 'Approval requires a source citation');
        if (
          !before.owner.resolved ||
          !(before.owner.resolved_department || before.owner.resolved_role)
        ) {
          throw new ReviewError(409, 'Approval requires a resolved owner');
        }
        if (!this.routing.enabled(pending.org_slug)) {
          throw new ReviewError(409, 'Routing is not configured for the organization');
        }
        await client.query(
          "UPDATE obligations SET status = 'verified', review_decision = 'approved', updated_at = CURRENT_TIMESTAMP WHERE id = $1",
          [pending.id],
        );
        const routed = await this.routing.routeStored(client, docId, obligationId, audit);
        if (typeof routed === 'string') throw new Error('Could not route approved obligation');
      } else {
        await client.query(
          "UPDATE obligations SET status = 'rejected', review_decision = 'rejected', review_reason = $2, updated_at = CURRENT_TIMESTAMP WHERE id = $1",
          [pending.id, change.reason],
        );
        assignmentChange = await changeAssignment(client, pending.id, null);
      }
      const [obligation] = await listStoredObligations(
        client,
        { doc_id: docId },
        audit.orgId,
        obligationId,
      );
      if (!obligation) throw new Error('Reviewed obligation is missing');
      const { status, owner, deadline, review_decision, review_reason } = obligation;
      await writeAudit(client, audit, {
        action: 'obligation.review.' + change.action,
        entityType: 'obligation',
        entityId: obligationId,
        before: { ...before, ...(assignmentChange && { assignment: assignmentChange.before }) },
        after: {
          doc_id: docId,
          status,
          owner,
          deadline,
          review_decision,
          review_reason,
          ...(assignmentChange && { assignment: null }),
        },
      });
      return obligation;
    });
  }

  private resolveOwner(pending: ReviewRow, selection: OwnerSelection): ObligationOwner {
    const config = this.configs.get(pending.org_slug);
    const role = selection.role;
    const roles = config?.roles ?? {};
    if (role !== undefined && !Object.hasOwn(roles, role))
      throw new ReviewError(422, 'Unknown owner role');
    const roleDepartment = role === undefined ? undefined : roles[role]?.department;
    const department = selection.department ?? roleDepartment;
    if (department !== undefined && !config?.departments.includes(department)) {
      throw new ReviewError(422, 'Unknown owner department');
    }
    if (roleDepartment !== undefined && department !== roleDepartment) {
      throw new ReviewError(422, 'Owner role belongs to a different department');
    }
    return {
      raw_text: pending.extracted_owner.raw_text,
      resolved_department: department ?? null,
      resolved_role: role ?? null,
      resolved: true,
    };
  }
}
