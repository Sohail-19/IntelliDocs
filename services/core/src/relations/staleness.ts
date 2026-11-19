import type { Pool, PoolClient } from 'pg';
import type { AuthorityWarning, Citation } from '../../../../contracts/core-api.v1.js';

import { writeAudit } from '../audit.js';
import type { AuditContext } from '../audit.js';

export const effectiveAction = 'COALESCE(o.stale_action_override, o.action)';
export const effectiveCitations = 'COALESCE(o.stale_citations_override, o.citations)';
export const pendingStale =
  "EXISTS (SELECT 1 FROM obligation_staleness s WHERE s.obligation_id = o.id AND s.decision = 'pending')";
export const pendingStaleIds =
  "ARRAY(SELECT s.relation_id FROM obligation_staleness s WHERE s.obligation_id = o.id AND s.decision = 'pending' ORDER BY s.relation_id)";

export interface AuthorityChange {
  id: string;
  source_doc_id: string;
  target_doc_id: string;
  relation_type: 'amends' | 'supersedes' | 'withdraws';
  scope: 'whole_document' | 'clauses';
  target_citations: Citation[];
}

export function isFullChange(change: AuthorityChange) {
  return change.scope === 'whole_document' && change.relation_type !== 'amends';
}

function clause(value: string | undefined) {
  return value?.normalize('NFKC').trim().toLowerCase().replace(/\.$/u, '') || undefined;
}

function heading(citation: Citation) {
  return citation.heading_path?.map(clause).filter(Boolean).join('/') || undefined;
}

export function affects(change: AuthorityChange, citation: Citation): boolean {
  if (change.target_doc_id !== citation.doc_id) return false;
  if (isFullChange(change)) return true;
  return change.target_citations.some((target) => {
    const left = clause(citation.clause_number);
    const right = clause(target.clause_number);
    if (left && right) {
      if (left !== right) return false;
      const leftHeading = heading(citation);
      const rightHeading = heading(target);
      if (!leftHeading || !rightHeading || leftHeading === rightHeading) return true;
    }
    return citation.char_start < target.char_end && target.char_start < citation.char_end;
  });
}

export async function readConfirmedChanges(
  client: Pool | PoolClient,
  orgId: number,
  ids: string[],
): Promise<AuthorityChange[]> {
  if (!ids.length) return [];
  const { rows } = await client.query<AuthorityChange>(
    'SELECT r.id, source.external_id AS source_doc_id, target.external_id AS target_doc_id, ' +
      'r.relation_type, r.scope, r.target_citations FROM document_relations r ' +
      'JOIN documents source ON source.id = r.source_id JOIN documents target ON target.id = r.target_id ' +
      "WHERE r.org_id = $1 AND r.status = 'confirmed' AND r.relation_type IN ('amends','supersedes','withdraws') " +
      'AND target.external_id = ANY($2::text[]) ORDER BY source.doc_date DESC NULLS LAST, source.external_id, r.id',
    [orgId, ids],
  );
  return rows;
}

export function authorityWarnings(
  changes: AuthorityChange[],
  citations: Citation[],
): AuthorityWarning[] {
  const selected = new Map<string, AuthorityWarning>();
  for (const citation of citations) {
    const documentChanges = changes.filter((change) => change.target_doc_id === citation.doc_id);
    const full = documentChanges.find(isFullChange);
    for (const change of full
      ? [full]
      : documentChanges.filter((item) => affects(item, citation))) {
      selected.set(change.id, authorityWarning(change));
    }
  }
  return [...selected.values()];
}

function authorityWarning(change: AuthorityChange): AuthorityWarning {
  const status = !isFullChange(change)
    ? 'partially_amended'
    : change.relation_type === 'withdraws'
      ? 'withdrawn'
      : 'superseded';
  return {
    doc_id: change.target_doc_id,
    newer_doc_id: change.source_doc_id,
    relation_id: change.id,
    status,
    message:
      status === 'partially_amended'
        ? 'A newer document changes this cited clause.'
        : 'This document is ' + status + '. See the linked document for the change.',
  };
}

export function documentAuthorityWarnings(changes: AuthorityChange[]): AuthorityWarning[] {
  const full = changes.find(isFullChange);
  return full ? [authorityWarning(full)] : changes.map(authorityWarning);
}

export async function hasPendingStaleness(client: PoolClient, id: number) {
  const { rows } = await client.query(
    "SELECT 1 FROM obligation_staleness WHERE obligation_id = $1 AND decision = 'pending' LIMIT 1",
    [id],
  );
  return rows.length > 0;
}

export interface StaleObligation {
  id: number;
  doc_id: string;
  obligation_id: string;
  action: string;
  citations: Citation[];
  status: string;
  task_status: string;
}

export function openObligation(task: Pick<StaleObligation, 'task_status' | 'status'>) {
  return (
    task.status !== 'rejected' && task.task_status !== 'done' && task.task_status !== 'rejected'
  );
}

export async function flagStaleness(
  client: PoolClient,
  obligation: StaleObligation,
  changes: AuthorityChange[],
  audit: AuditContext,
) {
  if (!openObligation(obligation)) return;
  for (const change of changes) {
    if (!obligation.citations.some((citation) => affects(change, citation))) continue;
    const { rows } = await client.query(
      'INSERT INTO obligation_staleness (org_id, obligation_id, relation_id) VALUES ($1, $2, $3) ' +
        'ON CONFLICT DO NOTHING RETURNING relation_id',
      [audit.orgId, obligation.id, change.id],
    );
    if (!rows.length) continue;
    await writeAudit(client, audit, {
      action: 'obligation.stale.flag',
      entityType: 'obligation',
      entityId: obligation.obligation_id,
      before: null,
      after: { doc_id: obligation.doc_id, relation_id: change.id, decision: 'pending' },
    });
  }
}

export const staleObligationFields =
  'o.id, d.external_id AS doc_id, o.external_id AS obligation_id, o.status, o.task_status, ' +
  effectiveAction +
  ' AS action, ' +
  effectiveCitations +
  ' AS citations ';
