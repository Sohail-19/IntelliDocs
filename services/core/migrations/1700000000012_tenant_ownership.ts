import type { MigrationBuilder } from 'node-pg-migrate';

export function up(pgm: MigrationBuilder): void {
  pgm.sql(`
    ALTER TABLE obligations ADD COLUMN org_id integer;
    UPDATE obligations o SET org_id = d.org_id FROM documents d WHERE d.id = o.document_id;
    ALTER TABLE assignments ADD COLUMN org_id integer;
    UPDATE assignments a SET org_id = o.org_id FROM obligations o WHERE o.id = a.obligation_id;
    ALTER TABLE refresh_tokens ADD COLUMN org_id integer;
    UPDATE refresh_tokens r SET org_id = u.org_id FROM users u WHERE u.id = r.user_id;

    CREATE FUNCTION inherit_tenant_ownership() RETURNS trigger AS $$
    BEGIN
      IF NEW.org_id IS NULL THEN
        IF TG_TABLE_NAME = 'obligations' THEN
          SELECT org_id INTO NEW.org_id FROM documents WHERE id = NEW.document_id;
        ELSIF TG_TABLE_NAME = 'assignments' THEN
          SELECT org_id INTO NEW.org_id FROM obligations WHERE id = NEW.obligation_id;
        ELSE
          SELECT org_id INTO NEW.org_id FROM users WHERE id = NEW.user_id;
        END IF;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER obligations_inherit_tenant BEFORE INSERT ON obligations
      FOR EACH ROW EXECUTE FUNCTION inherit_tenant_ownership();
    CREATE TRIGGER assignments_inherit_tenant BEFORE INSERT ON assignments
      FOR EACH ROW EXECUTE FUNCTION inherit_tenant_ownership();
    CREATE TRIGGER refresh_tokens_inherit_tenant BEFORE INSERT ON refresh_tokens
      FOR EACH ROW EXECUTE FUNCTION inherit_tenant_ownership();

    -- Historical rows without an attributable tenant stay inaccessible. Any new row must have one.
    ALTER TABLE documents ADD CONSTRAINT documents_tenant_required CHECK (org_id IS NOT NULL) NOT VALID,
      ADD CONSTRAINT documents_tenant_parent_fk FOREIGN KEY (org_id, parent_document_id)
        REFERENCES documents (org_id, id) NOT VALID;
    ALTER TABLE users ADD CONSTRAINT users_tenant_required CHECK (org_id IS NOT NULL) NOT VALID;
    ALTER TABLE roles ADD CONSTRAINT roles_tenant_required CHECK (org_id IS NOT NULL) NOT VALID;
    ALTER TABLE audit_log ADD CONSTRAINT audit_tenant_required CHECK (org_id IS NOT NULL) NOT VALID,
      ADD CONSTRAINT audit_tenant_actor_fk FOREIGN KEY (org_id, actor_user_id)
        REFERENCES users (org_id, id) NOT VALID;
    ALTER TABLE obligations ADD CONSTRAINT obligations_tenant_required CHECK (org_id IS NOT NULL) NOT VALID,
      ADD CONSTRAINT obligations_tenant_id_key UNIQUE (org_id, id),
      ADD CONSTRAINT obligations_tenant_document_fk FOREIGN KEY (org_id, document_id)
        REFERENCES documents (org_id, id) NOT VALID,
      ADD CONSTRAINT obligations_tenant_assignee_fk FOREIGN KEY (org_id, assigned_user_id)
        REFERENCES users (org_id, id) NOT VALID;
    ALTER TABLE assignments ADD CONSTRAINT assignments_tenant_required CHECK (org_id IS NOT NULL) NOT VALID,
      ADD CONSTRAINT assignments_tenant_obligation_fk FOREIGN KEY (org_id, obligation_id)
        REFERENCES obligations (org_id, id) NOT VALID,
      ADD CONSTRAINT assignments_tenant_assignee_fk FOREIGN KEY (org_id, assignee_user_id)
        REFERENCES users (org_id, id) NOT VALID;
    ALTER TABLE refresh_tokens ADD CONSTRAINT refresh_tokens_tenant_required CHECK (org_id IS NOT NULL) NOT VALID,
      ADD CONSTRAINT refresh_tokens_tenant_user_fk FOREIGN KEY (org_id, user_id)
        REFERENCES users (org_id, id) ON DELETE CASCADE NOT VALID;
    ALTER TABLE obligation_staleness ADD CONSTRAINT staleness_tenant_obligation_fk
      FOREIGN KEY (org_id, obligation_id) REFERENCES obligations (org_id, id) NOT VALID;
  `);
}

export function down(pgm: MigrationBuilder): void {
  pgm.sql(`
    ALTER TABLE obligation_staleness DROP CONSTRAINT staleness_tenant_obligation_fk;
    DROP TRIGGER assignments_inherit_tenant ON assignments;
    DROP TRIGGER obligations_inherit_tenant ON obligations;
    DROP TRIGGER refresh_tokens_inherit_tenant ON refresh_tokens;
    ALTER TABLE assignments DROP COLUMN org_id;
    ALTER TABLE obligations DROP COLUMN org_id;
    ALTER TABLE refresh_tokens DROP COLUMN org_id;
    ALTER TABLE audit_log DROP CONSTRAINT audit_tenant_actor_fk, DROP CONSTRAINT audit_tenant_required;
    ALTER TABLE roles DROP CONSTRAINT roles_tenant_required;
    ALTER TABLE users DROP CONSTRAINT users_tenant_required;
    ALTER TABLE documents DROP CONSTRAINT documents_tenant_parent_fk, DROP CONSTRAINT documents_tenant_required;
    DROP FUNCTION inherit_tenant_ownership();
  `);
}
