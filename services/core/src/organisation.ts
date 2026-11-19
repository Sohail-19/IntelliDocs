import { Router } from 'express';
import type { OrgOverview } from '../../../contracts/core-api.v1.js';
import { principal } from './auth/routes.js';
import type { Guard } from './auth/routes.js';
import type { OrgConfig } from './config/loader.js';

export function organisationRouter(configs: ReadonlyMap<string, OrgConfig>, guard: Guard) {
  const router = Router();
  router.get('/api/organisation', guard('org:read'), (_request, response) => {
    const org_id = principal(response).orgSlug;
    const config = configs.get(org_id);
    if (!config) {
      response.status(404).json({ error: 'Organisation configuration is unavailable' });
      return;
    }
    const { languages, departments, document_types, ocr_engine, routing_rules, routing_policy } =
      config;
    const overview: OrgOverview = {
      org_id,
      languages,
      departments,
      document_types,
      ocr_engine,
      routing_rules,
      ...(routing_policy ? { routing_policy } : {}),
    };
    response.set('Cache-Control', 'no-store').json(overview);
  });
  return router;
}
