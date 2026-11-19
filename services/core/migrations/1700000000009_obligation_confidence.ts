import type { MigrationBuilder } from 'node-pg-migrate';

export function up(pgm: MigrationBuilder): void {
  pgm.addColumns('obligations', { confidence_breakdown: { type: 'jsonb' } });
}

export function down(pgm: MigrationBuilder): void {
  pgm.dropColumns('obligations', ['confidence_breakdown']);
}
