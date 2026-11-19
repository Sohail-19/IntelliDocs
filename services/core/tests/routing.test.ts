import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { up as initial } from '../migrations/1700000000000_initial.js';
import { up as parents } from '../migrations/1700000000001_document_parents.js';
import { up as indexing } from '../migrations/1700000000002_index_status.js';
import { up as extraction } from '../migrations/1700000000003_obligation_extraction.js';
import { up as auth } from '../migrations/1700000000004_auth.js';
import { up as audit } from '../migrations/1700000000005_audit_trail.js';
import { up as assignments } from '../migrations/1700000000006_routing_assignments.js';
import { up as workflow } from '../migrations/1700000000007_task_workflow.js';
import { up as review } from '../migrations/1700000000008_obligation_review.js';
import { up as confidenceMigration } from '../migrations/1700000000009_obligation_confidence.js';
import { up as documentMetadata } from '../migrations/1700000000010_document_metadata.js';
import type { AuditContext } from '../src/audit.js';
import { loadOrgConfig } from '../src/config/loader.js';
import { PostgresDocumentRepository } from '../src/documents.js';
import type { IngestResponse } from '../src/intelligence/client.js';
import { PostgresObligationRepository } from '../src/obligations.js';
import type { DocumentExtractionResult } from '../src/obligations.js';
import { configuredRouting } from '../src/routing/config.js';
import { RoutingEngine } from '../src/routing/engine.js';
import type { RoutingFacts, RoutingRule } from '../src/routing/engine.js';
import { PostgresRoutingService } from '../src/routing/service.js';
import { createTestDatabase } from './postgres.js';

const examplePath = fileURLToPath(new URL('../../../config/orgs/example.yaml', import.meta.url));
const now = new Date('2026-11-19T12:00:00Z');
const policy = { min_confidence: 0.6, review_role: 'manager' };
const defaultRule: RoutingRule = {
  id: 'default',
  priority: 0,
  conditions: {},
  target: { kind: 'role_queue', value: 'officer' },
};
const facts: RoutingFacts = {
  action: 'Submit the ＭＯＮＴＨＬＹ report',
  condition: 'After approval',
  obligationType: 'reporting',
  ownerDepartment: 'finance',
  ownerResolved: true,
  documentType: 'agreement',
  deadline: '2026-11-22',
  confidence: 0.8,
  status: 'verified',
};

describe('routing rules', () => {
  it('matches all conditions, runs higher priorities first, and resolves ties in config order', () => {
    const selected: RoutingRule = {
      id: 'reporting',
      priority: 20,
      conditions: {
        obligation_type: 'reporting',
        owner_department: 'finance',
        document_type: 'agreement',
        keywords: ['monthly REPORT', 'approval'],
        deadline_within_days: 3,
      },
      target: { kind: 'department_queue', value: 'finance' },
      escalation_after_days: 2,
    };
    const fallback: RoutingRule = {
      id: 'agreement',
      priority: 10,
      conditions: { document_type: 'agreement' },
      target: { kind: 'department_queue', value: 'legal' },
    };
    const engine = new RoutingEngine(
      [
        defaultRule,
        fallback,
        selected,
        { ...selected, id: 'later', target: { kind: 'role_queue', value: 'officer' } },
      ],
      policy,
    );
    expect(engine.decide(facts, now)).toEqual({
      target: { kind: 'department_queue', value: 'finance' },
      ruleId: 'reporting',
      reason: 'rule',
      escalateAt: new Date('2026-11-21T12:00:00Z'),
    });
    for (const change of [
      { ownerDepartment: 'legal' },
      { obligationType: 'duty' as const },
      { condition: 'awaiting signoff' },
      { deadline: null },
      { deadline: '2026-11-18' },
      { deadline: '2026-11-23' },
    ]) {
      expect(engine.decide({ ...facts, ...change }, now).ruleId).toBe('agreement');
    }
    expect(engine.decide({ ...facts, documentType: 'invoice' }, now).ruleId).toBe('default');
  });

  it('sends unresolved, low-confidence and rejected items to the configured manager queue', () => {
    const engine = new RoutingEngine([defaultRule], policy);
    for (const change of [
      { ownerResolved: false },
      { confidence: 0.59 },
      { status: 'rejected' as const },
    ]) {
      expect(engine.decide({ ...facts, ...change }, now)).toMatchObject({
        target: { kind: 'role_queue', value: 'manager' },
        reason: 'review',
        ruleId: null,
      });
    }
    expect(engine.decide({ ...facts, confidence: 0.6 }, now).ruleId).toBe('default');
  });

  it('rejects missing defaults, unknown queues and invalid rule shape during config loading', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'intellidocs-routing-'));
    const path = join(directory, 'org.yaml');
    const sample = await loadOrgConfig(examplePath);
    try {
      await writeFile(path, JSON.stringify({ ...sample, routing_rules: [] }));
      await expect(loadOrgConfig(path)).rejects.toThrow(/org\.yaml: .*exactly one default/);
      await writeFile(
        path,
        JSON.stringify({
          ...sample,
          routing_rules: [
            { ...defaultRule, target: { kind: 'department_queue', value: 'unknown' } },
          ],
        }),
      );
      await expect(loadOrgConfig(path)).rejects.toThrow('unknown department queue');
      await writeFile(
        path,
        JSON.stringify({
          ...sample,
          routing_rules: [{ ...defaultRule, escalation_after_days: -1 }],
        }),
      );
      await expect(loadOrgConfig(path)).rejects.toThrow(/routing_rules\/0\/escalation_after_days/);
    } finally {
      await rm(directory, { recursive: true });
    }
  });
});

it('routes stored obligations, closes old assignments, and keeps history through re-extraction', async () => {
  const { db, pool, apply } = createTestDatabase();
  try {
    for (const migrate of [
      initial,
      parents,
      indexing,
      extraction,
      auth,
      audit,
      assignments,
      workflow,
      review,
      confidenceMigration,
      documentMetadata,
    ])
      await apply(migrate);
    await db.exec(`
      INSERT INTO organizations (id, slug) VALUES (42, 'example'), (43, 'other');
      INSERT INTO users (id, org_id, email, name, password_hash, active)
      VALUES (11, 42, 'owner@example.test', 'Fixture', 'unused fixture', true);
    `);
    const config = await loadOrgConfig(examplePath);
    const engine = configuredRouting({
      ...config,
      routing_rules: [
        {
          id: 'specific-user',
          priority: 50,
          conditions: { keywords: ['monthly'] },
          target: { kind: 'user', value: 'owner@example.test' },
        },
        ...config.routing_rules,
      ],
    });
    if (!engine) throw new Error('Expected routing policy');
    const context: AuditContext = {
      orgId: 42,
      actorUserId: null,
      requestId: 'routing-test',
      ip: null,
    };
    const routing = new PostgresRoutingService(pool, new Map([['example', engine]]), () => now);
    const documents = new PostgresDocumentRepository(pool);
    const obligations = new PostgresObligationRepository(pool, routing);
    const parsed: IngestResponse = {
      document_id: 'doc-001',
      parse_result: {
        document: {
          id: 'doc-001',
          filename: 'agreement.pdf',
          mime_type: 'application/pdf',
          source_uri: 'file:///agreement.pdf',
          status: 'ready',
          pages: [],
          doc_type: 'agreement',
        },
        parser_name: 'test',
        parser_version: '1',
        warnings: [],
      },
    };
    await documents.save(parsed, context);
    await documents.setStatus('doc-001', context, 'indexed');
    const sample = JSON.parse(
      await readFile(
        new URL('../../../contracts/examples/extraction-result.v1.json', import.meta.url),
        'utf8',
      ),
    ) as DocumentExtractionResult;
    const original = sample.obligations[0];
    if (!original) throw new Error('Expected example obligation');
    const { confidence_breakdown, ...legacy } = original;
    expect(confidence_breakdown?.quote_exact).toBeGreaterThan(0);
    const result: DocumentExtractionResult = {
      ...sample,
      obligations: [
        {
          ...legacy,
          confidence: 0.9,
          status: 'verified',
          owner: {
            raw_text: 'Finance',
            resolved_department: 'finance',
            resolved_role: null,
            resolved: true,
          },
        },
      ],
    };
    await obligations.replace(result, context);
    await obligations.replace(result, context);
    expect(await obligations.list({ doc_id: 'doc-001' }, 42)).toMatchObject([
      { assigned_user_id: 11, assignment: { rule_id: 'specific-user', status: 'active' } },
    ]);
    await expect(routing.reroute('doc-001', original.id, { ...context, orgId: 43 })).resolves.toBe(
      'not_found',
    );
    await db.query('UPDATE users SET active = false WHERE id = 11');
    await expect(routing.reroute('doc-001', original.id, context)).resolves.toMatchObject({
      queue_type: 'role',
      queue_name: 'manager',
      reason: 'unavailable_user',
      rule_id: 'specific-user',
    });
    const current = result.obligations[0];
    if (!current) throw new Error('Expected stored result');
    const editedItem = { ...current, action: 'Submit the report', condition: 'On request' };
    const edited = { ...result, obligations: [editedItem] };
    await obligations.replace(edited, context);
    expect(await obligations.list({}, 42)).toMatchObject([
      { assignment: { queue_type: 'department', queue_name: 'legal', rule_id: 'agreement' } },
    ]);
    const history = await db.query<{ obligation_id: number }>(
      'SELECT obligation_id, status, closed_at FROM assignments ORDER BY id',
    );
    expect(history.rows).toMatchObject([
      { status: 'closed' },
      { status: 'closed' },
      { status: 'active', closed_at: null },
    ]);
    expect(new Set(history.rows.map((row) => row.obligation_id)).size).toBe(1);
    await obligations.replace(
      { ...result, obligations: [{ ...editedItem, confidence: 0.1 }] },
      context,
    );
    expect(await obligations.list({}, 42)).toMatchObject([
      { assignment: { queue_type: 'role', queue_name: 'manager', reason: 'review' } },
    ]);
    await obligations.replace({ ...result, obligations: [] }, context);
    expect(await obligations.list({}, 42)).toEqual([]);
    expect((await db.query('SELECT status FROM assignments ORDER BY id')).rows).toEqual([
      { status: 'closed' },
      { status: 'closed' },
      { status: 'closed' },
      { status: 'closed' },
    ]);
    await obligations.replace(edited, context);
    const reopened = await db.query<{ obligation_id: number }>(
      'SELECT obligation_id, status FROM assignments ORDER BY id',
    );
    expect(reopened.rows).toHaveLength(5);
    expect(new Set(reopened.rows.map((row) => row.obligation_id)).size).toBe(1);
    const events = await db.query(
      "SELECT COUNT(*)::int AS count FROM audit_log WHERE action = 'obligation.route'",
    );
    expect(events.rows).toEqual([{ count: 5 }]);
  } finally {
    await db.close();
  }
}, 15_000);
