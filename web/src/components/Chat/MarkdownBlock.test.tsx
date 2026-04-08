import { describe, expect, it } from 'vitest'
import { normalizeMathDelimiters } from './MarkdownBlock'

describe('MarkdownBlock', () => {
  it('normalizes supported math delimiters', () => {
    expect(
      normalizeMathDelimiters('Inline: \\(x + y\\)\n\n\\[x^2 + y^2\\]'),
    ).toBe('Inline: $x + y$\n\n$$x^2 + y^2$$')
  })

  it('keeps code spans and code blocks unchanged', () => {
    const input = [
      'Inline formula: \\(x\\)',
      '',
      'Single tick: `\\(a\\)`',
      '',
      'Double tick: ``foo \\(b\\) bar``',
      '',
      '```js',
      'const s = "\\(c\\)"',
      '```',
      '',
      '    const t = "\\(d\\)"',
    ].join('\n')

    expect(normalizeMathDelimiters(input)).toBe(
      [
        'Inline formula: $x$',
        '',
        'Single tick: `\\(a\\)`',
        '',
        'Double tick: ``foo \\(b\\) bar``',
        '',
        '```js',
        'const s = "\\(c\\)"',
        '```',
        '',
        '    const t = "\\(d\\)"',
      ].join('\n'),
    )
  })

  it('leaves unclosed delimiters unchanged', () => {
    expect(normalizeMathDelimiters('oops \\(x and \\[y')).toBe(
      'oops \\(x and \\[y',
    )
  })
})
