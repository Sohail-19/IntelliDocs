export const effectiveOwner = 'COALESCE(o.owner_override, o.owner_details)';
export const effectiveDeadline = 'COALESCE(o.deadline_override, o.deadline)';
export const resolvedDeadline = effectiveDeadline + "->'resolved' = 'true'::jsonb";
export const normalizedDeadline = effectiveDeadline + "->>'normalized_date'";

export function isCalendarDate(value: string): boolean {
  return (
    /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    !value.startsWith('0000') &&
    Number.isFinite(Date.parse(value)) &&
    new Date(value).toISOString().slice(0, 10) === value
  );
}
