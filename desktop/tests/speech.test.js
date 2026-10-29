import test from 'node:test';
import assert from 'node:assert/strict';
import { spokenText } from '../src/speech.js';

test('spoken response removes markdown and internal reasoning without discarding prose', () => {
  const result = spokenText('## Direct answer\nHere is **the result**. <think>private reasoning</think>\n- First point\n- Second point');
  assert.equal(result, 'Direct answer Here is the result. First point Second point');
  assert.doesNotMatch(result, /[*_#>|]/);
  assert.doesNotMatch(result, /private reasoning/);
});

test('spoken response keeps link labels and refers to raw URLs on screen', () => {
  const result = spokenText('Read [the guide](https://example.com/guide) and https://example.org/docs.');
  assert.equal(result, 'Read the guide and link available on screen.');
});

test('spoken response summarizes code as visible on screen and preserves table values', () => {
  const result = spokenText('Use this:\n```python\nprint("hello")\n```\n| Tool | Use |\n| --- | --- |\n| git | changes |');
  assert.match(result, /Code is shown on screen/);
  assert.match(result, /Tool: git\. Use: changes/);
  assert.doesNotMatch(result, /print\("hello"\)|\|/);
});
