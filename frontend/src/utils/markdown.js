function getFenceMarker(line) {
  const match = /^( {0,3})(`{3,}|~{3,})/.exec(line)
  if (!match) return null

  return {
    char: match[2][0],
    length: match[2].length,
  }
}

function isClosingFence(line, marker) {
  const indentMatch = /^( {0,3})/.exec(line)
  if (!indentMatch) return false

  const content = line.slice(indentMatch[0].length)
  let index = 0
  while (index < content.length && content[index] === marker.char) {
    index += 1
  }

  return index >= marker.length && /^[ \t]*$/.test(content.slice(index))
}

function splitFenceSegments(markdown) {
  const lines = markdown.match(/.*(?:\n|$)/g) || ['']
  const segments = []
  let buffer = ''
  let activeFence = null

  for (const line of lines) {
    const plainLine = line.replace(/\r?\n$/, '')

    if (!activeFence) {
      const marker = getFenceMarker(plainLine)
      if (marker) {
        if (buffer) {
          segments.push({ type: 'text', value: buffer })
        }
        buffer = line
        activeFence = marker
        continue
      }

      buffer += line
      continue
    }

    buffer += line
    if (isClosingFence(plainLine, activeFence)) {
      segments.push({ type: 'code', value: buffer })
      buffer = ''
      activeFence = null
    }
  }

  if (buffer) {
    segments.push({
      type: activeFence ? 'code' : 'text',
      value: buffer,
    })
  }

  return segments
}

function appendDisplayMath(output, inner, nextChar) {
  const prefix = output && !output.endsWith('\n') ? '\n' : ''
  const suffix = nextChar && nextChar !== '\n' ? '\n' : ''
  return `${prefix}$$\n${inner}\n$$${suffix}`
}

function normalizeTextMath(segment) {
  let output = ''
  let index = 0
  let inlineCodeTicks = 0

  while (index < segment.length) {
    if (segment[index] === '`') {
      let tickCount = 0
      while (segment[index + tickCount] === '`') {
        tickCount += 1
      }

      output += segment.slice(index, index + tickCount)

      if (inlineCodeTicks === 0) {
        inlineCodeTicks = tickCount
      } else if (tickCount === inlineCodeTicks) {
        inlineCodeTicks = 0
      }

      index += tickCount
      continue
    }

    if (inlineCodeTicks > 0) {
      output += segment[index]
      index += 1
      continue
    }

    if (segment.startsWith('\\[', index)) {
      const closeIndex = segment.indexOf('\\]', index + 2)
      if (closeIndex !== -1) {
        const inner = segment.slice(index + 2, closeIndex)
        output += appendDisplayMath(output, inner, segment[closeIndex + 2])
        index = closeIndex + 2
        continue
      }
    }

    if (segment.startsWith('\\(', index)) {
      const closeIndex = segment.indexOf('\\)', index + 2)
      if (closeIndex !== -1) {
        const inner = segment.slice(index + 2, closeIndex)
        if (!inner.includes('\n')) {
          output += `$${inner}$`
          index = closeIndex + 2
          continue
        }
      }
    }

    output += segment[index]
    index += 1
  }

  return output
}

export function normalizeMarkdownMath(markdown) {
  return splitFenceSegments(markdown)
    .map((segment) =>
      segment.type === 'code'
        ? segment.value
        : normalizeTextMath(segment.value),
    )
    .join('')
}
