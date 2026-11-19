/** Core v1 browser contract. Core and web both use these wire types. */
import type { components } from "./intelligence.openapi.v1.js";

export type Permission =
  | "users:register"
  | "users:roles"
  | "audit:read"
  | "org:read"
  | "documents:upload"
  | "documents:search"
  | "documents:answer"
  | "obligations:read"
  | "obligations:extract"
  | "obligations:status"
  | "obligations:assign"
  | "obligations:review"
  | "tasks:read"
  | "tasks:transition";

export interface AuthCredentials {
  org_id: string;
  email: string;
  password: string;
}

export interface RefreshCredentials {
  refresh_token: string;
}

export interface AuthTokens extends RefreshCredentials {
  access_token: string;
  token_type: "Bearer";
  expires_in: number;
}

export interface AuthProfile {
  user_id: number;
  org_id: string;
  roles: string[];
  permissions: string[];
}

export interface CoreAuthApi {
  "/api/auth/login": {
    method: "POST";
    body: AuthCredentials;
    result: AuthTokens;
  };
  "/api/auth/refresh": {
    method: "POST";
    body: RefreshCredentials;
    result: AuthTokens;
  };
  "/api/auth/logout": {
    method: "POST";
    body: RefreshCredentials;
    result: void;
  };
  "/api/auth/me": { method: "GET"; body: undefined; result: AuthProfile };
}

export interface DocumentUploadFields {
  department?: string;
  doc_type?: string;
  doc_date?: string;
}

export interface DocumentUploadOptions {
  departments: string[];
  document_types: string[];
  max_upload_bytes: number;
  upload_defaults: DocumentUploadFields;
}

export interface DocumentRecord {
  id: string;
  filename: string;
  department: string | null;
  doc_type: string | null;
  doc_date: string | null;
  mime_type: string | null;
  languages: string[];
  page_count: number | null;
  parent_document_id: string | null;
  status: "parsed" | "indexed" | "failed";
  extraction_status: "pending" | "completed" | "failed";
  index_error: string | null;
  extraction_error: string | null;
  created_at: string;
  indexed_at: string | null;
}

export interface DocumentObligations {
  obligations: {
    id: string;
    action: string;
    status: "extracted" | "verified" | "needs_review" | "rejected";
    owner: {
      raw_text: string;
      resolved_department: string | null;
      resolved_role: string | null;
    };
    deadline: { raw_text: string; normalized_date: string | null };
    citations: Citation[];
  }[];
  extraction_status: DocumentRecord["extraction_status"];
  extraction_error: string | null;
}

export interface DocumentUploadReceipt {
  document_id: string;
  index_failed_document_ids?: string[];
  extraction_failed_document_ids?: string[];
}

export interface CoreDocumentApi {
  "/api/documents": {
    method: "GET";
    body: undefined;
    result: { documents: DocumentRecord[] };
  };
  "/api/documents/upload-options": {
    method: "GET";
    body: undefined;
    result: DocumentUploadOptions;
  };
  "/api/documents/:id": {
    method: "GET";
    body: undefined;
    result: { document: DocumentRecord };
  };
  "/api/documents/:id/obligations": {
    method: "GET";
    body: undefined;
    result: DocumentObligations;
  };
  "/api/documents/:id/index": {
    method: "POST";
    body: undefined;
    result: unknown;
  };
  "/v1/upload": {
    method: "POST";
    body: FormData;
    result: DocumentUploadReceipt;
  };
}

export type Citation = components["schemas"]["citation.v1.schema"];
export type SearchResult = components["schemas"]["search-result.v1.schema"];
export type Answer = components["schemas"]["answer.v1.schema"];

export interface SearchOptions {
  departments: string[];
  document_types: string[];
  languages: string[];
}

export interface SearchParameters {
  query: string;
  top_k: number;
  department?: string;
  doc_type?: string;
  doc_date_from?: string;
  doc_date_to?: string;
  language?: string;
}

export interface CoreSearchApi {
  "/api/search/options": {
    method: "GET";
    body: undefined;
    result: SearchOptions;
  };
  "/api/search": {
    method: "GET";
    body: undefined;
    result: { results: SearchResult[] };
  };
  "/api/answer": { method: "GET"; body: undefined; result: Answer };
  "/api/documents/:id/source": { method: "GET"; body: undefined; result: Blob };
}

export type TaskScope = "mine" | "department" | "review";
export type TaskStatus =
  "open" | "acknowledged" | "in_progress" | "done" | "rejected" | "overdue";
export type TaskAction = Exclude<TaskStatus, "open" | "overdue">;
export type TaskTransition =
  | { status: Exclude<TaskAction, "rejected"> }
  | { status: "rejected"; reason: string };

export interface TaskRecord {
  id: number;
  obligation_id: string;
  doc_id: string;
  action: string;
  owner: string | null;
  due_date: string | null;
  overdue: boolean;
  status: TaskStatus;
  rejection_reason: string | null;
  citations: Citation[];
  scope: TaskScope;
  assignee_user_id: number | null;
  queue_type: "role" | "department" | null;
  queue_name: string | null;
  allowed_transitions: TaskAction[];
}

export interface TaskFeed {
  tasks: TaskRecord[];
  available_scopes: TaskScope[];
}

export interface CoreTaskApi {
  "/api/tasks": { method: "GET"; body: undefined; result: TaskFeed };
  "/api/tasks/:id/status": {
    method: "PATCH";
    body: TaskTransition;
    result: { task: TaskRecord };
  };
}

export type ObligationOwner =
  components["schemas"]["obligation-owner.v1.schema"];
export type ObligationDeadline =
  components["schemas"]["obligation-deadline.v1.schema"];
export type ReviewObligation = Extract<
  components["schemas"]["obligation.v1.schema"],
  { status: "extracted" | "verified" | "needs_review" | "rejected" }
> & {
  extracted_owner?: ObligationOwner;
  extracted_deadline?: ObligationDeadline;
  owner_override?: ObligationOwner | null;
  deadline_override?: ObligationDeadline | null;
};

export interface ReviewOptions {
  departments: string[];
  roles: { name: string; department: string | null }[];
}

export interface ReviewOverride {
  owner?: { department?: string; role?: string };
  due_date?: string | null;
}

export interface CoreReviewApi {
  "/api/obligations/review": {
    method: "GET";
    body: undefined;
    result: { obligations: ReviewObligation[] };
  };
  "/api/obligations/review/options": {
    method: "GET";
    body: undefined;
    result: ReviewOptions;
  };
  "/api/documents/:id/obligations/:obligation_id/review": {
    method: "PATCH";
    body: ReviewOverride;
    result: { obligation: ReviewObligation };
  };
  "/api/documents/:id/obligations/:obligation_id/review/approve": {
    method: "POST";
    body: undefined;
    result: { obligation: ReviewObligation };
  };
  "/api/documents/:id/obligations/:obligation_id/review/reject": {
    method: "POST";
    body: { reason: string };
    result: { obligation: ReviewObligation };
  };
}

export type JsonValue =
  string | number | boolean | null | JsonValue[] | JsonObject;
export interface JsonObject {
  [key: string]: JsonValue;
}

export interface AuditEntry {
  id: number;
  timestamp: string;
  actor_user_id: number | null;
  action: string;
  entity_type: string;
  entity_id: string;
  before: JsonObject | null;
  after: JsonObject | null;
  request_id: string;
  ip: string | null;
}

export interface AuditQuery {
  actor_user_id?: string;
  entity_type?: string;
  entity_id?: string;
  action?: string;
  from?: string;
  to?: string;
  before_id?: number;
}

export interface UserRecord {
  id: number;
  name: string;
  email: string;
  roles: string[];
  department: string | null;
  active: boolean;
}

export type UserChange = { department: string | null } | { active: false };

export interface RoutingRule {
  id: string;
  priority: number;
  conditions: {
    obligation_type?: NonNullable<
      components["schemas"]["obligation.v1.schema"]["obligation_type"]
    >;
    owner_department?: string;
    document_type?: string;
    keywords?: string[];
    deadline_within_days?: number;
  };
  target: { kind: "user" | "role_queue" | "department_queue"; value: string };
  escalation_after_days?: number;
}

export interface RoutingPolicy {
  min_confidence: number;
  review_role: string;
}

export interface OrgOverview {
  org_id: string;
  languages: string[];
  ocr_engine: string;
  document_types: string[];
  departments: string[];
  routing_rules: (
    RoutingRule | { document_type: string; department: string }
  )[];
  routing_policy?: RoutingPolicy;
}

export interface CoreAdminApi {
  "/api/audit": {
    method: "GET";
    body: undefined;
    result: { entries: AuditEntry[]; next_cursor: number | null };
  };
  "/api/users": {
    method: "GET";
    body: undefined;
    result: { users: UserRecord[]; roles: string[]; departments: string[] };
  };
  "/api/users/:id": {
    method: "PATCH";
    body: UserChange;
    result: { user: UserRecord };
  };
  "/api/users/:id/roles": {
    method: "PUT";
    body: { roles: string[] };
    result: void;
  };
  "/api/organisation": { method: "GET"; body: undefined; result: OrgOverview };
}
