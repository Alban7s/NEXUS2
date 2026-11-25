import test from 'node:test';
import assert from 'node:assert/strict';
import { greeting } from './src/greeting.js';

test('greeting includes the name', () => {
  assert.match(greeting('NEXUS'), /NEXUS/);
});
