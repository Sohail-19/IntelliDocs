import { Files } from 'lucide-react';

export function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark">
        <Files size={22} strokeWidth={1.9} aria-hidden="true" />
      </span>
      <span>IntelliDocs</span>
    </div>
  );
}
