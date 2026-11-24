import { randomUUID } from 'node:crypto';
import type { RequestHandler } from 'express';
import type { Pool } from 'pg';

import { inTransaction, writeAudit } from './audit.js';
import { principal } from './auth/routes.js';
import type { IntelligenceClient } from './intelligence/client.js';
import type { PostgresRelationRepository } from './relations/repository.js';
import { logEvent, requestContext } from './runtime.js';
import type { Runtime } from './runtime.js';

class UploadQuotaExceeded extends Error {}

export async function reserveUpload(
  pool: Pool,
  actor: number,
  globalLimit: number,
  userLimit: number,
): Promise<void> {
  await inTransaction(pool, async (client) => {
    for (const [scope, cap] of [
      [0, globalLimit],
      [actor, userLimit],
    ] as const) {
      if (cap === 0) throw new UploadQuotaExceeded();
      const { rowCount } = await client.query(
        `INSERT INTO demo_upload_usage (day, actor_id, used)
          VALUES (timezone('UTC', CURRENT_TIMESTAMP)::date, $1, 1)
          ON CONFLICT (day, actor_id) DO UPDATE SET used = demo_upload_usage.used + 1
          WHERE demo_upload_usage.used < $2`,
        [scope, cap],
      );
      if (rowCount !== 1) throw new UploadQuotaExceeded();
    }
  });
}

export function demoUploadAdmission(pool: Pool, runtime: Runtime): RequestHandler {
  let uploading = 0;
  return (_request, response, next) => {
    if (uploading >= runtime.concurrentUploads) {
      response.status(429).json({ error: 'The demo upload queue is busy. Try again shortly.' });
      return;
    }
    uploading++;
    const release = () => {
      response.off('finish', release);
      response.off('close', release);
      uploading--;
    };
    response.once('finish', release).once('close', release);
    void reserveUpload(pool, principal(response).id, runtime.uploadsGlobal, runtime.uploadsPerUser)
      .then(() => {
        if (!response.destroyed) next();
      })
      .catch((error: unknown) => {
        if (error instanceof UploadQuotaExceeded) {
          response
            .status(429)
            .json({ error: 'Daily demo upload limit reached. Try again after midnight UTC.' });
        } else next(error);
      });
  };
}

type Purge = IntelligenceClient['purgeDemoDocument'];
type Authority = Pick<PostgresRelationRepository, 'refreshAuthority'>;
type Document = { id: number; external_id: string; demo_upload: boolean };

export class DemoCleanup {
  constructor(
    private readonly pool: Pool,
    private readonly purge: Purge,
    private readonly relations: Authority,
    private readonly retentionDays: number,
  ) {}

  async sweep(now = new Date()): Promise<void> {
    const cutoff = new Date(now.getTime() - this.retentionDays * 86_400_000);
    const { rows } = await this.pool.query<{ id: number }>(
      `SELECT id FROM documents WHERE demo_upload AND parent_document_id IS NULL
       AND created_at < $1 ORDER BY created_at, id LIMIT 50`,
      [cutoff],
    );
    for (const { id } of rows) {
      const requestId = randomUUID();
      try {
        await requestContext.run({ id: requestId }, () => this.remove(id, cutoff, requestId));
      } catch {
        // Keep the database rows so a partially completed cross-service purge can be retried.
        logEvent('demo_cleanup_failed', { request_id: requestId });
      }
    }
    await this.pool.query(
      `DELETE FROM demo_upload_usage WHERE day < timezone('UTC', $1::timestamptz)::date - 2`,
      [now],
    );
  }

  private remove(id: number, cutoff: Date, requestId: string): Promise<void> {
    return inTransaction(this.pool, async (client) => {
      const {
        rows: [root],
      } = await client.query<{ org_id: number; slug: string }>(
        `SELECT d.org_id, o.slug FROM documents d JOIN organizations o ON o.id=d.org_id
         WHERE d.id=$1 AND d.demo_upload AND d.created_at < $2 FOR UPDATE OF d SKIP LOCKED`,
        [id, cutoff],
      );
      if (!root) return;
      const { rows: tree } = await client.query<Document>(
        `WITH RECURSIVE tree AS (
           SELECT id, external_id, demo_upload FROM documents WHERE id=$1 AND org_id=$2
           UNION ALL SELECT d.id, d.external_id, d.demo_upload FROM documents d
           JOIN tree ON d.parent_document_id=tree.id WHERE d.org_id=$2
         ) SELECT * FROM tree ORDER BY id`,
        [id, root.org_id],
      );
      if (tree.some((item) => !item.demo_upload)) throw new Error('Mixed retention tree');
      const ids = tree.map((item) => item.id);
      const external = tree.map((item) => item.external_id);
      const org = root.org_id;
      const { rows: removedRelations } = await client.query<{ id: string; target: string }>(
        `SELECT r.id, d.external_id AS target FROM document_relations r
         JOIN documents d ON d.id=r.target_id AND d.org_id=r.org_id
         WHERE r.org_id=$1 AND (r.source_id=ANY($2::int[]) OR r.target_id=ANY($2::int[]))`,
        [org, ids],
      );
      const relationIds = removedRelations.map((item) => item.id);
      const audit = { orgId: org, orgSlug: root.slug, actorUserId: null, requestId, ip: null };
      const obligations =
        'SELECT id FROM obligations WHERE org_id=$1 AND document_id=ANY($2::int[])';
      await client.query(
        `DELETE FROM obligation_staleness WHERE org_id=$1
         AND (obligation_id IN (${obligations}) OR relation_id=ANY($3::text[]))`,
        [org, ids, relationIds],
      );
      await client.query('DELETE FROM document_relations WHERE org_id=$1 AND id=ANY($2::text[])', [
        org,
        relationIds,
      ]);
      await client.query(
        `DELETE FROM assignments WHERE org_id=$1 AND obligation_id IN (${obligations})`,
        [org, ids],
      );
      await client.query(
        `UPDATE obligations SET stale_action_override=NULL, stale_citations_override=NULL
         WHERE org_id=$1 AND EXISTS (SELECT FROM jsonb_array_elements(stale_citations_override) cite
                                    WHERE cite->>'doc_id'=ANY($2::text[]))`,
        [org, external],
      );
      await client.query('DELETE FROM documents WHERE org_id=$1 AND id=ANY($2::int[])', [org, ids]);
      for (const target of [...new Set(removedRelations.map((item) => item.target))].sort()) {
        if (!external.includes(target))
          await this.relations.refreshAuthority(client, target, audit);
      }
      for (const document of tree) {
        await this.purge(document.external_id, root.slug);
        await writeAudit(client, audit, {
          action: 'document.demo_expired',
          entityType: 'document',
          entityId: document.external_id,
          before: null,
          after: null,
        });
      }
    });
  }
}

export function scheduleDemoCleanup(job: DemoCleanup, interval: number): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  async function tick() {
    try {
      await job.sweep();
    } catch {
      logEvent('demo_cleanup_failed');
    }
    if (!stopped) timer = setTimeout(() => void tick(), interval).unref();
  }
  void tick();
  return () => {
    stopped = true;
    clearTimeout(timer);
  };
}
