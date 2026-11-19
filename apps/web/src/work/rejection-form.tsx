import { useEffect, useRef, useState } from 'react';

export function RejectionForm({
  pending,
  onReject,
  onCancel,
}: {
  pending: boolean;
  onReject: (reason: string) => void;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState('');
  const field = useRef<HTMLTextAreaElement>(null);
  useEffect(() => field.current?.focus(), []);
  return (
    <form
      className="work-reject"
      onSubmit={(event) => {
        event.preventDefault();
        if (reason.trim() && !pending) onReject(reason.trim());
      }}
    >
      <label className="field">
        Rejection reason
        <textarea
          ref={field}
          required
          maxLength={2000}
          rows={3}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          disabled={pending}
        />
      </label>
      <div className="work-actions">
        <button
          type="submit"
          className="secondary-button work-danger"
          disabled={pending || !reason.trim()}
        >
          Confirm rejection
        </button>
        <button type="button" className="text-link" disabled={pending} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
