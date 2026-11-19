import { CircleAlert, Inbox, LoaderCircle } from 'lucide-react';
import type { ReactNode } from 'react';

export function LoadingState({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="status-card" role="status">
      <LoaderCircle className="status-icon motion-safe:animate-spin" aria-hidden="true" />
      <p>{label}</p>
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <section className="status-card">
      <div className="status-symbol">
        <Inbox size={23} aria-hidden="true" />
      </div>
      <h2>{title}</h2>
      {description && <p>{description}</p>}
      {action}
    </section>
  );
}

export function ErrorState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="error-state" role="alert">
      <CircleAlert size={21} aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        {description && <p>{description}</p>}
        {action}
      </div>
    </div>
  );
}
