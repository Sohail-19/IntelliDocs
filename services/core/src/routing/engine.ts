import type { Obligation } from '../intelligence/client.js';
import type { RoutingPolicy, RoutingRule } from '../../../../contracts/core-api.v1.js';

export type { RoutingPolicy, RoutingRule };
export type RoutingTarget = RoutingRule['target'];
export type RoutingConditions = RoutingRule['conditions'];

export interface RoutingFacts {
  obligationType: NonNullable<Obligation['obligation_type']>;
  ownerDepartment: string | null;
  ownerResolved: boolean;
  documentType: string | null;
  action: string;
  condition: string | null;
  deadline: string | null;
  confidence: number;
  status: Obligation['status'];
  humanApproved?: boolean;
}

export interface RoutingDecision {
  target: RoutingTarget;
  ruleId: string | null;
  reason: 'rule' | 'review' | 'unavailable_user';
  escalateAt: Date | null;
}

const dayMs = 86_400_000;

function normalize(text: string): string {
  return text.normalize('NFKC').toLowerCase().trim().replace(/\s+/gu, ' ');
}

function matches(conditions: RoutingConditions, facts: RoutingFacts, now: Date): boolean {
  if (
    conditions.obligation_type !== undefined &&
    conditions.obligation_type !== facts.obligationType
  )
    return false;
  if (
    conditions.owner_department !== undefined &&
    conditions.owner_department !== facts.ownerDepartment
  )
    return false;
  if (conditions.document_type !== undefined && conditions.document_type !== facts.documentType)
    return false;
  if (conditions.keywords !== undefined) {
    const text = normalize(facts.action + ' ' + (facts.condition ?? ''));
    if (!conditions.keywords.every((keyword) => text.includes(normalize(keyword)))) return false;
  }
  if (conditions.deadline_within_days !== undefined) {
    const today = Math.floor(now.getTime() / dayMs);
    const days = facts.deadline === null ? NaN : Date.parse(facts.deadline) / dayMs - today;
    if (!Number.isInteger(days) || days < 0 || days > conditions.deadline_within_days) return false;
  }
  return true;
}

export class RoutingEngine {
  private readonly ordered: RoutingRule[];

  constructor(
    rules: RoutingRule[],
    private readonly policy: RoutingPolicy,
  ) {
    this.ordered = [...rules].sort((left, right) => right.priority - left.priority);
    const defaults = this.ordered.filter(({ conditions }) => Object.keys(conditions).length === 0);
    if (defaults.length !== 1 || this.ordered.at(-1) !== defaults[0]) {
      throw new Error(
        'routing_rules must have exactly one default with empty conditions, evaluated last',
      );
    }
  }

  review(ruleId: string | null = null): RoutingDecision {
    return {
      target: { kind: 'role_queue', value: this.policy.review_role },
      ruleId,
      reason: ruleId === null ? 'review' : 'unavailable_user',
      escalateAt: null,
    };
  }

  decide(facts: RoutingFacts, now: Date): RoutingDecision {
    if (
      !facts.ownerResolved ||
      facts.status !== 'verified' ||
      (!facts.humanApproved &&
        (!Number.isFinite(facts.confidence) || facts.confidence < this.policy.min_confidence))
    ) {
      return this.review();
    }
    const rule = this.ordered.find((candidate) => matches(candidate.conditions, facts, now));
    if (!rule) throw new Error('Default routing rule was not evaluated');
    return {
      target: rule.target,
      ruleId: rule.id,
      reason: 'rule',
      escalateAt:
        rule.escalation_after_days === undefined
          ? null
          : new Date(now.getTime() + rule.escalation_after_days * dayMs),
    };
  }
}
