import { Building2, Eye, EyeOff, LoaderCircle, LockKeyhole, Mail } from 'lucide-react';
import { useState } from 'react';
import type { FormEvent } from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import { ApiError } from './api.js';
import { useAuth } from './auth.js';
import { Brand } from './brand.js';
import { ErrorState } from './states.js';

function returnTo(state: unknown): string {
  if (typeof state !== 'object' || state === null || !('from' in state)) return '/';
  const { from } = state;
  return typeof from === 'string' &&
    from.startsWith('/') &&
    !from.startsWith('//') &&
    !from.startsWith('/login') &&
    !from.includes('\\')
    ? from
    : '/';
}

export function Login() {
  const { client, sessionId } = useAuth();
  const location = useLocation();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [visible, setVisible] = useState(false);
  if (sessionId) return <Navigate to={returnTo(location.state)} replace />;

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const org = fields.get('org_id');
    const email = fields.get('email');
    const password = fields.get('password');
    if (typeof org !== 'string' || typeof email !== 'string' || typeof password !== 'string')
      return;
    setPending(true);
    setError('');
    try {
      await client.login({
        org_id: org.trim(),
        email: email.trim(),
        password,
      });
    } catch (failure) {
      setError(
        failure instanceof ApiError ? failure.message : 'Could not reach the server. Try again.',
      );
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="login-page">
      <aside className="login-brand">
        <Brand />
        <div className="login-art" aria-hidden="true">
          <div className="art-grid" />
          <div className="art-paper art-paper-back" />
          <div className="art-paper art-paper-middle" />
          <div className="art-paper art-paper-front">
            <div className="art-paper-square">
              <span className="art-page-glyph" />
            </div>
            <span />
            <span />
            <span />
            <span />
          </div>
          <div className="art-ring" />
          <div className="art-spark" />
        </div>
        <p className="login-caption">Document intelligence workspace</p>
      </aside>
      <section className="login-content" aria-labelledby="sign-in-title">
        <div className="login-form">
          <h1 id="sign-in-title">Sign in</h1>
          <p className="login-description">Use your organization account to open your workspace.</p>
          <form onSubmit={(event) => void signIn(event)} aria-busy={pending}>
            <label className="field" htmlFor="organization">
              <span>Organization ID</span>
              <span className="input-wrap">
                <Building2 size={18} aria-hidden="true" />
                <input
                  id="organization"
                  name="org_id"
                  required
                  autoCapitalize="none"
                  spellCheck={false}
                  autoComplete="organization"
                />
              </span>
            </label>
            <label className="field" htmlFor="email">
              <span>Email address</span>
              <span className="input-wrap">
                <Mail size={18} aria-hidden="true" />
                <input id="email" name="email" type="email" required autoComplete="username" />
              </span>
            </label>
            <div className="field">
              <label htmlFor="password">Password</label>
              <div className="input-wrap">
                <LockKeyhole size={18} aria-hidden="true" />
                <input
                  id="password"
                  name="password"
                  type={visible ? 'text' : 'password'}
                  required
                  autoComplete="current-password"
                />
                <button
                  className="icon-button"
                  type="button"
                  aria-label={visible ? 'Hide password' : 'Show password'}
                  onClick={() => setVisible(!visible)}
                >
                  {visible ? (
                    <EyeOff size={18} aria-hidden="true" />
                  ) : (
                    <Eye size={18} aria-hidden="true" />
                  )}
                </button>
              </div>
            </div>
            {error && <ErrorState title="Unable to sign in" description={error} />}
            <button className="primary-button" type="submit" disabled={pending}>
              {pending && (
                <LoaderCircle size={18} className="motion-safe:animate-spin" aria-hidden="true" />
              )}
              {pending ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
        </div>
      </section>
    </main>
  );
}
