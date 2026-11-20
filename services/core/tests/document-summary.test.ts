import express from 'express';
import request from 'supertest';
import { beforeEach, expect, it, vi } from 'vitest';
import type { DocumentRecord, DocumentSummary } from '../../../contracts/core-api.v1.js';
import { assignRequestId } from '../src/audit.js';
import type { Guard } from '../src/auth/routes.js';
import { loadOrgConfig } from '../src/config/loader.js';
import { documentSummaryRouter } from '../src/document-summary.js';
import { IntelligenceClient } from '../src/intelligence/client.js';
import type { Summary, SummaryRequest } from '../src/intelligence/client.js';
import type { StoredObligation } from '../src/obligations.js';
import type { AuthorityChange } from '../src/relations/staleness.js';
import { fileURLToPath } from 'node:url';

const config = await loadOrgConfig(
  fileURLToPath(new URL('../../../config/orgs/example.yaml', import.meta.url)),
);
const document: DocumentRecord = {
  id: 'original',
  filename: 'original.pdf',
  department: 'legal',
  doc_type: 'agreement',
  doc_date: null,
  mime_type: 'application/pdf',
  languages: ['en'],
  page_count: 1,
  parent_document_id: null,
  status: 'indexed',
  extraction_status: 'completed',
  index_error: null,
  extraction_error: null,
  created_at: '2026-01-01',
  indexed_at: '2026-01-01',
};
const citation = {
  doc_id: 'original',
  page: 1,
  char_start: 0,
  char_end: 10,
  quote: 'Submit it.',
  clause_number: '1.2',
};
const second = { ...citation, char_start: 15, char_end: 25, clause_number: '2.1' };
const audit = vi.fn().mockResolvedValue(undefined);
const documentChanges = vi.fn<() => Promise<AuthorityChange[]>>().mockResolvedValue([]);
const summarize = vi.fn((body: SummaryRequest): Promise<Summary> =>
  Promise.resolve({
    doc_id: body.doc_id,
    language: body.language,
    audience: {
      ...(body.department ? { department: body.department } : {}),
      ...(body.role ? { role: body.role } : {}),
    } as Summary['audience'],
    points: [
      { text: 'Old instruction', citations: [citation] },
      { text: 'Current instruction', citations: [second] },
    ],
    not_relevant: false,
    relevance_reason: '',
    generated_by: 'fixture',
    verifier_version: 'fixture',
  }),
);

function obligation(id: string, queue: string | null, assignee: number | null): StoredObligation {
  return {
    id,
    doc_id: 'original',
    action: 'Submit the report',
    status: 'verified',
    confidence: 0.8,
    obligation_type: 'reporting',
    extractor_version: 'fixture',
    citations: [citation],
    owner: {
      raw_text: 'Finance',
      resolved: true,
      resolved_department: 'finance',
      resolved_role: null,
    },
    deadline: {
      raw_text: '2026-12-01',
      normalized_date: '2026-12-01',
      resolved: true,
      anchor_date: null,
      is_relative: false,
    },
    assignment: {
      id: 1,
      status: 'active',
      assignee_user_id: assignee,
      queue_type: queue ? 'department' : null,
      queue_name: queue,
      rule_id: null,
      escalation_due_at: null,
      assigned_at: new Date(),
      closed_at: null,
      reason: 'manual',
    },
  };
}

const listObligations = vi
  .fn()
  .mockResolvedValue([
    obligation('personal', null, 7),
    obligation('other-user', null, 8),
    obligation('finance-queue', 'finance', null),
    obligation('legal-queue', 'legal', null),
    { ...obligation('done', 'finance', null), task_status: 'done' },
  ]);
const guard: Guard = () => (req, res, next) => {
  res.locals.principal = {
    id: 7,
    orgId: 1,
    orgSlug: 'example',
    department: 'finance',
    roles: req.get('authorization') === 'admin' ? ['admin'] : ['viewer', 'finance_officer'],
  };
  next();
};
const app = express();
app.use(assignRequestId);
app.use(
  documentSummaryRouter(
    {
      reader: {
        list: vi.fn(),
        get: vi.fn((_org: number, id: string) =>
          Promise.resolve(id === 'original' ? document : undefined),
        ),
      },
      configs: new Map([['example', config]]),
      listObligations,
      summarize,
      canPreview: (actor) =>
        actor.roles.some((role) =>
          config.access?.role_permissions[role]?.includes('documents:summary-preview'),
        ),
      auditPreview: audit,
      relations: { documentChanges },
    },
    guard,
  ),
);

beforeEach(() => {
  vi.clearAllMocks();
  documentChanges.mockResolvedValue([]);
});

it('uses the authenticated department and profiled role and only sends active assignments', async () => {
  const res = await request(app).get('/api/documents/original/summary');
  expect(res.status, JSON.stringify(res.body)).toBe(200);
  const [sent] = summarize.mock.calls[0] ?? [];
  expect(sent).toMatchObject({
    doc_id: 'original',
    org_id: 'example',
    department: 'finance',
    role: 'finance_officer',
    language: 'en',
  });
  expect(sent?.assigned_obligations?.map((item) => item.id)).toEqual(['personal', 'finance-queue']);
  expect(sent?.assigned_obligations?.[0]).not.toHaveProperty('assignment');
  expect(res.headers['cache-control']).toBe('private, no-store');
  expect(audit).not.toHaveBeenCalled();
  expect(res.body).not.toHaveProperty('preview_options');
});

it('rejects a reader-supplied audience before sending any assignment data', async () => {
  const res = await request(app).get('/api/documents/original/summary?department=legal');
  expect(res.status).toBe(403);
  expect(summarize).not.toHaveBeenCalled();
  expect(listObligations).not.toHaveBeenCalled();
  expect(audit).not.toHaveBeenCalled();
});

it('audits the privileged preview and uses queue assignments for the selected audience', async () => {
  const res = await request(app)
    .get('/api/documents/original/summary?department=legal')
    .set('authorization', 'admin');
  expect(res.status, JSON.stringify(res.body)).toBe(200);
  const [sent] = summarize.mock.calls[0] ?? [];
  expect(sent?.department).toBe('legal');
  expect(sent?.assigned_obligations?.map((item) => item.id)).toEqual(['legal-queue']);
  expect(audit).toHaveBeenCalledWith(expect.objectContaining({ actorUserId: 7 }), {
    action: 'document.summary.preview',
    entityType: 'document',
    entityId: 'original',
    before: null,
    after: { audience: { department: 'legal' }, language: 'en' },
  });
  expect(res.body).toHaveProperty('preview_options.departments', ['legal', 'finance']);
});

it('warns on amendments and removes only the points citing changed clauses', async () => {
  documentChanges.mockResolvedValue([
    {
      id: 'change-1',
      source_doc_id: 'newer',
      target_doc_id: 'original',
      relation_type: 'amends',
      scope: 'clauses',
      target_citations: [citation],
    },
  ]);
  const res = await request(app).get('/api/documents/original/summary');
  expect(res.status).toBe(200);
  const payload = res.body as DocumentSummary;
  expect(payload.summary?.points.map((point) => point.text)).toEqual(['Current instruction']);
  expect(payload.authority_warnings[0]?.newer_doc_id).toBe('newer');
  expect(summarize.mock.calls[0]?.[0].assigned_obligations).toEqual([]);
});

it('returns a supersession warning without requesting obsolete content', async () => {
  documentChanges.mockResolvedValue([
    {
      id: 'change-2',
      source_doc_id: 'newer',
      target_doc_id: 'original',
      relation_type: 'supersedes',
      scope: 'whole_document',
      target_citations: [citation],
    },
  ]);
  const res = await request(app).get('/api/documents/original/summary');
  expect(res.status).toBe(200);
  expect(res.body).toMatchObject({
    summary: null,
    authority_warnings: [{ newer_doc_id: 'newer' }],
  });
  expect(summarize).not.toHaveBeenCalled();
});

it('posts the typed snapshot to the Intelligence summarize endpoint', async () => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({}));
  const client = new IntelligenceClient('http://intelligence.test', fetcher);
  const input: SummaryRequest = {
    doc_id: 'original',
    org_id: 'example',
    department: 'legal',
    language: 'en',
    assigned_obligations: [],
  };
  await client.summarize(input);
  expect(fetcher).toHaveBeenCalledWith(
    'http://intelligence.test/v1/summarize',
    expect.objectContaining({
      method: 'POST',
      body: JSON.stringify(input),
    }),
  );
});
