import eslint from '@eslint/js';
import prettier from 'eslint-config-prettier';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    ignores: ['dist/', 'coverage/', '*.config.js', '*.config.ts'],
  },
  eslint.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  {
    files: ['src/**/*.ts', 'tests/**/*.ts', 'migrations/**/*.ts'],
    languageOptions: {
      globals: { ...globals.node },
      parserOptions: { project: './tsconfig.eslint.json', tsconfigRootDir: import.meta.dirname },
    },
    rules: {
      '@typescript-eslint/consistent-type-imports': 'error',
    },
  },
  prettier,
);
