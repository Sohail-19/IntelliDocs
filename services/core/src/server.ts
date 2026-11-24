import { createApp } from './app.js';
import { inTransaction, PostgresAuditReader, writeAudit } from './audit.js';
import { orgPolicy } from './auth/policy.js';
import { PostgresAuthRepository } from './auth/repository.js';
import { AuthService, authSettingsFromEnv } from './auth/service.js';
import { loadNamedOrgConfigs } from './config/loader.js';
import { configuredCors, CorsConfigurationError } from './cors.js';
import { createDatabasePool, databaseReady } from './database.js';
import { DemoCleanup, demoUploadAdmission, scheduleDemoCleanup } from './demo.js';
import { PostgresDocumentRepository } from './documents.js';
import { IntelligenceClient } from './intelligence/client.js';
import { PostgresObligationRepository } from './obligations.js';
import { ObligationReviewRepository } from './review/repository.js';
import { PostgresRelationRepository } from './relations/repository.js';
import { configuredRouting } from './routing/config.js';
import { PostgresRoutingService } from './routing/service.js';
import { flag, logEvent, runtimeFromEnv } from './runtime.js';
import { scheduleTaskJob, TaskJob, taskIntervalMs } from './tasks/job.js';
import { TaskRepository } from './tasks/repository.js';

async function start(): Promise<void> {
  const port = Number(process.env.CORE_PORT || process.env.PORT || '3000');
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid listen port');
  const taskInterval = taskIntervalMs();
  const runtime = runtimeFromEnv();
  flag('LLM_ENABLED', true);
  const settings = authSettingsFromEnv(runtime.production);
  configuredCors(undefined, runtime.production);
  const configs = await loadNamedOrgConfigs(process.env.ORG_CONFIG_DIR);
  const policies = new Map(
    configs.flatMap(({ slug, config }) => {
      const policy = orgPolicy(slug, config);
      return policy ? [[slug, policy] as const] : [];
    }),
  );
  if (!policies.size || new Set(configs.map(({ slug }) => slug)).size !== configs.length) {
    throw new Error('At least one organization needs access settings and slugs must be unique');
  }
  const pool = createDatabasePool();
  try {
    const engines = new Map(
      configs.flatMap(({ slug, config }) => {
        const engine = configuredRouting(config);
        return engine ? [[slug, engine] as const] : [];
      }),
    );
    const routing = new PostgresRoutingService(pool, engines);
    const reviewRoles = new Map(
      configs.flatMap(({ slug, config }) =>
        config.routing_policy ? [[slug, config.routing_policy.review_role] as const] : [],
      ),
    );
    const users = new PostgresAuthRepository(pool);
    for (const { slug, config } of configs) {
      await users.seed(slug, config.departments, Object.keys(config.roles ?? {}));
    }
    const auth = await AuthService.create(users, policies, settings);
    const documents = new PostgresDocumentRepository(pool, runtime.demo);
    const intelligence = new IntelligenceClient();
    const relations = new PostgresRelationRepository(pool);
    const orgConfigs = new Map(configs.map(({ slug, config }) => [slug, config]));
    const server = createApp(
      {
        intelligence,
        readiness: {
          postgres: () => databaseReady(pool),
          intelligence: () => intelligence.ready(),
        },
        ...(runtime.demo && { uploadAdmission: demoUploadAdmission(pool, runtime) }),
        documents,
        orgConfigs,
        documentBrowser: { reader: documents, configs: orgConfigs },
        obligations: new PostgresObligationRepository(pool, routing),
        routing,
        tasks: new TaskRepository(pool, reviewRoles),
        reviews: new ObligationReviewRepository(pool, routing, orgConfigs),
        relations,
        audit: new PostgresAuditReader(pool),
        summaryPreviewAudit: (context, event) =>
          inTransaction(pool, (transaction) => writeAudit(transaction, context, event)),
      },
      { auth },
    ).listen(port, '0.0.0.0', () => {
      logEvent('server_started');
      server.once('close', scheduleTaskJob(new TaskJob(pool, reviewRoles), taskInterval));
      if (runtime.demo)
        server.once(
          'close',
          scheduleDemoCleanup(
            new DemoCleanup(
              pool,
              intelligence.purgeDemoDocument.bind(intelligence),
              relations,
              runtime.retentionDays,
            ),
            runtime.sweepMs,
          ),
        );
    });
    server.once('error', () => {
      logEvent('startup_failed');
      process.exitCode = 1;
      void pool.end();
    });
  } catch (error) {
    await pool.end();
    throw error;
  }
}

void start().catch((error: unknown) => {
  logEvent(
    'startup_failed',
    error instanceof CorsConfigurationError ? { reason: error.message } : {},
  );
  process.exitCode = 1;
});
