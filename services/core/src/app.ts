import express from 'express';
import { answerRouter } from './answer.js';
import type { IntelligenceClient } from './intelligence/client.js';
import { obligationRouter, runExtraction } from './obligation-routes.js';
import type { ObligationRepository } from './obligations.js';
import { searchRouter } from './search.js';
import { uploadRouter } from './upload.js';
import type { UploadDependencies } from './upload.js';

type AppDependencies = UploadDependencies & {
  intelligence: Partial<Pick<IntelligenceClient, 'search' | 'answer' | 'extractObligations'>>;
  obligations?: ObligationRepository;
};

export function createApp(dependencies?: AppDependencies): express.Express {
  const app = express();
  app.use(express.json());
  app.get('/health', (_request, response) => {
    response.status(200).json({ status: 'ok' });
  });
  if (dependencies) {
    const { intelligence } = dependencies;
    const extraction =
      dependencies.obligations && intelligence.extractObligations
        ? runExtraction(
            dependencies.obligations,
            intelligence.extractObligations.bind(intelligence),
          )
        : undefined;
    app.use(uploadRouter(dependencies, extraction));
    if (dependencies.obligations && extraction) {
      app.use(obligationRouter(dependencies.obligations, extraction));
    }
    if (intelligence.search) app.use(searchRouter(intelligence.search.bind(intelligence)));
    if (intelligence.answer) app.use(answerRouter(intelligence.answer.bind(intelligence)));
  }
  app.use(
    (
      _error: unknown,
      _request: express.Request,
      response: express.Response,
      _next: express.NextFunction,
    ) => {
      void _next;
      response.status(500).json({ error: 'Document persistence failed' });
    },
  );
  return app;
}
