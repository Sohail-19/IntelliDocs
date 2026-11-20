import { readFileSync } from 'node:fs';

import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { Router } from 'express';
import type { DocumentSummary, SummaryPreview } from '../../../contracts/core-api.v1.js';

import { auditContext } from './audit.js';
import type { AuditContext, AuditEvent } from './audit.js';
import type { Principal } from './auth/policy.js';
import { principal } from './auth/routes.js';
import type { Guard } from './auth/routes.js';
import type { OrgConfig } from './config/loader.js';
import type { DocumentReader } from './documents.js';
import type { IntelligenceClient, Summary, SummaryRequest } from './intelligence/client.js';
import { llmProblem } from './intelligence/client.js';
import type { ExtractedObligation, ObligationRepository } from './obligations.js';
import type { PostgresRelationRepository } from './relations/repository.js';
import {
  authorityWarnings,
  documentAuthorityWarnings,
  isFullChange,
} from './relations/staleness.js';

const ajv = new Ajv2020({ strict: true });
addFormats.default(ajv);
for (const name of ['citation', 'summary']) {
  const file = new URL(`../../../contracts/schemas/${name}.v1.schema.json`, import.meta.url);
  ajv.addSchema(JSON.parse(readFileSync(file, 'utf8')) as object);
}
const validateSummary = ajv.compile<Summary>({ $ref: 'summary.v1.schema.json' });

export type PreviewAudit = (context: AuditContext, event: AuditEvent) => Promise<void>;

function ownAudience(actor: Principal, config: OrgConfig): SummaryPreview {
  const department =
    actor.department && config.departments.includes(actor.department)
      ? actor.department
      : undefined;
  const roles = actor.roles.filter((role) => {
    if (!Object.hasOwn(config.roles ?? {}, role)) return false;
    const roleDepartment = config.roles?.[role]?.department;
    return !department || !roleDepartment || roleDepartment === department;
  });
  const role =
    roles.find((name) => Object.hasOwn(config.summary?.role_profiles ?? {}, name)) ?? roles[0];
  return { ...(department ? { department } : {}), ...(role ? { role } : {}) };
}

interface Dependencies {
  reader: DocumentReader;
  configs: ReadonlyMap<string, OrgConfig>;
  listObligations: ObligationRepository['list'];
  summarize: IntelligenceClient['summarize'];
  canPreview: (actor: Principal) => boolean;
  auditPreview: PreviewAudit;
  relations?: Pick<PostgresRelationRepository, 'documentChanges'> | undefined;
}

export function documentSummaryRouter(deps: Dependencies, guard: Guard): Router {
  const router = Router();
  router.get(
    '/api/documents/:id/summary',
    guard('documents:upload', 'documents:search', 'documents:answer'),
    (request, response, next) => {
      response.set('Cache-Control', 'private, no-store');
      void (async () => {
        const actor = principal(response);
        const { id } = request.params;
        const { department, role, ...extra } = request.query;
        const preview = department !== undefined || role !== undefined;
        const canPreview = deps.canPreview(actor);
        if (preview && !canPreview) {
          response.status(403).json({ error: 'Summary preview permission is required' });
          return;
        }
        if (
          typeof id !== 'string' ||
          !id.trim() ||
          id.length > 255 ||
          id.includes('\0') ||
          Object.keys(extra).length ||
          [department, role].some(
            (value) =>
              value !== undefined &&
              (typeof value !== 'string' || !value.trim() || value.length > 255),
          )
        ) {
          response.status(422).json({ error: 'Invalid document ID or summary audience' });
          return;
        }
        const document = await deps.reader.get(actor.orgId, id);
        const config = deps.configs.get(actor.orgSlug);
        if (!document || !config) {
          response.status(404).json({ error: 'Document not found' });
          return;
        }
        const audience = preview
          ? {
              ...(typeof department === 'string' ? { department } : {}),
              ...(typeof role === 'string' ? { role } : {}),
            }
          : ownAudience(actor, config);
        const roleDepartment = audience.role
          ? config.roles?.[audience.role]?.department
          : undefined;
        if (
          (audience.department && !config.departments.includes(audience.department)) ||
          (audience.role && !Object.hasOwn(config.roles ?? {}, audience.role)) ||
          (audience.department && roleDepartment && roleDepartment !== audience.department)
        ) {
          response
            .status(422)
            .json({ error: 'Choose a configured department and a compatible role' });
          return;
        }
        const language = config.languages[0];
        const current = await deps.relations?.documentChanges(actor.orgId, [id]);
        const payload: DocumentSummary = {
          summary: null,
          authority_warnings: current
            ? documentAuthorityWarnings(current)
            : (document.authority_warnings ?? []),
          ...(canPreview && config.summary
            ? {
                preview_options: {
                  departments: config.departments,
                  roles: Object.entries(config.roles ?? {}).map(([name, settings]) => ({
                    name,
                    ...(settings.department && { department: settings.department }),
                  })),
                },
              }
            : {}),
        };
        if (preview) {
          await deps.auditPreview(auditContext(request, response, actor), {
            action: 'document.summary.preview',
            entityType: 'document',
            entityId: id,
            before: null,
            after: { audience, language },
          });
        }
        const audienceKey = audience.department
          ? { department: audience.department, ...audience }
          : audience.role
            ? { role: audience.role, ...audience }
            : null;
        if (
          !config.summary ||
          !language ||
          !audienceKey ||
          document.status !== 'indexed' ||
          document.extraction_status === 'pending' ||
          current?.some(isFullChange) ||
          document.authority_status === 'superseded' ||
          document.authority_status === 'withdrawn'
        ) {
          response.json(payload);
          return;
        }
        const assigned: ExtractedObligation[] = [];
        for (const obligation of await deps.listObligations({ doc_id: id }, actor.orgId)) {
          const assignment = obligation.assignment;
          if (
            !assignment ||
            assignment.status !== 'active' ||
            obligation.stale ||
            obligation.status === 'rejected' ||
            ['done', 'rejected'].includes(obligation.task_status ?? '') ||
            !obligation.citations.length ||
            obligation.citations.some((cite) => cite.doc_id !== id) ||
            authorityWarnings(current ?? [], obligation.citations).length
          )
            continue;
          const applies =
            (!preview && assignment.assignee_user_id === actor.id) ||
            (assignment.queue_type === 'department' &&
              assignment.queue_name === audience.department) ||
            (assignment.queue_type === 'role' &&
              (preview
                ? assignment.queue_name === audience.role
                : assignment.queue_name !== null && actor.roles.includes(assignment.queue_name)));
          if (!applies) continue;
          const {
            id: obligationId,
            doc_id,
            action,
            owner,
            deadline,
            obligation_type,
            citations,
            confidence,
            status,
            extractor_version,
            condition,
          } = obligation;
          assigned.push({
            id: obligationId,
            doc_id,
            action,
            owner,
            deadline,
            obligation_type,
            citations,
            confidence,
            status,
            extractor_version,
            ...(condition === undefined ? {} : { condition }),
          });
        }
        const input: SummaryRequest = {
          ...audienceKey,
          doc_id: id,
          org_id: actor.orgSlug,
          language,
          assigned_obligations: assigned,
        };
        let summary: unknown;
        try {
          summary = await deps.summarize(input);
        } catch (error) {
          const problem = llmProblem(error);
          response
            .status(problem?.status ?? 502)
            .json({ error: problem?.message ?? 'Intelligence summary failed' });
          return;
        }
        if (
          !validateSummary(summary) ||
          summary.doc_id !== id ||
          summary.language !== language ||
          (summary.audience.department ?? undefined) !== audience.department ||
          (summary.audience.role ?? undefined) !== audience.role ||
          summary.points.some(
            (point) =>
              point.citations.some((citation) => citation.doc_id !== id) ||
              (point.obligation_id !== undefined &&
                !assigned.some((item) => item.id === point.obligation_id)),
          )
        ) {
          response.status(502).json({ error: 'Invalid intelligence summary response' });
          return;
        }
        const changes = await deps.relations?.documentChanges(actor.orgId, [id]);
        if (changes) payload.authority_warnings = documentAuthorityWarnings(changes);
        payload.summary = {
          ...summary,
          points: summary.points.filter(
            (point) => !authorityWarnings(changes ?? [], point.citations).length,
          ),
        };
        response.json(payload);
      })().catch(next);
    },
  );
  return router;
}
