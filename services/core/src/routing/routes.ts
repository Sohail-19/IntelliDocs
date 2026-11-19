import { Router } from 'express';

import { auditContext } from '../audit.js';
import { principal } from '../auth/routes.js';
import type { Guard } from '../auth/routes.js';
import type { PostgresRoutingService } from './service.js';

export function routingRouter(service: PostgresRoutingService, guard: Guard): Router {
  const router = Router();
  router.post(
    '/api/documents/:id/obligations/:obligation_id/route',
    guard('obligations:assign'),
    (request, response, next) => {
      const { id, obligation_id } = request.params;
      if (typeof id !== 'string' || typeof obligation_id !== 'string') {
        response.status(422).json({ error: 'Invalid document or obligation ID' });
        return;
      }
      void service
        .reroute(id, obligation_id, auditContext(request, response, principal(response)))
        .then((result) => {
          if (result === 'not_found') response.status(404).json({ error: 'Obligation not found' });
          else if (result === 'disabled')
            response.status(409).json({ error: 'Routing is not configured for the organization' });
          else if (result === 'rejected')
            response.status(409).json({ error: 'The obligation was rejected in review' });
          else response.json({ assignment: result });
        })
        .catch(next);
    },
  );
  return router;
}
