import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';

import type { Permission } from '../../../contracts/core-api.v1.js';
import { useAuth } from './auth.js';
import { DocumentsPage, DocumentDetail } from './documents/pages.js';
import { Login } from './login.js';
import { AuditPage } from './oversight/audit-page.js';
import { OrganisationPage } from './oversight/organisation-page.js';
import { UsersPage } from './oversight/users-page.js';
import { SearchPage } from './search/page.js';
import { ErrorState, LoadingState } from './states.js';
import { ReviewPage } from './work/review-page.js';
import { TasksPage } from './work/tasks-page.js';
import { Home, NotFound, SectionPlaceholder, Workspace, sections } from './workspace.js';

function RequireSession() {
  const location = useLocation();
  const { client, profile, sessionId } = useAuth();
  if (!sessionId) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: location.pathname + location.search + location.hash }}
      />
    );
  }
  if (profile.isPending) {
    return (
      <main className="centered-state">
        <LoadingState label="Opening your workspace…" />
      </main>
    );
  }
  if (profile.isError) {
    return (
      <main className="centered-state">
        <ErrorState
          title="Unable to open your workspace"
          description="Check your connection and try again."
          action={
            <div className="state-actions">
              <button
                className="secondary-button"
                type="button"
                onClick={() => void profile.refetch()}
              >
                Try again
              </button>
              <button
                className="secondary-button"
                type="button"
                onClick={() => void client.logout().catch(() => undefined)}
              >
                Sign out
              </button>
            </div>
          }
        />
      </main>
    );
  }
  return <Outlet context={profile.data} />;
}

function RequireDocumentAccess() {
  const { profile } = useAuth();
  return profile.data?.permissions.includes('documents:upload') ? (
    <Outlet context={profile.data} />
  ) : (
    <ErrorState
      title="Access denied"
      description="Your account does not have access to documents."
    />
  );
}

function RequirePermission({ permission }: { permission: Permission }) {
  const { profile } = useAuth();
  return profile.data?.permissions.includes(permission) ? (
    <Outlet context={profile.data} />
  ) : (
    <ErrorState
      title="Access denied"
      description="Your account does not have access to this section."
    />
  );
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireSession />}>
        <Route element={<Workspace />}>
          <Route index element={<Home />} />
          <Route element={<RequireDocumentAccess />}>
            <Route path="/documents" element={<DocumentsPage />} />
            <Route path="/documents/:id" element={<DocumentDetail />} />
          </Route>
          <Route path="/search" element={<SearchPage />} />
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/review" element={<ReviewPage />} />
          <Route element={<RequirePermission permission="audit:read" />}>
            <Route path="/audit" element={<AuditPage />} />
          </Route>
          <Route element={<RequirePermission permission="users:roles" />}>
            <Route path="/users" element={<UsersPage />} />
          </Route>
          <Route element={<RequirePermission permission="org:read" />}>
            <Route path="/organisation" element={<OrganisationPage />} />
          </Route>
          {sections
            .filter((section) => section.path === '/obligations')
            .map((section) => (
              <Route
                key={section.path}
                path={section.path}
                element={<SectionPlaceholder section={section} />}
              />
            ))}
          <Route path="*" element={<NotFound />} />
        </Route>
      </Route>
    </Routes>
  );
}
