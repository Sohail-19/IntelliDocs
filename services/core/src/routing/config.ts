import type { OrgConfig } from '../config/loader.js';
import { RoutingEngine } from './engine.js';
import type { RoutingRule } from './engine.js';

export function configuredRouting(config: OrgConfig): RoutingEngine | undefined {
  const rules = config.routing_rules.filter((rule): rule is RoutingRule => 'conditions' in rule);
  const policy = config.routing_policy;
  if (!policy) {
    if (rules.length) throw new Error('routing_policy is required with structured routing_rules');
    return undefined;
  }
  if (rules.length !== config.routing_rules.length) {
    throw new Error('routing_rules must use the structured format when routing_policy is set');
  }
  const roles = new Set(Object.keys(config.roles ?? {}));
  if (!roles.has(policy.review_role)) {
    throw new Error('routing_policy.review_role must name a configured role');
  }
  const departments = new Set(config.departments);
  const documentTypes = new Set(config.document_types);
  const seen = new Set<string>();
  for (const rule of rules) {
    if (seen.has(rule.id)) throw new Error('Duplicate routing rule id: ' + rule.id);
    seen.add(rule.id);
    const { owner_department, document_type } = rule.conditions;
    if (owner_department !== undefined && !departments.has(owner_department)) {
      throw new Error('Routing rule ' + rule.id + ' names an unknown owner department');
    }
    if (document_type !== undefined && !documentTypes.has(document_type)) {
      throw new Error('Routing rule ' + rule.id + ' names an unknown document type');
    }
    const { kind, value } = rule.target;
    if (kind === 'department_queue' && !departments.has(value)) {
      throw new Error('Routing rule ' + rule.id + ' names an unknown department queue');
    }
    if (kind === 'role_queue' && !roles.has(value)) {
      throw new Error('Routing rule ' + rule.id + ' names an unknown role queue');
    }
    if (kind === 'user' && (value.length > 320 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value))) {
      throw new Error('Routing rule ' + rule.id + ' must target a user by email');
    }
  }
  return new RoutingEngine(rules, policy);
}
