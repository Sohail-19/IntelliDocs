import express from 'express';
import helmet from 'helmet';
import { auditRouter } from './audit-routes.js';
import { assignRequestId } from './audit.js';
import type { AuditReader } from './audit.js';
import { authRouter, requirePermission, userRouter } from './auth/routes.js';
import type { Guard } from './auth/routes.js';
import type { AuthService } from './auth/service.js';
import { answerRouter } from './answer.js';
import { configuredCors } from './cors.js';
import type { OrgConfig } from './config/loader.js';
import { documentRouter } from './document-routes.js';
import type { DocumentBrowser } from './document-routes.js';
import { documentSummaryRouter } from './document-summary.js';
import type { PreviewAudit } from './document-summary.js';
import type { IntelligenceClient } from './intelligence/client.js';
import { obligationRouter, runExtraction } from './obligation-routes.js';
import { organisationRouter } from './organisation.js';
import type { ObligationRepository } from './obligations.js';
import type { ObligationReviewRepository } from './review/repository.js';
import { reviewRouter } from './review/routes.js';
import { runDetection } from './relations/detection.js';
import type { PostgresRelationRepository } from './relations/repository.js';
import { relationRouter } from './relations/routes.js';
import { routingRouter } from './routing/routes.js';
import { runtimeFromEnv } from './runtime.js';
import type { PostgresRoutingService } from './routing/service.js';
import { searchRouter } from './search.js';
import { sourceRouter } from './source-routes.js';
import type { TaskRepository } from './tasks/repository.js';
import { taskRouter } from './tasks/routes.js';
import { uploadLimit, uploadRouter } from './upload.js';
import type { UploadDependencies } from './upload.js';

type AppDependencies = UploadDependencies & {
  intelligence: Partial<
    Pick<
      IntelligenceClient,
      'search' | 'answer' | 'summarize' | 'extractObligations' | 'pdfSource' | 'detectRelations'
    >
  >;
  obligations?: ObligationRepository;
  audit?: AuditReader;
  orgConfigs?: ReadonlyMap<string, OrgConfig>;
  routing?: PostgresRoutingService;
  tasks?: TaskRepository;
  reviews?: ObligationReviewRepository;
  relations?: PostgresRelationRepository;
  documentBrowser?: DocumentBrowser;
  summaryPreviewAudit?: PreviewAudit;
  readiness?: { postgres(): Promise<unknown>; intelligence(): Promise<unknown> };
};

export function createApp(
  dependencies?: AppDependencies,
  security: { auth?: AuthService; guard?: Guard } = {},
): express.Express {
  const app = express();
  const runtime = runtimeFromEnv();
  if (runtime.proxyHops) app.set('trust proxy', runtime.proxyHops);
  app.use(assignRequestId);
  app.use(helmet());
  app.use(configuredCors(undefined, runtime.production));
  const { auth } = security;
  if (auth) app.use('/api/auth', authRouter(auth, runtime.production));
  app.use(express.json({ limit: runtime.jsonBytes, inflate: false }));
  // Health is public; data routes deny requests when no security guard is configured.
  app.get('/health', (_request, response) => {
    response.status(200).json({ status: 'ok' });
  });
  app.get('/ready', (_request, response) => {
    const checks = Object.entries(dependencies?.readiness ?? {});
    void Promise.allSettled(checks.map(([, check]) => Promise.resolve().then(check))).then(
      (results) => {
        const ready =
          results.length === 2 && results.every((result) => result.status === 'fulfilled');
        response
          .set('Cache-Control', 'no-store')
          .status(ready ? 200 : 503)
          .json({
            status: ready ? 'ready' : 'not_ready',
            checks: Object.fromEntries(
              checks.map(([name], i) => [
                name,
                results[i]?.status === 'fulfilled' ? 'ok' : 'unavailable',
              ]),
            ),
          });
      },
    );
  });
  const guard: Guard = auth
    ? (...permissions) => requirePermission(auth, ...permissions)
    : (security.guard ??
      (() => (_request, response) => {
        response.sendStatus(401);
      }));
  if (auth) app.use(userRouter(auth, guard));
  if (dependencies) {
    if (dependencies.audit) app.use(auditRouter(dependencies.audit, guard));
    if (dependencies.orgConfigs) app.use(organisationRouter(dependencies.orgConfigs, guard));
    if (dependencies.routing) app.use(routingRouter(dependencies.routing, guard));
    if (dependencies.tasks)
      app.use(
        taskRouter(
          dependencies.tasks,
          guard,
          auth ? (actor) => auth.allows(actor, 'tasks:transition') : undefined,
          auth ? (actor) => auth.allows(actor, 'relations:review') : undefined,
        ),
      );
    if (dependencies.reviews) app.use(reviewRouter(dependencies.reviews, guard));
    if (dependencies.relations) app.use(relationRouter(dependencies.relations, guard));
    if (dependencies.documentBrowser) {
      app.use(
        documentRouter(dependencies.documentBrowser, guard, uploadLimit(dependencies.maxBytes)),
      );
      app.use(
        sourceRouter(
          dependencies.documentBrowser,
          guard,
          dependencies.intelligence.pdfSource?.bind(dependencies.intelligence),
          auth
            ? async (actor, id) => {
                if (
                  auth.allows(actor, 'documents:upload') ||
                  auth.allows(actor, 'documents:search') ||
                  auth.allows(actor, 'documents:answer')
                )
                  return true;
                if (
                  auth.allows(actor, 'relations:review') &&
                  (await dependencies.relations?.hasDocument(actor.orgId, id))
                )
                  return true;
                if (
                  auth.allows(actor, 'tasks:read') &&
                  (await dependencies.tasks?.hasVisibleTask(actor, id))
                )
                  return true;
                return (
                  auth.allows(actor, 'obligations:review') &&
                  !!(await dependencies.reviews?.list(actor.orgId, id))?.length
                );
              }
            : undefined,
        ),
      );
    }
    const { intelligence } = dependencies;
    if (
      dependencies.documentBrowser &&
      dependencies.orgConfigs &&
      dependencies.obligations &&
      intelligence.summarize &&
      dependencies.summaryPreviewAudit
    ) {
      app.use(
        documentSummaryRouter(
          {
            reader: dependencies.documentBrowser.reader,
            configs: dependencies.orgConfigs,
            listObligations: dependencies.obligations.list.bind(dependencies.obligations),
            summarize: intelligence.summarize.bind(intelligence),
            canPreview: (actor) => auth?.allows(actor, 'documents:summary-preview') ?? false,
            auditPreview: dependencies.summaryPreviewAudit,
            relations: dependencies.relations,
          },
          guard,
        ),
      );
    }
    const extraction =
      dependencies.obligations && intelligence.extractObligations
        ? runExtraction(
            dependencies.obligations,
            intelligence.extractObligations.bind(intelligence),
          )
        : undefined;
    const { obligations } = dependencies;
    const detection =
      dependencies.relations && intelligence.detectRelations
        ? runDetection(dependencies.relations, intelligence.detectRelations.bind(intelligence))
        : undefined;
    app.use(
      uploadRouter(
        dependencies,
        guard,
        extraction,
        obligations && (async (id, orgId) => Boolean(await obligations.documentState(id, orgId))),
        detection,
        runtime.production || runtime.demo,
      ),
    );
    if (dependencies.obligations && extraction) {
      app.use(obligationRouter(dependencies.obligations, extraction, guard, detection));
    }
    if (intelligence.search)
      app.use(searchRouter(intelligence.search.bind(intelligence), guard, dependencies.relations));
    if (intelligence.answer)
      app.use(answerRouter(intelligence.answer.bind(intelligence), guard, dependencies.relations));
  }
  app.use(
    (
      _error: unknown,
      _request: express.Request,
      response: express.Response,
      _next: express.NextFunction,
    ) => {
      void _next;
      const status =
        typeof _error === 'object' && _error !== null && 'status' in _error
          ? _error.status
          : undefined;
      if (status === 400 || status === 413 || status === 415) {
        response.status(status).json({ error: 'Invalid request body' });
      } else {
        response.status(500).json({ error: 'Request processing failed' });
      }
    },
  );
  return app;
}
