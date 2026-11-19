import express from 'express';
import helmet from 'helmet';
import { auditRouter } from './audit-routes.js';
import { assignRequestId } from './audit.js';
import type { AuditReader } from './audit.js';
import { authRouter, requirePermission, userRoleRouter } from './auth/routes.js';
import type { Guard } from './auth/routes.js';
import type { AuthService } from './auth/service.js';
import { answerRouter } from './answer.js';
import type { IntelligenceClient } from './intelligence/client.js';
import { obligationRouter, runExtraction } from './obligation-routes.js';
import type { ObligationRepository } from './obligations.js';
import type { ObligationReviewRepository } from './review/repository.js';
import { reviewRouter } from './review/routes.js';
import { routingRouter } from './routing/routes.js';
import type { PostgresRoutingService } from './routing/service.js';
import { searchRouter } from './search.js';
import type { TaskRepository } from './tasks/repository.js';
import { taskRouter } from './tasks/routes.js';
import { uploadRouter } from './upload.js';
import type { UploadDependencies } from './upload.js';

type AppDependencies = UploadDependencies & {
  intelligence: Partial<Pick<IntelligenceClient, 'search' | 'answer' | 'extractObligations'>>;
  obligations?: ObligationRepository;
  audit?: AuditReader;
  routing?: PostgresRoutingService;
  tasks?: TaskRepository;
  reviews?: ObligationReviewRepository;
};

export function createApp(
  dependencies?: AppDependencies,
  security: { auth?: AuthService; guard?: Guard } = {},
): express.Express {
  const app = express();
  app.use(assignRequestId);
  app.use(helmet());
  const { auth } = security;
  if (auth) app.use('/api/auth', authRouter(auth));
  app.use(express.json());
  // Health is public; data routes deny requests when no security guard is configured.
  app.get('/health', (_request, response) => {
    response.status(200).json({ status: 'ok' });
  });
  const guard: Guard = auth
    ? (permission) => requirePermission(auth, permission)
    : (security.guard ??
      (() => (_request, response) => {
        response.sendStatus(401);
      }));
  if (auth) app.use(userRoleRouter(auth, guard));
  if (dependencies) {
    if (dependencies.audit) app.use(auditRouter(dependencies.audit, guard));
    if (dependencies.routing) app.use(routingRouter(dependencies.routing, guard));
    if (dependencies.tasks) app.use(taskRouter(dependencies.tasks, guard));
    if (dependencies.reviews) app.use(reviewRouter(dependencies.reviews, guard));
    const { intelligence } = dependencies;
    const extraction =
      dependencies.obligations && intelligence.extractObligations
        ? runExtraction(
            dependencies.obligations,
            intelligence.extractObligations.bind(intelligence),
          )
        : undefined;
    const { obligations } = dependencies;
    app.use(
      uploadRouter(
        dependencies,
        guard,
        extraction,
        obligations && (async (id, orgId) => Boolean(await obligations.documentState(id, orgId))),
      ),
    );
    if (dependencies.obligations && extraction) {
      app.use(obligationRouter(dependencies.obligations, extraction, guard));
    }
    if (intelligence.search) app.use(searchRouter(intelligence.search.bind(intelligence), guard));
    if (intelligence.answer) app.use(answerRouter(intelligence.answer.bind(intelligence), guard));
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
      if (status === 400 || status === 413) {
        response.status(status).json({ error: 'Invalid request body' });
      } else {
        response.status(500).json({ error: 'Request processing failed' });
      }
    },
  );
  return app;
}
