import {
  Building2,
  ClipboardCheck,
  ClipboardList,
  FileText,
  GitCompareArrows,
  History,
  House,
  ListChecks,
  LogOut,
  Menu,
  Search,
  Users,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { useState } from 'react';
import { Link, NavLink, Outlet, useOutletContext } from 'react-router-dom';

import type { AuthProfile, Permission } from '../../../contracts/core-api.v1.js';
import { useAuth } from './auth.js';
import { Brand } from './brand.js';
import { EmptyState, ErrorState } from './states.js';

export interface Section {
  path: string;
  title: string;
  permission: Permission;
  icon: LucideIcon;
}

export const sections = [
  { path: '/documents', title: 'Documents', permission: 'documents:upload', icon: FileText },
  { path: '/search', title: 'Search', permission: 'documents:search', icon: Search },
  { path: '/obligations', title: 'Obligations', permission: 'obligations:read', icon: ListChecks },
  {
    path: '/review',
    title: 'Review queue',
    permission: 'obligations:review',
    icon: ClipboardCheck,
  },
  { path: '/tasks', title: 'Task feed', permission: 'tasks:read', icon: ClipboardList },
  {
    path: '/relations',
    title: 'Relations',
    permission: 'relations:review',
    icon: GitCompareArrows,
  },
  { path: '/audit', title: 'Audit trail', permission: 'audit:read', icon: History },
  { path: '/users', title: 'Users and roles', permission: 'users:roles', icon: Users },
  { path: '/organisation', title: 'Organisation', permission: 'org:read', icon: Building2 },
] satisfies Section[];

function canView(profile: AuthProfile, section: Section) {
  return (
    profile.permissions.includes(section.permission) ||
    (section.path === '/search' && profile.permissions.includes('documents:answer'))
  );
}

export function Workspace() {
  const profile = useOutletContext<AuthProfile>();
  const { client } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const visible = sections.filter((section) => canView(profile, section));

  return (
    <div className="workspace">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <header className="workspace-header">
        <button
          className="icon-button menu-toggle"
          type="button"
          aria-label={menuOpen ? 'Close navigation' : 'Open navigation'}
          aria-controls="workspace-sidebar"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen(!menuOpen)}
        >
          <Menu size={22} aria-hidden="true" />
        </button>
        <div className="organization">
          <span className="organization-label">Organization</span>
          <strong>{profile.org_id}</strong>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={() => void client.logout().catch(() => undefined)}
        >
          <LogOut size={17} aria-hidden="true" /> Sign out
        </button>
      </header>
      <aside className={'sidebar' + (menuOpen ? ' sidebar-open' : '')} id="workspace-sidebar">
        <Brand />
        <nav aria-label="Workspace">
          <NavLink to="/" end onClick={() => setMenuOpen(false)}>
            <House size={19} aria-hidden="true" /> Workspace
          </NavLink>
          {visible.map(({ path, title, icon: Icon }) => (
            <NavLink key={path} to={path} onClick={() => setMenuOpen(false)}>
              <Icon size={19} aria-hidden="true" /> {title}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main id="main-content" tabIndex={-1} className="workspace-main">
        <Outlet context={profile} />
      </main>
    </div>
  );
}

export function Home() {
  const profile = useOutletContext<AuthProfile>();
  const hasSections = sections.some((section) => canView(profile, section));
  return (
    <>
      <h1>Workspace</h1>
      {hasSections ? (
        <p className="page-description">Choose a section from the workspace navigation.</p>
      ) : (
        <EmptyState
          title="No sections assigned"
          description="Contact your organization administrator to request access."
        />
      )}
    </>
  );
}

function WorkspaceLink() {
  return (
    <Link to="/" className="text-link">
      Back to workspace
    </Link>
  );
}

export function SectionPlaceholder({ section }: { section: Section }) {
  const profile = useOutletContext<AuthProfile>();
  return (
    <>
      <h1>{section.title}</h1>
      {profile.permissions.includes(section.permission) ? (
        <EmptyState title="This section is coming later" action={<WorkspaceLink />} />
      ) : (
        <ErrorState
          title="Access denied"
          description="Your account does not have access to this section."
          action={<WorkspaceLink />}
        />
      )}
    </>
  );
}

export function NotFound() {
  return (
    <>
      <h1>Page not found</h1>
      <EmptyState title="This page is unavailable" action={<WorkspaceLink />} />
    </>
  );
}
