import { createApp } from './app.js';
import { PostgresAuditReader } from './audit.js';
import { orgPolicy } from './auth/policy.js';
import { PostgresAuthRepository } from './auth/repository.js';
import { AuthService, authSettingsFromEnv } from './auth/service.js';
import { loadNamedOrgConfigs, OrgConfigError } from './config/loader.js';
import { createDatabasePool } from './database.js';
import { PostgresDocumentRepository } from './documents.js';
import { IntelligenceClient } from './intelligence/client.js';
import { PostgresObligationRepository } from './obligations.js';
import { ObligationReviewRepository } from './review/repository.js';
import { configuredRouting } from './routing/config.js';
import { PostgresRoutingService } from './routing/service.js';
import { scheduleTaskJob, TaskJob, taskIntervalMs } from './tasks/job.js';
import { TaskRepository } from './tasks/repository.js';

async function start(): Promise<void> {
  const port = Number.parseInt(process.env.CORE_PORT ?? '3000', 10);
  const taskInterval = taskIntervalMs();
  const settings = authSettingsFromEnv();
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
    const documents = new PostgresDocumentRepository(pool);
    const orgConfigs = new Map(configs.map(({ slug, config }) => [slug, config]));
    const server = createApp(
      {
        intelligence: new IntelligenceClient(),
        documents,
        orgConfigs,
        documentBrowser: { reader: documents, configs: orgConfigs },
        obligations: new PostgresObligationRepository(pool, routing),
        routing,
        tasks: new TaskRepository(pool, reviewRoles),
        reviews: new ObligationReviewRepository(pool, routing, orgConfigs),
        audit: new PostgresAuditReader(pool),
      },
      { auth },
    ).listen(port, () => {
      console.log('Core service listening on port ' + port);
      server.once('close', scheduleTaskJob(new TaskJob(pool, reviewRoles), taskInterval));
    });
  } catch (error) {
    await pool.end();
    throw error;
  }
}

void start().catch((error: unknown) => {
  console.error(
    error instanceof OrgConfigError
      ? error.message
      : 'Core startup failed. Check auth environment settings, org config, and database migrations.',
  );
  process.exitCode = 1;
});
